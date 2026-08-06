# blackletter-gpu-worker


GPU worker image for [RunPod Serverless]. Runs the two GPU-heavy steps
of the blackletter pipeline (`detect` and `analyze_pdf`) so the rest of
the scanning app can stay on CPU-only boxes.

The image contains only inference code + model weights. No Django, no
database client, no AWS credentials. All sensitive configuration lives
in RunPod endpoint env vars, never in the image.

[RunPod Serverless]: https://docs.runpod.io/serverless/overview

## How it fits


```
┌─────────────────────┐        ┌──────────────────────┐
│ scanning daemon     │        │ RunPod Serverless    │
│ (scanning/services) │        │ endpoint             │
│                     │  POST  │                      │
│ runpod_client.py    ├───────▶│ handler.py           │
│  - upload PDF to S3 │  /run  │  - download PDF via  │
│  - presign GET + PUT│        │    presigned GET     │
│  - submit job       │        │  - blackletter.api   │
│  - poll /status     │◀───────│    .detect / analyze │
│  - read result key  │  key   │  - PUT result to S3  │
│  - fetch from S3    │        │  - return the key    │
└─────────────────────┘        └──────────────────────┘
          │                               │
          │        ┌──────────────┐       │
          └───────▶│      S3      │◀──────┘
             GET   │  PDF in,     │  PUT
                   │  result out  │
                   └──────────────┘
```

Full design rationale, payload-size math, and tradeoffs are in
`../../RUNPOD_MIGRATION_PLAN.md` at the repo root.

## What's in the image

- Ubuntu 24.04 LTS base with CUDA 12.6 + cuDNN runtime libraries.
- Python 3.12 (system python, no venv managers at runtime).
- PyTorch 2.6 cu126 + paddlepaddle-gpu 3.1.0 cu126 (aligned on one
  CUDA minor version).
- `blackletter` pinned to PyPI (`>=0.1.1`).
- Pre-baked weights so cold start skips the network:
  - YOLO `small.pt`, `medium.pt`, `large.pt` (blackletter >=0.1.1
    bundles none; all three are downloaded at build from the public
    HF repo `freelawproject/blackletter-weights`).
  - PaddleOCR PP-OCRv5 server det + rec weights in `/opt/paddlex`.
- A `libcuda.so.1` stub from the CUDA `-devel-` variant so
  `import paddle` succeeds during `docker build`; the host's real
  `libcuda.so.1` is bind-mounted over it at RunPod runtime via
  `--gpus all`.

## Building locally

Run from the scanning repo root so the build context is only this
directory:

```bash
docker build -t blackletter-gpu-worker:local scanning/runpod/
```

Expect 15–45 min on a first build, mostly bandwidth-bound (CUDA base,
CUDA devel stub layer, torch cu126, paddlepaddle-gpu cu126,
`large.pt`, PaddleOCR weights). Subsequent rebuilds are dramatically
faster thanks to layer caching.

You do **not** need a GPU on the build host. The Dockerfile's
warmup steps load models in CPU fallback mode just long enough to
verify files are valid.

### Running the image locally

Requires a GPU plus the [nvidia-container-toolkit]:

```bash
docker run --rm --gpus all \
  -e HANDLER_MAX_PAGES=5000 \
  blackletter-gpu-worker:local
```

On a machine without a GPU the container starts, loads models on CPU,
and can serve the `handler` function for smoke-tests (slow, but
functional).

[nvidia-container-toolkit]: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html

## Pushing to a registry

RunPod Serverless pulls the image from a container registry at worker
start-up. Use whichever registry your deployment workflow prefers.

### Docker Hub (personal test)

```bash
docker login -u <dockerhub-user>
docker tag blackletter-gpu-worker:local \
           <dockerhub-user>/blackletter-gpu-worker:<tag>
docker push <dockerhub-user>/blackletter-gpu-worker:<tag>
```

### GitHub Container Registry (production target)

```bash
docker login ghcr.io -u <github-user>   # use a PAT with packages:write
docker tag blackletter-gpu-worker:local \
           ghcr.io/freelawproject/blackletter-gpu-worker:<tag>
docker push ghcr.io/freelawproject/blackletter-gpu-worker:<tag>
```

## Releasing a new image version

Pushing a new image tag to the registry alone does **not** update the
running endpoint; the RunPod template that backs the endpoint has to
be told about the new tag. We support two ways to do this:

### Automated release (default)

The `Build and Push RunPod Worker` GitHub Actions workflow
(`.github/workflows/build-runpod-worker.yml`) handles this end-to-end.
On any push to `main` that touches `scanning/runpod/**` (or a manual
`workflow_dispatch` run), it:

1. Builds `freelawproject/blackletter-gpu-worker` and pushes both
   `:<sha_short>` and `:latest` tags to Docker Hub.
2. Calls `PATCH https://rest.runpod.io/v1/templates/<id>` to update
   the endpoint's backing template `imageName` to the SHA-pinned tag.
3. With `Min Workers = 0`, warm workers drain on the 5-min Idle
   Timeout and the next job dispatched to the endpoint cold-starts on
   the new image.

The workflow needs two GitHub Actions repo secrets:

- `RUNPOD_API_KEY` — a RunPod API key with **Restricted** permission
  set to **GraphQL: Read/Write** (leave AI API at None). This is the
  tightest scope RunPod allows. There is no per-template scope, so
  this key grants full management of every template, endpoint, and
  pod on the account; treat it as a production credential.
- `RUNPOD_TEMPLATE_ID` — the ID of the endpoint's backing template.
  Endpoints created without explicitly picking a template still have
  one, hidden from the default listing. Surface it via:
  ```bash
  curl -sS -H "Authorization: Bearer <API_KEY>" \
    "https://rest.runpod.io/v1/templates?includeEndpointBoundTemplates=true"
  ```
  Find the entry where `isServerless: true` and `name` matches the
  endpoint (e.g. `Blackletter gpu worker`); copy its `id`.

### Manual release (via the RunPod console)

Useful when publishing a hotfix from a non-`main` branch, or when the
workflow is unavailable. Create a new release by hand so workers boot
from the new image:

1. Go to the **Serverless** page and open your endpoint.
2. Click **Manage**.
3. Select **New Release**.
4. In the **Container Image** field, paste the full new tag, e.g.
   `freelawproject/blackletter-gpu-worker:ff73140`.
5. Save. Existing warm workers drain their current jobs; new workers
   start from the updated image.

### Why we pin a commit SHA instead of using `:latest`

Using `:latest` is unreliable for serverless deployments for three reasons:

- **Caching conflicts.** RunPod caches images on each node for faster
  cold starts. Pushing a new `:latest` does not invalidate that cache,
  so workers on cached nodes may continue running the old image even
  after you "updated" it.
- **Unpredictable deployments.** You cannot guarantee which version of
  the code is running, making it hard to reproduce issues or roll back
  to a known-good state.
- **Debugging difficulties.** When a problem occurs, you won't know
  which exact image version caused it.

Pinning to a commit SHA (e.g. `:8c71f45`) makes every release
explicit, diffable in git history, and instantly reversible.

See also: [RunPod image versioning docs](https://docs.runpod.io/serverless/workers/deploy#image-versioning).

## Configuring the RunPod endpoint

In the RunPod Serverless console:

1. **New Endpoint → Custom Source (Docker Image)**.
2. **Container image**: the tag you just pushed.
3. **Container registry credentials**: only needed for private images.
4. **GPU**: RTX A5000 (24 GB) is the recommended starting tier. Any
   GPU ≥ 16 GB should work.
5. **Min Workers**: `0` (pay only while a job is in flight).
6. **Idle Timeout**: `300s` (5 min) — warm workers handle back-to-back
   submissions without cold-starting again.
7. **Env vars** (set in the endpoint config, NOT in the image):
   - `SENTRY_DSN_GPU` — separate Sentry project from Django.
   - `SENTRY_ENV` — `prod` / `staging` / etc.
   - `GIT_SHA` — optional, used as the Sentry release tag.
   - `HANDLER_MAX_PAGES` — reject PDFs above this many pages
     (default 5000).
   - `HANDLER_DOWNLOAD_TIMEOUT` — seconds to wait for the presigned
     GET URL download (default 300).

Save the endpoint; note the **Endpoint ID** — you'll set it in the
scanning daemon env as `RUNPOD_ENDPOINT_ID`.

## Manual testing with `curl`

Once the endpoint is deployed, run these in order to confirm each
layer works before flipping the daemon to remote mode.

Set your variables once:

```bash
ENDPOINT_ID=<your-endpoint-id>
API_KEY=<your-runpod-api-key>
```

### Submit + poll (async, recommended)

Async submission returns a job ID immediately; poll `/status/{id}`
until terminal. This is the path `runpod_client.py` uses in
production, so it's the most representative smoke test.

```bash
# Submit a detect job with a small public PDF
RESP=$(curl -sX POST https://api.runpod.ai/v2/${ENDPOINT_ID}/run \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"input":{"action":"detect","scan_pk":0,"pdf_url":"https://arxiv.org/pdf/1706.03762.pdf","models":["small"]}}')
echo "$RESP"
JOB_ID=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

# Poll
while true; do
  S=$(curl -s -H "Authorization: Bearer ${API_KEY}" \
    https://api.runpod.ai/v2/${ENDPOINT_ID}/status/${JOB_ID})
  STATE=$(echo "$S" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("status"))')
  echo "-> ${STATE}"
  [[ "$STATE" != "IN_QUEUE" && "$STATE" != "IN_PROGRESS" ]] && { echo "$S" | python3 -m json.tool; break; }
  sleep 3
done
```

### Quick sanity check (sync)

`/runsync` blocks until completion (subject to its ~60 s timeout —
cold-starting workers will exceed this and fall back to `IN_QUEUE`,
requiring follow-up polls).

```bash
curl -sX POST https://api.runpod.ai/v2/${ENDPOINT_ID}/runsync \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"input":{"action":"detect","scan_pk":0,"pdf_url":"https://arxiv.org/pdf/1706.03762.pdf","models":["small"]}}' \
  | python3 -m json.tool
```

Note: sync-mode has specific quirks with the `runpod` SDK's job
dispatching — if you see workers being killed with "Failed to get
job. | missing field(s): id or input", switch to `/run` above.

### What a successful response looks like

```json
{
  "delayTime": 8200,
  "executionTime": 9564,
  "id": "...",
  "status": "COMPLETED",
  "workerId": "...",
  "output": {
    "detections": [ /* per-page detection dicts */ ],
    "page_count": 30,
    "duration_ms": 6344,
    "worker_boot_ms": 7669,
    "worker_uptime_ms": 11087,
    "gpu_available": true
  }
}
```

Interpreting the fields:

- `delayTime` (RunPod): total wall-clock from submission to execution
  start. Combines queue wait (free) + worker boot (billed).
- `executionTime` (RunPod): wall-clock spent inside the handler.
- `duration_ms` (handler): time spent in `detect()` / `analyze_pdf()`
  itself, excluding download + upload.
- `worker_boot_ms` (handler): cold-start cost of this worker
  process. Constant across every response from the same worker.
- `worker_uptime_ms` (handler): ms since preload finished at job
  start. Small = cold; large and paired with a recurring
  `worker_boot_ms` = warm reuse.
- `gpu_available` (handler): `true` means torch saw a CUDA device.
  `false` means you hit the NO_GPU fast-fail gate, and the daemon
  will re-queue the scan.

### Verifying cold vs. warm

Submit the same job twice, back-to-back. Expect on call #1:

- `delayTime` of seconds-to-minutes (cold start, image pull on first
  invocation per node).
- Small `worker_uptime_ms`.

On call #2 within the Idle Timeout:

- `delayTime` near zero.
- Same `workerId`, same `worker_boot_ms`.
- `worker_uptime_ms` grows by roughly the gap between the two calls.

## Billing model (what costs money)

RunPod Serverless charges per-second for **worker time**, not for
queue time. Concretely:

| Phase | Status | Billed? |
|---|---|---|
| Waiting for a worker slot (`IN_QUEUE`, `Throttled`) | Queue | No |
| Image pull onto a fresh node | Cold boot | Yes |
| Container start + `_preload()` | Cold boot | Yes |
| Handler executing (`IN_PROGRESS`) | Execution | Yes |
| Warm worker idling (before Idle Timeout) | Idle | Yes |
| Warm worker killed after Idle Timeout | — | No |

Practical implications:

- Leaving jobs queued for hours during a capacity crunch costs
  nothing. You only start paying when a worker actually boots for
  them.
- The `delayTime` field in responses mixes queue wait (free) and
  worker boot (billed). The RunPod dashboard under **Usage →
  Serverless** separates them.
- Setting `Min Workers` > 0 reserves always-warm workers and bills
  for their idle time 24/7. Leave at `0` unless cold-start latency
  is hurting users.
- Raising `Idle Timeout` keeps a worker warm for longer after the
  last job, improving subsequent-job latency at the cost of paying
  for that extra idle time.

### Current Serverless rates (16 GB tier)

The A4000 / A4500 / RTX 2000 Ada / RTX 4000 Ada Serverless tier bills
at approximately **$0.00016/s** ≈ $0.576/hr of active worker time.
Confirm the exact per-second rate in your endpoint's settings, as
RunPod adjusts pricing over time and per tier.

### Per-scan cost, measured on a 1359-page volume

Real numbers from a production-sized test:

| Phase | Duration | Cost @ $0.00016/s |
|---|---|---|
| `detect` execution (3 YOLO models) | 298 s | **$0.048** |
| `analyze` execution (PaddleOCR + YOLO large) | 138 s | **$0.022** |
| Cold boot per GPU call | ~3 s | ~$0.001 |
| Idle between detect and analyze (warm reuse) | 0–300 s | $0–$0.048 |
| Idle after last call (until Idle Timeout) | 0–300 s | $0–$0.048 |

**Per-scan total: ~$0.07 (tight warm reuse) to ~$0.17 (max idle at
a 5-min Idle Timeout).** Smaller scans scale roughly linearly with
page count on execution time; idle cost is fixed.

### When Serverless stops being the cheapest option

Dedicated **GPU Pods** (see next section) rent the same GPU at
**~$0.24–$0.27/hr on-demand** for the 16–24 GB tier — less than
Serverless when you actually saturate the GPU's time.

| Daily volume | Cheapest option |
|---|---|
| < ~10 scans/day | Serverless (pay per use, no idle cost when nothing's happening) |
| ~10–40 scans/day in a predictable batch window | Scheduled pod, 4–8 h/night |
| > ~40 scans/day or latency-sensitive throughout the day | 24/7 dedicated pod |

Rule of thumb: if your Serverless bill exceeds about **$1.50/day**
for a single endpoint, a scheduled pod covering your active hours
is probably cheaper.

## GPU Pods as an alternative

A GPU Pod is a dedicated VM with a GPU attached. You pay per-second
while it's running, and nothing (besides negligible disk storage)
while it's stopped. Good fit for predictable batch workloads, or
when GPU availability on the Serverless scheduler becomes
unreliable.

### Pod lifecycle

| Action | What happens | Billing |
|---|---|---|
| Create + Start | Provisions disk, pulls image, boots VM. 2-5 min. | Per-second from GPU attach |
| Stop | Powers off the VM but preserves disks. | Disk-only (~$0.10/GB/month) |
| Terminate | Destroys the pod and its local disk. | $0 |

### On-Demand vs Spot pricing

When you rent a pod, RunPod typically offers two tiers side-by-side:

| Tier | Price | Preemption? | Use for |
|---|---|---|---|
| **On-Demand** | Higher (e.g. $0.25/hr A4000) | Never. GPU is reserved until you stop the pod. | Our pipeline. Each scan takes minutes and can't resume from the middle. |
| **Spot / Interruptible** | 30-50% off On-Demand | Preempted at ~30 s notice if an On-Demand customer wants the GPU. | Long trainings that checkpoint. **Avoid for scan processing.** |

### Secure Cloud vs Community Cloud

Orthogonal to the pricing tier:

- **Secure Cloud**: RunPod-operated datacenters (Tier 3+). More
  reliable, modestly more expensive. Preferred for production legal-
  document processing.
- **Community Cloud**: GPUs from individual hosts (often miners with
  spare capacity). Cheaper, less reliable. Typical source for
  consumer GPUs like RTX 4090. Queue times and uptime are less
  predictable.

### Three patterns for "turn the pod on only when you need it"

1. **Scheduled via cron.** Start the pod on a fixed schedule
   (`runpodctl start pod <id>`) and stop it when the window ends.
   Simplest. Good for predictable overnight batches.
2. **Daemon-triggered.** Have the scanning daemon check the scan
   queue; when non-empty, start the pod via the RunPod API and
   dispatch work; when empty, stop it. Best cost profile, more code.
3. **Serverless endpoint backed by an always-on pod worker.**
   Attach the pod to your existing Serverless endpoint as a
   "dedicated worker." The daemon code doesn't change at all — it
   still calls `api.runpod.ai/v2/{endpoint}/run` — but the worker
   never cold-starts. Scale the pod up/down via cron. Least invasive
   if you already have the Serverless integration working.

Pod path is not implemented on the daemon side today. `runpod_client`
talks only to Serverless `/run` + `/status` URLs. If you move to a
pod, the lowest-friction option is #3 — no code changes required.

## Result retention

RunPod purges job records after a short window. You **must** poll
`/status` before it expires to learn a job's fate. The *payload*
survives longer than that when the worker writes it to S3 (see
[Result delivery](#result-delivery)); only the job record itself is
subject to the retention below.

| Submission path | Result retention |
|---|---|
| `/run` (async)    | 30 minutes after completion |
| `/runsync` (sync) | 1 minute default, 5 minutes max |
| Job TTL           | 24 hours default (configurable) |

The last row is separate and important: if a job's TTL expires
**while it's still running**, RunPod removes it immediately and
`/status` starts returning 404 even if execution would otherwise
have completed normally.

Our client at `scanning/runpod_client.py` polls continuously from
submission through a terminal state and writes the output straight
to Postgres (`Scan.ocr_results`, `Detection` rows, etc.). Once
committed locally, RunPod's retention window no longer matters.

If the daemon crashes between submission and completion, the in-
flight job ID is lost. The scan sits in `PROCESSING` until the stale-
recovery timeout (`DAEMON_PROCESSING_TIMEOUT`, default 3600 s)
re-queues it. Persisting the job ID and result key on a job row, so a
restarted daemon can resume polling or harvest the object instead of
re-running the work, is issue #150.

Result objects live only in S3, under each scan's
`processing/{pk}/.../jobs/` prefix. Neither side writes them to disk:
the worker serialises the envelope in memory and PUTs it, and the
daemon reads it back into memory and parses it. They're also excluded
from the processing-file sync in both directions, so nothing lands in
the `/tmp` tree that `cleanup_processing_tmp` sweeps, and nothing is
copied into `approved/`.

On S3 the footprint is bounded: one object per scan and action, which
a re-run overwrites. The consumed data lives in Postgres, so the
objects left behind by finished scans are dead weight — a lifecycle
expiry rule on the prefix is the intended cleanup (not yet
configured).

## Using this image from the scanning daemon

The daemon has three modes, picked by env vars only. No code change
is needed to move between them, and the pipeline entry points
(`services.run_full_pipeline`, etc.) are identical across all three.

### Mode 1: local CPU processing (default)

When `RUNPOD_ENABLED` is unset or `False`, the daemon runs
blackletter in-process on the CPU. The scanning project installs
**CPU-only** torch / ultralytics / paddleocr wheels (see
`pyproject.toml`), so local mode never uses a GPU even if one is
present on the host. Fine for dev, CI, the test suite, and small
test PDFs; too slow for production-sized volumes (hundreds to
thousands of pages).

No RunPod account, no image, no extra config needed.

Two equivalent ways to actually process queued scans in this mode:

- **Daemon container** (`scanning-daemon` service in
  `docker/scanning/docker-compose.yml`) runs `run_daemon`
  continuously in the background. This is the normal path.
- **Ad-hoc in the Django container** for debugging a single scan:

  ```bash
  docker exec scanning-django python manage.py run_daemon
  ```

  Or claim and process exactly one QUEUED scan then exit:

  ```bash
  docker exec scanning-django python manage.py process_next_scan
  ```

### Mode 2: RunPod enabled from local dev (smoke test)

Point your local daemon at a real RunPod endpoint. Useful for
verifying end-to-end wiring before deploying to staging, and the
only way to exercise the GPU path from a dev machine (since the
local install is CPU-only). Set in your local `.env`:

```bash
RUNPOD_ENABLED=True
RUNPOD_ENDPOINT_ID=<endpoint-id>
RUNPOD_API_KEY=<runpod-api-key>
# Optional tuning (defaults shown). Leave as-is unless you have a
# reason to change them.
RUNPOD_REQUEST_TIMEOUT=1800       # wall-clock deadline for submit+poll
RUNPOD_MAX_RETRIES=2              # transport-error retries on /run
RUNPOD_PRESIGNED_TTL=86400        # presigned URL lifetime, in seconds
```

Caveat: remote mode uploads the PDF to S3 under the scan's
processing prefix and generates a presigned GET URL the worker uses
to fetch it, plus a presigned PUT the worker writes its result to, so
AWS credentials
(`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` /
`AWS_STORAGE_BUCKET_NAME`) must also be set locally. Without those
you'll get `RunpodError: RUNPOD_ENABLED is true but no AWS
credentials are configured`.

After setting those, restart the daemon (or run `process_next_scan`
manually) and submit a scan through the UI. Daemon logs should show
`runpod <action> job <id> submitted` / `COMPLETED`.

### Mode 3: RunPod in production

The staging / production daemon should set:

```bash
RUNPOD_ENABLED=True
RUNPOD_ENDPOINT_ID=<endpoint-id>         # from the RunPod console
RUNPOD_API_KEY=<runpod-api-key>          # keep secret; never log
# Optional tuning (defaults shown)
RUNPOD_REQUEST_TIMEOUT=1800              # wall-clock cap on submit+poll
RUNPOD_MAX_RETRIES=2                     # transport-error retries on /run
RUNPOD_PRESIGNED_TTL=86400               # presigned URL lifetime, seconds
```

`RUNPOD_PRESIGNED_TTL` has to outlive queue time plus execution,
because it now signs the **result upload** as well as the input
download: a job that finishes after its signature dies can't deliver
what it computed. The default (1 day) leaves the 30-minute
`RUNPOD_REQUEST_TIMEOUT` deadline well inside it, so we cancel a
wedged job long before that becomes possible.

Plus the AWS credentials the rest of the app already uses, and
`AWS_S3_REGION_NAME` if the buckets aren't in the `us-west-2` default.
That one is newly load-bearing: presigned URLs are now SigV4, which
signs the region into the credential scope, so a mismatch fails with
`AuthorizationQueryParametersError` rather than being silently
redirected. Confirm with
`aws s3api get-bucket-location --bucket <name>`.

Env vars that belong on the **RunPod endpoint** (not the daemon):
`SENTRY_DSN_GPU`, `SENTRY_ENV`, `GIT_SHA`, `HANDLER_MAX_PAGES`,
`HANDLER_DOWNLOAD_TIMEOUT`. See
[Configuring the RunPod endpoint](#configuring-the-runpod-endpoint).

Full per-setting documentation:
`scanning/settings/project/runpod.py`.

### Debugging the client

By default the `scanning` logger runs at `INFO`, which emits one line
per job on submit, one line per status **change**
(`IN_QUEUE → IN_PROGRESS → COMPLETED`), and one line on completion.
That's enough for routine operations.

When a job is misbehaving (stuck in `IN_QUEUE`, silent failures,
suspicious boot times), raise the level:

```bash
# .env or environment
SCANNING_LOG_LEVEL=DEBUG
```

Then restart the daemon. You'll see every poll tick, e.g.:

```
DEBUG poll runpod job 63b2d3f6-...-u2 -> IN_QUEUE
DEBUG poll runpod job 63b2d3f6-...-u2 -> IN_QUEUE
INFO  runpod job 63b2d3f6-...-u2 -> IN_PROGRESS      # status change
DEBUG poll runpod job 63b2d3f6-...-u2 -> IN_PROGRESS
INFO  runpod detect job 63b2d3f6-...-u2 COMPLETED in 7883 ms
```

Set back to `INFO` for production.

The presigned `pdf_url` is **never logged** at any level. Submit-time
log lines show the full input dict except that `pdf_url` is replaced
with `***`:

```
INFO runpod detect job 63b2d3f6-...-u2 submitted
  (input={'action': 'detect', 'scan_pk': 1861,
          'pdf_url': '***', 'models': [...], 'confidence': 0.2})
```

### Result expiry is recoverable

RunPod purges job records 30 minutes after completion (async path).
If the daemon misses the retention window (long pause, crash, etc.),
`GET /status/{id}` returns 404. That used to throw the run away. Now
the result object outlives the job record, and the client holds the
key in memory for the duration of the poll, so it checks S3 first:

1. `/status` says COMPLETED — read the object at `result_key`.
2. `/status` returns 404 — `head_object` on `result_key`. An object
   written after this job was submitted means the worker finished and
   uploaded before RunPod dropped the record, so harvest it. Nothing
   there, or something older, means this run produced nothing:
   re-queue and resubmit as before.
3. `/status` says FAILED — the existing retry path, unchanged.

Presence is sufficient evidence of completeness because a single PUT
is atomic: a partial upload never becomes a gettable object. The
write-time check is what makes step 2 safe on a key that gets reused
across runs.

The key only lives in the daemon's memory, so this covers a job that
outlives RunPod's retention, not a daemon that restarts mid-flight.
That needs the key persisted on a job row (#150), which is also what
turns the blocking poll into a sweep.

Polling S3 alone would never be enough either: a failed job also
writes nothing, so absence can't distinguish "still queued" from
"failed". Job status stays the authority; S3 is only ever consulted
after it.

## Development workflow

- **Editing scanning-side code** (client, services wiring,
  daemon): you don't need this image. Keep `RUNPOD_ENABLED=False` and
  iterate against the in-process blackletter path.
- **Editing handler-side code** (`handler.py`, `Dockerfile`): rebuild
  the image and push a new tag. Hot-reload isn't a thing for RunPod
  Serverless — each code change is a new image.
- **Updating Python deps**: edit `pyproject.toml`, then
  `uv lock` inside `scanning/runpod/` to refresh `uv.lock`. Commit
  both files. `uv sync --frozen` in the Dockerfile will error out if
  the two ever drift.

## Handler contract

`handler(job)` dispatches on `job["input"]["action"]`.

### Result delivery

Every action supports two ways of returning its payload, picked per
job by whether the daemon put a `result_url` in the input:

| Input field  | Meaning |
|---|---|
| `result_url` | Presigned S3 **PUT**, covering exactly one object. Its presence switches the worker to S3 delivery. |
| `result_key` | The key that URL signs — `processing/{pk}/.../jobs/{action}/result.json`. Echoed back in the response; the worker never derives it. |

The key is per scan and action, not per run, so a re-run overwrites
its predecessor instead of leaving orphans nobody will read. The
daemon never reads an object without first checking (via
`head_object`) that it was written after the reading job was
submitted, which is what stops a reused key from serving a previous
attempt's output.

With `result_url` present, the worker writes one JSON object with a
single PUT and answers with the key plus timings only:

```json
{
  "status": "succeed",
  "action": "detect",
  "result_key": "processing/123/f2d/12/1/jobs/detect/result.json",
  "bytes": 4823910,
  "sha256": "9f2c...",
  "upload_ms": 1840,
  "duration_ms": 47300,
  "model_durations_ms": {"small": 12000, "medium": 15000, "large": 20300},
  "page_count": 312,
  "worker_boot_ms": 42150,
  "worker_uptime_ms": 128,
  "gpu_available": true
}
```

Nothing touches the worker's disk on the way out: the envelope is
serialised in memory and streamed to S3.

The object itself is a self-describing envelope, so a consumer can
tell what it read without trusting where it read it from:

```json
{
  "schema_version": 1,
  "action": "detect",
  "scan_pk": 123,
  "job_id": "abc-123",
  "payload": {"detections": [...], "page_count": 312, "duration_ms": 47300}
}
```

The daemon checks every one of those fields before using the payload,
so an object from another run, another action, or a future schema
fails loudly instead of being consumed as this job's result.

Why bother: an inline response is capped at ~20 MB (the reason we
can't return per-word bounding boxes) and evaporates with RunPod's
retention window. An S3 object has neither limit. There is
deliberately **no inline fallback** when `result_url` is present:
carrying the payload in the response "just in case" would reinstate
the cap and create two sources of truth.

Without `result_url` the worker returns the payload inline, exactly
as the sections below describe. That is what happens in dev / CI
without AWS credentials, and with daemons predating this contract.
It's also the rollback: redeploying a worker image from before this
contract reverts result delivery to inline with no daemon change,
because the daemon accepts an inline response even when it sent a
`result_url`.

### `detect`

```json
{
  "input": {
    "action": "detect",
    "scan_pk": 123,
    "pdf_url": "https://s3.../bitonal.pdf?X-Amz-...",
    "models": ["small", "medium", "large"],
    "confidence": 0.20
  }
}
```

Returns:
```json
{
  "detections": [{"page_index": 0, "label": "CASE_CAPTION", ...}, ...],
  "page_count": 312,
  "duration_ms": 47300,
  "worker_boot_ms": 42150,
  "worker_uptime_ms": 128,
  "gpu_available": true
}
```

### `analyze`

```json
{
  "input": {
    "action": "analyze",
    "scan_pk": 123,
    "pdf_url": "https://s3.../.original.pdf?X-Amz-...",
    "exp_start": 1,
    "exp_end": 312,
    "max_pages": 5000
  }
}
```

Returns:
```json
{
  "results": [{"pdf_page": 1, "detected": "1", ...}, ...],
  "page_count": 312,
  "duration_ms": 128400,
  "worker_boot_ms": 42150,
  "worker_uptime_ms": 60030,
  "gpu_available": true
}
```

### Structured errors

Both actions return a structured `{"error": "...", "error_code": "..."}`
payload (alongside the same worker-meta fields) rather than crashing
on the following conditions:

| `error_code`    | Meaning                                     | Daemon behaviour |
|---|---|---|
| `NO_GPU`        | Worker scheduled without a GPU              | Re-queue the scan (transient) |
| `BAD_INPUT`     | `action` missing/wrong type                 | Mark scan ERROR    |
| `UNKNOWN_ACTION`| `action` not in `{"detect", "analyze"}`     | Mark scan ERROR    |
| `RESULT_UPLOAD_FAILED` | The PUT to S3 failed after retries (5xx, 429, dropped connections) | Re-queue the scan (transient) |
| `RESULT_URL_EXPIRED`   | The presigned PUT is dead (HTTP 403)  | Re-queue the scan; resubmitting mints a fresh signature, and the dead URL is never reused |
| `RESULT_UPLOAD_REJECTED` | S3 refused the write for a reason a re-run can't fix: a signature scoped to the wrong region, a bucket policy demanding a header we don't send, a malformed request | Mark scan ERROR |

The result codes only ever fire *after* the GPU work succeeded, so
they cost a run. `_put_result` retries hard (5 attempts, exponential
backoff with jitter) on the transient shapes, and skips the retries
for 403 and other 4xx because neither improves by being asked twice.

`RESULT_UPLOAD_REJECTED` is terminal on purpose. It means the
deployment is misconfigured, so re-queueing would re-run the GPU work
— and re-bill it — up to `RUNPOD_MAX_TRANSIENT_RETRIES` times before
failing with the same message. S3's XML explanation is included in the
error, which is usually enough to fix it outright.

A job that fails any of these ways never writes its object, so nothing
stale is left behind for the retry to trip over.

Real exceptions inside the action (network failure during download,
ValueError from the handler, CUDA OOM) propagate up and turn into
RunPod `FAILED` status with the traceback in the `error` field of
the status response.

The daemon side recomputes `seq_issues` / `duplicates` /
`missing_pages` locally from `results`, so the extras aren't sent
over the wire (keeps responses well under RunPod's ~20 MB cap).

## Security notes

### What the image contains

- Open-source code: blackletter, paddleocr, ultralytics, torch, etc.
- Publicly-distributed model weights: blackletter YOLO (HF
  `freelawproject/blackletter-weights`), PaddleOCR PP-OCRv5.
- No secrets. No AWS keys. No DB credentials. No Django `SECRET_KEY`.
  No Sentry DSN (read from env at runtime).

Pushing this image to a **public** Docker Hub / GHCR repo leaks no
proprietary or sensitive material. Private is fine too; just supply
registry credentials in the RunPod endpoint config.

### What MUST stay out of the image

Never bake into the Dockerfile or `pyproject.toml`:
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
- `RUNPOD_API_KEY`
- `SENTRY_DSN_GPU`
- Any Django settings file or `.env`

All of these are set as RunPod endpoint env vars at runtime instead.

### Input-URL handling

The handler downloads the PDF from whatever URL the job payload
specifies. Today the trust boundary is "only the scanning daemon
holds `RUNPOD_API_KEY`, so only the daemon can submit jobs." If that
ever changes, tighten the handler by validating the URL scheme
(`https` only), optionally pinning a host allowlist, and rejecting
suspicious redirects.

### The presigned PUT is a write capability

`result_url` hands a third-party GPU host the ability to write to our
bucket. It stays deliberately narrow: one key, one method, and a
bounded TTL (`RUNPOD_PRESIGNED_TTL`). The key is under the scan's own
`jobs/` subtree, so even a leaked or misused URL can only create one
object the daemon already expected, at a path nothing else reads.

The alternative -- scoped temporary credentials via STS -- would be
more flexible but would put an AWS credential in the worker's
environment, giving up the property this image deliberately has: no
AWS credentials anywhere in it. Both the GET and the PUT are
capabilities handed in per job and expiring on their own.

### Container root user

The image runs as root (standard for `nvidia/cuda:*-runtime-*`).
Acceptable for serverless workers (no shell access, no persistent
state between jobs), but follow the usual hardening if you ever run
it in a multi-tenant long-lived environment.

## Monitoring worker states

The endpoint's **Workers** tab in the RunPod console shows each
worker's current state. Understanding these is important for
diagnosing queue behaviour and for tracking down unexpected billing.

| State         | Meaning                                                                 | Billed? |
|---|---|---|
| `Initializing`  | Worker is provisioning: pulling image, starting container, running `_preload()`. | Yes (from the moment GPU is attached)  |
| `Running`       | Actively executing a job (`handler()` is running).                                | Yes |
| `Idle`          | Finished a job, holding the GPU warm, waiting for the next submission within Idle Timeout. | Yes |
| `Throttled`     | Logically allocated but waiting for physical GPU capacity. No GPU attached yet.   | No  |
| `Unhealthy`     | Failed a fitness check or crashed during a job. Scheduled for termination.        | Yes (while alive) |
| `Ready`         | Warm, provisioned, awaiting dispatch (rare transient state).                      | Yes |

A few operational notes:

- **`Throttled` is the only non-billed live state.** If RunPod can't
  find a physical GPU of your selected type, workers sit in
  `Throttled` forever at zero cost until capacity frees up. The
  scheduler prefers throttled-then-promote over outright rejecting
  the job.
- **`Idle Timeout` determines how long `Idle` lasts.** Setting this
  to 5 seconds means a worker is killed almost immediately after
  completing a job, so every subsequent job pays a cold start. 5
  minutes is a reasonable default for sporadic scan workloads.
- **`Unhealthy` workers accumulate if jobs crash.** Check logs for
  the specific worker ID to see the failure. Terminate them manually
  from the Workers tab if they linger, purely for dashboard
  cleanliness — RunPod auto-terminates them within a few minutes
  regardless.
- **Watching the tab during a scan is the fastest way to confirm
  warm reuse.** If the same `workerId` serves both the `detect` and
  the `analyze` call from a single `run_full_pipeline`, you'll see
  that worker cycle `Idle` → `Running` → `Idle` without a new worker
  starting. If a fresh worker appears for analyze, your Idle Timeout
  is too short.

## Troubleshooting

**`ImportError: libcuda.so.1: cannot open shared object file` at
build time** — the CUDA stub COPY layer was skipped or the base
image versions don't match. Make sure the `FROM` and the
`COPY --from=` multi-stage reference the same CUDA version.

**`AttributeError: 'AnalysisConfig' object has no attribute
'set_optimization_level'`** — paddlepaddle-gpu is too old. Needs
3.1.0+. Confirm `uv.lock` pins 3.1.0 against the
`https://www.paddlepaddle.org.cn/packages/stable/cu126/` index.

**`Model ... .pt not found, skipping`** — the HF download at
build time failed silently on all three retries. Rebuild. Consider
mirroring the weights to a private S3 and swapping the HF URL in
`ensure_weights` if HF reliability becomes an issue.

**Runs on CPU instead of GPU at runtime** — RunPod endpoint isn't
configured with a GPU, or `libcuda.so.1` isn't being bind-mounted.
Check that `docker run --gpus all ...` works locally; on RunPod,
verify the endpoint has a GPU worker assigned (not "CPU only").

**Slow first job on every worker** — expected for cold start
(container pull + CUDA init). If cold starts dominate your
workload, raise Min Workers to 1 (costs money even while idle) or
increase Idle Timeout so warm workers serve more jobs.

**`Failed to get job. | missing field(s): id or input` in worker
logs, followed by SIGTERM** — a known wrinkle with `/runsync`
submissions on some `runpod` SDK versions; the worker successfully
boots but the SDK's job-dispatcher gets confused by the sync-job
wrapper. Use `/run` (async + poll) instead, which is what
`runpod_client.py` uses. Only a problem for manual curl smoke tests.

**`HTTP 404 from /status` on a job that recently completed** —
you missed the retention window (30 min async, 1 min sync). The
result is lost. The daemon handles this case by raising a typed
`RunpodError`; in practice it means either the daemon was paused
long enough to lose a result, or the job's 24 h TTL expired while
running. If you see it under normal load, something is wrong —
check logs for a daemon crash around the submission time.

**Endpoint stuck in "initializing" for many minutes** — container
disk size is too small. The image extracts to ~22 GB; set the
endpoint's Container Disk to ≥ 30 GB.

**`retries: 1` in the status response on the first ever call to a
new endpoint** — usually image-pull timeout on the first fetch to a
cold node. RunPod re-dispatches and the second attempt uses the
partially-cached layers. Normal on the very first request to a new
endpoint; should not repeat after the image is cached.

## Updating to a newer CUDA / torch / paddle

When newer CUDA wheels become available on both indexes:

1. Bump the `FROM` in `Dockerfile` (both the base and the stub
   multi-stage line).
2. Update `[[tool.uv.index]]` URLs in `pyproject.toml` to match
   (e.g. `cu126` → `cu128`).
3. Run `uv lock` in this directory and commit the refreshed
   `uv.lock`.
4. Rebuild, test end-to-end with a real RunPod job before tagging
   as the new production image.
