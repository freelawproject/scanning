# bl-warm comparison viewer

Side-by-side evidence for replacing the three-model YOLO ensemble
(small/medium/large) with the single `bl_warm` model. The production
code change itself is the small diff on this branch
(`scanning/settings/project/runpod.py` + `scanning/services.py`);
this folder is a standalone viewer for judging the swap.

## Run

1. Get the data zip (shared separately) and unzip it here so `data/`
   sits next to `app.py`.
2. `uv run uvicorn app:app --port 8990`
3. Open http://localhost:8990

## Views

- **Classes** (`/`) — the 270 held-out hand-annotated golden pages;
  three synced panes, each with a source dropdown: ground truth,
  bl-warm (all classes — raw model output incl. heading/blockquote),
  bl-warm (redaction classes — what the scanning flow keeps), or the
  legacy ensemble. Redaction classes draw solid, layout containers
  (body, footnote_block, heading, blockquote, image) dashed. Class
  chips filter, the slider sets a confidence floor, `b` toggles
  boxes, `l` the class+conf labels, `r` the redaction group, `c` the
  container group, `[` `]` navigate, `f` flags a page (notes persist
  to `data/notes.json` — send that file back with feedback).
- **Redaction** (`/redaction`) — every page of three full val/test
  volumes, three synced panes: unredacted processing copy · bl-warm
  redaction · legacy ensemble redaction. Page arrows/slider,
  `✦ golden ›` (or `}`) jumps to the next hand-annotated page. The
  `redaction classes` button (or `b`) overlays each model's
  redaction-class detections on its own pane — all detections, so
  below-gate ones explain missing redactions (hover a box for
  label · confidence).

## Numbers behind it

End-to-end through the scanning redaction flow, macro F1 vs the
golden annotations: legacy ensemble 0.727 (val) / 0.705 (test);
bl-warm 0.965 / 0.907. Box-level and per-class tables live in the
blackletter branch's `docs/bl_warm.md`.
