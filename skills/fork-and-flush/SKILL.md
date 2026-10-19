---
name: fork-and-flush
description: Set up, launch and monitor a long-running fork-and-flush research run (arXiv:2610.07447) on a hard, open-ended problem with a numeric score, where Claude Code or Codex agents iterate for hours or days and the run periodically forks into fresh-context branches and keeps the best-scoring one. Use only when the user explicitly asks for a fork-and-flush run, or asks to check on, stop or collect the results of one.
---

# Fork-and-flush runs

Fork-and-flush runs headless agents (`claude` or `codex`) on one task for a fixed token budget. An
opening round runs K branches; the best-scoring one leads alone; at each fork point K new branches copy
the leader's files but start with **fresh chat contexts**; the best branch becomes the new leader. All
of this is done by the `fork-and-flush` CLI as a background process. Your job is the setup, with the
user, and later the monitoring.

If the environment variable `FF_BRANCH` is set, you are yourself an agent inside a run: do not use this
skill.

## When it fits

Only for problems with a **deterministic numeric score** that improves through many experiments
(heuristics, optimization, performance engineering, model compression, inverse problems). A pass/fail
test is a poor fit. Say so if the user's problem does not fit, and say what it would cost: the budget
is spent in full.

## Setup, step by step

The CLI is `fork-and-flush` (install: `uv tool install <path or git URL of this repo>`). If commands
in this environment are sandboxed, run `init`, `start` and `stop` outside the sandbox (request
escalated permissions): the run directory lives under `~/.local/share/fork-and-flush/runs/` and the
orchestrator launches agent CLIs that need their own permissions.

1. **Create the run.** Agree with the user on the workspace (the directory agents start from; it is
   copied, never modified), the agent, and the budget in millions of weighted tokens (input 1, cache
   read 0.1, cache write 1.25, output 5 per token):

       fork-and-flush init <workspace> --agent claude|codex --budget-mtok <N> --name <name>

   It prints the run directory and the three files to fill in.

2. **Write `TASK.md`** in the run directory with the user: the goal, what may be changed, constraints
   (time limits, allowed libraries, determinism), and what the score rewards. Delete the template
   marker line. Keep it to the problem: do **not** pass on ideas or approaches from this conversation
   unless the user wants them there, because branches must explore with fresh contexts, and never
   mention the budget.

3. **Write the grader** at `<run>/private/grader/grade.py` (or point `grader.command` in
   `config.json` at another command). Everything under `<run>/private/` is hidden from the agents
   by their CLI's sandbox, so the grader, hidden test data and expected answers all go there, never
   in the workspace. Agents get the goal in words (TASK.md) and a score, never the scoring code.
   Contract:
   - runs with the current directory and `$FF_SUBMISSION` set to a snapshot of the submitted
     workspace; `{private}`, `{run}` and `{submission}` in the command expand to those paths;
   - prints feedback for the agent on stdout, then the score as the **last stdout line** (higher is
     better); stderr is private; a non-zero exit marks the submission invalid. Keep the feedback
     short: everything on stdout reaches the agents;
   - deterministic: fixed seeds; prefer measures that do not depend on the machine (operation
     counts, simulated cycles) over wall-clock time.

4. **Ask whether the score depends on timing.** If it does, set `parallel` to 1 and
   `grader.exclusive` to true, so branches do not slow each other down and every agent is paused
   while the grader measures. The results are the same, only slower in wall-clock time.

5. **Check it:** `fork-and-flush check <run>` grades the initial workspace twice and fails if the
   results differ, then asks the agent, sandboxed as in a run, to read a random canary in
   `private/` and fails if it can (or if the sandbox is unavailable: on Linux it needs `bwrap`, and
   `socat` for Claude Code). Also try a deliberately bad or cheating submission by hand and confirm
   it scores badly; a grader that can be gamed will be.

6. **Review `config.json` with the user**: `agent`, `<agent>.model` and `.effort`, `budget_mtok`,
   `k`, `explore_frac`, `fork_at`, `parallel`, `episode_timeout_s`. Agents have no network by
   default; to allow some, use `claude.settings`
   (`{"sandbox": {"network": {"allowedDomains": ["pypi.org"]}}}`) or `codex.config`
   (`{"permissions": {"fork-and-flush": {"network": {"enabled": true}}}}`).

7. **Get an explicit go-ahead**, then `fork-and-flush start <run>`. It returns immediately.

## Monitoring and results

- `fork-and-flush status <run>` (or `--json`) shows the phase, spend, best score and branches;
  `fork-and-flush list` shows all runs.
- `<run>/events.jsonl` and `<run>/submissions.jsonl` are the full history; `<run>/logs/` holds each
  episode's agent output and each grade's stderr.
- `fork-and-flush stop <run>` stops it; `start` resumes where it left off.
- When finished, `<run>/best` is a read-only snapshot of the workspace at the best submission. Copy
  it into the user's workspace only if they ask.
