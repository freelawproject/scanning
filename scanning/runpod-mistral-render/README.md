# mistral-render-worker

CPU worker image for [RunPod Serverless] that does one thing: render
the pages of one original shard as the ensemble's canonical PNG
(1700x2200 RGB, 200 dpi on US Letter) and upload each page to Mistral's
files API. It answers the file ids in page order. The daemon builds the
batch manifest from them and creates the Mistral batch itself.

The image contains the render code and nothing else. No GPU, no Django,
no database client, no AWS credentials. The Mistral key lives in the
endpoint template's env, never in the image and never in a job input.

This worker is a sibling of `../runpod/`, `../runpod-dotsmocr/` and
`../runpod-surya/` and keeps their conventions: presigned-URL input,
structured error codes, worker meta on every response, the scaffold of
`runpod_common.py`. General RunPod operations knowledge is in
[`../runpod/README.md`](../runpod/README.md) and applies here, except
that this endpoint is a **CPU** endpoint.

[RunPod Serverless]: https://docs.runpod.io/serverless/overview

## Why

The Mistral read is cheap and fast; what costs minutes is our own
render and upload of the pages, which on the heavy pod happens one
shard after another (freelawproject/scanning#562 measured it). Here
every shard of a wave is its own job, so the shards of a volume render
at the same time on as many workers as the endpoint allows, and a
volume's render takes about as long as one shard's.

## How it fits

```
┌──────────────────────────┐          ┌──────────────────────────────┐
│ daemon (mistral_ocr.py)  │          │ RunPod CPU worker            │
│                          │  POST    │                              │
│  - claims the shard row  ├─────────▶│ handler.py                   │
│  - presigns the shard    │  /run    │  - downloads the shard       │
│  - submits one job       │          │  - renders each page         │
│  - polls /status         │◀─────────│  - paints the perpendiculars │
│  - writes the manifest   │ file ids │  - uploads each page ────────┼──▶ Mistral
│  - creates the batch ────┼──────────┼──────────────────────────────┼──▶ files API
└──────────────────────────┘          └──────────────────────────────┘
```

The daemon's wave (`mistral_ocr.submit_wave`) takes this path when
`RUNPOD_MISTRAL_RENDER_ENDPOINT_ID` is set beside `RUNPOD_ENABLED` and
`RUNPOD_API_KEY`; otherwise it renders on the pod as before. The wave
holds its submit lock from the claim to the batch id either way, so the
lost-claim rule of #534 is unchanged, and a job that fails or is
cancelled leaves files the daemon finds by their name prefix and
deletes.

## The job

Input:

| field | required | meaning |
| --- | --- | --- |
| `action` | yes | `"render"` |
| `pdf_url` | yes | presigned GET of the shard |
| `file_prefix` | yes | the daemon's name prefix; page `n` is uploaded as `{prefix}p{n}.png` |
| `page_count` | no | refused with `BAD_INPUT` when the PDF has another count |
| `whiteout_perpendiculars` | no | paint the marks out before the encode (default false) |
| `png_level` | no | PNG compression level 0 to 9 (default `HANDLER_PNG_LEVEL`, 1) |
| `scan_pk` | no | Sentry tag |

Output on success: `file_ids` (page order), `page_count`, `bytes`
(the PNGs), `shard_bytes` and `download_ms` (the shard), `render_ms`
and `upload_ms` (summed over the processes), `duration_ms` (wall),
`processes`, `png_level`, `whiteout_perpendiculars`, plus the worker
meta.

Output on failure: `error`, `error_code`, `page_count`, `uploaded`,
`deleted`, `files_left`. Every page this job uploaded is deleted
first, best effort, because the pages are unredacted and nothing will
name them.

Error codes and what the daemon does with them:

| code | meaning | row |
| --- | --- | --- |
| `RATE_LIMITED` | Mistral answered 429 past the retries | deferred, no attempt spent |
| `MISTRAL_UNREACHABLE`, `MISTRAL_HTTP_5xx`, `NOT_CONFIGURED`, `INPUT_DOWNLOAD_CORRUPT` | transient | one attempt spent |
| `BAD_INPUT`, `MISTRAL_HTTP_4xx`, `RENDER_FAILED` | terminal | failed |

## Inside one job

PyMuPDF holds the GIL, so the pages are rendered by a pool of
processes, each of which uploads the page it rendered: the PNG never
crosses a process boundary. The pool is `HANDLER_PROCESSES_PER_CPU`
(default 2) times the vCPUs, so a process waiting on its upload leaves
its core to another; `HANDLER_RENDER_PROCESSES` pins the size, and 1
renders in process. Pages go to the pool in runs of
`HANDLER_CHUNK_PAGES` (default 4).

A throttled upload (429), a 5xx and a transport failure are tried
again `HANDLER_UPLOAD_RETRIES` (5) times with a doubling backoff from
`HANDLER_UPLOAD_BACKOFF_SECONDS` (2); any other 4xx fails the page at
once. A failed page cancels the chunks not started, waits for the ones
running, deletes everything that went up and answers the code.

## Endpoint settings

- Compute: a CPU worker type, 4 vCPUs or more. The render is about a
  page every 0.3 s per core; a 100-page shard on 4 vCPUs is about a
  minute plus the download.
- Max workers: at least a volume's shard count (14 to 16) for one
  volume at full speed; more only helps when volumes arrive together.
  The daemon's own cap on this wave is `MISTRAL_SUBMITS_PER_TICK` and
  `MISTRAL_MAX_CONCURRENCY`.
- Env on the template: `MISTRAL_API_KEY` (required), `SENTRY_DSN_GPU`
  and `SENTRY_ENV` (optional), the `HANDLER_*` tunables above
  (optional).
- Idle timeout: short. A warm worker saves only the image pull.

## Building locally

Build with `scanning/` as the context. The handler imports the shared
`runpod_common.py` and `perpendiculars.py` that live there:

```bash
docker build -t mistral-render-worker:local \
    -f scanning/runpod-mistral-render/Dockerfile scanning/
```

## Manual testing with curl

Presign a shard (from a Django shell on a pod that reaches S3):

```python
from scanning import s3_sync
print(s3_sync.presign_get("processing/<scan>/shards/s0.pdf", 3600))
```

Submit a job and poll it:

```bash
curl -sS -X POST "https://api.runpod.ai/v2/$ENDPOINT/run" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input": {"action": "render", "pdf_url": "<presigned>", "file_prefix": "smoke-", "whiteout_perpendiculars": true}}'

curl -sS "https://api.runpod.ai/v2/$ENDPOINT/status/<job id>" \
  -H "Authorization: Bearer $RUNPOD_API_KEY"
```

The files the smoke test uploads stay at Mistral until deleted; list
them with `mistral_client.find_files("smoke-")` and delete them with
`mistral_client.delete_file`.

## Deploying

`.github/workflows/build-runpod-mistral-render-worker.yml` builds the
image on a push to `main` that touches this directory,
`runpod_common.py` or `perpendiculars.py`, pushes it to Docker Hub as
`freelawproject/mistral-render-worker` and, once the
`RUNPOD_MISTRAL_RENDER_TEMPLATE_ID` secret exists, PATCHes the
endpoint's template to the new image.
