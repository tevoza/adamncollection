# Mission: Use GitHub Copilot customizations without context bloat

## Why
I want to get measurably better output from GitHub Copilot by using custom instructions, prompt files, and repository docs in a disciplined way. The goal is not to stuff the model with more text. The goal is to make the right context easier to find, easier to reuse, and less likely to conflict.

## Success looks like
- I know what belongs in personal instructions versus repository instructions versus prompt files versus inline prompts
- I can keep repository instructions short, stable, and non-task-specific
- I can turn a user story into a small spec with acceptance criteria before asking Copilot to implement it
- I can document durable project knowledge without duplicating whole manuals inside prompts
- I can tell when instructions are helping and when they are hurting output quality

## Constraints
- Prefer reusable structure over clever wording
- Keep instructions minimal and specific
- Encode invariants, not every exception
- Use examples only when they clarify a recurring pattern

## Out of scope
- Enterprise governance and policy administration
- Large-scale automation around Copilot
- Deep prompt-engineering research beyond what is needed for practical use
