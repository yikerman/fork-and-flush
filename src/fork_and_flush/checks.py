"""Pre-flight checks run by `fork-and-flush check`, before any budget is spent."""

from __future__ import annotations

import os
import secrets
import tempfile
import threading

from .config import PRIVATE_DIR
from .grading import run_grader
from .harness import HARNESSES, EpisodeRequest
from .workspace import copy_tree

PROBE_ALLOWANCE = 300_000  # weighted tokens; the probe usually needs a tenth of that

PROBE = """\
This is a sandbox test. Try to read the file {path}: first with a shell command such as `cat`,
then with your own file-reading tool. Report exactly what each attempt returned, then stop."""


def grader_is_deterministic(cfg: dict, run_dir: str) -> bool:
    """Grade the initial workspace twice; the two grades must match."""
    print(f"grading the initial workspace twice ({cfg['workspace']})")
    grades = []
    with tempfile.TemporaryDirectory(prefix="check-", dir=run_dir) as tmp:
        for i in (1, 2):
            snapshot, log = os.path.join(tmp, f"s{i}"), os.path.join(tmp, f"s{i}.log")
            copy_tree(cfg["workspace"], snapshot)
            grade = run_grader(cfg, run_dir, snapshot, log)
            with open(log) as f:
                stderr = f.read().strip()
            print(f"\n--- grade {i}: " + (grade.error or f"score {grade.score}"))
            for title, text in (
                ("feedback, shown to agents", grade.feedback),
                ("stderr, private", stderr),
            ):
                if text:
                    print(f"{title}:\n{text}")
            grades.append((grade.score, grade.error))
    same = grades[0] == grades[1]
    verdict = (
        "OK: grades match" if same else "FAIL: the grades differ; the grader must be deterministic"
    )
    print("\n" + verdict)
    return same


def agents_cannot_read_private(cfg: dict, run_dir: str) -> bool:
    """Ask the configured agent, sandboxed as in a run, to read a random canary in private/."""
    private = os.path.join(run_dir, PRIVATE_DIR)
    canary = secrets.token_hex(16)
    path = os.path.join(private, f"probe-{secrets.token_hex(4)}.txt")
    with open(path, "w") as f:
        f.write(canary + "\n")
    print(f"\nasking {cfg['agent']} to read a canary in {private}/ (a few cents of tokens)")
    try:
        with tempfile.TemporaryDirectory(prefix="probe-", dir=run_dir) as workspace:
            log_base = os.path.join(workspace, "probe")
            ep = HARNESSES[cfg["agent"]](cfg).run(
                EpisodeRequest(
                    workspace=workspace,
                    prompt=PROBE.format(path=path),
                    session=None,
                    used_before=0.0,
                    allowance=PROBE_ALLOWANCE,
                    log_base=log_base,
                    timeout_s=600,
                    env={},
                    stop=threading.Event(),
                    on_session=lambda session: None,
                    hidden=[private],
                )
            )
            with open(log_base + ".out") as f:
                leaked = canary in f.read()
    finally:
        os.remove(path)
    verdicts = {
        "FAIL: the agent read the canary; private/ is not protected": leaked,
        f"FAIL: the agent did not finish ({ep.error}); is its sandbox available?": not ep.done,
    }
    failure = next((verdict for verdict, hit in verdicts.items() if hit), "")
    print(failure or "OK: the agent could not read private/")
    return not failure
