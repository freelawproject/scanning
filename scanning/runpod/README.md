# blackletter-gpu-worker

GPU worker image for [RunPod Serverless] that runs one step: YOLO
detection over every page of a PDF, with the `bl_warm` weight from
[blackletter]. It answers the merged detection list in blackletter's
own `Label` taxonomy.

The image contains only inference code and one model weight. No Django,
no database client, no AWS credentials. All sensitive configuration
lives in RunPod endpoint env vars, never in the image.

This worker is a sibling of `../runpod-dotsmocr/` (the dots.mocr
document parser). Both use the same conventions: presigned-URL input,
a JSON result, structured error codes, and worker meta on every
response. General RunPod operations knowledge (the billing model,
worker states, pods against serverless, result retention) is
documented here and applies to both.

[RunPod Serverless]: https://docs.runpod.io/serverless/overview
[blackletter]: https://github.com/freelawproject/blackletter

## How it fits

```
┌─────────────────────┐        ┌────────────────────────────────────┐
│ caller (daemon /    │        │ RunPod Serverless worker           │
│ curl smoke test)    │  POST  │                                    │
│                     ├───────▶│ handler.py                         │
│  - presign GET URL  │  /run  │  - downloads one original shard    │
│  - presign PUT URL  │        │  - renders pages at 200 dpi        │
│  - submit job       │◀───────│  - runs bl_warm over every page    │
│  - poll /status     │  JSON  │  - merges and returns the rows     │
│  - read the object  │        │  - PUTs the payload to S3          │
└─────────────────────┘        └────────────────────────────────────┘
```

The stage that drives this image is issue #195, and the redaction work
that reads its output is #196. The image ships first, so a fresh deploy
has no automatic caller: submit a job by hand (see
[Manual testing](#manual-testing-with-curl)) until #195 lands.

Detection reads the **original** shard, never the bitonal copy. bl-warm
was trained on greyscale renders, and its large region classes collapse
on 1-bit pages: caption F1 measured 0.99 against 0.25. Volumes arrive
already cut into shards (#164), and both GPU stages fan out over that
one shard set.

## What's in the image

- `python:3.12-slim-bookworm` base. There is no CUDA base layer: the
  torch `cu126` wheels declare the CUDA runtime themselves, cuDNN
  included, so a CUDA base would ship a second copy. The previous image
  needed one for PaddlePaddle, which links `libcuda` at import and
  forced both the cuDNN base and a build-time `libcuda.so.1` stub. Both
  went with the `analyze` step (#173).
- PyTorch `cu126` wheels plus ultralytics, installed into `/opt/venv`
  by uv from `uv.lock`.
- `blackletter[detect]` from PyPI, floor 0.3.0 — the release that
  carries the `bl_warm` adapter, its weight source and its confidence
  gates (blackletter #73).
- `bl_warm.pt` baked into blackletter's own weights directory, so cold
  start never touches the network (`HF_HUB_OFFLINE=1` at run time). It
  is downloaded at build from the public HF repo
  `freelawproject/blackletter-weights`, at the commit blackletter pins.
- No PaddleOCR, no tesseract, no Ghostscript. Page numbers now come
  from dots.mocr (#149), so the whole `analyze` stack is gone.

### The legacy trio needs a rebuild, not an input

Only `bl_warm.pt` is baked. `api.detect` calls `ensure_weights` itself
and would reach Hugging Face at run time for any other name, so the
handler refuses a weight that is not in the image
(`handler._missing_weights`). Running `small`/`medium`/`large` again
therefore means a build with those names added to the bake, not a
changed job input.

### The checkpoint owns `imgsz`

Ultralytics keeps the training resolution when it loads a `.pt`
(`Model._reset_ckpt_args`), and `predict` merges those overrides ahead
of its own defaults, which name no `imgsz`. `bl_warm.pt` carries
`imgsz = 1024`, so that is what it predicts at. Nothing in the handler
passes the value, because passing the library default of 640 would
quietly cost accuracy on the small classes (the key icon, the page
number, the state abbreviation).

The preload logs the value it found. A checkpoint that lost its training
arguments falls back to 640 with no error, and that log line is the only
warning you get. To check a weight file directly:

```bash
python -c "import torch; print(torch.load('bl_warm.pt', map_location='cpu', weights_only=False)['train_args']['imgsz'])"
```

The render resolution is fixed by blackletter (`scanner.DPI = 200`,
`scanner.YOLO_BATCH = 4`). The dpi matches `DOCTOR_BITONAL_DPI` and the
dots.mocr constant, so every bounding box in the corpus describes the
same pixel space.

## Building locally

Build with `scanning/` as the context. The handler imports the shared
`runpod_common.py` that lives there:

```bash
docker build -t blackletter-gpu-worker:local \
    -f scanning/runpod/Dockerfile scanning/
```

The build needs no GPU: the weight check only opens files on disk.

Measured on the first build of this image: about 4.2 GB to pull
(`docker image inspect --format '{{.Size}}'`) and about 8 GB unpacked
(the sum of `docker history`). `docker image ls` reports 12.1 GB under
the containerd image store, which counts the content and the snapshot
together. The three-model image it replaces extracted to about 22 GB.

### Running the image locally

Requires a GPU plus the nvidia-container-toolkit:

```bash
docker run --rm --gpus all blackletter-gpu-worker:local
```

Without a GPU the container starts, logs a warning, skips the weight
preload, and fails the fitness check — the same behaviour as on a
misprovisioned RunPod worker.

## Configuring the RunPod endpoint

This image replaces the previous one on the **existing** endpoint, so a
deploy needs no new endpoint and no new settings. For a fresh endpoint:

1. **New Endpoint → Custom Source (Docker Image)**, image =
   `freelawproject/blackletter-gpu-worker:<sha>`.
2. **GPU**: RTX A5000 (24 GB) is the recommended tier. Any GPU with
   16 GB or more should serve, since one 18-class model at
   `imgsz=1024` and batch 4 is a modest load.
3. **Container Disk**: 30 GB or more (the image unpacks to about
   8 GB, and the worker needs room for one shard plus its renders).
4. **Min Workers**: `0`; **Idle Timeout**: `300s`.
5. **Env vars** (endpoint config, NOT the image):
   - `SENTRY_DSN_GPU`, `SENTRY_ENV`, `GIT_SHA` — Sentry wiring.
   - `HANDLER_MAX_PAGES` — reject a PDF above this page count
     (default 5000).
   - `HANDLER_DOWNLOAD_TIMEOUT` and friends — see the `runpod_common`
     tunables.
   - `HANDLER_UPLOAD_MAX_ATTEMPTS`, `HANDLER_UPLOAD_TIMEOUT`,
     `HANDLER_UPLOAD_CONNECT_TIMEOUT` — the result-PUT tunables, also
     in `runpod_common`. **Caution:** rename any `HANDLER_RESULT_UPLOAD_*`
     variables left on a reused endpoint template, because this image
     silently ignores them — the legacy worker read that prefix, with a
     default of 5 attempts where `runpod_common` defaults to 3.

Note the **Endpoint ID**: the daemon reads it from settings when #195
lands.

## Handler contract

`handler(job)` dispatches on `job["input"]["action"]`. The only action
is `detect`:

```json
{
  "input": {
    "action": "detect",
    "scan_pk": 123,
    "pdf_url": "https://s3.../shards/0001.pdf?X-Amz-...",
    "result_url": "https://s3.../r1-s0-a1.json?X-Amz-...",
    "result_key": "processing/1/tc/164/1/jobs/detect/bl_warm/r1-s0-a1.json",
    "models": ["bl_warm"],
    "confidence": 0.20
  }
}
```

Everything but `pdf_url` is optional. `models` accepts a bare string as
well as a list, and every name must be baked into the image. An absent
key or JSON `null` means the default; an empty list is refused as
`BAD_INPUT`, because it is indistinguishable from a caller bug that
filtered every model away.
`confidence` must be in `(0, 1]`; the default 0.20 is blackletter's
`CONFIDENCE_THRESHOLD`, and the per-label gates that shape the
redactions are applied downstream.

There is deliberately no `max_pages` input: elsewhere in the repo that
name means "truncate to the first N pages", and a partial detection
merged as a whole volume is worse than a failure. A PDF over the
env-level `HANDLER_MAX_PAGES` is rejected with `error_code=BAD_INPUT`.

Returns:

```json
{
  "detections": [
    {
      "page_index": 0,
      "label": "CASE_CAPTION",
      "label_id": 7,
      "confidence": 0.91,
      "bbox": [10.0, 20.0, 300.0, 90.0],
      "img_width": 1700,
      "img_height": 2200,
      "found_by": [{"model": "bl_warm", "confidence": 0.91}],
      "model_count": 1
    }
  ],
  "page_count": 100,
  "models": ["bl_warm"],
  "duration_ms": 41027,
  "worker_boot_ms": 21840,
  "worker_uptime_ms": 128,
  "gpu_available": true
}
```

Notes on the rows:

- `page_index` counts from zero **inside this PDF**. A caller working
  on shards offsets it by the shard's own first page, exactly as #149
  does for dots.mocr.
- `bbox` is in rendered page-image pixel space, which `img_width` and
  `img_height` describe. Ultralytics rescales from model-input space
  itself, so `imgsz` never reaches the output.
- `found_by` is the provenance blackletter's merge writes in place of
  the per-model `model` key. Keep it: `blackletter.bl_warm
  .rows_are_bl_warm` reads it to pick the bl-warm confidence gates, and
  #196 depends on that.
- `label` is a `Label` name, not a bl-warm class name. The adapter maps
  the 18 classes onto the taxonomy, splits the whole-page `body` box
  into two `TEXT_COLUMN` boxes, and drops `heading` and `blockquote`,
  which have no `Label`.

### Result delivery

`result_url` decides where the payload goes, and the caller chooses:

- **With `result_url`** (a presigned PUT), the payload is wrapped in a
  self-describing envelope and uploaded, and the job response holds only
  a summary:

  ```json
  {
    "result_key": "processing/1/.../r1-s0-a1.json",
    "bytes": 481200,
    "detection_count": 1841,
    "page_count": 100,
    "models": ["bl_warm"],
    "duration_ms": 41027
  }
  ```

  The envelope at that key is:

  ```json
  {
    "schema_version": 1,
    "action": "detect",
    "scan_pk": 123,
    "result_key": "processing/1/.../r1-s0-a1.json",
    "payload": { "detections": [ ... ], "page_count": 100, ... }
  }
  ```

- **Without `result_url`**, the payload comes back inline. That is the
  path dev and continuous integration take without credentials, and the
  path a caller on an older contract gets — so rolling this image back
  needs no daemon change.

Why S3 rather than inline for a real volume: RunPod caps a response at
about 20 MB and discards it with the job record roughly 30 minutes
after the job ends. A dense shard produces thousands of rows, and a
caller whose daemon was down for an hour would lose work it had already
paid for. An S3 object has neither limit.

`Content-Type: application/json` is sent on the PUT because the caller
**signs** it into the URL. The two must match exactly; a mismatch is a
403 that reads like an expired signature.

### Structured errors

| `error_code` | Meaning | Suggested caller behaviour |
|---|---|---|
| `NO_GPU` | Worker scheduled without a GPU | Retry (transient) |
| `BAD_INPUT` | Invalid input: `action`/`pdf_url` missing or wrong type, a weight name the image does not carry, `confidence` out of range, page count over `HANDLER_MAX_PAGES` | Terminal |
| `UNKNOWN_ACTION` | `action` not `"detect"` | Terminal |
| `INPUT_DOWNLOAD_CORRUPT` | The downloaded PDF is empty, truncated, or will not open | Retry (transient) — the object in the bucket is sound, so the fault is in the transfer |
| `RESULT_UPLOAD_FAILED` | The result PUT never got through | Retry (transient) — a fresh job mints a fresh URL |
| `RESULT_URL_EXPIRED` | S3 answered 403: the signature died, or `Content-Type` disagrees with it | Retry (transient) — the two causes are indistinguishable from the worker, and a bounded retry costs less than losing a volume |
| `RESULT_UPLOAD_REJECTED` | S3 refused the request as formed (wrong region, a bucket policy demanding headers we do not send) | Terminal — every retry re-runs the GPU work and fails identically |

`NO_GPU` also sets `refresh_worker: true`, which tells the RunPod SDK
to terminate this worker after the response. A CPU-only worker never
grows a GPU, and keeping it warm would let it keep swallowing retried
jobs.

A real exception inside the action propagates and becomes RunPod
`FAILED` status, with the traceback in the status response's `error`
field.

## Manual testing with `curl`

```bash
ENDPOINT_ID=<your-endpoint-id>
API_KEY=<your-runpod-api-key>

RESP=$(curl -sX POST https://api.runpod.ai/v2/${ENDPOINT_ID}/run \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"input":{"action":"detect","scan_pk":0,"pdf_url":"https://arxiv.org/pdf/1706.03762.pdf"}}')
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

The first call on a fresh endpoint spends minutes in cold start (image
pull plus CUDA init); `worker_boot_ms` in the response shows the
in-container share of that. A test PDF that is not a scanned reporter
page returns few detections or none — that is the model working, not a
failure.

## Releasing a new image version

Pushing a new image tag to the registry alone does **not** update the
running endpoint. The RunPod template that backs the endpoint has to be
told about the new tag. There are two ways.

### Automated release (default)

The `Build and Push RunPod Worker` workflow
(`.github/workflows/build-runpod-worker.yml`) handles this end to end.
On any push to `main` that touches `scanning/runpod/**` or
`scanning/runpod_common.py`, or on a manual `workflow_dispatch` run, it:

1. Builds `freelawproject/blackletter-gpu-worker` and pushes both
   `:<sha_short>` and `:latest` to Docker Hub.
2. Calls `PATCH https://rest.runpod.io/v1/templates/<id>` to move the
   endpoint's backing template `imageName` to the SHA-pinned tag.
3. With `Min Workers = 0`, warm workers drain on the idle timeout and
   the next job cold-starts on the new image.

The workflow needs two repository secrets:

- `RUNPOD_API_KEY` — a RunPod API key with **Restricted** permission
  set to **GraphQL: Read/Write**. That is the tightest scope RunPod
  offers. There is no per-template scope, so the key manages every
  template, endpoint and pod on the account; treat it as a production
  credential.
- `RUNPOD_TEMPLATE_ID` — the ID of the endpoint's backing template.
  An endpoint created without an explicit template still has one,
  hidden from the default listing. Surface it with:
  ```bash
  curl -sS -H "Authorization: Bearer <API_KEY>" \
    "https://rest.runpod.io/v1/templates?includeEndpointBoundTemplates=true"
  ```
  Find the entry where `isServerless: true` and the name matches the
  endpoint, then copy its `id`.

### Manual release (the RunPod console)

Useful for a hotfix off a branch, or when the workflow is unavailable:

1. Open the endpoint on the **Serverless** page.
2. Click **Manage**, then **New Release**.
3. Paste the full tag, for example
   `freelawproject/blackletter-gpu-worker:ff73140`.
4. Save. Warm workers drain their current job; new workers start from
   the updated image.

### Why we pin a commit SHA instead of `:latest`

- **Caching conflicts.** RunPod caches images per node for faster cold
  starts. A new `:latest` does not invalidate that cache, so a worker
  on a cached node can keep running the old image after you "updated"
  it.
- **Unpredictable deployments.** You cannot tell which code is
  running, which makes an issue hard to reproduce and a rollback hard
  to trust.
- **Debugging.** When something breaks, the tag must name the build.

See also the [RunPod image versioning
docs](https://docs.runpod.io/serverless/workers/deploy#image-versioning).

## Updating Python deps

Edit `pyproject.toml`, then run `uv lock` in `scanning/runpod/` and
commit both files. `uv sync --frozen` in the Dockerfile fails the build
if the two ever drift. `.github/workflows/blackletter-dependency-pr.yml`
bumps blackletter in this project and in the repository root together,
which is how the image and the application stay on one version.

To move to a newer CUDA minor version:

1. Change the `[[tool.uv.index]]` URL in `pyproject.toml`, for example
   `cu126` to `cu128`.
2. Run `uv lock` in this directory and commit the result.
3. Rebuild, then test against a real RunPod job before the tag becomes
   the production image. A newer CUDA raises the host driver floor, and
   community hosts lag.

## Billing model (what costs money)

RunPod Serverless charges per second for **worker time**, not for queue
time:

| Phase | Status | Billed? |
|---|---|---|
| Waiting for a worker slot (`IN_QUEUE`, `Throttled`) | Queue | No |
| Image pull onto a fresh node | Cold boot | Yes |
| Container start plus `_preload()` | Cold boot | Yes |
| Handler executing (`IN_PROGRESS`) | Execution | Yes |
| Warm worker idling (before the idle timeout) | Idle | Yes |
| Warm worker killed after the idle timeout | — | No |

Practical points:

- Leaving jobs queued for hours during a capacity crunch costs nothing.
  You start paying when a worker boots for them.
- The `delayTime` field mixes queue wait (free) with worker boot
  (billed). **Usage → Serverless** in the dashboard separates them.
- `Min Workers > 0` reserves always-warm workers and bills their idle
  time around the clock. Leave it at `0` unless cold-start latency
  hurts a user.
- A higher idle timeout keeps a worker warm longer after the last job,
  which helps the next job and costs the extra idle time.

### Cost per volume

The old three-model image measured 298 s of detect execution on a
1359-page volume, about $0.048 at the 16 GB tier rate of roughly
$0.00016/s. One model instead of three should cost less per page, but
that is not measured yet — record a real number here after the first
production volume.

Two things changed around the number. Volumes now arrive sharded
(#164), so several jobs run in parallel and each pays its own cold
start; and RunPod bills every worker's boot, so parallel shards on cold
workers can cost more than a few shards in series on one warm worker.

### When serverless stops being the cheapest option

A dedicated **GPU Pod** rents the same class of GPU at roughly
$0.24–$0.27 per hour on demand for the 16–24 GB tier, which beats
serverless once you saturate the GPU.

| Daily volume | Cheapest option |
|---|---|
| Under about 10 scans a day | Serverless |
| About 10–40 scans a day in a predictable window | A scheduled pod, 4–8 hours a night |
| Over about 40 scans a day, or latency-sensitive all day | A pod around the clock |

Rule of thumb: if one endpoint's serverless bill passes about $1.50 a
day, a pod covering the active hours is probably cheaper.

## GPU Pods as an alternative

A GPU Pod is a dedicated virtual machine with a GPU attached. You pay
per second while it runs, and only for disk while it is stopped. It
suits a predictable batch, or a period when serverless GPU availability
is unreliable.

| Action | What happens | Billing |
|---|---|---|
| Create and start | Provisions disk, pulls image, boots. 2–5 min. | Per second from GPU attach |
| Stop | Powers the machine off and keeps the disks. | Disk only (about $0.10/GB/month) |
| Terminate | Destroys the pod and its local disk. | None |

On-demand against spot: spot saves 30–50% and can be preempted at
about 30 seconds' notice. A shard takes minutes and cannot resume from
the middle, so use on demand. Secure Cloud (RunPod's own datacentres)
is more reliable than Community Cloud (individual hosts) and modestly
more expensive; prefer it for production.

Three ways to run a pod only when work exists:

1. **Cron.** Start and stop on a fixed schedule (`runpodctl start pod
   <id>`). Simplest, and good for an overnight batch.
2. **Daemon-triggered.** The daemon starts the pod when the queue is
   not empty and stops it when it drains. Best cost, most code.
3. **A serverless endpoint backed by an always-on pod worker.** Attach
   the pod to the existing endpoint as a dedicated worker. The caller
   still posts to `/run` and nothing in our code changes.

`runpod_client` talks only to serverless `/run` and `/status`, so
option 3 is the lowest-friction move.

## Result retention

RunPod purges a job record after a short window, so a caller must poll
`/status` before it expires to learn the job's fate. The *payload*
outlives that window when the worker writes it to S3 (see [Result
delivery](#result-delivery)); only the job record expires.

| Submission path | Result retention |
|---|---|
| `/run` (async) | 30 minutes after completion |
| `/runsync` (sync) | 1 minute by default, 5 minutes at most |
| Job TTL | 24 hours by default (configurable) |

The last row matters on its own: if a job's TTL expires **while it is
still running**, RunPod removes it and `/status` answers 404 even
though execution would have finished.

Because the result key is scoped to the run, the shard and the attempt,
a 404 is recoverable: the caller asks S3 for the object before it
writes the job off (`jobs.py` does this for dots.mocr today).

Result objects live only in S3, under each scan's
`processing/{pk}/.../jobs/` prefix, and are excluded from the
processing-file sync in both directions. Admin scan deletion sweeps the
prefix.

## Monitoring worker states

The endpoint's **Workers** tab shows each worker's state:

| State | Meaning | Billed? |
|---|---|---|
| `Initializing` | Pulling the image, starting the container, running `_preload()`. | Yes, from GPU attach |
| `Running` | Executing a job. | Yes |
| `Idle` | Holding the GPU warm, waiting inside the idle timeout. | Yes |
| `Throttled` | Allocated but waiting for physical GPU capacity. No GPU attached. | No |
| `Unhealthy` | Failed a fitness check, or crashed during a job. | Yes, while alive |
| `Ready` | Warm and awaiting dispatch (rare, transient). | Yes |

- `Throttled` is the only live state that is free. With no physical GPU
  of the chosen type, workers wait there at no cost.
- The idle timeout decides how long `Idle` lasts. Five seconds means
  every job pays a cold start; five minutes suits sporadic work.
- `Unhealthy` workers accumulate when jobs crash. RunPod terminates
  them within a few minutes.
- Watching the tab is the fastest way to confirm warm reuse across the
  shards of one volume.

## Security notes

### What the image contains

- Open-source code: blackletter, ultralytics, torch.
- One publicly distributed model weight: `bl_warm.pt` from the HF repo
  `freelawproject/blackletter-weights`.
- No secrets. No AWS keys, no database credentials, no Django
  `SECRET_KEY`, no Sentry DSN (read from the environment at run time).

Pushing this image to a public registry leaks nothing proprietary.

### What must stay out of the image

Never bake `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`,
`RUNPOD_API_KEY`, `SENTRY_DSN_GPU`, a Django settings file, or a `.env`
into the Dockerfile or `pyproject.toml`. All of them are endpoint env
vars instead.

### Input-URL handling

The handler downloads the PDF from whatever URL the job names. The
trust boundary today is that only the daemon holds `RUNPOD_API_KEY`, so
only the daemon submits jobs. If that changes, validate the scheme,
pin a host allowlist, and reject a suspicious redirect.

### The presigned PUT is a write capability

`result_url` hands a third-party GPU host the ability to write to our
bucket. It stays narrow: one key, one method, and a bounded lifetime
(`RUNPOD_PRESIGNED_TTL`). The key sits under the scan's own `jobs/`
subtree, so a leaked URL can only create the one object the caller
already expects.

The alternative — temporary credentials through STS — would put an AWS
credential in the worker's environment and give up the property this
image deliberately has: no AWS credentials anywhere in it.

### Container root user

The image runs as root, which is standard for a serverless worker with
no shell access and no state between jobs. Harden it if you ever run it
in a long-lived multi-tenant environment.

## Troubleshooting

**`weight missing or truncated` at build time** — the Hugging Face
download failed on all three attempts, or the file arrived short.
Rebuild. If HF reliability becomes a pattern, mirror the weight to a
private bucket and change the source in blackletter's
`ensure_weights`.

**The preload logs `imgsz=640`** — the checkpoint lost its training
arguments (a re-export, a hand-converted file). Detection still runs,
at a lower score on the small classes. Replace the weight file.

**`error_code=BAD_INPUT` naming a weight** — the job asked for a
weight the image does not carry. The message lists what it does carry.
Bake the weight rather than letting the worker download it.

**Runs on CPU rather than GPU** — the endpoint has no GPU worker, or
the host driver is not bind-mounted. Confirm `docker run --gpus all`
works locally, then check the endpoint's GPU selection. The fitness
check should keep such a worker out of the pool; a job that leaks
through answers `NO_GPU`.

**A slow first job on every worker** — expected cold start (image pull
plus CUDA init). Raise the idle timeout so a warm worker serves more
shards, or raise `Min Workers` to 1 and pay for idle time.

**`HTTP 404 from /status` on a job that recently finished** — the
retention window closed (30 minutes async, 1 minute sync), or the job's
24-hour TTL expired while it ran. With `result_url` in the input the
payload is still in S3, which is what makes this recoverable.

**The endpoint sits in "initializing" for many minutes** — the
container disk is too small for the extracted image. Raise it.

**`retries: 1` on the first ever call to a new endpoint** — usually an
image-pull timeout on a cold node. RunPod re-dispatches and the second
attempt uses the cached layers. It should not repeat.
