# Plan a change

Read the user's request, relevant project instructions, current code and named specifications.
Write a concrete implementation plan to `.subagent/PLAN.md`. Include the goal, owned files,
interfaces, expected behavior, edge cases, constraints and verification steps. Do not implement
the plan or edit its implementation files.

Finish the plan with a `## Goal block` section. Put one criterion on each row, numbered
consecutively from 1, in this form:

`<number> <label>: <check>`

Use a short label such as `tests` or `behavior`. Wrap each executable check in exactly one pair of
inline backticks. Leave prose checks unwrapped. Keep rows consecutive with no blank line or other
text between them. Make checks concrete enough to decide whether the plan is complete.
