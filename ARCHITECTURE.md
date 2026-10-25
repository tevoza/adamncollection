# Architecture

How this Hugo site is put together. For install/run/publish steps, see [`readme.md`](readme.md).

## Content model

Each top-level folder under `content/` is a Hugo section: `software`, `thoughts`, `food`, `book-reviews`, `posts`. A section's `_index.md` carries:

```yaml
title: "Software"
description: "Build notes, technical write-ups, and small experiments."
```

An individual entry carries:

```yaml
title: "..."
date: 2026-04-05T19:52:55+02:00
draft: false
tags: []
description: ""
```

New entries are scaffolded with `hugo new content <section>/<name>.md`, which fills this in from `archetypes/default.md` (defaults to `draft: true`).

Adding a new section means: create `content/<section>/_index.md` with `title`/`description`, and add a matching entry under `[[menu.main]]` in `hugo.toml` if it should appear in the nav.

## Templates

No theme, no theme submodule — `layouts/` is hand-written:

- `layouts/_default/baseof.html` — page shell: `<head>`, header/nav (renders `.Site.Menus.main`), footer, and a `{{ block "main" . }}` that child templates fill in.
- `layouts/index.html` — homepage: hero, hand-built section cards for `software`/`thoughts`/`food`/`book-reviews` with live entry counts, and the 6 most recent posts site-wide.
- `layouts/_default/list.html` — section/list pages (e.g. `/software/`): entry count + list of pages in that section, newest first.
- `layouts/_default/single.html` — a single entry: date, title, description, tags, content.
- `layouts/cv/list.html` — the CV at `/cv/` (see below). Standalone: it doesn't use `baseof.html`.

Styling is hand-written CSS with no bundler. `site.css` and `chroma.css` are linked from `baseof.html`; the CV has its own.

- `static/css/site.css` — the site's own layout and typography, with the colour palette as `:root` custom properties.
- `static/css/chroma.css` — syntax highlighting token colours for code blocks, defined against that same palette. Required because `[markup.highlight] noClasses = false` makes Hugo emit CSS classes rather than inline styles, so without these rules code renders unhighlighted. `hugo gen chromastyles --style=<name>` generates an equivalent file if you'd rather not hand-maintain it.
- `assets/css/cv.css` — the CV page's own stylesheet. Not linked: `layouts/cv/list.html` inlines it via Hugo Pipes (`resources.Get | minify`), so it must stay in `assets/`, not `static/`.

## CV page (`/cv/`)

`content/cv/_index.md` holds the CV as structured front matter (contact, skills, experience, education, training) with the profile paragraph as body, plus `photo.jpg` alongside it. The page is shared by direct link and doesn't advertise the blog, but isn't secret: the URL contains the repo name and the source is in this public repo.

- `build.list: never` keeps it out of every page collection (homepage, `/posts/`, section counts); `sitemap.disable` and `outputs: ["HTML"]` keep it out of the sitemap and RSS. Nothing in the menu or templates links to it.
- `layouts/cv/list.html` renders a complete page without `baseof.html` and sets `noindex, nofollow` and `no-referrer`. Its "Download PDF" link points to a PDF that the deploy workflow prints from the built page with headless Chrome, using the A4 `@media print` rules in `cv.css` (so the link 404s under `hugo server`). The filename appears in both `content/cv/_index.md` (`pdf`) and `.github/workflows/hugo.yml`; keep them in sync.
- `photo.jpg` is committed already resized and metadata-free, and published as-is — keep any replacement small.

## Config (`hugo.toml`)

- `baseURL` — deployed site URL (GitHub Pages).
- `[menu.main]` entries — the nav bar; each maps to a section route (`/software/`, `/thoughts/`, etc.) or `/posts/` for the full archive.
- `[taxonomies] tag = "tags"` — enables the `tags` front-matter field as a taxonomy.
- `[markup.highlight] noClasses = false` — syntax highlighting emits CSS classes (styled via `site.css`) rather than inline styles.

## Build & deploy

`.github/workflows/hugo.yml` runs on push to `main` (and manual dispatch): sets up Hugo **extended**, version pinned via the `HUGO_VERSION` env var, builds with `hugo --gc --minify`, prints the CV PDF into `public/cv/`, and deploys via GitHub's official `actions/configure-pages` + `actions/upload-pages-artifact` + `actions/deploy-pages` (not a third-party gh-pages action). Locally, `hugo server -D` serves the site with drafts included.

## `learning/`

A separate, unpublished workspace for practicing agent-assisted learning — not part of the Hugo build, not linked from any section or menu. See [`learning/README.md`](learning/README.md) for its own structure and conventions.
