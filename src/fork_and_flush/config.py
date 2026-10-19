"""Run configuration: defaults, loading and validation.

A run directory holds config.json, TASK.md and grader/. Everything else in it is written by the
orchestrator.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile

from .harness import HARNESSES, Usage
from .util import merge

CONFIG_FILE = "config.json"
TASK_FILE = "TASK.md"
TEMPLATE_MARKER = "<!-- fork-and-flush: replace this template -->"
# The run's secrets: the grader, hidden test data and grade logs. Agents cannot read it.
PRIVATE_DIR = "private"

DEFAULTS = {
    # Directory the run starts from. It is copied, never modified.
    "workspace": None,
    "agent": "claude",
    # Total spend for the whole run, in millions of weighted tokens (see token_weights).
    "budget_mtok": None,
    # Spend per token, relative to an uncached input token. The defaults follow typical API price
    # ratios: cache reads ~10x cheaper than input, cache writes slightly dearer, output ~5x dearer.
    "token_weights": Usage(input=1.0, cache_read=0.1, cache_write=1.25, output=5.0)._asdict(),
    # Paper defaults: K=4 branches per round, ~6% of the budget per branch, forks at 33% and 66%.
    "k": 4,
    "explore_frac": 0.06,
    "fork_at": [0.33, 0.66],
    # Start with K branches from the initial workspace instead of a single run.
    "opening_round": True,
    # Branches per fork round told to try a fundamentally different approach (the paper: 2 of 4).
    "directed_branches": 2,
    # How many branch agents may run at once. Results are the same, only slower, with fewer, since
    # the budget is spend, not wall-clock. Use 1 when the score depends on timing (see below).
    "parallel": 4,
    # Stop an agent episode after this many seconds (null: no limit).
    "episode_timeout_s": None,
    # A branch stops after this many consecutive episodes that use no tokens (crashes, auth errors).
    "max_failed_episodes": 3,
    "keep_discarded_workspaces": False,
    "grader": {
        # Runs with cwd set to a snapshot of the submitted workspace; FF_SUBMISSION holds its path.
        # {private} and {submission} expand to those directories. The last stdout line is the
        # score (higher is better); earlier stdout lines are shown to the agent as feedback; stderr
        # stays private. A non-zero exit marks the submission invalid.
        "command": ["python3", "{private}/grader/grade.py"],
        "timeout_s": 600,
        "workers": 1,
        # Pause every running agent while grading, so timing-based scores are not disturbed.
        "exclusive": False,
        # How long `.ff/submit` waits for its result before telling the agent to check later.
        "submit_wait_s": 480,
    },
    # Agents run sandboxed: they write only their workspace, cannot read private/, and have no
    # network. model and effort: null keeps the CLI's default. args: extra command-line flags.
    "claude": {
        "model": None,
        "effort": None,
        "args": ["--permission-mode", "auto"],
        # Merged into the --settings that sandbox the agent, e.g. to allow network domains:
        # {"sandbox": {"network": {"allowedDomains": ["pypi.org", "files.pythonhosted.org"]}}}
        "settings": {},
    },
    "codex": {
        "model": None,
        "effort": None,
        "args": [],
        # Merged into the thread's config, which holds the permission profile, e.g. for network:
        # {"permissions": {"fork-and-flush": {"network": {"enabled": true}}}}
        "config": {},
    },
}


class ConfigError(Exception):
    pass


def defaults() -> dict:
    return copy.deepcopy(DEFAULTS)


def is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def is_count(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x >= 1


def is_fraction(x) -> bool:
    return is_number(x) and 0 < x < 1


def inside(path: str, parent: str) -> bool:
    path, parent = os.path.realpath(path), os.path.realpath(parent)
    return path == parent or path.startswith(parent + os.sep)


def temp_roots() -> list:
    """Directories every agent sandbox lets agents write to, besides their own workspace."""
    return [tempfile.gettempdir(), "/tmp", "/var/tmp"]


# (check, message) pairs; a check gets the merged config and the run directory.
CHECKS = [
    (
        lambda c, run: os.path.isdir(c["workspace"] or ""),
        "workspace must be an existing directory",
    ),
    (
        lambda c, run: (
            not (inside(run, c["workspace"] or "/") or inside(c["workspace"] or "/", run))
        ),
        "the run directory and the workspace must not contain each other",
    ),
    (
        lambda c, run: not any(inside(run, tmp) for tmp in temp_roots()),
        "the run directory must not be in a temporary directory, which agents can write to",
    ),
    (
        lambda c, run: c["agent"] in HARNESSES,
        f"agent must be one of: {', '.join(HARNESSES)}",
    ),
    (
        lambda c, run: shutil.which(str(c["agent"])) is not None,
        "the agent's CLI is not on PATH",
    ),
    (
        lambda c, run: is_number(c["budget_mtok"]) and c["budget_mtok"] > 0,
        "budget_mtok must be > 0",
    ),
    (
        lambda c, run: all(is_number(c["token_weights"].get(k)) for k in Usage._fields),
        f"token_weights needs a number for each of: {', '.join(Usage._fields)}",
    ),
    (lambda c, run: is_count(c["k"]), "k must be a positive integer"),
    (
        lambda c, run: is_fraction(c["explore_frac"]),
        "explore_frac must be between 0 and 1",
    ),
    (
        lambda c, run: all(map(is_fraction, c["fork_at"])) and c["fork_at"] == sorted(c["fork_at"]),
        "fork_at must be increasing fractions between 0 and 1",
    ),
    (
        lambda c, run: is_count(c["k"]) and c["directed_branches"] in range(c["k"] + 1),
        "directed_branches must be between 0 and k",
    ),
    (lambda c, run: is_count(c["parallel"]), "parallel must be a positive integer"),
]


def validate(cfg: dict, run_dir: str) -> None:
    errors = [message for check, message in CHECKS if not check(cfg, run_dir)]
    if errors:
        raise ConfigError("; ".join(errors))


def load(run_dir: str) -> dict:
    path = os.path.join(run_dir, CONFIG_FILE)
    try:
        with open(path) as f:
            cfg = merge(DEFAULTS, json.load(f))
    except FileNotFoundError:
        raise ConfigError(f"{path} not found; create the run with `fork-and-flush init`") from None
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from None
    validate(cfg, run_dir)
    return cfg


def read_task(run_dir: str) -> str:
    path = os.path.join(run_dir, TASK_FILE)
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        raise ConfigError(f"{path} not found") from None
    if TEMPLATE_MARKER in text or not text.strip():
        raise ConfigError(f"{path} still needs the task statement")
    return text
