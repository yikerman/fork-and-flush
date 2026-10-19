"""Messages sent to branch agents; CONTINUE, DIRECTIVE and INCUMBENT follow Appendix I.1.

Agents are never told the budget or how much of it remains.
"""

from __future__ import annotations

from .mailbox import INCUMBENT_PATH

SUBMIT = """\
## Submitting

Your work is scored by a grader you cannot see. To grade the current state of this directory, run:

    python3 .ff/submit

It snapshots the directory, waits for the grader (up to {wait_min} minutes) and prints the score,
followed by any feedback the grader gives. Higher is better. Submit as often as you like: your best
submission is what counts, so a worse submission never loses earlier progress. Past results are in
`.ff/scores.log`. Grading can take a while, so give the command a long enough timeout; if it stops
waiting, the result still lands in `.ff/scores.log`.

Work inside this directory. A copy of this task statement is at `.ff/TASK.md`."""

CONTINUE = (
    "Continue — this task is open-ended and continuously scored; there is no fixed 'done'. "
    "Keep working in this directory. Do not stop."
)

DIRECTIVE = (
    "Before continuing, deliberately pick a DIFFERENT approach from the one currently implemented. "
    "Do not spend this session incrementally tuning what is already there. Ask whether a "
    "fundamentally different algorithm, data structure, or representation would do better, "
    "and if it would, implement that instead. It is acceptable for the measured result to get "
    "worse before it gets better."
)

INCUMBENT = """\
— PROTECTED LINEAGE INCUMBENT —
The inherited lineage-best submission scored {score}. A read-only copy of the directory as it was
at that submission is preserved at {path}/. The current directory may differ from it. Preserve the
inherited result or improve it, recovering files from the protected copy if needed; do not
silently replace it with something worse.
— END PROTECTED LINEAGE INCUMBENT —"""


def first_message(task: str, wait_s: float, directed: bool, inherited: float | None) -> str:
    """A branch's first message; inherited is the score of the best submission it inherited."""
    parts = [
        task.strip(),
        SUBMIT.format(wait_min=max(1, round(wait_s / 60))),
        DIRECTIVE if directed else "",
        INCUMBENT.format(score=format_score(inherited), path=INCUMBENT_PATH)
        if inherited is not None
        else "",
    ]
    return "\n\n".join(filter(None, parts)) + "\n"


def format_score(score: float | None) -> str:
    return "none" if score is None else format(score, ".10g")
