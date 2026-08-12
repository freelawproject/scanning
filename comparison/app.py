"""Side-by-side comparison viewer: legacy 3-model ensemble vs bl-warm.

Two views, both reading the gitignored data/ folder (shared
separately as a zip — unzip so data/ sits next to this file):

  /            classes — golden ground truth vs model detections on
               the 270 held-out golden pages; per-pane source
               dropdown (GT / bl-warm all-classes raw output /
               bl-warm redaction classes / legacy ensemble), class
               chips, confidence slider
  /redaction   full-volume redaction — every page of three val/test
               volumes, three synced panes: unredacted processing
               copy · bl-warm redaction · legacy ensemble redaction

Run:  uv run uvicorn app:app --port 8990
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
NOTES = DATA / "notes.json"

# container=True marks layout containers (reading-order structure,
# drawn dashed); the rest are redaction-relevant classes (solid).
TAXONOMY: list[dict[str, Any]] = [
    {"name": "key_icon", "color": "#e74c3c"},
    {"name": "keycite", "color": "#b03a2e"},
    {"name": "headnote_bracket", "color": "#9b59b6"},
    {"name": "background", "color": "#556b2f"},
    {"name": "syllabus", "color": "#d4ac0d"},
    {"name": "editorial", "color": "#2c3e50"},
    {"name": "divider", "color": "#7f8c8d"},
    {"name": "caption", "color": "#3498db"},
    {"name": "case_metadata", "color": "#2ecc71"},
    {"name": "case_sequence", "color": "#f39c12"},
    {"name": "page_header", "color": "#e67e22"},
    {"name": "page_number", "color": "#607d8b"},
    {"name": "state_abbreviation", "color": "#00bcd4"},
    {"name": "body", "color": "#5dade2", "container": True},
    {"name": "footnote_block", "color": "#16a085", "container": True},
    {"name": "image", "color": "#ff5722", "container": True},
    {"name": "blockquote", "color": "#7d3c98", "container": True},
    {"name": "heading", "color": "#c0392b", "container": True},
]

SOURCES = ["gt", "bl_warm_raw", "bl_warm", "ensemble"]
SOURCE_TITLES = {
    "gt": "ground truth",
    "bl_warm_raw": "bl-warm (all classes)",
    "bl_warm": "bl-warm (redaction classes)",
    "ensemble": "legacy 3-model ensemble",
}
VERSIONS = ["unredacted", "new", "legacy"]
VERSION_TITLES = {
    "unredacted": "unredacted (processing copy)",
    "legacy": "legacy 3-model redaction",
    "new": "bl-warm redaction",
}

# blackletter detection label -> viewer taxonomy color, for the
# redaction-tab box overlay (containers are excluded at build time;
# CITATION/COURT/DATE/DOCKET/JUDGES are legacy-only caption parts)
_TAX_COLOR = {t["name"]: t["color"] for t in TAXONOMY}
REDACTION_LABEL_COLORS = {
    "KEY_ICON": _TAX_COLOR["key_icon"],
    "HEADNOTE": _TAX_COLOR["keycite"],
    "HEADNOTE_BRACKET": _TAX_COLOR["headnote_bracket"],
    "BACKGROUND": _TAX_COLOR["background"],
    "SYLLABUS": _TAX_COLOR["syllabus"],
    "EDITORIAL": _TAX_COLOR["editorial"],
    "DIVIDER": _TAX_COLOR["divider"],
    "CASE_CAPTION": _TAX_COLOR["caption"],
    "CASE_METADATA": _TAX_COLOR["case_metadata"],
    "CASE_SEQUENCE": _TAX_COLOR["case_sequence"],
    "PAGE_HEADER": _TAX_COLOR["page_header"],
    "PAGE_NUMBER": _TAX_COLOR["page_number"],
    "STATE_ABBREVIATION": _TAX_COLOR["state_abbreviation"],
    "CITATION": "#888888",
    "COURT": "#888888",
    "DATE": "#888888",
    "DOCKET": "#888888",
    "JUDGES": "#888888",
}


def _jsonl(path: Path) -> dict[str, list]:
    out: dict[str, list] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            d = json.loads(line)
            out[d["stem"]] = d["boxes"]
    return out


def _load_classes_sample() -> list[dict]:
    meta_path = DATA / "classes" / "meta.json"
    if not meta_path.exists():
        return []
    meta = json.loads(meta_path.read_text())
    gt = _jsonl(DATA / "classes" / "gt.jsonl")
    preds = {
        "bl_warm_raw": _jsonl(
            DATA / "classes" / "preds" / "bl_warm_raw.jsonl"
        ),
        "bl_warm": _jsonl(DATA / "classes" / "preds" / "bl_warm.jsonl"),
        "ensemble": _jsonl(DATA / "classes" / "preds" / "ensemble.jsonl"),
    }
    pages = []
    for stem in sorted(meta):
        m = meta[stem]
        sources = {
            "gt": {"kind": "golden", "boxes": gt.get(stem, [])},
        }
        for k in ("bl_warm_raw", "bl_warm", "ensemble"):
            sources[k] = {"kind": k, "boxes": preds[k].get(stem, [])}
        pages.append(
            {
                "stem": stem,
                "split": m["split"],
                "file": m["file"],
                "width": m["width"],
                "height": m["height"],
                "sources": sources,
            }
        )
    return pages


def _load_volumes() -> dict:
    p = DATA / "redaction" / "volumes.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _load_notes() -> dict:
    if NOTES.exists():
        try:
            return json.loads(NOTES.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


app = FastAPI(title="bl-warm comparison")
app.mount(
    "/static", StaticFiles(directory=ROOT / "ui" / "static"), name="static"
)
templates = Jinja2Templates(directory=ROOT / "ui" / "templates")
templates.env.globals["asset_v"] = str(
    int(
        max(
            (
                p.stat().st_mtime
                for p in (ROOT / "ui" / "static").rglob("*")
                if p.is_file()
            ),
            default=0,
        )
    )
)


@app.get("/", response_class=HTMLResponse)
def classes_view(request: Request) -> HTMLResponse:
    sample = _load_classes_sample()
    return templates.TemplateResponse(
        request,
        "classes.html",
        {
            "page": "classes",
            "sample": sample,
            "notes": _load_notes(),
            "sources": SOURCES,
            "source_titles": SOURCE_TITLES,
            "taxonomy": TAXONOMY,
            "have_data": bool(sample),
        },
    )


@app.get("/redaction", response_class=HTMLResponse)
def redaction_view(request: Request) -> HTMLResponse:
    volumes = _load_volumes()
    return templates.TemplateResponse(
        request,
        "redaction.html",
        {
            "page": "redaction",
            "volumes": volumes,
            "versions": VERSIONS,
            "version_titles": VERSION_TITLES,
            "label_colors": REDACTION_LABEL_COLORS,
            "have_data": bool(volumes),
        },
    )


def _serve_under(base: Path, *parts: str) -> FileResponse:
    """Serve a file strictly inside *base*, refusing traversal."""
    target = os.path.normpath(os.path.join(str(base.resolve()), *parts))
    if not target.startswith(str(base.resolve()) + os.sep):
        raise HTTPException(status_code=404, detail="not found")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target)


@app.get("/redaction_boxes/{vol}/{version}")
def redaction_boxes(vol: str, version: str) -> FileResponse:
    if version not in ("new", "legacy"):
        raise HTTPException(status_code=404, detail="unknown version")
    return _serve_under(DATA / "redaction", vol, f"boxes_{version}.json")


@app.get("/classes_pages/{name}")
def classes_page_image(name: str) -> FileResponse:
    return _serve_under(DATA / "classes" / "pages", name)


@app.get("/redaction_pages/{vol}/{version}/{name}")
def redaction_page_image(vol: str, version: str, name: str) -> FileResponse:
    if version not in VERSIONS:
        raise HTTPException(status_code=404, detail="unknown version")
    return _serve_under(DATA / "redaction", vol, version, name)


@app.post("/api/note/{stem}")
async def save_note(stem: str, request: Request) -> JSONResponse:
    body = await request.json()
    notes = _load_notes()
    entry = {
        "note": str(body.get("note", ""))[:2000],
        "flag": bool(body.get("flag")),
    }
    if entry["note"] or entry["flag"]:
        notes[stem] = entry
    else:
        notes.pop(stem, None)
    NOTES.parent.mkdir(parents=True, exist_ok=True)
    tmp = NOTES.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(notes, indent=1))
    tmp.replace(NOTES)
    return JSONResponse({"ok": True})
