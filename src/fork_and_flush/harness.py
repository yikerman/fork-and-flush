"""Headless agent CLIs, driven only through their documented machine interfaces.

Claude Code: `claude -p --output-format stream-json` (headless mode). Every assistant message
carries its token usage; the final `result` message ends the episode.

Codex: `codex app-server`, the JSON-RPC protocol behind Codex's IDE integrations (one JSON message
per line, no "jsonrpc" field; shapes as in `codex app-server generate-json-schema`). Each episode
starts or resumes a thread and runs one turn; `thread/tokenUsage/updated` reports running totals.

Both report usage while they work, so an episode that goes over its allowance is stopped mid-run.
"""

from __future__ import annotations

import contextlib
import json
import math
import queue
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, NamedTuple

from .proc import clean_env, kill_group
from .util import merge

# How long an agent asked to stop gets to wind down before it is killed.
INTERRUPT_GRACE_S = 30.0


class Usage(NamedTuple):
    """Token counts. The field names are also the keys of the `token_weights` setting."""

    input: float = 0
    cache_read: float = 0
    cache_write: float = 0
    output: float = 0

    def weigh(self, weights: dict) -> float:
        return sum(count * weights[kind] for kind, count in self._asdict().items())


@dataclass
class EpisodeRequest:
    workspace: str
    prompt: str
    session: str | None  # None starts a new session
    used_before: float  # weighted tokens the session had used before this episode
    allowance: float  # weighted tokens this episode may use
    log_base: str  # the CLI's stdout and stderr go to log_base + ".out" / ".err"
    timeout_s: float | None
    env: dict
    stop: threading.Event  # set when the whole run is stopping
    on_session: Callable[[str], None]  # called as soon as the session id is known
    hidden: list = field(default_factory=list)  # paths the agent must not be able to read
    on_process: Callable = lambda proc: None  # called with the CLI's process, then with None


@dataclass
class Episode:
    """A running CLI invocation. Harnesses update it from the messages the CLI prints."""

    proc: subprocess.Popen
    request: EpisodeRequest
    session: str | None
    used: float  # weighted tokens for the whole session so far
    done: bool = False
    error: str = ""
    scratch: dict = field(default_factory=dict)  # harness-specific bookkeeping

    def send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()


def stop_reason(ep: Episode, started: float) -> str:
    req = ep.request
    reasons = {
        "the run is stopping": req.stop.is_set(),
        "episode allowance used up": ep.used - req.used_before > req.allowance,
        f"timed out after {req.timeout_s}s": bool(req.timeout_s)
        and time.time() - started > (req.timeout_s or 0),
    }
    return next((reason for reason, hit in reasons.items() if hit), "")


def read_messages(stream, log) -> tuple[queue.Queue, threading.Thread]:
    """Queue the JSON objects the CLI prints, logging every line; None marks the end of output."""
    messages: queue.Queue = queue.Queue()

    def pump() -> None:
        for line in stream:
            log.write(line)
            log.flush()
            with contextlib.suppress(ValueError):
                message = json.loads(line)
                if isinstance(message, dict):
                    messages.put(message)
        messages.put(None)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    return messages, reader


def finish(proc: subprocess.Popen) -> None:
    """Close the CLI's input, give it a moment to exit, then make sure nothing of it is left."""
    with contextlib.suppress(OSError):
        if proc.stdin:
            proc.stdin.close()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)
    kill_group(proc)


def ignore(ep: Episode, payload: dict) -> None:
    pass


class AbstractAgentHarness(ABC):
    """A headless agent CLI that reads its input and reports progress as JSON lines."""

    name: str  # executable, and the config section holding its settings

    def __init__(self, cfg: dict):
        self.settings = cfg[self.name]
        self.weights = cfg["token_weights"]

    @abstractmethod
    def command(self, req: EpisodeRequest) -> list:
        """Command line for an episode (a new session if req.session is None)."""

    @abstractmethod
    def begin(self, ep: Episode) -> None:
        """Send whatever starts the episode."""

    @abstractmethod
    def handle(self, ep: Episode, message: dict) -> None:
        """Update the episode from one JSON message the CLI printed."""

    def interrupt(self, ep: Episode) -> None:
        """Stop the agent early. Without a protocol for it, the process is killed."""
        kill_group(ep.proc)

    def run(self, req: EpisodeRequest) -> Episode:
        with (
            open(req.log_base + ".out", "w") as out,
            open(req.log_base + ".err", "w") as err,
        ):
            proc = subprocess.Popen(
                self.command(req),
                cwd=req.workspace,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=err,
                text=True,
                bufsize=1,
                env=clean_env(**req.env),
                start_new_session=True,
            )
            req.on_process(proc)
            ep = Episode(proc, req, req.session, req.used_before)
            messages, reader = read_messages(proc.stdout, out)
            try:
                self.begin(ep)
                self.follow(ep, messages)
            except OSError as e:  # the CLI went away while we were writing to it
                ep.error = ep.error or f"lost the connection to {self.name}: {e}"
            finally:
                finish(proc)
                reader.join(timeout=5)
                req.on_process(None)
        if not (ep.done or ep.error):
            ep.error = f"{self.name} exited with status {proc.returncode} before finishing"
        return ep

    def follow(self, ep: Episode, messages: queue.Queue) -> None:
        started, deadline = time.time(), math.inf
        while not ep.done and time.time() < deadline:
            try:
                message = messages.get(timeout=1.0)
            except queue.Empty:
                message = {}
            if message is None:
                return  # the CLI exited
            known = ep.session
            self.handle(ep, message)
            if ep.session and ep.session != known:
                ep.request.on_session(ep.session)
            if deadline == math.inf and (reason := stop_reason(ep, started)):
                ep.error = f"stopped: {reason}"
                deadline = time.time() + INTERRUPT_GRACE_S
                self.interrupt(ep)


def flags(options: dict) -> list:
    """`--flag value` pairs for the options that are set."""
    return [part for flag, value in options.items() if value for part in (flag, value)]


class ClaudeCodeHarness(AbstractAgentHarness):
    name = "claude"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.handlers = {"assistant": self.on_assistant, "result": self.on_result}

    def command(self, req):
        s = self.settings
        options = {
            "--resume": req.session,
            "--model": s["model"],
            "--effort": s["effort"],
            "--settings": json.dumps(merge(self.sandbox(req.hidden), s["settings"])),
        }
        base = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
        return [*base, *flags(options), *s["args"]]

    @staticmethod
    def sandbox(hidden: list) -> dict:
        """Shell commands run in Claude Code's sandbox, which refuses to run anything outside it and
        fails instead of running unsandboxed; file tools, which the sandbox does not cover, get
        matching permission rules. `//` starts an absolute path in a permission rule."""
        rules = [f"{tool}(/{path}/**)" for path in hidden for tool in ("Read", "Edit")]
        return {
            "sandbox": {
                "enabled": True,
                "allowUnsandboxedCommands": False,
                "failIfUnavailable": True,
                "filesystem": {"denyRead": hidden},
            },
            "permissions": {"deny": rules},
        }

    def begin(self, ep):
        assert ep.proc.stdin is not None
        ep.proc.stdin.write(ep.request.prompt)
        ep.proc.stdin.close()

    def handle(self, ep, message):
        ep.session = ep.session or message.get("session_id")
        self.handlers.get(message.get("type"), ignore)(ep, message)

    def on_assistant(self, ep, message):
        msg = message.get("message") or {}
        seen = ep.scratch.setdefault("seen", set())
        # The stream can repeat a message once per content block, with the same id and usage.
        if "usage" in msg and msg.get("id") not in seen:
            seen.add(msg.get("id"))
            u = msg["usage"]
            usage = Usage(
                input=u.get("input_tokens", 0),
                cache_read=u.get("cache_read_input_tokens", 0),
                cache_write=u.get("cache_creation_input_tokens", 0),
                output=u.get("output_tokens", 0),
            )
            ep.used += usage.weigh(self.weights)

    def on_result(self, ep, message):
        ep.done = True
        ep.error = ep.error or (str(message.get("subtype")) if message.get("is_error") else "")


class CodexHarness(AbstractAgentHarness):
    name = "codex"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        # Replies to our requests, by request id, and notifications, by method.
        self.replies = {
            "initialize": self.on_initialized,
            "thread": self.on_thread,
            "turn/start": self.on_turn_started,
        }
        self.notifications = {
            "thread/tokenUsage/updated": self.on_usage,
            "turn/completed": self.on_turn_completed,
        }

    def command(self, req):
        return ["codex", "app-server", *self.settings["args"]]

    @staticmethod
    def permissions(hidden: list) -> dict:
        """A Codex permission profile like its built-in `:workspace` one (write the workspace and
        the temp directories, read everything else) except that the hidden paths are denied."""
        filesystem = {
            ":root": "read",
            ":workspace_roots": {".": "write"},
            ":tmpdir": "write",
            ":slash_tmp": "write",
            **dict.fromkeys(hidden, "deny"),
        }
        profile = {"filesystem": filesystem, "network": {"enabled": False}}
        return {"default_permissions": "fork-and-flush", "permissions": {"fork-and-flush": profile}}

    @staticmethod
    def request(ep: Episode, method: str, params: dict, id: str | None = None) -> None:
        ep.send({"method": method, "id": id or method, "params": params})

    def begin(self, ep):
        self.request(
            ep,
            "initialize",
            {"clientInfo": {"name": "fork_and_flush", "version": "0.1"}},
        )

    def interrupt(self, ep):
        turn = ep.scratch.get("turn")
        if not turn:
            return super().interrupt(ep)
        self.request(ep, "turn/interrupt", {"threadId": ep.session, "turnId": turn})

    def handle(self, ep, message):
        if "method" in message and "id" in message:
            # A request to us (an approval or a question). With approvalPolicy "never" none are
            # expected, and there is nobody to answer.
            ep.send(
                {
                    "id": message["id"],
                    "error": {"code": -32601, "message": "unsupported"},
                }
            )
        elif "error" in message:
            ep.done, ep.error = True, f"{message.get('id')} failed: {message['error']}"
        elif "id" in message:
            self.replies.get(message["id"], ignore)(ep, message.get("result") or {})
        else:
            self.notifications.get(message.get("method"), ignore)(ep, message.get("params") or {})

    def on_initialized(self, ep, result):
        ep.send({"method": "initialized", "params": {}})
        s, req = self.settings, ep.request
        params = {
            "cwd": req.workspace,
            "model": s["model"],
            "approvalPolicy": "never",
            "config": merge(self.permissions(req.hidden), s["config"]),
        }
        resume = {"threadId": ep.session} if ep.session else {}
        method = "thread/resume" if ep.session else "thread/start"
        self.request(ep, method, {**params, **resume}, id="thread")

    def on_thread(self, ep, result):
        ep.session = result["thread"]["id"]
        prompt = [{"type": "text", "text": ep.request.prompt}]
        params = {
            "threadId": ep.session,
            "input": prompt,
            "effort": self.settings["effort"],
        }
        self.request(ep, "turn/start", params)

    def on_turn_started(self, ep, result):
        ep.scratch["turn"] = result["turn"]["id"]

    def on_usage(self, ep, params):
        total = params["tokenUsage"]["total"]
        cached = total.get("cachedInputTokens", 0)
        usage = Usage(
            input=total.get("inputTokens", 0) - cached,  # inputTokens includes the cached ones
            cache_read=cached,
            cache_write=total.get("cacheWriteInputTokens", 0),
            output=total.get("outputTokens", 0),
        )
        ep.used = usage.weigh(self.weights)

    def on_turn_completed(self, ep, params):
        turn = params["turn"]
        failed = turn.get("status") == "failed"
        ep.done = True
        ep.error = ep.error or (
            (turn.get("error") or {}).get("message", "failed") if failed else ""
        )


HARNESSES = {h.name: h for h in (ClaudeCodeHarness, CodexHarness)}
