"""Command line: set up, check, start, watch and stop fork-and-flush runs."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

from . import checks, config
from . import state as runstate
from .harness import HARNESSES
from .proc import clean_env

PID_FILE = "orchestrator.pid"
# One line of the `status` branch table; the header uses it too.
ROW = " {mark}{branch:8}{parent:8}{status:11}{spent:>9}{best:>14}{episodes:>10}{note}"
COLUMNS = ("branch", "parent", "status", "spent", "best", "episodes")
LOG_FILE = "orchestrator.log"

TASK_TEMPLATE = f"""{config.TEMPLATE_MARKER}
# Task

Describe the problem the agents work on: the goal, what they may change, constraints, and what the
grader rewards (without revealing anything that would let them game it). Do not mention the budget.
"""

GRADER_TEMPLATE = '''#!/usr/bin/env python3
"""Grade one submission.

Runs with the current directory (and $FF_SUBMISSION) set to a snapshot of the submitted workspace.
Print feedback for the agent on stdout, then the score (higher is better) as the LAST stdout line.
Anything on stderr stays private. Exit non-zero for an invalid submission.
Must be deterministic: fix every random seed, never depend on wall-clock time.
"""
import os
import sys

submission = os.environ["FF_SUBMISSION"]
sys.exit("grader not written yet")
'''


# ---- helpers ----


def runs_root() -> str:
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.environ.get("FF_HOME") or os.path.join(data, "fork-and-flush", "runs")


def resolve(run: str) -> str:
    """A run given by path or by name."""
    for candidate in (run, os.path.join(runs_root(), run)):
        if os.path.isfile(os.path.join(candidate, config.CONFIG_FILE)):
            return os.path.abspath(candidate)
    sys.exit(f"no run {run!r} here or in {runs_root()}")


def loaded(run_dir: str) -> dict:
    """The run's config, after checking that it and the task are ready."""
    try:
        cfg = config.load(run_dir)
        config.read_task(run_dir)
    except config.ConfigError as e:
        sys.exit(f"error: {e}")
    return cfg


def refuse_inside_branch() -> None:
    if os.environ.get("FF_BRANCH"):
        sys.exit("refusing: this is an agent inside a fork-and-flush branch")


def running_pid(run_dir: str) -> int | None:
    try:
        with open(os.path.join(run_dir, PID_FILE)) as f:
            pid = int(f.read())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    return pid


def run_status(run_dir: str, state: dict | None) -> str:
    if state is None:
        return "not started"
    if state["done"]:
        return "finished"
    pid = running_pid(run_dir)
    return f"running (pid {pid})" if pid else "stopped"


def mtok(tokens: float) -> str:
    return f"{tokens / 1e6:.2f}M"


def write(path: str, text: str, mode: int = 0o644) -> None:
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


# ---- commands ----


def cmd_init(args) -> int:
    refuse_inside_branch()
    ws = os.path.abspath(args.workspace)
    name = args.name or f"{os.path.basename(ws)}-{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir = os.path.abspath(args.run_dir or os.path.join(runs_root(), name))
    if os.path.exists(run_dir):
        sys.exit(f"{run_dir} already exists")
    cfg = {
        **config.defaults(),
        "workspace": ws,
        "agent": args.agent,
        "budget_mtok": args.budget_mtok,
    }
    try:
        config.validate(cfg, run_dir)
    except config.ConfigError as e:
        sys.exit(f"error: {e}")
    grader = os.path.join(config.PRIVATE_DIR, "grader", "grade.py")
    files = {
        config.TASK_FILE: ("task", TASK_TEMPLATE, 0o644),
        grader: ("grader", GRADER_TEMPLATE, 0o755),
        config.CONFIG_FILE: ("settings", json.dumps(cfg, indent=2) + "\n", 0o644),
    }
    os.makedirs(os.path.dirname(os.path.join(run_dir, grader)))
    print(f"created {run_dir}")
    for name, (what, text, mode) in files.items():
        write(os.path.join(run_dir, name), text, mode)
        print(f"  {what + ':':10}{os.path.join(run_dir, name)}")
    print(f"hidden test data goes in {os.path.join(run_dir, config.PRIVATE_DIR)}/")
    print(f"then: fork-and-flush check {run_dir}")
    return 0


def cmd_check(args) -> int:
    run_dir = resolve(args.run)
    cfg = loaded(run_dir)
    print("config and task OK")
    ok = checks.grader_is_deterministic(cfg, run_dir)
    ok = ok and (args.no_probe or checks.agents_cannot_read_private(cfg, run_dir))
    return 0 if ok else 1


def cmd_start(args) -> int:
    refuse_inside_branch()
    run_dir = resolve(args.run)
    loaded(run_dir)
    if running_pid(run_dir):
        sys.exit("this run is already running")
    package_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pythonpath = os.pathsep.join(filter(None, [package_parent, os.environ.get("PYTHONPATH")]))
    with open(os.path.join(run_dir, LOG_FILE), "a") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "fork_and_flush", "run", run_dir],
            cwd=run_dir,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=clean_env(PYTHONPATH=pythonpath),
            start_new_session=True,
        )
    print(f"started (pid {proc.pid}); log: {os.path.join(run_dir, LOG_FILE)}")
    print(f"watch with: fork-and-flush status {run_dir}")
    return 0


def cmd_run(args) -> int:
    from .orchestrator import Orchestrator

    run_dir = resolve(args.run)
    loaded(run_dir)
    pid_file = os.path.join(run_dir, PID_FILE)
    with open(os.path.join(run_dir, "lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("this run is already being orchestrated")
        write(pid_file, str(os.getpid()))
        try:
            return Orchestrator(run_dir).run()
        finally:
            os.remove(pid_file)


def cmd_stop(args) -> int:
    run_dir = resolve(args.run)
    pid = running_pid(run_dir)
    if pid:
        os.kill(pid, signal.SIGTERM)
    print(
        f"asked pid {pid} to stop; `fork-and-flush start` resumes later" if pid else "not running"
    )
    return 0


def cmd_status(args) -> int:
    run_dir = resolve(args.run)
    with open(os.path.join(run_dir, config.CONFIG_FILE)) as f:
        cfg = json.load(f)
    state = runstate.load(run_dir)
    status = run_status(run_dir, state)
    if args.json:
        print(
            json.dumps(
                {"run_dir": run_dir, "status": status, "config": cfg, "state": state},
                indent=1,
            )
        )
        return 0
    print(f"{run_dir}\n  {status}, agent {cfg['agent']}")
    if state is None:
        return 0
    budget, spent, best = cfg["budget_mtok"] * 1e6, runstate.spent(state), runstate.best(state)
    print(f"  phase {state['phase'] + 1} of {len(state['phases'])}")
    print(f"  spent {mtok(spent)} of {mtok(budget)} weighted tokens ({spent / budget:.0%})")
    if best:
        print(f"  best {best['best']:.10g} ({best['best_submission']}): {best['best_snapshot']}")
    print("\n" + ROW.format(mark=" ", note="", **{c: c for c in COLUMNS}))
    for b in state["branches"].values():
        print(
            ROW.format(
                mark="*" if b["id"] == state["leader"] else " ",
                branch=b["id"],
                parent=b["parent"] or "-",
                status=b["status"],
                spent=mtok(b["spent"]),
                best="-" if b["best"] is None else format(b["best"], ".8g"),
                episodes=b["episodes"],
                note="  directed" if b["directed"] else "",
            )
        )
    return 0


def cmd_list(args) -> int:
    root = runs_root()
    names = sorted(os.listdir(root)) if os.path.isdir(root) else []
    runs = [
        os.path.join(root, n)
        for n in names
        if os.path.isfile(os.path.join(root, n, config.CONFIG_FILE))
    ]
    for run_dir in runs:
        state = runstate.load(run_dir)
        best = state and runstate.best(state)
        name, status = os.path.basename(run_dir), run_status(run_dir, state)
        print(f"{name:40} {status:22} best={best and best['best']}")
    if not runs:
        print(f"no runs in {root}")
    return 0


COMMANDS = {
    "check": (cmd_check, "validate the run and grade the initial workspace twice"),
    "start": (cmd_start, "start or resume the run in the background"),
    "run": (cmd_run, "run in the foreground"),
    "stop": (cmd_stop, "stop a running run (resumable)"),
    "status": (cmd_status, "show progress"),
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fork-and-flush", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a run directory for a workspace")
    p.add_argument("workspace", help="directory the agents start from (copied, never modified)")
    p.add_argument(
        "--budget-mtok",
        type=float,
        required=True,
        help="budget, millions of weighted tokens",
    )
    p.add_argument("--agent", default="claude", choices=list(HARNESSES))
    p.add_argument("--name", help="run name (default: workspace name and time)")
    p.add_argument("--run-dir", help=f"where to put the run (default: {runs_root()}/NAME)")
    p.set_defaults(func=cmd_init)

    for name, (func, text) in COMMANDS.items():
        p = sub.add_parser(name, help=text)
        p.add_argument("run", help="run directory or name")
        p.set_defaults(func=func)
        if name == "status":
            p.add_argument("--json", action="store_true")
        if name == "check":
            p.add_argument("--no-probe", action="store_true", help="skip the agent sandbox probe")

    sub.add_parser("list", help="list runs").set_defaults(func=cmd_list)
    args = parser.parse_args(argv)
    return args.func(args)
