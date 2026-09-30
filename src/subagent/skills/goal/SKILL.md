# Write the workspace goal file

Use the user's request and approved plan to create or update `GOAL.md` at the workspace root. The
file must contain only the goal block: its `## Goal block` heading followed by concrete criteria
numbered consecutively from 1, one per row, in this form:

`<number> <label>: <check>`

Use short labels such as `tests` or `behavior`. Wrap each executable check in exactly one pair of
inline backticks. Leave prose checks unwrapped. Do not add a preamble, summary, code fence,
frontmatter or text after the final row. Do not implement the goal.
