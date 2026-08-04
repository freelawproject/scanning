# lighton-gpu-worker

GPU worker image for [RunPod Serverless] that runs
[LightOnOCR-2-1B](https://huggingface.co/lightonai/LightOnOCR-2-1B) —
LightOn's 1B vision-language OCR model — as the **tiebreaker** for the
case-law extraction pipeline.

Where a whole-page OCR worker reads every page, this one re-reads only
the small regions the pipeline could not agree on. Enumeration (which
regions are disputed) and arbitration (whose reading wins) stay with the
caller; this worker does inference and nothing else.

The image contains only inference code + model weights. No Django, no
database client, no AWS credentials. All sensitive configuration lives
in RunPod endpoint env vars, never in the image.

Sibling of `../runpod/` (the blackletter YOLO/PaddleOCR worker), and
follows the same conventions: presigned-URL input, JSON output,
structured error codes, worker meta on every response. General RunPod
operations knowledge (billing model, worker states, pods vs serverless,
result retention) is documented once in
[`../runpod/README.md`](../runpod/README.md) and applies here unchanged.

[RunPod Serverless]: https://docs.runpod.io/serverless/overview

## Why the job carries a PDF, not crop images

The obvious design is to cut each disputed region locally and send N
crop PNGs. This worker takes **the PDF plus page/bbox coordinates**
instead, so one job covers every disputed region in a volume:

- the PDF is downloaded once, not N crops uploaded;
- each referenced page is rendered once and every crop it owns is cut
  from that one render;
- job payloads stay small — coordinates instead of images.

The trade is that the worker has to reproduce the caller's coordinate
space exactly, which is what the next section is about.

## Coordinate space (read before changing the renderer)

Disputed bboxes are computed against the pipeline's **canonical page
render**, so this worker must reproduce that render byte-for-byte in
geometry or every bbox means the wrong pixels:

| | |
|---|---|
| size | 1700×2200 by default (`render_width` / `render_height` per job) |
| fit | zoom to target **width**, then **force-resize** to the target size |
| colour | RGB |
| redactions | filled black **before** cropping |

Two of these are easy to "fix" into bugs:

- **The force-resize is deliberate.** A page whose aspect ratio isn't
  8.5×11 gets squashed to the canonical size rather than letterboxed,
  because that is what makes a bbox mean the same region for every
  engine regardless of the source page's true dimensions. Changing it
  to an aspect-preserving fit silently shifts every crop.
- **Redactions must be applied before cropping.** The canonical render
  is black-redacted before any engine sees it, and downstream stages
  rely on that (a mostly-black region is treated as redaction
  territory, not text). Skip it and the model reads text that is
  supposed to be gone.

Crops smaller than 64 px on a side are scaled up, aspect preserved. This
is not cosmetic: a disputed region can be a single glyph, LightOn merges
image patches 2×2, and a crop that renders to fewer than two patches on
a side crashes the vision tower rather than returning a bad read.

## Decode policy

Small crops make this decoder repeat and degenerate, so each request
gets a token ceiling scaled to the region's **area**
(`max(128, min(1024, area / 500))`) and goes out greedy with
`repetition_penalty: 1.15` and `no_repeat_ngram_size: 12`. The request
carries the image and **no text prompt** — this model reads what it is
shown.

**One attempt per crop.** There is no quality retry here. Whether a read
is usable is decided by the caller's degeneration guards; a read it
discards comes back as another entry carrying its own `decode` object,
which this worker applies verbatim in place of the defaults. Retry
settings are deliberately *tighter* than the first attempt — the failure
being corrected is usually a model that rambled, and more room makes
that worse.

(The `INFERENCE_ATTEMPTS` env var is a *transport* retry for a dropped
connection, not a quality retry. Different thing.)

## Handler contract

`handler(job)` dispatches on `job["input"]["action"]`. The only action
is `read_crops`:

```json
{
  "input": {
    "action": "read_crops",
    "scan_pk": 123,
    "pdf_url": "https://s3.../volume.pdf?X-Amz-...",
    "crops": [
      {
        "key": "page_0007_84_132_1620_230",
        "page_index": 6,
        "bbox": [84, 132, 1620, 230],
        "area": 151008,
        "expect": "the main engine's reading",
        "decode": {
          "max_new_tokens": 96,
          "repetition_penalty": 1.25,
          "no_repeat_ngram_size": 8
        }
      }
    ],
    "redactions": {"6": [[100, 400, 900, 460]]},
    "render_width": 1700,
    "render_height": 2200,
    "concurrency": 16
  }
}
```

Everything but `pdf_url` and `crops` is optional. Per crop, only `key`,
`page_index` and `bbox` are required:

| Field | Meaning |
|---|---|
| `key` | caller's identifier, echoed back on the read. Must be unique within the job |
| `page_index` | zero-based page in the PDF |
| `bbox` | `[x0, y0, x1, y1]` in render space |
| `area` | sizes the token budget; derived from `bbox` when omitted |
| `expect` | the main engine's reading. Accepted and ignored — kept so a caller can send its manifest unmodified; it is never shown to the model |
| `decode` | overrides for a retry entry, applied verbatim |

Returns:

```json
{
  "reads": [
    {"key": "page_0007_84_132_1620_230", "text": "…", "duration_ms": 412}
  ],
  "failed": [],
  "crop_count": 1,
  "pages_rendered": 1,
  "duration_ms": 5210,
  "worker_boot_ms": 74120,
  "worker_uptime_ms": 128,
  "gpu_available": true
}
```

Reads are short by construction (a region, not a page), so a whole
volume's worth stays far under RunPod's ~20 MB response cap — which is
why this worker returns results inline rather than writing to S3.

A crop that fails all attempts appears in `failed` instead of `reads`;
one bad crop doesn't sink the job, and the caller treats a missing read
as an honest no-vote. If *every* crop fails the job raises, because that
is a worker problem rather than a data one.

### Structured errors

| `error_code` | Meaning | Suggested caller behaviour |
|---|---|---|
| `NO_GPU` | Worker scheduled without a GPU | Re-queue (transient) |
| `VLLM_UNHEALTHY` | vLLM server died on this worker | Re-queue (transient) |
| `BAD_INPUT` | Missing/malformed `action`, `pdf_url` or `crops` | Terminal |
| `UNKNOWN_ACTION` | `action` not `"read_crops"` | Terminal |

Other exceptions (download failure, expired presigned URL, a corrupt
PDF) propagate and turn into RunPod `FAILED` status with the traceback
in the status response's `error` field.

## Building locally

Run from the scanning repo root so the build context is only this
directory:

```bash
docker build -t lighton-gpu-worker:local scanning/runpod-lighton/
```

You do **not** need a GPU on the build host.

### The two build gates

Both exist so an unusable image fails on a cheap build host rather than
several minutes into cold start on a billed GPU worker.

1. **Arch gate** (before anything else, no network). LightOnOCR-2's
   architecture has to be registered in the running vLLM — the model
   ships no vLLM plugin. The build asks vLLM's model registry directly.
2. **Offline load gate** (after the weights bake, no network). Loads the
   config and processor with `HF_HUB_OFFLINE=1`, exactly as the worker
   does at boot, so a `transformers` too old for this model's processor
   is caught here instead of at model load.

If either fails, bump the base image:

```bash
docker build --build-arg VLLM_TAG=<newer-tag> \
  -t lighton-gpu-worker:local scanning/runpod-lighton/
```

Do **not** pin `transformers` to fix a load error. Recent vLLM needs
transformers v5, and pinning it is the documented way to break this
model.

**On the arch name**, since it is an easy trap: the registry key is the
`architectures` entry in the model's `config.json` —
`LightOnOCRForConditionalGeneration`, all caps on OCR. The model card's
Transformers example names a `LightOnOcrForConditionalGeneration` Python
class instead, and gating on that spelling reports a false miss on a
base image that supports the model fine.

Verified working: `vllm/vllm-openai:v0.20.1` (vLLM 0.20.1, transformers
5.7.0, processor resolves to `PixtralProcessor`).

## Configuring the RunPod endpoint

1. **New Endpoint → Custom Source (Docker Image)**, image =
   `freelawproject/lighton-gpu-worker:<sha>`.
2. **GPU**: 16 GB is enough — the weights are ~2 GB and crops are small,
   so this is the cheapest tier of the three OCR engines. 24 GB buys
   headroom for higher `concurrency`.
3. **Container Disk**: ≥ 40 GB.
4. **Min Workers**: `0`; **Idle Timeout**: `300s`.
5. **Env vars** (endpoint config, NOT the image):
   - `SENTRY_DSN_GPU`, `SENTRY_ENV`, `GIT_SHA` — Sentry wiring.
   - `HANDLER_MAX_CROPS` — reject jobs above this crop count
     (default 20000).
   - `HANDLER_CONCURRENCY` — in-flight requests against the local vLLM
     server (default 16; per-job `concurrency` overrides it).
   - `HANDLER_RENDER_W` / `HANDLER_RENDER_H` — canonical render size
     (defaults 1700/2200). Keep in step with the pipeline's render
     stage.
   - `HANDLER_MIN_CROP_SIDE` — min crop side in px (default 64).
   - `VLLM_GPU_MEMORY_UTILIZATION` (default 0.9),
     `VLLM_STARTUP_TIMEOUT` (default 900 s), `VLLM_EXTRA_ARGS`.

## Local smoke test (no endpoint)

The RunPod SDK can run the handler once and exit, which exercises the
whole path — download, render, crop, transcribe — without an endpoint.
Requires a GPU on the host:

```bash
docker run --rm --gpus all lighton-gpu-worker:local \
  /opt/venv/bin/python -u handler.py --test_input '{"input":{"action":"read_crops","pdf_url":"https://arxiv.org/pdf/1706.03762.pdf","crops":[{"key":"notice","page_index":0,"bbox":[300,180,1400,320]}]}}'
```

Without a GPU the container starts, logs a warning, skips the vLLM
startup and fails the fitness check — the same behaviour as on a
misprovisioned RunPod worker.

## Manual testing with `curl`

```bash
ENDPOINT_ID=<your-endpoint-id>
API_KEY=<your-runpod-api-key>

RESP=$(curl -sX POST https://api.runpod.ai/v2/${ENDPOINT_ID}/run \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"input":{"action":"read_crops","scan_pk":0,
       "pdf_url":"https://arxiv.org/pdf/1706.03762.pdf",
       "crops":[{"key":"notice","page_index":0,
                 "bbox":[300,180,1400,320]}]}}')
JOB_ID=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

while true; do
  S=$(curl -s -H "Authorization: Bearer ${API_KEY}" \
    https://api.runpod.ai/v2/${ENDPOINT_ID}/status/${JOB_ID})
  STATE=$(echo "$S" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("status"))')
  echo "-> ${STATE}"
  [[ "$STATE" != "IN_QUEUE" && "$STATE" != "IN_PROGRESS" ]] && { echo "$S" | python3 -m json.tool | head -40; break; }
  sleep 5
done
```

That bbox reads the arXiv permission notice at the very top of page 1,
not the paper's title — a deliberately boring target that proves the
render/crop/transcribe path without depending on where a title lands.

## Measured behaviour

From one job on a fresh endpoint, so treat these as first measurements
rather than a benchmark:

| | |
|---|---|
| cold start (`worker_boot_ms`, weights baked) | ~124 s |
| whole job, 1 crop (`executionTime`) | ~35 s |
| that crop's own `duration_ms` | ~34 s |

**The per-crop figure above is warm-up, not throughput.** That crop's
token budget was 308 (area 154,000 ÷ 500) and it emitted ~35 tokens
before EOS, so it was not over-generating: the time is first-request
cost on a cold engine (kernel selection, CUDA graph capture). A
one-crop job pays all of it and amortizes none. Re-measure on a job
with hundreds of crops before using any number for capacity planning.

Cold start is paid per worker per idle period, so it is the endpoint's
`Max Workers` that decides how often you pay it. A caller submitting a
batch of jobs against a deliberately narrow pool pays boot once per
worker rather than once per job; queue time is free.

## Updating Python deps

Edit `pyproject.toml`, then run `uv lock` inside
`scanning/runpod-lighton/` and commit both files. `uv sync --frozen` in
the Dockerfile errors out if the two ever drift.
