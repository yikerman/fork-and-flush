"""The fork-and-flush schedule (Section 4 of the paper).

A run is a fixed sequence of phases (see state.plan), each ending at a fraction of the budget:
  round  K branches, each with its own copy of the files and a fresh session; the best-scoring one
         becomes the leader. The opening round copies the initial workspace, later rounds copy the
         leader's (the fork), and every branch starts a new session (the flush).
  lead   the leader continues alone, with the same files and session, until the next fork point.

State is saved after every episode and grade, so a stopped or crashed run resumes where it left off.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import threading
import time
from operator import itemgetter

from . import config, prompts
from . import state as runstate
from .grading import run_grader
from .harness import HARNESSES, EpisodeRequest
from .mailbox import Mailroom, install
from .proc import paused
from .workspace import copy_tree, make_readonly, remove_tree

# Below this allowance (weighted tokens) an episode is not worth starting.
MIN_EPISODE_TOKENS = 20_000


class Orchestrator:
    def __init__(self, run_dir: str):
        self.run_dir = os.path.abspath(run_dir)
        self.cfg = config.load(self.run_dir)
        self.task = config.read_task(self.run_dir)
        self.harness = HARNESSES[self.cfg["agent"]](self.cfg)
        self.budget = self.cfg["budget_mtok"] * 1e6
        self.state = runstate.load(self.run_dir) or runstate.new(self.cfg)
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.mailroom = Mailroom(
            self.grade, self.path("submissions"), self.cfg["grader"]["workers"]
        )
        self.phases = {"round": self.round, "lead": self.lead}
        self.private = self.path(config.PRIVATE_DIR)  # agents cannot read it
        self.agents: dict = {}  # branch id -> running agent CLI process

    # ---- bookkeeping ----

    def path(self, *parts: str) -> str:
        return os.path.join(self.run_dir, *parts)

    def save(self) -> None:
        with self.lock:
            runstate.save(self.run_dir, self.state)

    def update(self, branch: dict, **fields) -> None:
        with self.lock:
            branch.update(fields)
        self.save()

    def spent(self) -> float:
        return runstate.spent(self.state)

    def event(self, kind: str, **fields) -> None:
        with self.lock, open(self.path("events.jsonl"), "a") as f:
            f.write(json.dumps({"time": time.time(), "event": kind, **fields}) + "\n")
        detail = " ".join(f"{k}={v}" for k, v in fields.items())
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {kind} {detail}",
            file=sys.stderr,
            flush=True,
        )

    def request_stop(self) -> None:
        """Agents are asked to stop within a second; the run can be resumed later."""
        self.stopping.set()

    # ---- phases ----

    def run(self) -> int:
        if self.state["done"]:
            print("this run has already finished", file=sys.stderr)
            return 0
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: self.request_stop())
        self.mailroom.start()
        self.event("run_started", phase=self.state["phase"], spent=int(self.spent()))
        try:
            self.run_phases()
        finally:
            self.mailroom.close()
            self.save()
        return 0

    def run_phases(self) -> None:
        plan = self.state["phases"]
        while self.state["phase"] < len(plan) and not self.stopping.is_set():
            i = self.state["phase"]
            self.event("phase_started", phase=i, type=plan[i]["kind"], spent=int(self.spent()))
            self.phases[plan[i]["kind"]](i, plan[i])
            if self.stopping.is_set():
                break
            with self.lock:
                self.state["phase"] = i + 1
            self.save()
            self.sweep()
        if self.state["phase"] == len(plan):
            self.finish()
        else:
            self.event("run_stopped", phase=self.state["phase"], spent=int(self.spent()))

    def round(self, i: int, phase: dict) -> None:
        parent = None if phase["fresh"] else self.state["leader"]
        k, directed = phase["k"], phase["directed"]
        ids = runstate.members(self.state, i)  # some may exist already if the run was resumed
        # Directed branches come last, so ties in selection go to undirected ones.
        ids += [self.new_branch(i, parent, j >= k - directed) for j in range(len(ids), k)]
        explore = self.cfg["explore_frac"] * self.budget
        slots = threading.Semaphore(self.cfg["parallel"])

        def explore_branch(bid: str) -> None:
            b = self.state["branches"][bid]
            with slots:
                self.run_branch(bid, lambda: min(explore - b["spent"], self.budget - self.spent()))

        threads = [threading.Thread(target=explore_branch, args=(bid,)) for bid in ids]
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive():
                t.join(timeout=0.5)
        if not self.stopping.is_set():
            self.select(ids, parent)

    def lead(self, i: int, phase: dict) -> None:
        target = phase["until"] * self.budget
        self.run_branch(self.state["leader"], lambda: target - self.spent())

    def select(self, ids: list, parent: str | None) -> None:
        branches = self.state["branches"]
        winner = max(ids, key=lambda bid: (runstate.score(branches[bid]), -ids.index(bid)))
        with self.lock:
            for bid in ids:
                branches[bid]["status"] = "leader" if bid == winner else "discarded"
            if parent:
                branches[parent]["status"] = "retired"
            self.state["leader"] = winner
        self.event(
            "round_closed",
            winner=winner,
            scores={bid: branches[bid]["best"] for bid in ids},
        )

    def sweep(self) -> None:
        """Delete the workspaces of branches that will never run again (after the state that says so
        is saved, so a resumed run never needs them)."""
        if self.cfg["keep_discarded_workspaces"]:
            return
        for b in self.state["branches"].values():
            if b["status"] in ("discarded", "retired"):
                remove_tree(b["workspace"])

    def finish(self) -> None:
        best = runstate.best(self.state)
        with self.lock:
            self.state["done"] = True
        self.save()
        link = self.path("best")
        remove_tree(link)
        if best:
            os.symlink(os.path.relpath(best["best_snapshot"], self.run_dir), link)
        self.event(
            "run_finished",
            spent=int(self.spent()),
            best=best and best["best"],
            submission=best and best["best_submission"],
        )

    # ---- branches ----

    def new_branch(self, phase: int, parent: str | None, directed: bool) -> str:
        bid = f"b{len(self.state['branches']) + 1}"
        source = self.state["branches"][parent]["workspace"] if parent else self.cfg["workspace"]
        workspace = self.path("workspaces", bid)
        inherited = self.lineage_best(parent) if parent else None
        copy_tree(source, workspace)
        incumbent = inherited["snapshot"] if inherited else None
        install(workspace, self.task, self.cfg["grader"]["submit_wait_s"], incumbent)
        record = runstate.new_branch(bid, workspace, phase, parent, directed, inherited)
        with self.lock:
            self.state["branches"][bid] = record
        self.save()
        self.event("branch_created", branch=bid, parent=parent, directed=directed)
        return bid

    def lineage_best(self, bid: str) -> dict | None:
        b = self.state["branches"][bid]
        own = {"score": b["best"], "snapshot": b["best_snapshot"]}
        candidates = [c for c in (b["inherited"], own) if c and c["score"] is not None]
        return max(candidates, key=itemgetter("score"), default=None)

    def run_branch(self, bid: str, allowance) -> None:
        b = self.state["branches"][bid]
        self.update(b, status="running", unproductive=0)
        self.mailroom.watch(bid, b["workspace"])
        try:
            while self.keep_going(b, allowance()):
                self.episode(b, allowance())
        finally:
            self.mailroom.unwatch(bid, self.stopping)
        self.update(b, status=self.outcome(b))

    def keep_going(self, b: dict, allowance: float) -> bool:
        return (
            not self.stopping.is_set()
            and allowance >= MIN_EPISODE_TOKENS
            and b["unproductive"] < self.cfg["max_failed_episodes"]
        )

    def outcome(self, b: dict) -> str:
        if self.stopping.is_set():
            return "stopped"
        return "failed" if b["unproductive"] >= self.cfg["max_failed_episodes"] else "finished"

    def prompt_for(self, b: dict) -> str:
        """The first message of a session carries the task; later ones only say to continue."""
        if b["session"]:
            return prompts.CONTINUE
        wait = self.cfg["grader"]["submit_wait_s"]
        inherited = b["inherited"]["score"] if b["inherited"] else None
        return prompts.first_message(self.task, wait, b["directed"], inherited)

    def episode(self, b: dict, allowance: float) -> None:
        n = b["episodes"] + 1
        logs = self.path("logs", b["id"])
        os.makedirs(logs, exist_ok=True)
        self.event("episode_started", branch=b["id"], episode=n, allowance=int(allowance))
        ep = self.harness.run(
            EpisodeRequest(
                workspace=b["workspace"],
                prompt=self.prompt_for(b),
                session=b["session"],
                used_before=b["used"],
                allowance=allowance,
                log_base=os.path.join(logs, f"ep{n}"),
                timeout_s=self.cfg["episode_timeout_s"],
                env={"FF_BRANCH": b["id"]},
                stop=self.stopping,
                on_session=lambda session: self.update(b, session=session),
                hidden=[self.private],
                on_process=lambda proc: self.track(b["id"], proc),
            )
        )
        # Usage is cumulative per session, so this also charges what a crashed episode used.
        cost = max(0.0, ep.used - b["used"])
        self.update(
            b,
            episodes=n,
            session=ep.session,
            used=ep.used,
            spent=b["spent"] + cost,
            unproductive=0 if cost else b["unproductive"] + 1,
        )
        spent = int(self.spent())
        self.event(
            "episode_finished",
            branch=b["id"],
            episode=n,
            tokens=int(cost),
            spent=spent,
            error=ep.error,
        )

    # ---- grading ----

    def track(self, bid: str, proc) -> None:
        with self.lock:
            self.agents[bid] = proc
            if proc is None:
                del self.agents[bid]

    def quiet(self):
        """With exclusive grading, every running agent is paused while the grader measures."""
        if not self.cfg["grader"]["exclusive"]:
            return contextlib.nullcontext()
        with self.lock:
            return paused(list(self.agents.values()))

    def grade(self, bid: str, sid: str, snapshot: str) -> tuple:
        """Grade a submission snapshot; returns the reply for the agent and a one-line summary."""
        log = os.path.join(self.private, "grades", f"{sid}.log")
        with self.quiet():
            result = run_grader(self.cfg, self.run_dir, snapshot, log)
        b = self.state["branches"][bid]
        with self.lock:
            previous = b["best_snapshot"]
            improved = result.score is not None and result.score > runstate.score(b)
            if improved:
                b.update(best=result.score, best_submission=sid, best_snapshot=snapshot)
        self.save()
        if improved:
            make_readonly(snapshot)
        # Only each branch's best snapshot is kept; it is also the protected incumbent for forks.
        remove_tree(previous if improved else snapshot)
        record = {
            "time": time.time(),
            "submission": sid,
            "branch": bid,
            "score": result.score,
            "error": result.error,
        }
        with self.lock, open(self.path("submissions.jsonl"), "a") as f:
            f.write(json.dumps(record) + "\n")
        self.event("graded", branch=bid, submission=sid, score=result.score, error=result.error)

        valid = result.score is not None
        outcome = (
            f"score {prompts.format_score(result.score)}" if valid else f"INVALID: {result.error}"
        )
        reply = f"submission {sid}: {outcome}\nyour best so far: {prompts.format_score(b['best'])}"
        return "\n\n".join(filter(None, [reply, result.feedback[-4000:]])), f"{sid}  {outcome}"
