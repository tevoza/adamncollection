# SVG diagrams for posts

Diagrams sit in the post's page bundle and render on a dark card. They carry their own dark background so they read the same everywhere.

## Palette

| Use | Value |
|---|---|
| Background | `#08111f` |
| Box fill | `#13233b` |
| Box stroke | `#3a4d6b` |
| Highlight stroke / main arrows | `#7dd3fc` |
| Secondary highlight / second path | `#c084fc` (the site's `--accent-strong`) |
| Text | `#ebf1ff` |
| Muted text / secondary arrows | `#9baeca` |
| "Bad" (before, rejected, smell) | stroke `#f87171`, text `#fca5a5`, fill `#2a1620` |
| Inner/terminal region | `#0d1829` |

## Template

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 760 400" role="img" aria-labelledby="t">
  <title id="t">One or two sentences describing what the diagram shows</title>
  <style>
    text { font-family: ui-monospace, "SFMono-Regular", Consolas, Menlo, monospace; fill: #ebf1ff; font-size: 14px; }
    .sm { font-size: 12px; }
    .muted { fill: #9baeca; font-size: 12px; }
    .bad { fill: #fca5a5; font-size: 12px; }
    .box { fill: #13233b; stroke: #3a4d6b; stroke-width: 1.5; }
    .hl { fill: #13233b; stroke: #7dd3fc; stroke-width: 2; }
    .badbox { fill: #2a1620; stroke: #f87171; stroke-width: 1.5; }
    .ptr { fill: none; stroke: #7dd3fc; stroke-width: 1.75; }
    .dir { fill: none; stroke: #9baeca; stroke-width: 1.5; }
    .badptr { fill: none; stroke: #f87171; stroke-width: 1.75; stroke-dasharray: 5 4; }
    .dash { fill: none; stroke: #3a4d6b; stroke-width: 1.5; stroke-dasharray: 6 4; }
  </style>
  <defs>
    <marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#7dd3fc"/>
    </marker>
    <marker id="m" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#9baeca"/>
    </marker>
    <marker id="r" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#f87171"/>
    </marker>
  </defs>
  <rect width="760" height="400" fill="#08111f"/>
  <!-- boxes: <rect class="box" .../> then centred <text text-anchor="middle"> -->
  <!-- arrows: <path class="ptr" d="M x1 y1 H x2" marker-end="url(#a)"/> -->
  <!-- caption: one muted line near the bottom, centred -->
</svg>
```

Only include the classes and markers you use.

## Making text fit

Monospace makes this arithmetic reliable:

- 12px ≈ **7.2px** per character, 13px ≈ 7.8px, 14px ≈ 8.4px.
- A label fits if `chars × width + ~16px padding ≤ box width`. Check every label, especially long type names like `SqlPaymentRepository` (20 chars → 168px at 14px).
- If it doesn't fit, split onto two lines (`BankTransfer` / `Provider`), drop to 12px, or widen the box — in that order of preference.
- Inside circles, the available width at height `y` is `2·√(r² − (y − cy)²)`; check the cap height (about 10px above the baseline), not just the baseline.
- Arrow ends should stop ~4px short of the target box edge so the marker tip touches it.

## Before finishing a diagram

- Every arrow starts and ends on the box it claims to connect.
- No label overlaps an arrow or another label.
- The caption agrees with what's drawn (e.g. "every solid arrow…" if a dashed one means something else).
- The `<title>` and the markdown alt text both describe what the drawing actually shows — reviewers compare them to the drawing.

Typical widths: 720–820. Keep text ≥ 12px; the diagram scales down on phones.
