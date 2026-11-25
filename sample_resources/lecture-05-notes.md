# Lecture 5 — Testing With AI (my notes)

Date: 2026-09-22

- Unit tests vs integration tests: unit tests isolate one function, integration
  tests exercise the seams (API ↔ DB). Claude Code writes decent unit tests if
  you give it the function AND an example of the project's existing test style.
- Vitest: `describe`/`it`/`expect`, mock with `vi.fn()`. Coverage isn't the
  goal — catching regressions is.
- Key idea from class: write the failing test FIRST, then let the agent
  implement until green. The test is the spec.
- TODO: try mutation testing on P1 before the deadline.
