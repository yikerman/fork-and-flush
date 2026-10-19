# fork-and-flush

An implementation of **fork-and-flush** ([arXiv:2610.07447](https://arxiv.org/abs/2610.07447)) for
Claude Code and Codex. It drives a long-running "autoresearch" agent on a problem with a numeric
score and periodically forks it into fresh-context branches, keeping the best-scoring one:

1. **Opening round:** K agents start from your workspace, each with its own copy and session.
2. **Lead:** the best-scoring branch continues alone (same files, same session) until the next fork point.
3. **Fork and flush:** K new branches copy the leader's files but start **fresh chat contexts**. Each
   gets a slice of the budget, and the best one becomes the new leader.
4. Repeat at every fork point; the leader spends what is left.

The paper's defaults are K=4, about 6% of the budget per branch, and forks at 33% and 66%. In the
paper this beat both a single run and best-of-4 by 66% and 44% (normalized score, equal compute),
because independent runs tend to stay in one "idea basin", and a fresh context with the same files
can jump out of it.

It is meant to be **invoked by hand on hard research problems**: from Claude Code or Codex you ask for
a fork-and-flush run, and the skill walks you through writing the task and grader, then launches and
monitors a background run. Requires Linux or macOS, git, Python 3.9+, and `claude` and/or `codex`.

## Install

```sh
uv tool install git+https://github.com/yikerman/fork-and-flush   # or, from a checkout: uv tool install .
```

Claude Code plugin (the skill):

```
/plugin install fork-and-flush --marketplace yikerman/fork-and-flush
```

or from a local checkout: `/plugin marketplace add /path/to/fork-and-flush`, then
`/plugin install fork-and-flush@fork-and-flush`.

Codex skill: `ln -s "$PWD/skills/fork-and-flush" ~/.codex/skills/fork-and-flush`

## Use

```sh
fork-and-flush init ~/my-problem --agent claude --budget-mtok 50 --name my-problem
# edit  <run>/TASK.md                 the goal, in words: what the agents see
#       <run>/private/grader/grade.py the grader; hidden test data also goes in private/
#       <run>/config.json             model, effort, K, schedule, parallelism...
fork-and-flush check my-problem    # grader deterministic? agents unable to read private/?
fork-and-flush start my-problem    # background; resumable
fork-and-flush status my-problem
fork-and-flush stop my-problem
```

Runs live in `~/.local/share/fork-and-flush/runs/<name>` (`$FF_HOME` overrides). When a run finishes,
`<run>/best` is a read-only snapshot of the workspace at the best submission.

### The grader

Agents get the research goal in words and a score, never the implementation of the score, so they
cannot overfit to it. `grader.command` (default `python3 {private}/grader/grade.py`) runs on a
snapshot of the submitted workspace, with the current directory and `$FF_SUBMISSION` set to that
snapshot. It prints feedback for the agent on stdout and the **score as the last stdout line**
(higher is better). stderr stays private. A non-zero exit marks the submission invalid. It must be
deterministic.

Agents submit by running `python3 .ff/submit` in their workspace as often as they like. That drops a
request in `.ff/mailbox/`; the orchestrator, outside any agent sandbox, snapshots the workspace, runs
the grader and writes the result back. Only each branch's best snapshot is kept.

### Isolation

Agents run inside their CLI's own sandbox, configured through documented settings: they can write
only their workspace, cannot read `<run>/private/`, and have no network unless you allow it.

- **Claude Code:** `--settings` with `sandbox.enabled`, `filesystem.denyRead`, no unsandboxed
  retries and `failIfUnavailable`, plus `Read`/`Edit` deny rules for the file tools the sandbox
  does not cover. Extra settings go in `claude.settings`.
- **Codex:** a permission profile (read everywhere but `private/`, write the workspace, no
  network), passed as the thread's config. Extra config goes in `codex.config`. OpenAI labels
  permission profiles beta.

Both sandboxes also let agents write temporary directories, so a run directory may not live in one
(the default, `~/.local/share/fork-and-flush/runs`, is fine). Both use bubblewrap on Linux (Claude Code also needs `socat`) and Seatbelt on macOS. `check` asks the
agent to read a random canary in `private/` and fails if it can, or if the sandbox is unavailable.

Branches share the machine, so they can slow each other down. When the score depends on timing,
set `parallel` to 1 and `grader.exclusive` to true: branches then run one at a time and all agents
are paused while the grader measures. Results are the same, only slower in wall-clock time.

### Budget

The budget is in **millions of weighted tokens**, the same unit for both agents. The default weights
are input 1, cache read 0.1, cache write 1.25 and output 5, roughly API price ratios. With them,
1M weighted tokens costs about one million input tokens' worth on the model you use.

Usage is read while the agent works, so an episode that exceeds its allowance is stopped mid-run.
Codex gets `turn/interrupt`; Claude Code's headless mode has no interrupt, so it is killed.

## How the agents are driven

Only documented machine interfaces are used:

- **Claude Code:** `claude -p --output-format stream-json --verbose`, plus `--resume <session>` for
  later episodes. Usage comes from each streamed assistant message.
- **Codex:** `codex app-server` (JSON-RPC over stdio): `initialize`, `thread/start` or
  `thread/resume`, `turn/start`, and running totals from `thread/tokenUsage/updated`. OpenAI labels
  the app server experimental.

Both share `AbstractAgentHarness` (`src/fork_and_flush/harness.py`). A harness only says how to start
the CLI, what to send, and how to read one JSON message. Adding another CLI means one more subclass.

Claude Code runs with `--permission-mode auto`, Codex with `approvalPolicy: never`; both inside the
sandboxes above.

## Limits of this version

- Workspaces are plain full copies (no exclusions, no copy-on-write). Large data belongs outside the
  workspace, referenced by absolute path.
- Branches share the machine's CPUs, GPUs and ports (see Isolation for timing-sensitive scores).
- Agent processes, including background jobs they started, are stopped at the end of every episode.
- A Claude Code episode cut off by an orchestrator crash is not charged.

## Development

```sh
uv sync
uv run pytest           # end-to-end tests against fake claude/codex CLIs; no model calls
uv run ruff check . && uv run ruff format --check . && uv run ty check
```
