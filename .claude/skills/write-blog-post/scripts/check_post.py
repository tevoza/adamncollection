"""Build the site with drafts and check one post's footnotes and images.

Usage (from the repo root):
    python .claude/skills/write-blog-post/scripts/check_post.py content/<section>/<slug>

Exits non-zero if the build fails or the post has problems.
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile


# Keep in sync with references/svg-diagrams.md.
PALETTE = {"#08111f", "#13233b", "#3a4d6b", "#7dd3fc", "#c084fc", "#ebf1ff",
           "#9baeca", "#f87171", "#fca5a5", "#2a1620", "#0d1829"}


def find_hugo():
    on_path = shutil.which("hugo")
    if on_path:
        return on_path
    # WinGet installs Hugo without putting it on PATH for every shell.
    local = os.environ.get("LOCALAPPDATA", "")
    hits = glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "Hugo.Hugo*", "**", "hugo.exe"), recursive=True)
    return hits[0] if hits else None


def check_post(bundle):
    problems = []
    index = os.path.join(bundle, "index.md")
    with open(index, encoding="utf-8") as f:
        text = f.read()

    front = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not front:
        problems.append("no front matter")
    else:
        fm = front.group(1)
        draft = re.search(r"^draft:\s*true\s*$", fm, re.M) is not None
        print("draft: " + ("true" if draft else "false (published; leave it unless the user asks)"))
        if '"ai-generated"' not in fm:
            problems.append('tags are missing "ai-generated" (it drives the disclosure banner)')

    defined = set(re.findall(r"^\[\^([^\]]+)\]:", text, re.M))
    referenced = set(re.findall(r"\[\^([^\]]+)\](?!:)", text))
    for name in sorted(defined - referenced):
        problems.append(f"footnote [^{name}] is defined but never referenced")
    for name in sorted(referenced - defined):
        problems.append(f"footnote [^{name}] is referenced but never defined")

    for img in re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text):
        if not img.startswith(("http://", "https://")) and not os.path.exists(os.path.join(bundle, img)):
            problems.append(f"image {img} not found in the bundle")

    body = text[front.end():] if front else text
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    body = re.sub(r"^\[\^[^\]]+\]:.*$", "", body, flags=re.M)
    sections = [("(intro)", 0)]
    for line in body.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), 0))
        else:
            name, count = sections[-1]
            sections[-1] = (name, count + len(line.split()))
    words = sum(c for _, c in sections)

    warnings = []
    for svg in sorted(glob.glob(os.path.join(bundle, "*.svg"))):
        with open(svg, encoding="utf-8") as f:
            src = f.read()
        name = os.path.basename(svg)
        small = sorted({float(s) for s in re.findall(r"font-size[:=]\s*\"?([\d.]+)", src) if float(s) < 12})
        if small:
            problems.append(f"{name}: text below 12px ({', '.join(f'{s:g}px' for s in small)})")
        off = sorted({c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b", src)} - PALETTE)
        if off:
            warnings.append(f"{name}: colours outside the palette: {', '.join(off)}")
    return problems, warnings, len(defined), words, sections


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    bundle = sys.argv[1].rstrip("/\\")

    hugo = find_hugo()
    if not hugo:
        print("Hugo not found on PATH or under WinGet packages.")
        return 1
    dest = os.path.join(tempfile.gettempdir(), "hugo-check")
    build = subprocess.run([hugo, "-D", "--destination", dest, "--quiet"], capture_output=True, text=True)
    if build.returncode != 0:
        print("Hugo build failed:\n" + build.stdout + build.stderr)
        return 1
    print("build: ok")

    problems, warnings, footnotes, words, sections = check_post(bundle)
    print(f"footnotes: {footnotes}, prose words (excluding code and footnotes): {words}")
    for name, count in sections:
        print(f"  {count:>6}  {name}")
    for w in warnings:
        print("warning: " + w)
    for p in problems:
        print("problem: " + p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
