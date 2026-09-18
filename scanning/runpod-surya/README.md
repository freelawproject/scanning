# surya-gpu-worker

GPU worker image for [RunPod Serverless] that runs
[Surya OCR 2](https://huggingface.co/datalab-to/surya-ocr-2), Datalab's
Qwen3.5-based vision-language OCR model, for whole-page reads: one
request per page, answered as layout blocks that each carry a label, a
bbox and the block's HTML, in reading order.

The image contains only inference code + model weights. No Django, no
database client, no AWS credentials. All sensitive configuration lives
in RunPod endpoint env vars, never in the image.

This worker is a sibling of `../runpod-dotsmocr/` (the other
vLLM-backed OCR worker) and follows the same conventions: presigned-URL
input, JSON output, structured error codes, worker meta on every
response, the result envelope of `runpod_common.py`. General RunPod
operations knowledge (billing model, worker states, pods vs serverless,
result retention) is documented once in
[`../runpod/README.md`](../runpod/README.md) and applies here unchanged.

It is the serverless form of the ai-research pod kit
(`runpod/kits/surya/`, branch `extraction_align`), which is where the
parameters come from. Issue: freelawproject/scanning#320, part of #301.

[RunPod Serverless]: https://docs.runpod.io/serverless/overview

## How it works

```
┌─────────────────────┐        ┌────────────────────────────────────┐
│ caller (daemon /    │        │ RunPod Serverless worker           │
│ curl smoke test)    │  POST  │                                    │
│                     ├───────▶│ handler.py                         │
│  - presign GET URL  │  /run  │  - downloads PDF (presigned URL)   │
│  - presign PUT URL  │        │  - renders pages with PyMuPDF      │
│  - submit job       │        │  - one surya read per page ───────┐│
│  - poll /status     │◀───────│  - records the raw answer         ││
│  - HEAD result_key  │ summary│  - PUTs the result to S3          ││
└─────────────────────┘        │                                   ││
                               │ surya RecognitionPredictor        ││
                               │   └─▶ vllm serve surya-ocr-2 ◀────┘│
                               │       (subprocess, localhost:8000) │
                               └────────────────────────────────────┘
```

`handler.py` spawns `vllm serve` at worker boot (cold start pays the
model load once) and hands each rendered page to the `surya-ocr`
package's `RecognitionPredictor`, pointed at that server through the
`SURYA_INFERENCE_*` environment. The reading is surya's own, exactly as
the kit calls it (`rec([image], full_page=True)`): its prompt, its
image fit, its HTML parse into blocks, its loop detection and its
block-mode fallback. Page fan-out is a thread pool of one-page calls,
so a finished page frees its slot at once and a slow page holds only
itself; vLLM's continuous batching turns the concurrent requests into
GPU batches.

## The decode is surya's

This worker sets **no decode parameter** and refuses one in the job
input (`temperature`, `top_p`, `max_tokens`, `max_completion_tokens`
answer `BAD_INPUT`). The parent issue's lesson from dots.mocr is that a
knob added "to help" drifts from what the experiments measured. What
the model sees is what the kit measured:

| Where | Parameter | Value |
|---|---|---|
| `vllm serve` | `--dtype` | `bfloat16` (Ampere or newer; a T4 wants `VLLM_DTYPE=float16`) |
| | `--max-model-len` | `18000` |
| | `--gpu-memory-utilization` | `0.85` |
| | `--mm-processor-kwargs` | `{"min_pixels": 3136, "max_pixels": 6291456}` |
| | `--enable-prefix-caching` | on |
| | `--speculative-config` (MTP) | off; `VLLM_ENABLE_MTP=1` turns Datalab's default back on. Greedy decoding gives the same answer either way, so it is a throughput switch |
| surya | `SURYA_INFERENCE_BACKEND` | `vllm` |
| | `SURYA_INFERENCE_URL` | the local server, derived from `HANDLER_VLLM_PORT` |
| | `SURYA_INFERENCE_PARALLEL` | `8` (`HANDLER_SURYA_PARALLEL`): requests one page's block-mode fallback may open at once |
| | the decode | surya's defaults: greedy, `SURYA_MAX_TOKENS_FULL_PAGE=12288`, `SURYA_FULLPAGE_REGEN` off |

What surya does with a bad answer is the package's contract, not this
worker's: its client retries an answer that loops or errors up to three
times (a loop is retried at a higher temperature, by surya's design),
and a page whose full-page answer still fails is re-read in **block
mode** (a layout pass, then one request per block). The page then
carries `fallback: "block"` and its `raw` is the full-page answer that
failed, so the loop can be looked at.

## Coordinate space

Pages are rendered with PyMuPDF at `dpi` (default **200**) and handed to
surya at that size. surya fits the image for the model itself and
scales the boxes back to the image it was given, so every `bbox` is in
the render's pixel space, `origin_width` × `origin_height`. US Letter
at 200 dpi is 1700×2200: the canonical page the ai-research pipeline
staged for this model, and the pixel space of the dots.mocr cells and
the detection rows in this repo, so a box from this worker lands on the
same pixels as theirs.

## What's in the image

- `vllm/vllm-openai:v0.20.1` base. **Not negotiable downwards**:
  surya-ocr-2's architecture is `Qwen3_5ForConditionalGeneration`,
  registered in vLLM from 0.20.1 (Datalab pins the same tag). The
  dots.mocr image's 0.17.1 refuses the model at load. surya-ocr has no
  vLLM dependency and registers no plugin, so the arch must be native
  to the running server. The build gates on the registry (a cheap
  lookup, no GPU) so a wrong base fails on the build host and not on a
  billed worker. The plain (non `-cu130`) tag keeps the image
  compatible with RunPod hosts whose drivers predate CUDA 13.
- The `datalab-to/surya-ocr-2` snapshot baked into `/opt/hf`
  (`HF_HUB_OFFLINE=1` at runtime; the repo's README assets are left
  out), and an offline load gate that proves the base image's
  transformers can open it.
- A separate uv-managed venv (`/opt/venv`) for the handler and its
  client deps: `surya-ocr`, and a **CPU torch** because surya imports
  torch at load even on the vLLM-client path, where nothing runs on
  it. The build asserts the venv's torch has no CUDA: a CUDA wheel
  there would be a second GPU stack next to vLLM's. Keeping the two
  environments apart means surya's pins (`pillow<11`, an exact
  `opencv-python-headless`) never touch vLLM's stack.
- `runpod_common.py`, copied from `scanning/` next to `handler.py` and
  imported as a top-level module: the result envelope, the transfer
  code and the error codes the daemon classifies.

## Building locally

Run from the scanning repo root with `scanning/` as the context, so the
build can COPY the shared `runpod_common.py`:

```bash
docker build -t surya-gpu-worker:local \
    -f scanning/runpod-surya/Dockerfile scanning/
```

The first build downloads the ~8 GB base image and the ~1.4 GB model
snapshot; expect it to be bandwidth-bound. You do **not** need a GPU on
the build host: the gates are a registry lookup, a file-size check and
a processor load.

### Running the image locally

Requires a GPU plus the nvidia-container-toolkit:

```bash
docker run --rm --gpus all surya-gpu-worker:local
```

Without a GPU the container starts, logs a warning, skips the vLLM
startup, and fails the fitness check, the same behaviour as on a
misprovisioned RunPod worker.

## Configuring the RunPod endpoint

1. **New Endpoint → Custom Source (Docker Image)**, image =
   `freelawproject/surya-gpu-worker:<sha>`.
2. **GPU**: an Ampere or newer card with 24 GB (RTX A5000, A40, L4).
   The weights are ~1.4 GB; the rest of the 0.85 is KV cache for the
   18000-token context (14.4 GiB on the smoke run's 24 GB card, room
   for 57 concurrent requests). The kit measured about 3 s a page on
   an A40, and the smoke run did 15 pages in 43 s; add cards rather
   than buying a faster one, because full-page OCR is decode-bound.
   See "Running several workers" below.
3. **Container Disk**: ≥ 30 GB (the image extracts to ~20 GB).
4. **Min Workers**: `0`; **Idle Timeout**: `300s`.
5. **Env vars** (endpoint config, NOT the image):
   - `SENTRY_DSN_GPU`, `SENTRY_ENV`, `GIT_SHA`: Sentry wiring.
   - `HANDLER_MAX_PAGES`: reject PDFs above this page count (default
     5000).
   - `HANDLER_NUM_THREADS`: pages in flight against the local server
     (default 16, the value the kit measured as good on an A40; a job
     may set `num_threads` up to 64). The first smoke run at 8 showed
     vLLM with `Running: 8, Waiting: 0` and KV cache for 57 requests,
     so the threads were the ceiling. Back off if the server log shows
     500s or `Waiting` climbing.
   - `HANDLER_SURYA_PARALLEL`: requests one page's block-mode fallback
     may open at once (default 8).
   - `HANDLER_DPI`: page render DPI (default 200).
   - `HANDLER_ABORT_STREAK`: pages in a row that may come back failed
     or empty before the worker asks the server whether it is still
     alive and, if not, fails the job (default 16). See "Hardening".
   - `VLLM_DTYPE`, `VLLM_MAX_MODEL_LEN`, `VLLM_GPU_MEMORY_UTILIZATION`,
     `VLLM_ENABLE_MTP`, `VLLM_STARTUP_TIMEOUT` (default 900 s),
     `VLLM_EXTRA_ARGS` (extra `vllm serve` flags). The first five names
     are also fields of surya's own settings, which it parses when the
     package is imported on the first job: surya reads them only on
     its docker-spawn path, which this worker never takes, but a value
     its parser rejects (`VLLM_ENABLE_MTP=yes` instead of `1`) fails
     that import. Keep them to the shapes shown.
   - `SURYA_*`: any surya setting; the handler sets only the ones in
     the table above, with `setdefault`, so an endpoint value wins.
   - `HANDLER_DOWNLOAD_TIMEOUT` and friends: see `runpod_common.py`.

## Running several workers

The daemon submits one job per shard (up to 100 pages, #164) and the
endpoint runs up to eight workers, so eight shards read at once and a
worker handles one job at a time. What that asks of the endpoint:

- **Max Workers**: 8, or whatever the daemon's per-engine concurrency
  cap is set to; a cap above the worker count only queues jobs on
  RunPod's side (queue time is not billed, but it is time).
- **FlashBoot**: on. Every fresh worker pays the same cold start, about
  200 s in-container on the smoke run (3 s of weights, 41 s of
  `torch.compile`, about 100 s of CUDA-graph profiling and capture)
  plus the image pull. FlashBoot keeps a stopped worker's state so a
  later start skips most of it. It costs nothing when idle.
- **Idle Timeout**: 300 s, so a warm worker survives the gap between
  one shard's end and the next submit tick. Eight cold starts a wave
  is what a short timeout buys.
- **Execution Timeout**: at least 1200 s. A 100-page shard at 3 s a
  page is about 300 s of GPU time, but a shard with a few looping pages
  pays surya's 12288-token ladder on each, and a job killed by the
  timeout is paid for and lost. The abort of "Hardening" below ends the
  one case where waiting is pointless.
- **Optional, a network volume** mounted on the endpoint with
  `VLLM_CACHE_ROOT=/runpod-volume/vllm` in the env: vLLM keys its
  `torch.compile` cache by configuration, so the second and later
  workers reuse the first one's 41 s of compile. The graph profiling
  is not cached, so this is a minority of the cold start; try FlashBoot
  first.

Inside a worker, `HANDLER_NUM_THREADS` (default 16) is the pages in
flight; the GPU has room for more, and the kit's measurements say the
gains flatten past 16 on a 24 GB card. Adding workers is the way to go
faster, not depth: full-page OCR is decode-bound.

## Handler contract

`handler(job)` dispatches on `job["input"]["action"]`. The only action
is `ocr`:

```json
{
  "input": {
    "action": "ocr",
    "scan_pk": 123,
    "pdf_url": "https://s3.../volume.pdf?X-Amz-...",
    "result_url": "https://s3.../r1-s0-a1.json?X-Amz-...",
    "result_key": "processing/1/tc/164/1/jobs/analyze/surya/r1-s0-a1.json",
    "dpi": 200,
    "num_threads": 16
  }
}
```

Everything but `pdf_url` is optional. There is deliberately no
`max_pages` input (elsewhere in the repo that name means "truncate to
the first N pages", and a partial read returned as a success is worse
than a failure) and no decode parameter (see above). PDFs over the
env-level `HANDLER_MAX_PAGES` are rejected with `error_code=BAD_INPUT`.

Returns:

```json
{
  "pages": [
    {
      "page_no": 0,
      "origin_width": 1700,
      "origin_height": 2200,
      "blocks": [
        {
          "order": 0,
          "label": "SectionHeader",
          "raw_label": "Section-Header",
          "bbox": [612.0, 187.4, 1088.6, 231.0],
          "confidence": 0.98,
          "html": "<h2>OPINION</h2>",
          "text": "OPINION",
          "skipped": false,
          "error": false
        }
      ],
      "text": "OPINION\n...",
      "raw": "<div data-bbox=\"360 85 640 105\" data-label=\"Section-Header\"><h2>OPINION</h2></div>...",
      "raw_divs": 14,
      "parsed_blocks": 14,
      "requests": 1,
      "completion_tokens": 1830,
      "confidence": 0.98,
      "attempts": 1,
      "duration_ms": 3120
    }
  ],
  "page_count": 100,
  "failed_pages": [],
  "empty_pages": [],
  "fallback_pages": [],
  "dropped_block_pages": [],
  "duration_ms": 311034,
  "worker_boot_ms": 96210,
  "worker_uptime_ms": 128,
  "gpu_available": true
}
```

Notes on the page dicts:

- `blocks` are surya's, in reading order. `raw_label` is the label the
  model wrote, hyphenated (`Section-Header`, `Page-Header`,
  `Complex-Block`); `label` is surya's canonical CamelCase form of it
  (`surya/layout/label.py`, `LAYOUT_PRED_RELABEL`): `Text`,
  `SectionHeader`, `PageHeader`, `PageFooter`, `Caption`, `Footnote`,
  `Table`, `ListGroup`, `Equation`, `Code`, `Form`, `TableOfContents`,
  `Bibliography`, `ChemicalBlock`, `Picture` (the model's `Image`),
  `Figure` (the model's `Figure` **and** `Complex-Block`), `Diagram`,
  `BlankPage`. Key on `label`, with that spelling. `html` is the
  block's inner HTML as surya's parser hands it over: the model's
  markup, with the `data-bbox` and `data-label` attributes of nested
  elements removed (`parse_full_page_html` strips them), so any
  sub-block geometry the model wrote is in `raw` only. `text` is that
  HTML flattened (tags dropped; `<br>`, `<hr>` and closed paragraphs,
  headings, list items, table cells and rows as newlines; entities
  unescaped). `skipped: true` marks a label surya does not OCR:
  `Figure`, `Picture`, `Diagram` and `BlankPage`. surya empties the
  HTML of such a block; the worker puts it back from the parsed
  answer (see "What surya drops" below), so a figure carries its
  `<img/>` and a `Complex-Block`, which canonicalizes to `Figure`,
  keeps the text the model read in it. `error: true` marks a block
  whose read failed in block mode.
- `raw` is the full-page answer as the model wrote it, on every page
  that got one (`null` when there was no answer): `blocks` is surya's
  parse of it, so `raw` is what a later post-processor starts from,
  and on a page surya could not parse it is the only evidence of what
  the model wrote. surya keeps no copy of it. It is the **last**
  full-page answer of the read, which is the only one while
  `SURYA_FULLPAGE_REGEN` is off; an endpoint that turns that setting
  on makes surya ask again at a higher temperature until an answer
  parses, and the last one is then the answer `blocks` came from.
  `requests` and `completion_tokens` count the model requests the read
  made as surya reports them, block-mode fallback included.
  `confidence` is surya's mean token probability of that same answer
  and of no other request.
- **What the output cannot show.** surya's client retries a looping or
  errored answer up to three times *inside* one request
  (`surya/inference/backends/openai_client.py`, at a temperature surya
  raises itself, 0.2 then 0.4 then 0.6, with `top_p` 0.95), and only
  the final answer comes back. A page whose greedy pass looped and
  whose first retry parsed therefore looks exactly like a clean page:
  `requests: 1`, `attempts: 1`, no `fallback`, and `raw`,
  `completion_tokens` and `confidence` of the sampled answer.
  `fallback: "block"` marks only the page where all three retries
  failed too. This worker adds no decode parameter and changes none
  of surya's; it also cannot see past surya's client. If the team
  needs to know which pages surya sampled, that is a hook into
  `chat_completions_batch`, not a knob here.
- `attempts: 2` means the first read came back with no block and the
  page was read again. `empty: true` means the second did too: the
  page is kept (so the shard converges) and listed in `empty_pages`
  (so a reader checks it). A truly blank page is rare; even a
  near-blank one normally yields its page number.
- `fallback: "block"` means surya re-read the page block by block
  after the full-page answer looped or would not parse; `error_blocks`
  counts the blocks that failed in that pass. Such pages are listed in
  `fallback_pages`.
- `raw_divs`, `parsed_blocks`, `refused_divs` and `dropped_blocks` say
  what the answer lost on the way to `blocks`; see the next section.
  `dropped_block_pages` lists the pages that lost something either
  way.
- A page whose read raised appears as `{"page_no": N, "error": "..."}`,
  plus `raw` when the model had answered before the failure, and is
  listed in `failed_pages`; one bad page doesn't sink the job.

### What surya drops, and how the output shows it

The dots.mocr and YOLO post-processing incidents were each a step
that threw data away with no count anyone could read. surya has four
such steps between the model's answer and `blocks`, all in
`surya/recognition/__init__.py` and `surya/inference/parsers.py`:

| Step | What is lost | Signal |
|---|---|---|
| `parse_full_page_html` skips a top-level div whose `data-label` or `data-bbox` is missing or malformed | the block, silently | `raw_divs` > `parsed_blocks`, and `refused_divs` |
| `_drop_blank_text_blocks` deletes a text-labelled block whose crop is blank (over 99 percent of pixels at or above 245 on every channel, or a per-channel standard deviation under 8) | the block, on one INFO line of surya's logger | `dropped_blocks` |
| a skipped label (`Figure`, `Picture`, `Diagram`, `BlankPage`, and `Complex-Block` which maps to `Figure`) gets `html=""` | the block's HTML, so a complex text block drops out of `text` | restored from `raw` |
| nested `data-bbox` and `data-label` attributes are removed from every block's inner HTML | sub-block geometry | `raw` only |

The worker reads `raw` once more and takes two measurements, because
one parse cannot see both losses. It counts the answer's top-level
divs as `raw_divs`, with the library and the settings
`parse_full_page_html` itself uses, so the two see one tree. **That
count is the only way to see the first row**: the parser refuses those
divs, so a second call to it returns the same short list and could
never name what it left out. Each refused div is listed in
`refused_divs` as `{"div", "raw_label"}`, by a walk over the two lists
in order and never by a copy of the parser's own test; a walk that
cannot account for exactly the difference writes no name, and the two
counts still say how many were lost.

It then parses `raw` and records `parsed_blocks`, the entries the
parser kept, so `parsed_blocks - len(blocks)` is what surya dropped
after the parse. Each missing entry is listed in `dropped_blocks` as
`{"order", "raw_label"}`. A page that lost something either way is
listed in `dropped_block_pages`. On a page read whole the parsed list
and `blocks` align by `order` (surya numbers those blocks by their
index in the parsed list and a drop keeps the survivors' numbers), so
a skipped block gets its `html` and `text` back from its parsed
entry. A page read in block mode
(`fallback: "block"`) is numbered by the layout pass instead, and its
`raw` is the answer that failed, so it carries the two counts and
`refused_divs` but no `dropped_blocks`. A `raw` surya's parser refuses
leaves `parsed_blocks` null, and `raw_divs` then says how much was in
the answer.

Measured on the smoke run (the 15-page Transformer paper), which
predates `raw_divs`: 147 parsed entries, 147 blocks, no drop after the
parse; no nested geometry in any answer; six skipped `Figure` and
`Diagram` blocks whose inner HTML was `<img/>`. On a reporter volume the case to watch is `Complex-Block`
over a dense headnote or a table of parallel citations: with these
fields it is one number in the summary and the text is still there.
`raw` and these fields belong to the shard object on S3; the glue is
free to leave them out of the volume document.

### Hardening

Two failure shapes of the pod kit and the dots.mocr worker are handled
here so they never come back as a success with no text in it:

- **A server that answers nothing.** surya's predictor swallows
  transport errors: against a dead server every page comes back with
  no blocks and no exception, about twenty seconds each through
  surya's retry ladder. The worker reads an empty page once more; if
  `HANDLER_ABORT_STREAK` pages in a row come back failed or empty and
  the server no longer answers `/health`, the job stops (RunPod marks
  it `FAILED`, the daemon re-queues it onto a fresh worker). A shard
  whose every page failed or came back empty fails the job the same
  way, whatever the streak.
- **A dead engine on a warm worker.** A job that arrives when vLLM is
  down answers `VLLM_UNHEALTHY` with `refresh_worker: true`, so the SDK
  terminates the worker instead of letting it swallow re-queued jobs.

### Result delivery

`result_url` decides where the payload goes, and the caller chooses:

- **With `result_url`** (a presigned PUT), the payload is wrapped in a
  self-describing envelope and uploaded, and the job response holds only
  a summary. The caller confirms the write with a `HEAD` on
  `result_key`, the way the daemon does for every worker:

  ```json
  {
    "result_key": "processing/1/.../r1-s0-a1.json",
    "bytes": 4812004,
    "page_count": 100,
    "failed_pages": [],
    "empty_pages": [],
    "fallback_pages": [3],
    "dropped_block_pages": [],
    "duration_ms": 311034
  }
  ```

  The envelope at that key is:

  ```json
  {
    "schema_version": 1,
    "action": "ocr",
    "scan_pk": 123,
    "result_key": "processing/1/.../r1-s0-a1.json",
    "payload": { "pages": [ ... ], "page_count": 100, ... }
  }
  ```

- **Without `result_url`**, the payload comes back inline. That is the
  path dev and CI take without credentials.

Why S3 rather than inline for real volumes: RunPod caps a response at
about 20 MB and discards it with the job record roughly 30 minutes after
it finishes. A 100-page shard with `raw` on every page passes that cap,
and a caller whose daemon was down for an hour would lose work it had
already paid for. An S3 object has neither limit.

`Content-Type: application/json` is sent on the PUT because the caller
**signs** it into the URL. The two must match exactly; a mismatch is a
403 that reads like an expired signature.

### Structured errors

| `error_code` | Meaning | Suggested caller behaviour |
|---|---|---|
| `NO_GPU` | Worker scheduled without a GPU | Re-queue (transient) |
| `VLLM_UNHEALTHY` | vLLM server died on this worker | Re-queue (transient) |
| `BAD_INPUT` | Invalid input: `action`/`pdf_url` missing or wrong type, a decode parameter, a bad `dpi` or `num_threads`, page count over `HANDLER_MAX_PAGES` | Terminal |
| `UNKNOWN_ACTION` | `action` not `"ocr"` | Terminal |
| `INPUT_DOWNLOAD_CORRUPT` | The downloaded PDF is empty, truncated, or will not open | Re-queue (transient) |
| `RESULT_UPLOAD_FAILED` | The result PUT never got through | Re-queue (transient) |
| `RESULT_URL_EXPIRED` | S3 answered 403: the signature died, or `Content-Type` disagrees with it | Re-queue (transient) |
| `RESULT_UPLOAD_REJECTED` | S3 refused the request as formed | Terminal |

`NO_GPU` and `VLLM_UNHEALTHY` also set `refresh_worker: true`, which
tells the RunPod SDK to terminate this worker after the response.

Real exceptions inside the action (a download failure, a shard of
nothing, a dead server mid-job) propagate and turn into RunPod `FAILED`
status with the traceback in the status response's `error` field.

## Manual testing with `curl`

```bash
ENDPOINT_ID=<your-endpoint-id>
API_KEY=<your-runpod-api-key>

RESP=$(curl -sX POST https://api.runpod.ai/v2/${ENDPOINT_ID}/run \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"input":{"action":"ocr","scan_pk":0,"pdf_url":"https://arxiv.org/pdf/1706.03762.pdf"}}')
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

Identical flow to the other workers: the `Build and Push RunPod Surya
Worker` workflow (`.github/workflows/build-runpod-surya-worker.yml`)
builds on any push to `main` touching `scanning/runpod-surya/**` or
`scanning/runpod_common.py`, pushes
`freelawproject/surya-gpu-worker:<sha_short>` + `:latest`, and PATCHes
the RunPod template to the SHA-pinned tag.

Repo secrets used: `RUNPOD_API_KEY` (shared with the other workers) and
`RUNPOD_SURYA_TEMPLATE_ID` (this endpoint's backing template). The
PATCH step is skipped while the second is unset, so the image can be
published before the endpoint exists.

## Updating Python deps

Edit `pyproject.toml`, then run `uv lock` inside
`scanning/runpod-surya/` and commit both files. `uv sync --frozen` in
the Dockerfile errors out if the two ever drift. When bumping
`surya-ocr`, re-read `surya/inference/backends/openai_client.py` and
`surya/recognition/__init__.py` upstream for a changed default, and run
the handler tests.
