"""Stand-in for `claude -p --output-format stream-json` and `codex app-server`: the same messages
on stdin/stdout. It "works" by changing value.txt in the workspace and submitting via .ff/submit.

Usage: fake_agent.py claude|codex ARGS...
FAKE_SLOW=1 makes it keep using tokens for a long time, to exercise the allowance cutoff.
"""

import json
import os
import queue
import random
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# 100k weighted tokens per model call with the default weights.
CALL = {"input": 50_000, "output": 10_000}
LOG = Path(os.environ.get("FAKE_LOG", "."))


def emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def log_prompt(session: str, prompt: str) -> int:
    path = LOG / f"{session}.prompts"
    with path.open("a") as f:
        f.write(prompt + "\n=====\n")
    return len(path.read_text())


def work(session, prompt, charge, interrupted=lambda: False) -> None:
    seq = log_prompt(session, prompt)
    if prompt.startswith("This is a sandbox test"):  # no sandbox here: read the file and show it
        path = prompt.split("Try to read the file ")[1].split(":")[0]
        charge()
        emit({"type": "assistant", "message": {"content": Path(path).read_text()}})
        return
    if os.environ.get("FAKE_SLOW"):
        for _ in range(400):
            if interrupted():
                return
            charge()
            time.sleep(0.02)
        return
    charge()
    value_file = Path("value.txt")
    value = int(value_file.read_text()) if value_file.exists() else 0
    step = random.Random(f"{session}{seq}").randint(-2, 10)
    value += step * (5 if "DIFFERENT approach" in prompt else 1)
    value_file.write_text(str(value))
    out = subprocess.run(
        [sys.executable, ".ff/submit"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    with (LOG / f"{session}.submits").open("a") as f:
        f.write(out.stdout + "\n=====\n")


def claude(args: list) -> None:
    resumed = "--resume" in args
    session = args[args.index("--resume") + 1] if resumed else str(uuid.uuid4())
    prompt = sys.stdin.read()
    emit({"type": "system", "subtype": "init", "session_id": session})

    def charge() -> None:
        usage = {"input_tokens": CALL["input"], "output_tokens": CALL["output"]}
        msg = {"id": "msg_" + uuid.uuid4().hex, "usage": usage}
        for _ in range(2):  # the real CLI can emit a message once per content block
            emit({"type": "assistant", "session_id": session, "message": msg})

    work(session, prompt, charge)
    emit({"type": "result", "subtype": "success", "is_error": False})


class CodexThread:
    """Running token totals for one thread, kept on disk so resumed threads continue them."""

    def __init__(self, thread_id: str):
        self.id = thread_id
        self.path = LOG / "threads" / thread_id
        self.path.parent.mkdir(exist_ok=True)
        zero = {"inputTokens": 0, "cachedInputTokens": 0, "outputTokens": 0}
        self.total = json.loads(self.path.read_text()) if self.path.exists() else zero

    def charge(self) -> None:
        self.total["inputTokens"] += CALL["input"]
        self.total["outputTokens"] += CALL["output"]
        self.path.write_text(json.dumps(self.total))
        usage = {"total": self.total, "last": self.total}
        params = {"threadId": self.id, "turnId": "turn_1", "tokenUsage": usage}
        emit({"method": "thread/tokenUsage/updated", "params": params})


def codex(args: list) -> None:
    inbox: queue.Queue = queue.Queue()
    interrupts = []

    def read() -> None:
        for line in sys.stdin:
            msg = json.loads(line)
            if msg.get("method") == "turn/interrupt":
                interrupts.append(msg)
                emit({"id": msg["id"], "result": {}})
            else:
                inbox.put(msg)
        inbox.put(None)

    threading.Thread(target=read, daemon=True).start()
    thread = None
    while (msg := inbox.get()) is not None:
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params", {})
        if method == "initialize":
            emit({"id": mid, "result": {}})
        elif method in ("thread/start", "thread/resume"):
            assert params["approvalPolicy"] == "never"
            assert os.path.samefile(params["cwd"], os.getcwd())
            thread = CodexThread(params.get("threadId") or "thr_" + uuid.uuid4().hex)
            emit({"id": mid, "result": {"thread": {"id": thread.id}}})
        elif method == "turn/start":
            assert thread is not None
            emit(
                {
                    "id": mid,
                    "result": {"turn": {"id": "turn_1", "status": "inProgress"}},
                }
            )
            prompt = params["input"][0]["text"]
            work(thread.id, prompt, thread.charge, interrupted=lambda: bool(interrupts))
            status = "interrupted" if interrupts else "completed"
            turn = {"id": "turn_1", "status": status}
            emit(
                {
                    "method": "turn/completed",
                    "params": {"threadId": thread.id, "turn": turn},
                }
            )


if __name__ == "__main__":
    {"claude": claude, "codex": codex}[sys.argv[1]](sys.argv[2:])
