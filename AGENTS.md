# Agent Instructions

This repo is a personal Hugo blog (`content/`, `layouts/`, `static/css/site.css`, `hugo.toml`), plus `learning/`, a separate, unpublished workspace for practicing agent-assisted learning workflows.

For setup and daily workflow, see [`readme.md`](readme.md). For how the site is structured (content model, templates, config, build/deploy), see [`ARCHITECTURE.md`](ARCHITECTURE.md). Don't restate either here — keep this file short.

## Commands

- `hugo server -D` — preview locally, including drafts, at `http://localhost:1313`.
- `hugo` — build the site (rarely needed by hand; CI does this on deploy).
- `hugo new content <section>/<name>.md` — scaffold a new entry from `archetypes/default.md`.

There is no test suite, no linter, and no package manager here. Don't add one, and don't go looking for one that doesn't exist.

## Invariants

- No theme, no theme submodule. `layouts/` is hand-rolled on purpose — don't introduce a theme dependency.
- Content only lives under `content/<section>/`. New sections need a menu entry in `hugo.toml` and a matching `_index.md`.
- `learning/` is never published to the site. Don't reference it from `content/`, `layouts/`, or `hugo.toml`'s menu.
- Never flip a post's front matter from `draft: true` to `draft: false` unless asked — that's a publish decision.
