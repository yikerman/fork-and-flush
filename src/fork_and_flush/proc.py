"""Subprocess helpers shared by the agent adapters and the grader."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess

# Variables a parent Claude Code session sets for its own children. A branch agent must start as an
# independent top-level session, so these are removed before launching anything.
SESSION_VARS = (
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
)


def clean_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SESSION_VARS}
    env.update(extra)
    return env


def kill_group(proc: subprocess.Popen, grace_s: float = 10.0) -> None:
    """Terminate a process started with start_new_session=True, along with everything it spawned."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass
    # Stragglers (background jobs the agent left) can hold the group after the leader exits.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


@contextlib.contextmanager
def paused(procs: list):
    """Suspend the process groups of procs (started with start_new_session=True) meanwhile."""
    groups = [p.pid for p in procs]
    for group in groups:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(group, signal.SIGSTOP)
    try:
        yield
    finally:
        for group in groups:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signal.SIGCONT)
