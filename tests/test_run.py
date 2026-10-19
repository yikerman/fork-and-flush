import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from fork_and_flush import cli, mailbox
from fork_and_flush import state as runstate
from fork_and_flush.orchestrator import Orchestrator

HERE = Path(__file__).resolve().parent

GRADER = """\
import os
value = int(open(os.path.join(os.environ["FF_SUBMISSION"], "value.txt")).read())
print(f"value.txt holds {value}")
print(value)
"""


def read_json(path: Path):
    return json.loads(path.read_text())


def read_jsonl(path: Path) -> list:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fake agent CLIs on PATH, writing their side records under tmp_path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "codex"):
        exe = bin_dir / name
        script = HERE / "fake_agent.py"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" {name} "$@"\n')
        exe.chmod(0o755)
    (tmp_path / "fake-log").mkdir()
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake-log"))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(mailbox, "POLL_S", 0.02)
    return tmp_path


def make_run(tmp_path: Path, agent="claude", **overrides) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "value.txt").write_text("0")
    (ws / "notes.md").write_text("untracked notes are part of the workspace too\n")
    run_dir = tmp_path / "run"
    argv = [
        "init",
        str(ws),
        "--budget-mtok",
        "2",
        "--agent",
        agent,
        "--run-dir",
        str(run_dir),
    ]
    assert cli.main(argv) == 0
    cfg = read_json(run_dir / "config.json")
    cfg.update({"k": 2, "explore_frac": 0.1, "directed_branches": 1, "parallel": 2})
    cfg.update(overrides)
    (run_dir / "config.json").write_text(json.dumps(cfg))
    (run_dir / "TASK.md").write_text("Make value.txt as large as possible.\n")
    (run_dir / "private" / "grader" / "grade.py").write_text(GRADER)
    return run_dir


def prompts_of(tmp_path: Path, session: str) -> list:
    text = (tmp_path / "fake-log" / f"{session}.prompts").read_text()
    return text.split("\n=====\n")[:-1]


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_full_run(env, agent):
    run_dir = make_run(env, agent)
    assert Orchestrator(str(run_dir)).run() == 0
    state = read_json(run_dir / "state.json")

    assert state["done"]
    # Opening round plus two fork rounds of K=2; spend lands within one episode of the budget.
    assert len(state["branches"]) == 6
    assert 2.0e6 <= runstate.spent(state) <= 2.1e6

    subs = read_jsonl(run_dir / "submissions.jsonl")
    best = runstate.best(state)
    assert best is not None
    assert best["best"] == max(s["score"] for s in subs)
    assert float((run_dir / "best" / "value.txt").read_text()) == best["best"]
    assert (run_dir / "best" / "notes.md").exists()
    assert not (run_dir / "best" / ".ff").exists()

    branches = state["branches"]
    discarded = [b for b in branches.values() if b["status"] == "discarded"]
    assert discarded
    assert all(not os.path.exists(b["workspace"]) for b in discarded)
    for b in branches.values():
        mailbox = Path(b["workspace"]) / ".ff" / "mailbox"
        if mailbox.exists():
            assert not list(mailbox.glob("*.req"))

    # Fresh sessions per branch (the flush); the first message carries the task, submit help,
    # directive and incumbent notice; later episodes only get the continuation message.
    assert len({b["session"] for b in branches.values()}) == 6
    for b in branches.values():
        msgs = prompts_of(env, b["session"])
        assert "Make value.txt as large as possible." in msgs[0]
        assert ".ff/submit" in msgs[0]
        assert ("DIFFERENT approach" in msgs[0]) == b["directed"]
        has_incumbent = b["parent"] is not None and b["inherited"] is not None
        assert ("PROTECTED LINEAGE INCUMBENT" in msgs[0]) == has_incumbent
        assert all(m.startswith("Continue") for m in msgs[1:])
        assert b["episodes"] == len(msgs)


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_episode_stops_at_allowance(env, agent, monkeypatch):
    run_dir = make_run(env, agent, fork_at=[0.5], opening_round=False)
    monkeypatch.setenv("FAKE_SLOW", "1")
    Orchestrator(str(run_dir)).run()
    events = read_jsonl(run_dir / "events.jsonl")
    finished = [e for e in events if e["event"] == "episode_finished"]
    assert finished
    assert all("allowance" in e["error"] for e in finished)
    # Overshoot is bounded by what the agent uses before it is stopped, far below the budget.
    state = read_json(run_dir / "state.json")
    assert 2.0e6 <= runstate.spent(state) < 2.0e6 + 0.5e6


def test_stop_and_resume(env):
    run_dir = make_run(env)
    first = Orchestrator(str(run_dir))
    t = threading.Thread(target=first.run)
    t.start()
    deadline = time.time() + 30
    while time.time() < deadline:
        state_file = run_dir / "state.json"
        if state_file.exists() and read_json(state_file)["phase"] >= 2:
            break
        time.sleep(0.05)
    first.request_stop()
    t.join(timeout=30)
    assert not t.is_alive()
    assert not read_json(run_dir / "state.json")["done"]

    assert Orchestrator(str(run_dir)).run() == 0
    state = read_json(run_dir / "state.json")
    assert state["done"]
    assert len(state["branches"]) == 6
    assert 2.0e6 <= runstate.spent(state) <= 2.1e6


def test_check_requires_deterministic_grader(env, capsys):
    run_dir = make_run(env)
    assert cli.main(["check", str(run_dir), "--no-probe"]) == 0
    assert "OK: grades match" in capsys.readouterr().out
    grader = "import random\nprint(random.random())\n"
    (run_dir / "private" / "grader" / "grade.py").write_text(grader)
    assert cli.main(["check", str(run_dir), "--no-probe"]) == 1


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_probe_catches_a_readable_private_dir(env, agent, capsys):
    # The fake CLIs have no sandbox, so the probe must see the canary and fail.
    run_dir = make_run(env, agent)
    assert cli.main(["check", str(run_dir)]) == 1
    assert "the agent read the canary" in capsys.readouterr().out
    assert list((run_dir / "private").glob("probe-*")) == []


def test_exclusive_grading(env):
    run_dir = make_run(env, grader={"exclusive": True})
    assert Orchestrator(str(run_dir)).run() == 0
    state = read_json(run_dir / "state.json")
    assert state["done"]
    assert runstate.best(state) is not None


def test_template_task_is_rejected(env):
    ws = env / "w"
    ws.mkdir()
    run_dir = env / "r"
    cli.main(["init", str(ws), "--budget-mtok", "1", "--run-dir", str(run_dir)])
    with pytest.raises(SystemExit):
        cli.main(["check", str(run_dir)])


def test_run_dir_in_temp_is_refused(env):
    ws = env / "w"
    ws.mkdir()
    run_dir = Path(tempfile.mkdtemp()) / "run"
    with pytest.raises(SystemExit, match="temporary directory"):
        cli.main(["init", str(ws), "--budget-mtok", "1", "--run-dir", str(run_dir)])
