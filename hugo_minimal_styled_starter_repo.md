# Hugo Minimal Styled Starter Repo

## 1. hugo.toml
```toml
baseURL = "https://yourusername.github.io/"
languageCode = "en-us"
title = "Your Name"
theme = "PaperMod"

[params]
  defaultTheme = "auto"
  ShowReadingTime = true
  ShowPostNavLinks = true
  ShowCodeCopyButtons = true
  ShowBreadCrumbs = false
  ShowShareButtons = false

[params.assets]
  customCSS = ["css/custom.css"]

[outputs]
  home = ["HTML", "RSS"]

[taxonomies]
  tag = "tags"

[menu]
  [[menu.main]]
    name = "Posts"
    url = "/posts/"
    weight = 1
```

---

## 2. archetypes/default.md
```markdown
---
title: "{{ replace .Name "-" " " | title }}"
date: {{ .Date }}
tags: []
draft: false
---

Start writing here.
```

---

## 3. content/_index.md
```markdown
---
title: "Home"
---

Hi, I'm Adam.

This is a simple blog about:
- programming
- running
- systems thinking

## Recent posts

Check the posts section.
```

---

## 4. content/posts/first-post.md
```markdown
---
title: "First Post"
date: 2026-04-05
tags: ["meta"]
---

This is my first post.

I want a blog that is:
- fast
- simple
- mine
```

---

## 5. static/css/custom.css
```css
:root {
  --main-width: 65ch;
}

body {
  font-size: 18px;
  line-height: 1.7;
}

.post-content {
  max-width: 65ch;
  margin: auto;
}

h1, h2, h3 {
  line-height: 1.3;
  margin-top: 2em;
}

p {
  margin-bottom: 1.2em;
}

a {
  text-decoration: none;
  border-bottom: 1px solid #999;
}

a:hover {
  border-bottom: 1px solid #000;
}

pre, code {
  font-family: monospace;
  font-size: 0.9em;
}

pre {
  padding: 1em;
  border-radius: 6px;
}
```

---

## 6. .github/workflows/deploy.yml
```yaml
name: Deploy Hugo site

on:
  push:
    branches:
      - main

jobs:
  build-deploy:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v3
        with:
          submodules: true

      - name: Setup Hugo
        uses: peaceiris/actions-hugo@v2
        with:
          hugo-version: 'latest'

      - name: Build
        run: hugo --minify

      - name: Deploy
        uses: peaceiris/actions-gh-pages@v3
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: ./public
```

---

## 7. Setup Commands
```bash
hugo new site myblog
cd myblog
git init

git submodule add https://github.com/adityatelange/hugo-PaperMod themes/PaperMod

# copy files above into structure

git add .
git commit -m "initial"
git remote add origin https://github.com/yourusername/yourusername.github.io.git
git push -u origin main
```

---

## 8. Daily Workflow
```bash
# new post
hugo new posts/my-post.md

# preview
hugo server

# publish
git add .
git commit -m "post: my post"
git push
```

---

## Done
Your blog is now live and auto-deploying.

