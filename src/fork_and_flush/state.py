"""The run's persistent state (state.json) and the quantities derived from it.

Only facts are stored: the phase plan, the current phase and leader, and one record per branch.
Totals, the best result and round membership are computed from the branch records.
"""

from __future__ import annotations

import json
import math
import os

STATE_FILE = "state.json"


def plan(cfg: dict) -> list:
    """The phase sequence of Section 4 of the paper. Stored with the run, so later config edits
    cannot reshape a run already under way.

    Without an opening round, the run opens with a single fresh branch, which then leads.
    """

    def round_(fresh: bool, k: int, directed: int) -> dict:
        return {"kind": "round", "fresh": fresh, "k": k, "directed": directed}

    def lead(until: float) -> dict:
        return {"kind": "lead", "until": until}

    opening = round_(fresh=True, k=cfg["k"] if cfg["opening_round"] else 1, directed=0)
    fork = round_(fresh=False, k=cfg["k"], directed=cfg["directed_branches"])
    return [
        opening,
        *[p for until in cfg["fork_at"] for p in (lead(until), fork)],
        lead(1.0),
    ]


def new(cfg: dict) -> dict:
    return {
        "phases": plan(cfg),
        "phase": 0,
        "leader": None,
        "branches": {},
        "done": False,
    }


def new_branch(bid: str, workspace: str, phase: int, parent, directed: bool, inherited) -> dict:
    return {
        "id": bid,
        "workspace": workspace,
        "phase": phase,  # the phase that created it
        "parent": parent,
        "directed": directed,
        "inherited": inherited,  # the lineage's best submission at the fork: {score, snapshot}
        "status": "pending",
        "session": None,
        "episodes": 0,
        "used": 0.0,  # weighted tokens the session has used, as the agent CLI reports it
        "spent": 0.0,  # weighted tokens charged to this branch
        "unproductive": 0,  # consecutive episodes that used no tokens
        "best": None,
        "best_submission": None,
        "best_snapshot": None,
    }


def load(run_dir: str) -> dict | None:
    try:
        with open(os.path.join(run_dir, STATE_FILE)) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save(run_dir: str, state: dict) -> None:
    path = os.path.join(run_dir, STATE_FILE)
    with open(path + ".tmp", "w") as f:
        json.dump(state, f, indent=1)
    os.replace(path + ".tmp", path)


def spent(state: dict) -> float:
    return sum(b["spent"] for b in state["branches"].values())


def score(branch: dict) -> float:
    return -math.inf if branch["best"] is None else branch["best"]


def best(state: dict) -> dict | None:
    """The branch holding the best submission so far."""
    scored = [b for b in state["branches"].values() if b["best"] is not None]
    return max(scored, key=score, default=None)


def members(state: dict, phase: int) -> list:
    """Ids of the branches a round phase created, in creation order."""
    return [bid for bid, b in state["branches"].items() if b["phase"] == phase]
