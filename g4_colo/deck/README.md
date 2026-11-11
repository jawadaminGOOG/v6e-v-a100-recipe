# Deck: Maximizing CPU Utilization on GCP GPU and TPU machines with GKE

Jawad Amin and Karim Roukoz · AI Infra GTM at Google. Footer on every slide: the talk title and page number; names only on slide 1. Ten 1920×1080 slides and one appendix slide. Self-contained HTML that opens
offline.

## Open it

1. Open `index.html` in Chrome, or serve the folder (for example `python3 -m http.server 8097`). There are no network
   calls; the only external link is the slide 2 footnote to the Medium article.
2. Keys: `←` `→` (also `PgUp` `PgDn` `Space`), `Home` `End`, `1`–`9` jump (then →), `N` speaker notes (with the source of every
   number). On slide 4, `→` and `Space` step through the flow first (overview, then steps 1–3) and only then move to the
   next slide; `←` steps back. The dots and arrow buttons under the diagram do the same.
3. URL: `#slide=N`, plus `&step=K` on slide 4. `#deck=…&slide=N` also works. `?shot=1` turns off transitions and
   autoplay. Step slides then show their final frame and replays their key frame. Screenshots and Slides export use it.

| # | Slide | Animated element |
|---|---|---|
| 1 | Title (title, presenters, one-line snippet) | none |
| 2 | The idle half of the host (our numbers, claim, prior-work footnote) | none |
| 3 | Inside the host: G4 vs v6e (to-scale diagrams) | none |
| 4 | How it works on GKE (6 blocks, step-through flow) | steps 0–3 |
| 5 | Measured: G4 controlled experiment | none |
| 6 | G4: serving stays fast with the node full (TTFT vs pods, TTFT vs load) | none |
| 7 | G4: GKE reacts when demand changes (scale-out/in lanes, CPU spike) | replay player |
| 8 | TPU v6e: serving stays fast with the node full (+ per-CPU heatmap) | none |
| 9 | TPU v6e: GKE reacts when demand changes | replay player |
| 10 | What it saves (avoided N4D spend and G4 vs AWS, side by side) | none |
| 11 | Appendix: recipe components (detailed diagram) | none |

## Replays

The replay players show runs of the demo web app's scenario engine (CPU spike, scale-out, scale-in; `DEMO_MOCK=1`),
played through a light copy of the app's core-map renderer. The live web app shows the real clusters. The trimmed frames
are stored in `data/replays.json`. Each scenario plays in about 22 s at the default **Fit** speed: idle stretches are
skipped (`deck_charts.py` `compress()`). **1× / 4× / 8×** play the idle-trimmed recording at that multiple of recorded
time; the slider scrubs.

## Rebuild after new data lands

```bash
g4_colo/.venv/bin/python g4_colo/deck/deck_charts.py   # charts/*.svg|png + js/deck_data.js; prints remaining placeholders
python3 g4_colo/deck/export_frames.py                  # keyframes/*.png for Google Slides (needs Chrome + websockets)
```

| Input | Path | Used on |
|---|---|---|
| G4 controlled experiment | `g4_colo/results/analysis/*.csv`, `results/cells/<cell>/telemetry.csv` | slide 5 |
| Native G4 sweep | `.agents/bench/sweep1_merged/native.{samples.jsonl,marks.json}`; scale-out/in lanes from the rerun `.agents/bench/g4_scale2/` with `DECK_G4_SCALE2=1` | slides 6–7 |
| Native v6e sweep | `.agents/bench/v6e_sweep1/<name>.{samples.jsonl,marks.json,cpus.jsonl}`; used only with `DECK_V6E_FINAL=1` (placeholders until the sweep, incl. the scale-out/in rerun, is final) | slides 8–9 (same code as 6–7) |
| Replays | `data/replays.json` (rebuilt from raw `/api/state` jsonl when `DECK_MOCK_REPLAYS=<dir>` is set) | slides 7, 9 |
| Pricing | `$DECK_PRICING_DIR` (default: the planner's `scratch/deck/pricing/`): `pricing.json`, `g4_vs_aws.csv`, `guidance.csv` | slide 10 |

Every chart comes from real sweep or experiment data. A missing input becomes a chart marked **PLACEHOLDER**; no data
is ever invented.

## Google Slides version

Every slide is 16:9 at 1920×1080. Body text is at least 18 px and chart text 15–17 px at native size. Meaning never
depends on hover, scroll or animation, and every chart or diagram exists as one clean file.

| # | Slides layout | Elements to place |
|---|---|---|
| 1 | Title slide | Title, presenters, snippet (text boxes) |
| 2 | Title and body | Title, claim (text); host strips: crop `keyframes/slide-2.png` or rebuild as shapes; key and footnote with the Medium link (text) |
| 3 | Title only | Title, subtitle (text); diagram `keyframes/hosts.png`; callout as a text box |
| 4 | Title only | Title, subtitle, the 3 numbered steps (text); `keyframes/flow-step3.png` (final frame); optional build with `flow-step0..3.png` |
| 5 | Title only + 4 KPI text boxes | KPIs (text); `charts/exp_heatmap.png`, `charts/exp_delta.png`, `charts/exp_cdf.png` |
| 6 | Title, takeaway line, 2 KPI boxes | `charts/g4_latency.png` |
| 7 | Title, takeaway line, 2 KPI boxes | `charts/g4_gpu.png` (lanes, hero), `charts/g4_spike.png`, replay key frame `keyframes/replay-g4-*.png` |
| 8 | Same as 6 | `charts/v6e_latency.png`, `charts/v6e_heatmap.png` |
| 9 | Same as 7 | `charts/v6e_gpu.png`, `charts/v6e_spike.png`, `keyframes/replay-v6e-*.png` |
| 10 | Two columns | Two headline text boxes; guidance table as a native table (`guidance.csv`, 3-yr CUD); `charts/what_you_get.png` (machine cards + waterfall); footnote text box |
| 11 | Title only (appendix) | `keyframes/components.png`; controller-variant line as text |

Speaker notes: copy each slide's notes from `index.html` (`<aside class="notes">`, also shown with `N`).

## Files

| Path | What |
|---|---|
| `index.html` | The slides and their speaker notes |
| `css/deck.css` | Layout and type: Caprasimo headings over Figtree body on an ivory ground, matching the decks on jawadamingoog.github.io |
| `js/deck.js` | Engine (scaling, keys, hash, step slides), host strips, host diagrams, step-through flow, appendix diagram, KPIs, pricing table |
| `js/replay.js` | Replay player: a light copy of `demo/static/app.js` `buildCoreMap` / `updateCoreMap`, with speed control and scrub |
| `js/deck_data.js` | Generated: chart SVGs, KPIs, compressed replay frames, pricing |
| `bench_replay.py` | Replay playback benchmark in headless Chrome (rAF frame times, long tasks, scrub progress per speed) |
| `deck_charts.py` | Generates every chart (SVG for HTML, PNG at 2× for Slides) in the palette |
| `export_frames.py` | Static key frames for Slides (`keyframes/`) |
| `charts/`, `keyframes/` | Generated images |
| `palette.md` | The locked Sanzo Wada palette. Every colour in the deck comes from it |
| `fonts/` | Local copies of Caprasimo, Figtree, JetBrains Mono (SIL OFL 1.1, licences alongside) |
