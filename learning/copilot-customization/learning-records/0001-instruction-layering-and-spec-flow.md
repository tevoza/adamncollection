# 0001-instruction-layering-and-spec-flow

## Context
The goal is to improve Copilot output without adding unnecessary context bloat.

## Key insight
Use a layered approach:

- Personal instructions for stable style and tone
- Repository instructions for durable repo-wide invariants
- Path-specific instructions for local conventions
- Prompt files for repeatable tasks
- Inline prompts for one-off work

## Practical rule
If the text is temporary, task-specific, or likely to conflict later, do not promote it into an instruction. Keep it in the issue, PR, or prompt instead.

## Spec-driven flow
Start with a short user story, add acceptance criteria, and then ask Copilot for a plan before implementation when the task is non-trivial.

## Revisit when
- Instructions start getting long
- Copilot output gets repetitive or confused
- A task keeps appearing and deserves a prompt file
