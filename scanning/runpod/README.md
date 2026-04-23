# blackletter-gpu-pod

GPU pod image for the scanning pipeline. Runs the two GPU-heavy steps
of blackletter (`detect` and `analyze_pdf`) behind a FastAPI server so
the rest of the scanning app can stay on CPU-only boxes. Deployed as a
dedicated RunPod pod that the scanning daemon starts when work is
queued and stops when the queue drains.

The image contains only inference code + model weights. No Django, no
database client, no AWS credentials. All sensitive configuration lives
in pod env vars, never in the image.

This branch (`feat/runpod-pod`) replaces the earlier Serverless path
on `feat/runpod-serverless`. The measurement results in the `Measured
numbers` section below decide whether this becomes the default.

## How it fits

```
┌─────────────────────┐   POST /v1/pods/{id}/start   ┌──────────────┐
│ scanning daemon     │ ───────────────────────────> │ RunPod REST  │
│                     │ <──── publicIp + port ────── │              │
│ pod_manager.py      │   GET /v1/pods/{id}          └──────────────┘
│ runpod_client.py    │                                       │
│  - upload PDF to S3 │                                       │ spawns
│  - presign GET URL  │                                       ▼
│  - ensure pod ready │                              ┌──────────────┐
│  - POST /detect or  │  ───────── HTTP ───────────> │ GPU pod      │
│    /analyze         │       Bearer POD_API_KEY     │ FastAPI :8000│
│  - parse metrics    │                              │ actions.py   │
└─────────────────────┘                              └──────────────┘

        stop_idle_gpu_pod (every 30 s):
          if queued == 0 and processing == 0
             and last_activity > RUNPOD_POD_IDLE_GRACE_SECONDS
             and pod is RUNNING:
             POST /v1/pods/{id}/stop
```

## What's in the image

- Ubuntu 24.04 LTS base with CUDA 12.6 + cuDNN runtime libraries.
- Python 3.12 (system python, no venv managers at runtime).
- PyTorch 2.6 cu126 + paddlepaddle-gpu 3.1.0 cu126 (aligned on one
  CUDA minor version).
- FastAPI + uvicorn for the HTTP layer.
- `blackletter[analyze]` pinned to a git branch (see the TODO in
  `pyproject.toml`); flipped to a PyPI pin once released.
- Pre-baked weights so cold start skips the network:
  - YOLO `small.pt`, `medium.pt` (ship with blackletter), `large.pt`
    (downloaded at build from `flooie/blackletter-large`).
  - PaddleOCR PP-OCRv5 server det + rec weights in `/opt/paddlex`.
- A `libcuda.so.1` stub from the CUDA `-devel-` variant so
  `import paddle` succeeds during `docker build`; the host's real
  `libcuda.so.1` is bind-mounted over it at RunPod runtime via
  `--gpus all`.

## Building locally

Run from the scanning repo root so the build context is only this
directory:

```bash
docker build -t blackletter-gpu-pod:local scanning/runpod/
```

Expect 15-45 min on a first build, mostly bandwidth-bound (CUDA base,
CUDA devel stub layer, torch cu126, paddlepaddle-gpu cu126,
`large.pt`, PaddleOCR weights). Subsequent rebuilds are dramatically
faster thanks to layer caching.

You do **not** need a GPU on the build host. The Dockerfile's warmup
steps load models in CPU fallback mode just long enough to verify
files are valid.

### Running the image locally

Requires a GPU plus the [nvidia-container-toolkit]:

```bash
docker run --rm --gpus all -p 8000:8000 \
  -e POD_API_KEY=test-token \
  -e HANDLER_MAX_PAGES=5000 \
  blackletter-gpu-pod:local
```

Then in another terminal:

```bash
curl -H "Authorization: Bearer test-token" http://localhost:8000/health
# {"status":"ok","worker":{"boot_ms":...},"yolo_loaded":false,"paddle_loaded":false}

curl -X POST -H "Authorization: Bearer test-token" \
  http://localhost:8000/warmup
# {"worker":{...},"yolo_warmup_ms":{"small":4200,...},"paddle_warmup_ms":1800}
```

On a machine without a GPU the container starts and `/health` returns
200, but `/warmup`, `/detect`, and `/analyze` all respond with
`503 {"error_code":"NO_GPU"}`.

[nvidia-container-toolkit]: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html

## Pushing to a registry

RunPod pulls the image from a container registry at pod creation.

### Docker Hub

```bash
docker login -u <dockerhub-user>
docker tag blackletter-gpu-pod:local \
           <dockerhub-user>/blackletter-gpu-pod:<tag>
docker push <dockerhub-user>/blackletter-gpu-pod:<tag>
```

### GitHub Container Registry

```bash
docker login ghcr.io -u <github-user>   # use a PAT with packages:write
docker tag blackletter-gpu-pod:local \
           ghcr.io/freelawproject/blackletter-gpu-pod:<tag>
docker push ghcr.io/freelawproject/blackletter-gpu-pod:<tag>
```

## Creating the RunPod pod

In the RunPod console:

1. **Pods > Deploy** > pick a GPU tier (A4000 / A4500 / RTX 2000 Ada
   with 16 GB is the recommended starting point; A5000 24 GB if you
   expect larger volumes).
2. **Template > Custom > Container image**: the tag you pushed.
3. **Container disk**: >= 30 GB (image extracts to ~22 GB).
4. **Volume disk**: optional; not required since weights are baked in
   and PDFs are streamed from presigned URLs.
5. **Expose TCP Ports**: `8000` (internal). RunPod will allocate a
   public port mapping.
6. **Expose HTTP Ports**: `8000` (so the proxy URL
   `https://{podId}-8000.proxy.runpod.net` terminates TLS).
7. **Env vars** (set on the pod, NOT in the image):
   - `POD_API_KEY` - long random string (generate with
     `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
     Must match `RUNPOD_POD_API_KEY` on the daemon side.
   - `SENTRY_DSN_GPU` - Sentry project for pod-side errors.
   - `SENTRY_ENV` - `prod` / `staging`.
   - `GIT_SHA` - optional, used as the Sentry release tag.
   - `HANDLER_MAX_PAGES` - reject PDFs above this many pages
     (default 5000).
   - `HANDLER_DOWNLOAD_TIMEOUT` - seconds to wait for the presigned
     GET URL download (default 300).
8. **Stop after**: set an idle timeout of ~30 min as a safety net
   in case the daemon crashes without stopping the pod.
9. Save the pod. Note the **Pod ID**; you'll set it in the daemon env
   as `RUNPOD_POD_ID`.

Initially, **do NOT set "Start on deploy" to auto-start**. The daemon
will start the pod on demand.

## Manual testing with `curl`

Once the pod is deployed, test each endpoint before flipping the
daemon to remote mode.

```bash
POD_ID=<pod-id>
TOKEN=<pod-api-key>
RUNPOD_KEY=<runpod-rest-api-key>

# Start the pod
curl -X POST -H "Authorization: Bearer ${RUNPOD_KEY}" \
  https://rest.runpod.io/v1/pods/${POD_ID}/start

# Wait for it to come up; poll GET /pods/{id} until publicIp is set
watch -n 5 "curl -s -H 'Authorization: Bearer ${RUNPOD_KEY}' \
  https://rest.runpod.io/v1/pods/${POD_ID} | jq '.publicIp, .portMappings'"

BASE_URL="https://${POD_ID}-8000.proxy.runpod.net"

# Health
curl -H "Authorization: Bearer ${TOKEN}" ${BASE_URL}/health | jq

# Warmup
curl -X POST -H "Authorization: Bearer ${TOKEN}" \
  ${BASE_URL}/warmup | jq

# Detect on a small public PDF
curl -X POST -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"scan_pk":0,"pdf_url":"https://arxiv.org/pdf/1706.03762.pdf","models":["small"]}' \
  ${BASE_URL}/detect | jq '.metrics, .page_count'

# Stop the pod
curl -X POST -H "Authorization: Bearer ${RUNPOD_KEY}" \
  https://rest.runpod.io/v1/pods/${POD_ID}/stop
```

## Configuring the daemon

Env vars on the scanning daemon (or docker-compose service):

```bash
RUNPOD_ENABLED=True
RUNPOD_API_KEY=<runpod-rest-api-key>      # for pod lifecycle
RUNPOD_POD_ID=<pod-id>
RUNPOD_POD_API_KEY=<pod-api-key>          # bearer token; matches POD_API_KEY
# Optional tuning (defaults shown)
RUNPOD_POD_PORT=8000
RUNPOD_POD_BOOT_TIMEOUT=600               # max wait for pod /health 200
RUNPOD_POD_IDLE_GRACE_SECONDS=120         # idle before stop_idle_gpu_pod acts
RUNPOD_POD_STOP_POLL_SECONDS=30           # daemon tick interval for stop check
RUNPOD_REQUEST_TIMEOUT=1800               # per /detect or /analyze call
RUNPOD_MAX_RETRIES=2                      # retries on connection errors
RUNPOD_PRESIGNED_TTL=3600                 # presigned GET URL lifetime
```

When `RUNPOD_ENABLED` is unset or `False` (the default), the daemon
falls through to running blackletter in-process. This is how local
dev and the test suite work; no image needed.

Full per-setting documentation: `scanning/settings/project/runpod.py`.

## Handler contract

Every route requires `Authorization: Bearer $POD_API_KEY`.

### `GET /health`

No body. Returns:

```json
{
  "status": "ok",
  "worker": {"boot_ms": 12000, "uptime_ms": 5400, "gpu_available": true},
  "yolo_loaded": true,
  "paddle_loaded": false
}
```

### `POST /warmup`

No body. Eagerly loads YOLO + PaddleOCR; returns per-step durations.
Idempotent; subsequent calls return zeroes for already-warm models.

```json
{
  "worker": {"boot_ms": 12000, "uptime_ms": 11000, "gpu_available": true},
  "yolo_warmup_ms": {"small": 3200, "medium": 4100, "large": 5800},
  "paddle_warmup_ms": 2400
}
```

### `POST /detect`

```json
{
  "scan_pk": 123,
  "pdf_url": "https://s3.../bitonal.pdf?X-Amz-...",
  "models": ["small", "medium", "large"],
  "confidence": 0.20
}
```

Returns:

```json
{
  "detections": [{"page_index": 0, "label": "CASE_CAPTION", ...}, ...],
  "page_count": 312,
  "metrics": {
    "download_ms": 4100,
    "yolo_inference_ms": {"small": 62000, "medium": 76000, "large": 81000},
    "yolo_warmup_ms": {"small": 3200, "medium": 4100, "large": 5800},
    "postprocess_ms": 1200,
    "detect_ms": 220200,
    "total_ms": 224300
  },
  "worker": {"boot_ms": 12000, "uptime_ms": 230000, "gpu_available": true}
}
```

### `POST /analyze`

```json
{
  "scan_pk": 123,
  "pdf_url": "https://s3.../.original.pdf?X-Amz-...",
  "exp_start": 1,
  "exp_end": 312,
  "max_pages": 5000
}
```

Returns:

```json
{
  "results": [{"pdf_page": 1, "detected": "1", ...}, ...],
  "page_count": 312,
  "metrics": {
    "download_ms": 4200,
    "paddle_warmup_ms": 2400,
    "paddle_lazy_loaded_this_call": false,
    "analyze_ms": 137900,
    "total_ms": 142100
  },
  "worker": {"boot_ms": 12000, "uptime_ms": 350000, "gpu_available": true}
}
```

### Structured errors

All routes return a structured `{"error": "...", "error_code": "..."}`
payload on failure (alongside `worker`):

| `error_code`    | HTTP | Meaning                                      | Daemon behaviour |
|---|---|---|---|
| `NO_GPU`        | 503  | Pod scheduled without a GPU                  | Re-queue (transient) |
| `BAD_INPUT`     | 400  | Body missing / malformed                     | Mark scan ERROR      |
| `ACTION_FAILED` | 500  | Uncaught exception in action body            | Mark scan ERROR      |

## Security

### What the image contains

- Open-source code: blackletter, paddleocr, ultralytics, torch, etc.
- Publicly-distributed model weights.
- No secrets. No AWS keys. No DB credentials. No Sentry DSN (read
  from env at runtime).

Pushing to a **public** Docker Hub / GHCR repo leaks no proprietary
material.

### What MUST stay out of the image

Never bake into the Dockerfile or `pyproject.toml`:
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
- `POD_API_KEY`
- `RUNPOD_API_KEY`
- `SENTRY_DSN_GPU`
- Any Django settings file or `.env`

All of these are set as RunPod pod env vars at runtime instead.

### TLS and bearer tokens

The daemon talks to the pod through RunPod's proxy URL
(`https://{podId}-8000.proxy.runpod.net`), which terminates TLS on
RunPod's side. The bearer token travels over TLS end-to-end. Direct
IP fallback is plain HTTP; avoid relying on it for anything but
debugging.

### Input-URL handling

The handler downloads the PDF from whatever URL the request body
specifies. Today the trust boundary is "only the scanning daemon
holds `POD_API_KEY`, so only the daemon can submit jobs." If that
ever changes, tighten the handler by validating the URL scheme
(`https` only), pinning a host allowlist, and rejecting suspicious
redirects.

## Troubleshooting

**`ImportError: libcuda.so.1: cannot open shared object file` at
build time** - the CUDA stub COPY layer was skipped or the base
image versions don't match. Make sure the `FROM` and the
`COPY --from=` multi-stage reference the same CUDA version.

**`AttributeError: 'AnalysisConfig' object has no attribute
'set_optimization_level'`** - paddlepaddle-gpu is too old. Needs
3.1.0+. Confirm `uv.lock` pins 3.1.0 against the
`https://www.paddlepaddle.org.cn/packages/stable/cu126/` index.

**Pod starts but `/health` returns 401** - `POD_API_KEY` is missing
from the pod env or different from `RUNPOD_POD_API_KEY` on the
daemon. `/health` fails closed when `POD_API_KEY` is empty (503).

**`cudaErrorInitializationError` / SIGABRT mid-detect** - Paddle's
idle CUDA allocator clashing with torch during long runs. `actions.py`
only warms Paddle during `/warmup`; if you added a Paddle call
inside the `/detect` path, pull it out.

**Pod boot takes >5 min** - container disk too small (raise to
>= 30 GB). First-ever pod creation also pulls the image from
registry; subsequent starts reuse it.

**Daemon logs `RunpodTransientError: pod did not become ready
within 600s`** - raise `RUNPOD_POD_BOOT_TIMEOUT`, or check the
pod's `portMappings` in the RunPod console (port 8000 must be
exposed).

## Measured numbers

Populated during Test D on `feat/runpod-pod`. The 1359-page volume
below is the same test case used to measure Serverless in issue #42;
the two rows are directly comparable.

| Phase                       | Serverless (issue #42) | Pod (TBD) |
|---|---|---|
| Pod / worker boot           | ~3.3 s                 | TBD       |
| Warmup (YOLO + Paddle)      | baked into boot        | TBD       |
| Detect execution (3 models) | 298 s                  | TBD       |
| Analyze execution           | 138 s                  | TBD       |
| Idle between detect/analyze | 0-300 s billed         | ~0 s billed at pod rate |
| Per-scan cost               | $0.07-$0.17            | TBD       |

Once Test D runs, drop the measured numbers back into issue #42 as a
comment so the side-by-side comparison is visible to the team.

## Updating to a newer CUDA / torch / paddle

When newer CUDA wheels become available on both indexes:

1. Bump the `FROM` in `Dockerfile` (both the base and the stub
   multi-stage line).
2. Update `[[tool.uv.index]]` URLs in `pyproject.toml` to match
   (e.g. `cu126` -> `cu128`).
3. Run `uv lock` in this directory and commit the refreshed
   `uv.lock`.
4. Rebuild, test end-to-end with a real dispatch before tagging
   as the new production image.
