# Deck palette (locked)

Four muted colours from Sanzo Wada's *A Dictionary of Color Combinations* (1933–34), each the nearest muted
swatch to one Google brand colour, plus two Wada neutrals. Hex values are the digitised swatches from
[mattdesl/dictionary-of-colour-combinations](https://github.com/mattdesl/dictionary-of-colour-combinations)
(`colors.json`), picked by CIELAB distance to the Google colour and then by lower chroma.

| Role | Wada name | Hex | Sits next to (Google) | Used for |
|---|---|---|---|---|
| Blue | Olympic Blue | `#5A82B3` | Google Blue `#4285F4` | vLLM / the accelerator workload, GKE-owned blocks, slide 2 accent |
| Red | Eugenia Red (A) | `#E2625E` | Google Red `#EA4335` | Paid N4D overflow, memory-bandwidth neighbours, contention, slide 4 accent |
| Yellow | Yellow Ocher | `#E2B540` | Google Yellow `#FBBC04` | Idle cores, budgets / thresholds, highlights, slide 1 and 5 accent |
| Green | Green | `#489B6E` | Google Green `#34A853` | Backfill (CPU services on spare cores), recipe-owned blocks, slide 3 accent |
| Ink | Black | `#111314` | Google Grey 900 `#202124` | All text, axes, outlines |
| Warm neutral | Warm Gray | `#A1A39A` | Google Grey 500 `#9AA0A6` | Shared/system cores, grid lines, rules, placeholders |

Ground (not a Wada swatch, fixed by the spec): **ivory `#FFFDF7`**. Raised surfaces are ivory mixed with Warm
Gray (8 %).

## Rules

1. Every colour in the deck is one of the six above, the ivory ground, or an opacity mix of them on the ground
   (for example 35 % Olympic Blue for the 50 % colo series). No other hex value appears in `deck.css`,
   `deck.js` or `deck_charts.py`; the constants live in `deck_charts.py` (`PAL`) and in `deck.css` (`:root`).
2. One accent per slide (the slide's dot, link colour and emphasis): 1 Yellow Ocher, 2 Olympic Blue,
   3 Green, 4 Eugenia Red, 5 Yellow Ocher.
3. Data colours are semantic and do not change between slides: vLLM = Olympic Blue, backfill = Green,
   N4D (paid) = Eugenia Red, idle = Yellow Ocher, shared/system = Warm Gray.
4. Text is always Ink (secondary text at 72 % Ink, contrast ≥ 7:1 on ivory). Coloured text is used only at
   large sizes, where the colour pairs meet 3:1.
5. Sequential scale (heatmaps): ivory → Olympic Blue → Ink.
