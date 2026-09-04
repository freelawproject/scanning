# dotsmocr-gpu-worker

GPU worker image for [RunPod Serverless] that runs
[dots.mocr](https://github.com/rednote-hilab/dots.mocr) — rednote's 3B
Qwen-VL-based multimodal OCR model — for full-document parsing: per-page
layout detection (bboxes + categories) and text extraction to markdown,
with tables as HTML and formulas as LaTeX.

The image contains only inference code + model weights. No Django, no
database client, no AWS credentials. All sensitive configuration lives
in RunPod endpoint env vars, never in the image.

This worker is a sibling of `../runpod/` (the blackletter YOLO/PaddleOCR
worker) and follows the same conventions: presigned-URL input, JSON
output, structured error codes, worker meta on every response. General
RunPod operations knowledge (billing model, worker states, pods vs
serverless, result retention) is documented once in
[`../runpod/README.md`](../runpod/README.md) and applies here unchanged.

[RunPod Serverless]: https://docs.runpod.io/serverless/overview

## How it works

```
┌─────────────────────┐        ┌────────────────────────────────────┐
│ caller (daemon /    │        │ RunPod Serverless worker           │
│ curl smoke test)    │  POST  │                                    │
│                     ├───────▶│ handler.py                         │
│  - presign GET URL  │  /run  │  - downloads PDF (presigned URL)   │
│  - submit job       │        │  - renders pages with PyMuPDF      │
│  - poll /status     │◀───────│  - fans pages out to local vLLM ──┐│
│  - persist result   │  JSON  │  - rescales bboxes, builds md     ││
└─────────────────────┘        │                                   ││
                               │ vllm serve rednote-hilab/dots.mocr◀┘
                               │ (subprocess, localhost:8000)       │
                               └────────────────────────────────────┘
```

`handler.py` spawns `vllm serve` at worker boot (cold start pays the
model load once) and talks to it over the OpenAI-compatible API on
localhost, mirroring upstream's `dots_mocr/parser.py` client flow —
including the `<|img|><|imgpad|><|endofimg|>` text prefix and
`--chat-template-content-format string`, both of which are required
for correct output. Page fan-out uses a thread pool; vLLM's continuous
batching turns the concurrent requests into efficient GPU batches.

## What's in the image

- `vllm/vllm-openai:v0.17.1` base — the exact deployment path upstream
  recommends (dots.mocr is officially integrated in vLLM ≥ 0.11.0).
  The plain (non `-cu130`) tag keeps the image compatible with RunPod
  hosts whose drivers predate CUDA 13.
- The full `rednote-hilab/dots.mocr` HF snapshot baked into `/opt/hf`
  (`HF_HUB_OFFLINE=1` at runtime — cold start never touches the
  network for weights).
- A separate uv-managed venv (`/opt/venv`) for the handler and its
  client deps, so `dots_mocr`'s pins can never fight vLLM's stack.
- `dots_mocr` pinned to an exact upstream commit for prompts, page
  rendering, bbox rescaling, and markdown conversion.
- `runpod_common.py` and `layout_json.py`, copied from `scanning/`
  next to `handler.py` and imported as top-level modules. Both are
  shared with the daemon on purpose: the first is the result envelope
  and the error codes it classifies, the second is the layout-JSON
  repair of #242, which the daemon's glue also runs over the shard
  results already in the bucket. An arm that drifted between the two
  sides would repair a page differently depending on which side read
  it, so `layout_json.py` imports nothing but the standard library.

### Upstream packaging quirks (read before bumping the pin)

- At the pinned commit, `dots_mocr/model/` ships without an
  `__init__.py`, so the installed package is missing
  `dots_mocr.model` and its package `__init__` crashes on import.
  `handler._ensure_dots_mocr_importable()` shims the missing module
  (the handler uses its own vendored `_vllm_inference` instead), and
  becomes a no-op if a future pin fixes packaging. Re-run the tests
  after any pin bump.
- `dots_mocr`'s requirements.txt is demo-oriented (gradio, modelscope,
  the HF-transformers stack). `pyproject.toml` overrides those away
  with impossible markers; the handler only needs the vLLM client
  path.

## Building locally

Run from the scanning repo root so the build context is only this
directory:

```bash
docker build -t dotsmocr-gpu-worker:local scanning/runpod-dotsmocr/
```

The first build downloads the ~10 GB base image and the ~6 GB model
snapshot; expect it to be bandwidth-bound. You do **not** need a GPU on
the build host — the weights check only verifies files on disk.

To build the SVG variant instead (structured-graphics-to-SVG, not used
by scanning today):

```bash
docker build --build-arg DOTSMOCR_MODEL=rednote-hilab/dots.mocr-svg \
  -t dotsmocr-svg-gpu-worker:local scanning/runpod-dotsmocr/
```

### Running the image locally

Requires a GPU plus the nvidia-container-toolkit:

```bash
docker run --rm --gpus all dotsmocr-gpu-worker:local
```

Without a GPU the container starts, logs a warning, skips the vLLM
startup, and fails the fitness check — same behaviour as on a
misprovisioned RunPod worker.

## Configuring the RunPod endpoint

1. **New Endpoint → Custom Source (Docker Image)**, image =
   `freelawproject/dotsmocr-gpu-worker:<sha>`.
2. **GPU**: RTX A5000 (24 GB) recommended. 16 GB works for light use
   (weights are ~6.7 GB; the rest is KV cache — lower
   `VLLM_GPU_MEMORY_UTILIZATION` headroom means fewer concurrent
   pages).
3. **Container Disk**: ≥ 50 GB (the image extracts to ~30 GB).
4. **Min Workers**: `0`; **Idle Timeout**: `300s`.
5. **Env vars** (endpoint config, NOT the image):
   - `SENTRY_DSN_GPU`, `SENTRY_ENV`, `GIT_SHA` — Sentry wiring.
   - `HANDLER_MAX_PAGES` — reject PDFs above this page count
     (default 5000).
   - `HANDLER_NUM_THREADS` — concurrent in-flight pages against the
     local vLLM server (default 16).
   - `HANDLER_DPI` — page render DPI (default 200, upstream's
     default).
   - `HANDLER_MAX_COMPLETION_TOKENS` — generation cap per page
     (default 6144). Measured over 13159 production pages: median
     1498, p95 1950, max 3114. The cap is what ends a repetition loop,
     so keep it near twice the longest real page.
   - `HANDLER_RETRY_THRESHOLD` (default 100) — the retry of scanning
     #238. A page with no usable output is re-run once on the same
     render thresholded at this grey level, which removes the verso
     show-through that causes the loops. The render is the only
     change: same greedy decoding, so the stage stays deterministic.
     Good pages never take the retry. A page filtered on both rungs
     (an answer that was not layout JSON, and one the repair below
     could not put back together) keeps upstream's cleaned text in
     `md`; the summary lists it in `filtered_pages` beside
     `failed_pages`, `recovered_pages` and `repaired_pages`.
   - The layout-JSON repair of scanning #242 has no env var: it always
     runs, and it costs microseconds. A page whose answer was good
     layout JSON with one character wrong — a lone quotation mark
     inside a string, a lone backslash where `\"` belongs, a doubled
     closer — is put back together before the retry rung is spent,
     because the fault is in the escape and not in the render. The
     page then carries `repaired` (the edits) and
     `repaired_by: "worker"`, and it is **not** filtered. The arms
     live in `layout_json.py`, which the Dockerfile copies next to
     `handler.py`: the daemon's glue shares it, to repair the shard
     results already in the bucket.
   - Every page that got an answer carries `raw`, the answer as the
     model wrote it (about 6 KB a page). `cells` is upstream's parsed
     and rescaled copy, so `raw` is what a later post-processor
     starts from — the repair above reads it. On a failed page it is
     the last truncated answer. It lives in the shard result object on
     S3 only; the glue leaves it out of the volume document.
   - `VLLM_GPU_MEMORY_UTILIZATION` (default 0.9),
     `VLLM_STARTUP_TIMEOUT` (default 900 s), `VLLM_EXTRA_ARGS`
     (extra `vllm serve` flags, e.g. `--max-model-len 16384`).
   - `HANDLER_DOWNLOAD_TIMEOUT` and friends — see `handler.py`
     tunables.

## Handler contract

`handler(job)` dispatches on `job["input"]["action"]`. The only action
is `parse`:

```json
{
  "input": {
    "action": "parse",
    "scan_pk": 123,
    "pdf_url": "https://s3.../volume.pdf?X-Amz-...",
    "result_url": "https://s3.../r1-s0-a1.json?X-Amz-...",
    "result_key": "processing/1/tc/164/1/jobs/analyze/dots_mocr/r1-s0-a1.json",
    "prompt_mode": "prompt_layout_all_en",
    "dpi": 200,
    "num_threads": 16,
    "temperature": 0.0,
    "top_p": 1.0,
    "max_completion_tokens": 6144,
    "include_pictures": false
  }
}
```

Everything but `pdf_url` is optional. There is deliberately no
`max_pages` input: elsewhere in the repo that name means "truncate to
the first N pages", and a partial parse returned as a success is worse
than a failure here. PDFs over the env-level `HANDLER_MAX_PAGES` are
rejected with `error_code=BAD_INPUT`. `prompt_mode` is one of:

| Mode | Output per page |
|---|---|
| `prompt_layout_all_en` (default) | `cells` (bbox + category + text) and `md` |
| `prompt_layout_only_en` | `cells` (bbox + category), no text |
| `prompt_ocr` | `md` only (plain text extraction) |

Returns:

```json
{
  "pages": [
    {
      "page_no": 0,
      "input_width": 1708,
      "input_height": 2212,
      "origin_width": 1700,
      "origin_height": 2200,
      "completion_tokens": 2481,
      "filtered": false,
      "cells": [
        {"bbox": [84, 132, 1620, 230], "category": "Section-header", "text": "OPINION"}
      ],
      "md": "## OPINION\n...",
      "duration_ms": 5210
    }
  ],
  "page_count": 312,
  "failed_pages": [],
  "duration_ms": 811034,
  "worker_boot_ms": 96210,
  "worker_uptime_ms": 128,
  "gpu_available": true
}
```

Notes on the page dicts:

- `cells` bboxes are in **rendered page-image pixel space** —
  `origin_width` × `origin_height`, normally the render at `dpi` —
  already rescaled from model-input space by upstream's
  `post_process_output`. `input_width`/`input_height` are the
  model-input dims, kept for debugging. If the pinned upstream's
  silent 4500 px fallback re-rendered the page at 72 dpi, the page
  carries `render_fallback: true`: bboxes are still consistent with
  `origin_width`/`origin_height`, just not with the requested `dpi`.
- `completion_tokens` is the generated token count for the page (from
  vLLM's usage block) — watch its distribution to see how close pages
  get to `max_completion_tokens`.
- `filtered: true` means the model's output wasn't parseable JSON and
  `md` holds upstream's cleaned-text fallback (`cells` is null).
- A page that fails all retries appears as
  `{"page_no": N, "error": "..."}` and is listed in `failed_pages`;
  one bad page doesn't sink the job. A generation cut at
  `max_completion_tokens` (`finish_reason='length'`, in practice a
  repetition loop) is treated as a page failure, not silently
  degraded to `filtered: true`. If *every* page fails, the job raises
  and RunPod marks it FAILED.
- Markdown from `Picture` cells is stripped by default (upstream
  inlines them as base64 data URIs, which can blow RunPod's ~20 MB
  response cap); pass `include_pictures: true` to keep them.

### Result delivery

`result_url` decides where the payload goes, and the caller chooses:

- **With `result_url`** (a presigned PUT), the payload is wrapped in a
  self-describing envelope and uploaded, and the job response holds only
  a summary:

  ```json
  {
    "result_key": "processing/1/.../r1-s0-a1.json",
    "bytes": 4812004,
    "page_count": 100,
    "failed_pages": [],
    "duration_ms": 264120
  }
  ```

  The envelope at that key is:

  ```json
  {
    "schema_version": 1,
    "action": "parse",
    "scan_pk": 123,
    "result_key": "processing/1/.../r1-s0-a1.json",
    "payload": { "pages": [ ... ], "page_count": 100, ... }
  }
  ```

- **Without `result_url`**, the payload comes back inline exactly as it
  always did. That is the path dev and CI take without credentials, and
  the path a caller on an older contract gets — so rolling this image
  back needs no daemon change.

Why S3 rather than inline for real volumes: RunPod caps a response at
about 20 MB and discards it with the job record roughly 30 minutes after
it finishes. A 100-page shard of dense pages can approach that cap, and
a caller whose daemon was down for an hour would lose work it had already
paid for. An S3 object has neither limit.

`Content-Type: application/json` is sent on the PUT because the caller
**signs** it into the URL. The two must match exactly; a mismatch is a
403 that reads like an expired signature.

### Structured errors

| `error_code` | Meaning | Suggested caller behaviour |
|---|---|---|
| `NO_GPU` | Worker scheduled without a GPU | Re-queue (transient) |
| `VLLM_UNHEALTHY` | vLLM server died on this worker | Re-queue (transient) |
| `BAD_INPUT` | Invalid input: `action`/`pdf_url` missing or wrong type, bad `prompt_mode`, out-of-range pixel bounds, page count over `HANDLER_MAX_PAGES` | Terminal |
| `UNKNOWN_ACTION` | `action` not `"parse"` | Terminal |
| `INPUT_DOWNLOAD_CORRUPT` | The downloaded PDF is empty, truncated, or will not open | Re-queue (transient) — the object in the bucket is sound, so the fault is in the transfer |
| `RESULT_UPLOAD_FAILED` | The result PUT never got through | Re-queue (transient) — a fresh job mints a fresh URL |
| `RESULT_URL_EXPIRED` | S3 answered 403: the signature died, or `Content-Type` disagrees with it | Re-queue (transient) — the two causes are indistinguishable from the worker, and a bounded retry costs less than losing a volume to a slow queue |
| `RESULT_UPLOAD_REJECTED` | S3 refused the request as formed (wrong region, a bucket policy demanding headers we do not send) | Terminal — every retry re-runs the GPU work and fails identically |

`NO_GPU` and `VLLM_UNHEALTHY` also set `refresh_worker: true`, which
tells the RunPod SDK to terminate this worker after the response — a
worker in either state never heals on its own, and keeping it warm
would let it keep swallowing re-queued jobs.

Real exceptions inside the action (download failure, all pages
failing) propagate and turn into RunPod `FAILED` status with the
traceback in the status response's `error` field.

## Manual testing with `curl`

```bash
ENDPOINT_ID=<your-endpoint-id>
API_KEY=<your-runpod-api-key>

RESP=$(curl -sX POST https://api.runpod.ai/v2/${ENDPOINT_ID}/run \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"input":{"action":"parse","scan_pk":0,"pdf_url":"https://arxiv.org/pdf/1706.03762.pdf"}}')
JOB_ID=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

while true; do
  S=$(curl -s -H "Authorization: Bearer ${API_KEY}" \
    https://api.runpod.ai/v2/${ENDPOINT_ID}/status/${JOB_ID})
  STATE=$(echo "$S" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("status"))')
  echo "-> ${STATE}"
  [[ "$STATE" != "IN_QUEUE" && "$STATE" != "IN_PROGRESS" ]] && { echo "$S" | python3 -m json.tool | head -50; break; }
  sleep 5
done
```

Expect the first call on a fresh endpoint to spend several minutes in
cold start (image pull + model load into VRAM); `worker_boot_ms` in the
response shows the in-container share of that.

## Releasing a new image version

Identical flow to the blackletter worker (see
[`../runpod/README.md`](../runpod/README.md#releasing-a-new-image-version)
for the full rationale): the `Build and Push RunPod dots.mocr Worker`
workflow (`.github/workflows/build-runpod-dotsmocr-worker.yml`) builds
on any push to `main` touching `scanning/runpod-dotsmocr/**`, pushes
`freelawproject/dotsmocr-gpu-worker:<sha_short>` + `:latest`, and
PATCHes the RunPod template to the SHA-pinned tag.

Repo secrets used: `RUNPOD_API_KEY` (shared with the blackletter
worker) and `RUNPOD_DOTSMOCR_TEMPLATE_ID` (this endpoint's backing
template).

## Updating Python deps

Edit `pyproject.toml`, then run `uv lock` inside
`scanning/runpod-dotsmocr/` and commit both files. `uv sync --frozen`
in the Dockerfile errors out if the two ever drift. When bumping the
`dots_mocr` git pin, re-read "Upstream packaging quirks" above and run
the handler tests.
