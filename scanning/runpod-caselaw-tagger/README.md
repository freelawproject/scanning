# caselaw-tagger-gpu-worker

GPU worker image for [RunPod Serverless] that runs
[freelawproject/caselaw-block-tagger](https://huggingface.co/freelawproject/caselaw-block-tagger),
a ModernBERT-large token classifier that labels the structural parts
of a U.S. case-law opinion — party, separator, docket number, court,
attorneys, judges, date filed, other date, history, disposition,
author, heading — as character spans over the case text.

The image contains only inference code + model weights. No Django, no
database client, no AWS credentials. All sensitive configuration lives
in RunPod endpoint env vars, never in the image.

This worker is a sibling of `../runpod/` (bl_warm detection) and
`../runpod-dotsmocr/` (dots.mocr OCR) and follows the same conventions:
presigned-URL input, JSON output, structured error codes, worker meta on
every response, one shared `runpod_common.py`. General RunPod
operations knowledge (billing model, worker states, result retention)
is documented once in [`../runpod/README.md`](../runpod/README.md) and
applies here unchanged.

[RunPod Serverless]: https://docs.runpod.io/serverless/overview

## What this worker does, and does not do

It **tags sequences**. A sequence is one court case serialized as the
minimal HTML the model was trained on: a `<p>` per text block, plus
`<blockquote>`, `<em>` and `<sup>`, footnote content omitted, one `\n`
between blocks. Cases can exceed the model's 8,192-token context.
The worker returns, per sequence,
the labelled spans as `(start, end, label)` over the input string.

It **does not build the sequences.** Turning a volume's dots.mocr cells
into cases — splitting at the case boundaries the detection run marks,
dropping the footnote and headnote regions, serializing the blocks —
reads the volume's OCR document and its
detection run, and neither is here. That is the caller's job (the
daemon, in a later PR).

**Chunking happens inside the worker.** Each case is tokenized once.
A case that fits the model's 8,192-token context is run whole, as every
training sequence was. A longer one is divided at paragraph boundaries
into windows of at most 8,192 tokens including special tokens.
Windows overlap by up to approximately 800 tokens at block boundaries;
a paragraph too large for one window is split into token slices that
can overlap too. No content is truncated. Each paragraph or fallback
slice takes its predictions from the window where it has the most
context on its nearer edge. The worker merges token predictions before
decoding spans, preserving offsets into the original case string and
avoiding duplicate spans from overlap.

One input file may hold a whole volume's cases. Windows are batched
independently on the GPU, and the worker returns one entry per original
case in one result document. Inline input/output remain supported.

## What's in the image

- `python:3.12-slim-bookworm` plus the torch `cu126` wheels, which
  carry the CUDA runtime themselves (the rule of `../runpod/`).
- transformers 5: the checkpoint was saved by it (`TokenizersBackend`,
  `rope_parameters`) and transformers 4 cannot read it.
- The full model snapshot baked into `/opt/model` (~1.6 GB fp32
  safetensors, tokenizer, config), opened once at build time to prove
  the installed transformers reads it. `HF_HUB_OFFLINE=1` at run time.
- `runpod_common.py`, copied from `scanning/` next to `handler.py` and
  imported as a top-level module: the result envelope and the error
  codes the daemon classifies, shared with the other two workers.

## Building locally

Run from the repo root with `scanning/` as the build context, so the
Dockerfile can copy the shared module:

```bash
docker build -t caselaw-tagger-gpu-worker:local \
    -f scanning/runpod-caselaw-tagger/Dockerfile scanning/
```

The first build downloads ~6 GB of torch wheels and the 1.6 GB model.
No GPU is needed on the build host: the load check runs on the CPU.

### Running the image locally

With a GPU and the nvidia-container-toolkit:

```bash
docker run --rm --gpus all caselaw-tagger-gpu-worker:local
```

Without a GPU the container starts, logs a warning, loads nothing and
fails the fitness check — the behaviour wanted on a misprovisioned
RunPod worker. For a laptop smoke test, `HANDLER_ALLOW_CPU=1` puts the
model on the CPU instead; a 0.4B encoder answers a case in seconds
there:

```bash
docker run --rm -e HANDLER_ALLOW_CPU=1 caselaw-tagger-gpu-worker:local
```

Do not set that on the endpoint: it would turn a worker without a GPU
into one that bills for slow CPU work.

## Configuring the RunPod endpoint

This is a **new** endpoint, not a change to an existing one.

1. **New Template**, image = `freelawproject/caselaw-tagger-gpu-worker:<sha>`,
   container disk ≥ 20 GB (the image extracts to ~10 GB).
2. **New Endpoint** on that template. **GPU**: 16 GB is plenty (the
   weights are 0.8 GB in bf16; the rest is activations at 8,192
   tokens). 24 GB allows a larger `HANDLER_BATCH_SIZE`.
3. **Min Workers**: `0`; **Idle Timeout**: `300s`.
4. **Env vars** (endpoint config, NOT the image):
   - `SENTRY_DSN_GPU`, `SENTRY_ENV`, `GIT_SHA` — Sentry wiring.
   - `HANDLER_MAX_SEQUENCES` — reject inputs above this count
     (default 5000). A reporter volume holds a few hundred cases.
   - `HANDLER_BATCH_SIZE` — sequences per forward pass (default 4).
     Sequences are sorted by length so each batch pads to a near
     neighbour.
   - `HANDLER_DOWNLOAD_TIMEOUT` and friends — see `runpod_common.py`.
5. Put the template id in the repo secret
   `RUNPOD_CASELAW_TAGGER_TEMPLATE_ID`, so the build workflow can patch
   it (below). `RUNPOD_API_KEY` is shared with the other workers.

Nothing in the daemon reads this endpoint yet. Wiring it in is the next
PR: a `RunpodEngine` row in `jobs.py` (endpoint id, concurrency cap,
attempt cap, per-sequence allowance) plus a `RUNPOD_TAGGER_ENDPOINT_ID`
setting, the shape #195 gave detection.

## Handler contract

`handler(job)` dispatches on `job["input"]["action"]`. The only action
is `tag`. The sequences arrive one of two ways, and the caller chooses:

```json
{
  "input": {
    "action": "tag",
    "scan_pk": 123,
    "input_url": "https://s3.../cases.json?X-Amz-...",
    "result_url": "https://s3.../t1.json?X-Amz-...",
    "result_key": "processing/1/tc/164/1/jobs/tag/caselaw_tagger/t1.json",
    "batch_size": 4
  }
}
```

- **`input_url`**: a presigned GET of a JSON document, either
  `{"sequences": [...]}` or the bare list. A volume's worth of case
  text is megabytes, and RunPod caps a job input at about 10 MB.
- **`sequences`**: the same list inline in the job input. What a local
  test and `curl` use.

Exactly one of the two. Each entry is `{"id": ..., "text": ...}`: `id`
is a string or an integer the caller uses to place the answer (unique
within the job), `text` is the serialized case. `batch_size` is
optional.

Returns:

```json
{
  "sequences": [
    {
      "id": "c1",
      "token_count": 611,
      "window_count": 1,
      "spans": [
        {"start": 3, "end": 22, "label": "party", "text": "Jane ROE, Appellant"},
        {"start": 30, "end": 32, "label": "separator", "text": "v."},
        {"start": 40, "end": 63, "label": "party", "text": "STATE of Example, Appellee"},
        {"start": 71, "end": 81, "label": "docketnumber", "text": "No. 24-123"}
      ]
    },
    {
      "id": "c2",
      "token_count": 9014,
      "window_count": 3,
      "spans": []
    }
  ],
  "sequence_count": 2,
  "failed_sequences": [],
  "model": "freelawproject/caselaw-block-tagger",
  "max_tokens": 8192,
  "duration_ms": 2310,
  "worker_boot_ms": 41200,
  "worker_uptime_ms": 128,
  "gpu_available": true
}
```

Notes:

- `start`/`end` are **character offsets into the input `text`**, `end`
  exclusive, trimmed to the text they cover. `label` is the class
  without its BIO prefix. Spans come in text order.
- The decoder is lenient in one way: an `I-x` that follows nothing, or
  follows a different class, opens a span rather than being discarded.
  Predictions on markup and whitespace are ignored, because markup
  tokens were excluded from the training loss. Span edges are trimmed
  to actual text. The bytes of one multi-byte character (byte-level
  BPE gives each the same offsets) can neither open nor close a span
  in the middle of it, so a West key symbol never splits a heading. A span crossing inline formatting can still contain
  markup inside its `text`, which is the original source substring.
- `token_count` counts the original tokenization, including special
  tokens; it does not double-count overlap. `window_count` reports the
  number of model windows for that case (illustrative above; the exact
  count depends on paragraph sizes). `failed_sequences` is retained
  as an empty list for compatibility; long cases are now processed.
- Entries keep the input order, whatever order the batches ran in.

### Result delivery

`result_url` decides where the payload goes, exactly as for the other
workers:

- **With `result_url`** (a presigned PUT signed
  `Content-Type: application/json`), the payload is wrapped in the
  shared envelope (`schema_version`, `action`, `scan_pk`, `result_key`,
  `payload`) and uploaded, and the job response holds only a summary:
  `result_key`, `bytes`, `span_count`, `sequence_count`,
  `failed_sequences`, `model`, `max_tokens`, `duration_ms`.
- **Without `result_url`**, the payload comes back inline.

### Structured errors

| `error_code` | Meaning | Suggested caller behaviour |
|---|---|---|
| `NO_GPU` | Worker scheduled without a GPU (and `HANDLER_ALLOW_CPU` unset) | Re-queue (transient) |
| `BAD_INPUT` | Invalid input: neither or both of `input_url`/`sequences`, a document that is not JSON, an empty list or one over `HANDLER_MAX_SEQUENCES`, an entry with no string `text` or no string-or-integer `id`, a repeated `id`, a bad `batch_size` | Terminal |
| `UNKNOWN_ACTION` | `action` not `"tag"` | Terminal |
| `INPUT_DOWNLOAD_CORRUPT` | The input document arrived truncated | Re-queue (transient) |
| `RESULT_UPLOAD_FAILED` | The result PUT never got through | Re-queue (transient) |
| `RESULT_URL_EXPIRED` | S3 answered 403 | Re-queue (transient) |
| `RESULT_UPLOAD_REJECTED` | S3 refused the request as formed | Terminal |

`NO_GPU` also sets `refresh_worker: true`, which tells the RunPod SDK
to terminate this worker after the response.

A model exception inside the action propagates and turns into RunPod
`FAILED` status with the traceback in the status response's `error`
field.

## Manual testing with `curl`

```bash
ENDPOINT_ID=<your-endpoint-id>
API_KEY=<your-runpod-api-key>

RESP=$(curl -sX POST https://api.runpod.ai/v2/${ENDPOINT_ID}/run \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"input":{"action":"tag","scan_pk":0,"sequences":[{"id":"demo","text":"<p>Jane ROE, Appellant,</p>\n<p>v.</p>\n<p>STATE of Example, Appellee.</p>\n<p>No. 24-123</p>\n<p>Court of Appeals of Example.</p>\n<p>January 1, 2026</p>\n<p>Affirmed.</p>"}]}}')
JOB_ID=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

while true; do
  S=$(curl -s -H "Authorization: Bearer ${API_KEY}" \
    https://api.runpod.ai/v2/${ENDPOINT_ID}/status/${JOB_ID})
  STATE=$(echo "$S" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("status"))')
  echo "-> ${STATE}"
  [[ "$STATE" != "IN_QUEUE" && "$STATE" != "IN_PROGRESS" ]] && { echo "$S" | python3 -m json.tool; break; }
  sleep 5
done
```

Expect the first call on a fresh endpoint to spend a minute or two in
cold start (image pull + model load); `worker_boot_ms` in the response
shows the in-container share of that.

## Releasing a new image version

Identical flow to the other workers: the `Build and Push RunPod
Case-law Tagger Worker` workflow
(`.github/workflows/build-runpod-caselaw-tagger-worker.yml`) builds on
any push to `main` touching `scanning/runpod-caselaw-tagger/**` or
`scanning/runpod_common.py`, pushes
`freelawproject/caselaw-tagger-gpu-worker:<sha_short>` + `:latest`, and
PATCHes the RunPod template to the SHA-pinned tag.

Repo secrets used: `RUNPOD_API_KEY` (shared) and
`RUNPOD_CASELAW_TAGGER_TEMPLATE_ID` (this endpoint's backing template).

A new checkpoint is a rebuild, not an input change: `TAGGER_MODEL` is a
build arg, and the image is offline at run time.

## Updating Python deps

Edit `pyproject.toml`, then run `uv lock` inside
`scanning/runpod-caselaw-tagger/` and commit both files. `uv sync
--frozen` in the Dockerfile errors out if the two ever drift.
