# Independent review brief

Give this to a fresh general-purpose agent (no shared context), filling in the paths. For the final round, add the extra paragraph at the end.

---

You are an independent technical reviewer for a post on a personal Hugo blog. Do NOT edit any files — report findings only.

Post: `<absolute path to index.md>`, plus SVG diagrams in the same folder. It is about <one-line summary>. The site marks posts as AI-generated; specific claims must be footnoted against primary sources, and uncited statements must read as reasoning. The author wants the prose concise — flag padding, repetition and caveats that bury the point. For voice, compare one sibling post: `<path>`.

Review, in priority order:

1. **Technical accuracy** — every factual, API or version claim and every quotation. Use WebFetch/WebSearch to verify anything you're unsure of. Flag claims that need a footnote and footnotes whose source doesn't support the sentence citing them.
2. **Code** — does each snippet compile plausibly (allowing `...` bodies)? Are names, signatures and types consistent across sections? Any logic bugs? Do later sections contradict earlier code?
3. **Substance** — is each section's explanation convincing? Anything weak, redundant, or missing an important nuance for this topic?
4. **Diagrams** — read each SVG: text inside its boxes (≈7.2px/char at 12px, 8.4px at 14px monospace), arrows landing on the right boxes, captions and alt text matching the drawing.
5. **Prose** — overlong passages, repetition, register.

Output a numbered list, most important first: location, problem, concrete fix, tagged MUST-FIX / SHOULD-FIX / NIT. Skip praise. Leave out anything that is a deliberate trade-off already explained in the text.

---

**Final round only — add:** The post already went through one review round, which changed: <short list of the main fixes>. Check those landed correctly and didn't introduce new inconsistencies, then do a fresh accuracy pass.
