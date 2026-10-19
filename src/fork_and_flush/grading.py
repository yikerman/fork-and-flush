"""Running the user's grader on a submission snapshot."""

from __future__ import annotations

import math
import os
import subprocess
from typing import NamedTuple

from .config import PRIVATE_DIR
from .proc import clean_env, kill_group


class Grade(NamedTuple):
    score: float | None  # None: the submission is invalid
    feedback: str  # the grader's stdout before the score line, shown to the agent
    error: str  # why the submission is invalid


def parse_score(lines: list) -> float | None:
    try:
        score = float(lines[-1])
    except (IndexError, ValueError):
        return None
    return score if math.isfinite(score) else None


def expand(arg: str, values: dict) -> str:
    for key, value in values.items():
        arg = arg.replace("{" + key + "}", value)
    return arg


def run_grader(cfg: dict, run_dir: str, snapshot: str, log_path: str) -> Grade:
    g = cfg["grader"]
    places = {"run": run_dir, "private": os.path.join(run_dir, PRIVATE_DIR), "submission": snapshot}
    cmd = [expand(arg, places) for arg in g["command"]]
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w") as err:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=snapshot,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=err,
                text=True,
                env=clean_env(FF_SUBMISSION=snapshot, FF_RUN_DIR=run_dir),
                start_new_session=True,
            )
            out, _ = proc.communicate(timeout=g["timeout_s"])
        except OSError as e:
            return Grade(None, "", f"could not start the grader: {e}")
        except subprocess.TimeoutExpired:
            kill_group(proc)
            return Grade(None, "", f"the grader timed out after {g['timeout_s']}s")
    lines = [line for line in out.splitlines() if line.strip()]
    score = parse_score(lines) if proc.returncode == 0 else None
    if score is not None:
        return Grade(score, "\n".join(lines[:-1]), "")
    reason = f"exit status {proc.returncode}" if proc.returncode else "no numeric last line"
    return Grade(None, "\n".join(lines), f"the grader rejected it ({reason})")
