# Hugo Blog Starter

This repo is now set up as a minimal Hugo blog that can deploy to GitHub Pages with GitHub Actions.

## What Is Included

- a self-contained Hugo site
- no external theme or git submodules
- starter post and homepage
- GitHub Actions workflow for GitHub Pages

## Project Structure

```text
.
├── .github/workflows/hugo.yml
├── archetypes/default.md
├── content/
│   ├── software/
│   ├── thoughts/
│   ├── food/
│   └── book-reviews/
├── layouts/
├── static/css/site.css
└── hugo.toml
```

## Content Sections

This site is organized into Hugo sections.

Each top-level folder inside `content/` becomes its own section with its own list page:

- `content/software/` -> `/software/`
- `content/thoughts/` -> `/thoughts/`
- `content/food/` -> `/food/`
- `content/book-reviews/` -> `/book-reviews/`

This makes it easy to keep different kinds of writing separate while still showing recent writing together on the homepage.

## 1. Install Hugo

Install the extended version if possible.

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install hugo
```

### Arch

```bash
sudo pacman -S hugo
```

### macOS

```bash
brew install hugo
```

### Windows

```bash
choco install hugo-extended
```

Check it:

```bash
hugo version
```

## 2. Update Site Settings

Edit [hugo.toml](/home/adam/projects/adamncollection/hugo.toml) and set:

- `baseURL` to your final GitHub Pages URL
- `title` to your blog name
- `params.author` to your name
- `params.description` to your blog description
- `params.githubRepo` to your repo URL

Examples:

```toml
baseURL = "https://yourusername.github.io/"
```

or, if the repo is not your user site:

```toml
baseURL = "https://yourusername.github.io/adamncollection/"
```

## 3. Run Locally

```bash
hugo server -D
```

Open `http://localhost:1313`.

## 4. Write Posts

Create a post inside a section:

```bash
hugo new content software/my-post.md
```

You can also create posts in other sections:

```bash
hugo new content thoughts/my-essay.md
hugo new content food/weeknight-pasta.md
hugo new content book-reviews/how-to-win-friends-and-influence-people.md
```

Then edit the generated file in the matching `content/<section>/` folder.

If you want the post to appear on the live site, change:

```yaml
draft: true
```

to:

```yaml
draft: false
```

## 5. Deploy With GitHub Actions

1. Create a GitHub repo and push this project to the `main` branch.
2. In GitHub, open `Settings` -> `Pages`.
3. Set `Source` to `GitHub Actions`.
4. Push to `main` again if needed!

The workflow file is [hugo.yml](/home/adam/projects/adamncollection/.github/workflows/hugo.yml).

## 6. First Git Push

```bash
git init
git add .
git commit -m "Initial Hugo blog setup"
git branch -M main
git remote add origin https://github.com/yourusername/your-repo.git
git push -u origin main
```

## Daily Workflow

```bash
hugo new content software/my-post.md
hugo server -D
git add .
git commit -m "Add new post"
git push
```

## Notes

- Draft posts are shown locally with `-D` but are not published unless `draft: false`.
- If your site renders without styling or with broken links on GitHub Pages, double-check `baseURL`.
- This starter uses Hugo's built-in templates plus custom layouts, so there is no theme installation step.
