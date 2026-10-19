"""How agents submit work.

Every branch workspace gets `.ff/` with the task, a `submit` script and a `mailbox/`. Running
`python3 .ff/submit` drops a request into the mailbox. The mailroom, running in the orchestrator and
so outside any agent sandbox, snapshots the workspace, has it graded, and writes the reply next to
the request, where the waiting script picks it up.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Callable

from .workspace import FF_DIR, copy_tree, make_readonly, remove_tree

POLL_S = 1.0
# Where a forked branch finds a read-only copy of the best submission it inherited.
INCUMBENT_PATH = os.path.join(FF_DIR, "incumbent")

SUBMIT_SCRIPT = '''#!/usr/bin/env python3
"""Ask the fork-and-flush orchestrator to grade this directory, then print the result."""
import json, os, sys, time, uuid

WAIT_S = @WAIT@
box = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mailbox")
rid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
with open(os.path.join(box, rid + ".tmp"), "w") as f:
    json.dump({"time": time.time()}, f)
os.rename(os.path.join(box, rid + ".tmp"), os.path.join(box, rid + ".req"))
reply = os.path.join(box, rid + ".res")
print("submitted " + rid + ", waiting for the grader...", flush=True)
deadline = time.time() + WAIT_S
while time.time() < deadline:
    if os.path.exists(reply):
        print(open(reply).read())
        sys.exit(0)
    time.sleep(0.2)
print("still grading; the result will be appended to .ff/scores.log")
sys.exit(3)
'''


def ff_path(workspace: str, *parts: str) -> str:
    return os.path.join(workspace, FF_DIR, *parts)


def install(workspace: str, task: str, submit_wait_s: float, incumbent: str | None) -> None:
    """Set up .ff/ in a branch workspace; incumbent is the inherited best snapshot, if any."""
    remove_tree(ff_path(workspace))
    os.makedirs(ff_path(workspace, "mailbox"))
    files = {
        "TASK.md": task,
        "submit": SUBMIT_SCRIPT.replace("@WAIT@", repr(float(submit_wait_s))),
        "scores.log": "",
    }
    for name, text in files.items():
        with open(ff_path(workspace, name), "w") as f:
            f.write(text)
    os.chmod(ff_path(workspace, "submit"), 0o755)
    if incumbent:
        copy_tree(incumbent, os.path.join(workspace, INCUMBENT_PATH))
        make_readonly(os.path.join(workspace, INCUMBENT_PATH))


def requests(workspace: str) -> list:
    """Paths of the submissions in the workspace that have no reply yet."""
    try:
        names = sorted(os.listdir(ff_path(workspace, "mailbox")))
    except FileNotFoundError:
        return []
    return [ff_path(workspace, "mailbox", n) for n in names if n.endswith(".req")]


def answer(request: str, message: str, summary: str) -> None:
    """Reply to a request (atomically, since the submit script is polling) and log the outcome."""
    reply = request[: -len(".req")] + ".res"
    with open(reply + ".tmp", "w") as f:
        f.write(message + "\n")
    os.replace(reply + ".tmp", reply)
    with open(os.path.join(os.path.dirname(os.path.dirname(request)), "scores.log"), "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {summary}\n")
    os.remove(request)


# grade(branch, submission id, snapshot path) -> (reply for the agent, one-line summary)
Grader = Callable[[str, str, str], "tuple[str, str]"]


class Mailroom:
    """Watches the mailboxes of the branches that are running and grades what they submit."""

    def __init__(self, grade: Grader, snapshots_dir: str, workers: int):
        self.grade = grade
        self.snapshots_dir = snapshots_dir
        self.workers = workers
        self.watched: dict = {}  # branch id -> workspace
        self.taken: set = set()  # requests already snapshotted
        self.jobs: queue.Queue = queue.Queue()
        self.halt = threading.Event()
        self.threads: list = []

    def start(self) -> None:
        loops = [self._scan] + [self._work] * self.workers
        self.threads = [threading.Thread(target=loop, daemon=True) for loop in loops]
        for t in self.threads:
            t.start()

    def close(self) -> None:
        """Stop; unanswered requests stay in their mailboxes and are graded after a restart."""
        self.halt.set()
        for t in self.threads:
            t.join(timeout=5)

    def watch(self, branch: str, workspace: str) -> None:
        self.watched[branch] = workspace

    def unwatch(self, branch: str, stop: threading.Event) -> None:
        """Answer what the branch submitted before its agent stopped, then stop watching it."""
        while requests(self.watched[branch]) and not stop.is_set():
            time.sleep(POLL_S)
        del self.watched[branch]

    def _scan(self) -> None:
        while not self.halt.is_set():
            for branch, workspace in list(self.watched.items()):
                for request in requests(workspace):
                    if request not in self.taken:
                        self.taken.add(request)
                        self._take(branch, workspace, request)
            self.halt.wait(POLL_S)

    def _take(self, branch: str, workspace: str, request: str) -> None:
        submission = f"{branch}-{os.path.basename(request)[: -len('.req')]}"
        snapshot = os.path.join(self.snapshots_dir, submission)
        try:
            copy_tree(workspace, snapshot)
        except OSError as e:
            answer(
                request,
                f"{submission}: could not snapshot the workspace ({e})",
                submission,
            )
            return
        self.jobs.put((branch, submission, snapshot, request))

    def _work(self) -> None:
        while not self.halt.is_set():
            try:
                branch, submission, snapshot, request = self.jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                message, summary = self.grade(branch, submission, snapshot)
            except Exception as e:  # noqa: BLE001 - never leave a submit script waiting forever
                message = summary = f"{submission}: grading failed ({e})"
            answer(request, message, summary)
