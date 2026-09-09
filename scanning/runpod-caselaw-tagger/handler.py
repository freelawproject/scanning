"""RunPod Serverless handler for the case-law block tagger.

Dispatches on ``job["input"]["action"]``. There is one action:

- ``tag``: take a list of sequences -- one court case each, serialized
  as the minimal HTML the model was trained on -- run the
  ``freelawproject/caselaw-block-tagger`` token classifier over each,
  and return the labelled character spans (party, docket number,
  court, judges, date filed, disposition, ...).

The model is a ModernBERT-large fine-tune with an 8,192-token context.
It never generates text: it emits one BIO label per token, and this
handler decodes the runs back into ``(start, end, label)`` spans over
the input string, so a caller can place them without re-tokenizing.

**The worker does not build its input.** Turning the dots.mocr cells
of a volume into case sequences -- splitting at the case boundaries,
dropping footnote content, serializing ``<p>``/``<blockquote>``/
``<em>``/``<sup>`` -- is the caller's job,
because it reads the volume's OCR document and its detection run, and
neither is here. The worker splits long cases into overlapping windows
and combines their predictions into spans over the original case.

Input has two shapes, chosen by the caller:

- **S3** (``input_url`` present): a presigned GET of a JSON document
  ``{"sequences": [{"id": ..., "text": ...}, ...]}``. A volume's worth
  of case text is megabytes, and RunPod caps a job input at ~10 MB.
- **Inline** (``sequences`` present): the same list in the job input.
  What a local test and ``curl`` use.

Result delivery mirrors the other workers (``runpod_common``): with
``result_url`` the payload is PUT to S3 and the response carries a
summary; without it the payload comes back inline.

Either way the worker needs no AWS credentials: a presigned URL is a
capability handed in with the job, scoped to one key and one method.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from bisect import bisect_left
from pathlib import Path
from typing import Any

import runpod
import runpod_common
from runpod_common import (
    RESULT_SCHEMA_VERSION,  # noqa: F401  (part of this module's contract)
    BadInputError,
    WorkerClock,
    coerce_input,
    download_pdf,
    upload_result,
)

# Construct the clock as early as possible so ``boot_ms`` covers the
# full cold-start cost (module imports + model preload).
_CLOCK = WorkerClock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("caselaw_tagger.runpod")

# Populated in ``_preload``. Surfaced in every handler response so the
# caller can tell whether inference actually hit a GPU.
_CUDA_AVAILABLE = False
#: The torch device inference runs on: ``"cuda"``, ``"cpu"`` when
#: ``HANDLER_ALLOW_CPU=1`` and no GPU is present, else ``None`` -- the
#: state the fitness check refuses and ``handler()`` answers ``NO_GPU``.
_DEVICE: str | None = None
#: Loaded in ``_preload`` (or lazily by the first job if that failed).
_MODEL: Any = None
_TOKENIZER: Any = None

sentry_sdk = runpod_common.init_sentry(logger)


# ── Tunables ────────────────────────────────────────────────────────
#: Hugging Face id of the checkpoint the image carries. Set by the
#: Dockerfile from its build arg; reported in every payload as
#: provenance, since a later checkpoint is a rebuild and a reader of
#: two result objects must be able to tell which model wrote each.
MODEL_NAME = os.environ.get(
    "TAGGER_MODEL", "freelawproject/caselaw-block-tagger"
)
#: Where the Dockerfile baked the snapshot. Nothing reaches Hugging
#: Face at run time (``HF_HUB_OFFLINE=1`` in the image).
MODEL_DIR = Path(os.environ.get("TAGGER_MODEL_DIR", "/opt/model"))
#: Hard sanity guard on input size, worker-side. A reporter volume
#: holds a few hundred cases, so an input over this is a caller bug.
MAX_SEQUENCES = int(os.environ.get("HANDLER_MAX_SEQUENCES", "5000"))
#: Sequences per forward pass. Padded to the longest in the batch, so
#: the sequences are sorted by length first. 4 x 8,192 tokens fits a
#: 24 GB card in bf16 with room to spare; raise it on a bigger card.
DEFAULT_BATCH_SIZE = int(os.environ.get("HANDLER_BATCH_SIZE", "4"))
#: Run on the CPU when no GPU is present, instead of refusing. Off by
#: default: a serverless worker without a GPU is a misprovisioned one,
#: and refusing it routes the job to a healthy worker. On for a local
#: ``docker run`` smoke test on a laptop, where a 0.4B encoder answers
#: a page in seconds.
ALLOW_CPU = os.environ.get("HANDLER_ALLOW_CPU", "").strip() == "1"
#: Fallback for the context limit while no model is loaded. The loaded
#: model's own ``max_position_embeddings`` is what the check reads.
DEFAULT_MAX_TOKENS = 8192
#: Tokens per window, special tokens included, and capped at the loaded
#: model's own context in ``_plan_windows``. The full trained context on
#: purpose: a case that fits is shown to the model whole, as every
#: training sequence was, and only a longer one is split. A smaller
#: window would split cases the model can read in one pass, and buy
#: nothing but memory the batch size already controls.
WINDOW_TOKENS = 8192
#: Tokens two neighbouring windows share, so a span at a window edge is
#: read with context on both sides by one of them. Approximate: the cut
#: falls at a paragraph boundary, and ``_plan_windows`` caps it at a
#: quarter of the window.
OVERLAP_TOKENS = 800
# Download tunables (HANDLER_DOWNLOAD_*) live in runpod_common.


# ── Cold-start preload ──────────────────────────────────────────────
def _load_model() -> None:
    """Load the tokenizer and the model onto ``_DEVICE``.

    bf16 on the GPU (ModernBERT was trained in it, and the logits'
    argmax is all the handler reads), fp32 on the CPU. Split from
    :func:`_preload` so the first job can retry a load that failed at
    boot and surface the real error, instead of the worker answering
    every job with the same swallowed exception.

    :raises Exception: Whatever transformers raises: a missing or
        truncated snapshot, a config the installed version cannot read.
    """
    global _MODEL, _TOKENIZER

    import torch
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    t0 = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
    dtype = torch.bfloat16 if _DEVICE == "cuda" else torch.float32
    model = AutoModelForTokenClassification.from_pretrained(
        MODEL_DIR, dtype=dtype
    )
    model.to(_DEVICE)
    model.eval()
    _TOKENIZER, _MODEL = tokenizer, model
    logger.info(
        "loaded %s from %s in %.1fs (device=%s, dtype=%s, labels=%d, "
        "max_tokens=%d)",
        MODEL_NAME,
        MODEL_DIR,
        time.monotonic() - t0,
        _DEVICE,
        dtype,
        len(model.config.id2label),
        int(
            getattr(
                model.config, "max_position_embeddings", DEFAULT_MAX_TOKENS
            )
        ),
    )


def _preload() -> None:
    """Pay the import and weight-read cost at module import.

    Picks the device, then loads the model once so the first job pays
    neither the transformers import nor the safetensors read.

    Every phase is wrapped, so a preload failure cannot stop the worker
    from starting; the first real job retries the load and surfaces the
    real error.
    """
    global _CUDA_AVAILABLE, _DEVICE

    try:
        import torch

        _CUDA_AVAILABLE = bool(torch.cuda.is_available())
        logger.info(
            "torch %s cuda=%s devices=%s",
            torch.__version__,
            _CUDA_AVAILABLE,
            torch.cuda.device_count() if _CUDA_AVAILABLE else 0,
        )
    except Exception:
        logger.warning("torch diagnostic failed")
        _CUDA_AVAILABLE = False

    if _CUDA_AVAILABLE:
        _DEVICE = "cuda"
    elif ALLOW_CPU:
        _DEVICE = "cpu"
        logger.warning(
            "no GPU; HANDLER_ALLOW_CPU=1 so inference runs on the CPU"
        )
    else:
        # The fitness check (_require_gpu) blocks this worker from the
        # available pool, so queued jobs route to healthy GPU workers.
        # If a job does leak through it returns error_code=NO_GPU, which
        # the caller classifies as transient and retries.
        #
        # Logged at warning, not error: this is expected and fully
        # self-healing, so it must not raise a Sentry event.
        _DEVICE = None
        logger.warning(
            "GPU not available; skipping the model preload. Jobs will "
            "return error_code=NO_GPU; the caller retries them on "
            "another worker."
        )
        return

    try:
        _load_model()
    except Exception:
        logger.exception("model preload failed (the first job retries it)")


_preload()

_CLOCK.mark_ready()
logger.info(
    "worker ready: boot_ms=%d cuda=%s device=%s",
    _CLOCK.boot_ms,
    _CUDA_AVAILABLE,
    _DEVICE,
)


# ── Fitness check ────────────────────────────────────────────────────
@runpod.serverless.register_fitness_check
def _require_gpu() -> None:
    """Exit before accepting jobs if there is nothing to run on.

    Runs at startup before RunPod's heartbeat, so the worker is never
    added to the available pool and no jobs are ever assigned to it.
    Queued jobs are picked up automatically by healthy GPU workers.
    """
    if _DEVICE is None:
        raise RuntimeError(
            "GPU not available on this worker. Exiting to avoid CPU-only billing."
        )


# ── Helpers ─────────────────────────────────────────────────────────
# Thin wrappers over the runpod_common scaffold. They bind this
# module's globals at call time, so a test that patches
# ``_CUDA_AVAILABLE`` or ``upload_result`` on this module still steers
# the shared code.
def _with_worker_meta(payload: dict) -> dict:
    """See :func:`runpod_common.with_worker_meta`."""
    return runpod_common.with_worker_meta(
        payload, clock=_CLOCK, gpu_available=_CUDA_AVAILABLE
    )


def _tag_sentry(job: dict, action: str, scan_pk: Any) -> None:
    """See :func:`runpod_common.tag_sentry`."""
    runpod_common.tag_sentry(
        sentry_sdk,
        job,
        action,
        scan_pk,
        clock=_CLOCK,
        gpu_available=_CUDA_AVAILABLE,
        handler_logger=logger,
    )


def _ensure_model() -> None:
    """Load the model if the boot-time preload did not.

    :raises RuntimeError: If there is no device to load onto. The
        dispatcher answers ``NO_GPU`` before this is reached, so this
        is a guard on a direct call.
    """
    if _MODEL is not None and _TOKENIZER is not None:
        return
    if _DEVICE is None:
        raise RuntimeError("no device: the model cannot be loaded")
    _load_model()


def _max_tokens() -> int:
    """Return the longest sequence the loaded model accepts.

    :returns: The model's ``max_position_embeddings``, or the module
        default while no model is loaded.
    :rtype: int
    """
    if _MODEL is None:
        return DEFAULT_MAX_TOKENS
    return int(
        getattr(_MODEL.config, "max_position_embeddings", DEFAULT_MAX_TOKENS)
    )


def _id2label() -> dict[int, str]:
    """Return the model's label table with integer keys.

    A config loaded from JSON carries the ids as strings; one built in
    memory carries them as ints. The decoder wants one shape.

    :returns: ``{label_id: "B-party", ...}``.
    :rtype: dict[int, str]
    """
    return {int(k): str(v) for k, v in _MODEL.config.id2label.items()}


# ── Input ───────────────────────────────────────────────────────────
def _read_sequences(inputs: dict, tmp_dir: Path) -> list[tuple[Any, str]]:
    """Return the ``(id, text)`` pairs this job tags, validated.

    :param inputs: Handler input payload. Exactly one of ``input_url``
        (a presigned GET of a JSON document) or ``sequences`` (the list
        inline).
    :param tmp_dir: Per-job scratch directory for the download.
    :returns: The sequences in input order.
    :rtype: list[tuple[Any, str]]
    :raises BadInputError: On any malformed input: both or neither
        source, a document that is not JSON, a list that is empty or
        over ``MAX_SEQUENCES``, an entry without a string ``text`` or a
        string-or-integer ``id``, or a repeated id.
    """
    input_url = inputs.get("input_url")
    sequences = inputs.get("sequences")
    if bool(input_url) == (sequences is not None):
        raise BadInputError(
            "provide exactly one of 'input_url' or 'sequences'"
        )

    if input_url:
        # The shared downloader is a resumable, size-checked GET; the
        # name says PDF because that is what the other workers fetch,
        # but nothing in it reads the bytes. The PDF validation is a
        # separate function and is not called here.
        path = tmp_dir / "input.json"
        download_pdf(input_url, path)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            # The downloader verified the byte count against the
            # server's, so this is the caller's document, not a
            # truncated copy of it.
            raise BadInputError(f"input document is not JSON: {exc}") from exc
        if isinstance(document, dict):
            sequences = document.get("sequences")
        else:
            sequences = document

    if not isinstance(sequences, list) or not sequences:
        raise BadInputError(
            "'sequences' must be a non-empty list of "
            "{'id': ..., 'text': ...} objects"
        )
    if len(sequences) > MAX_SEQUENCES:
        raise BadInputError(
            f"{len(sequences)} sequences, exceeds "
            f"MAX_SEQUENCES={MAX_SEQUENCES}"
        )

    pairs: list[tuple[Any, str]] = []
    seen: set = set()
    for position, entry in enumerate(sequences):
        if not isinstance(entry, dict):
            raise BadInputError(
                f"sequences[{position}] must be an object, "
                f"got {type(entry).__name__}"
            )
        sid = entry.get("id")
        # ``bool`` is an ``int`` subclass; ``True`` is not an id.
        if isinstance(sid, bool) or not isinstance(sid, str | int):
            raise BadInputError(
                f"sequences[{position}].id must be a string or an integer, "
                f"got {sid!r}"
            )
        if sid in seen:
            raise BadInputError(f"duplicate sequence id: {sid!r}")
        seen.add(sid)
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            raise BadInputError(
                f"sequences[{position}].text must be a non-empty string"
            )
        pairs.append((sid, text))
    return pairs


# ── Inference ───────────────────────────────────────────────────────
def _tokenize(text: str) -> tuple[list[int], list[tuple[int, int]]]:
    """Tokenize one sequence with its character offsets.

    No truncation: windows reuse these token ids and offsets, so even
    a split inside a long paragraph preserves the original positions.

    :param text: The sequence.
    :returns: ``(input_ids, offsets)``, one offset pair per token.
        Special tokens carry ``(0, 0)``.
    :rtype: tuple[list[int], list[tuple[int, int]]]
    """
    encoded = _TOKENIZER(
        text,
        add_special_tokens=True,
        truncation=False,
        return_offsets_mapping=True,
        return_attention_mask=False,
    )
    return (
        list(encoded["input_ids"]),
        [tuple(pair) for pair in encoded["offset_mapping"]],
    )


def _plan_windows(text, ids, offsets, max_tokens):
    """Plan paragraph windows and select one owner for each text unit.

    Normally a unit is a paragraph. An oversized paragraph is divided
    into token slices small enough to overlap. Every window gets the
    tokenizer's original leading/trailing special tokens; content is
    never decoded and re-tokenized. This preserves Unicode offsets.

    Returns (windows, owned_ranges). Each window is (input_ids, start,
    end, prefix_length), with start/end indexing the full tokenization.
    owned_ranges maps each window to the token ranges whose predictions
    it should contribute. A unit belongs to the window where it has the
    most context on its nearer edge, matching encoder-testing.
    """
    first, last = 0, len(ids)
    while first < last and offsets[first][0] == offsets[first][1]:
        first += 1
    while last > first and offsets[last - 1][0] == offsets[last - 1][1]:
        last -= 1
    prefix, suffix = ids[:first], ids[last:]
    budget = min(WINDOW_TOKENS, max_tokens) - len(prefix) - len(suffix)
    if budget < 1:
        raise RuntimeError("model context leaves no room for text tokens")
    overlap = min(OVERLAP_TOKENS, budget // 4)

    # Keep all tokens, including separators and trailing text, even if
    # the caller sent plain text or HTML without a final closing tag.
    starts = [start for start, _ in offsets[first:last]]
    boundaries = {first, last}
    for match in re.finditer(r"</(?:p|blockquote)\s*>\s*", text, re.I):
        boundaries.add(first + bisect_left(starts, match.end()))
    boundaries = sorted(boundaries)
    units = []
    for a, b in zip(boundaries, boundaries[1:]):
        width = b - a if b - a <= budget else max(1, overlap)
        units.extend((s, min(s + width, b)) for s in range(a, b, width))

    windows = []
    owners = [-1] * len(units)
    scores = [-1] * len(units)
    a = 0
    while a < len(units):
        b = a + 1
        start = units[a][0]
        while b < len(units) and units[b][1] - start <= budget:
            b += 1
        end = units[b - 1][1]
        wi = len(windows)
        windows.append((prefix + ids[start:end] + suffix, start, end, first))
        for i in range(a, b):
            score = min(units[i][0] - start, end - units[i][1])
            if score > scores[i]:
                scores[i], owners[i] = score, wi
        if b == len(units):
            break
        nxt = b
        while nxt - 1 > a and end - units[nxt - 1][0] <= overlap:
            nxt -= 1
        a = nxt

    owned_ranges = [[] for _ in windows]
    for unit, owner in zip(units, owners, strict=True):
        owned_ranges[owner].append(unit)
    return windows, owned_ranges


def _predict_batch(batch: list[list[int]]) -> list[list[int]]:
    """Run one forward pass and return the argmax label per token.

    :param batch: Token id lists, unpadded.
    :returns: One label-id list per input, each as long as its input.
    :rtype: list[list[int]]
    """
    import torch

    pad_id = _TOKENIZER.pad_token_id
    if pad_id is None:
        pad_id = 0
    width = max(len(ids) for ids in batch)
    input_ids = torch.full((len(batch), width), pad_id, dtype=torch.long)
    attention = torch.zeros((len(batch), width), dtype=torch.long)
    for row, ids in enumerate(batch):
        input_ids[row, : len(ids)] = torch.tensor(ids, dtype=torch.long)
        attention[row, : len(ids)] = 1

    with torch.inference_mode():
        logits = _MODEL(
            input_ids=input_ids.to(_DEVICE),
            attention_mask=attention.to(_DEVICE),
        ).logits
        labels = logits.argmax(-1).to("cpu")

    return [labels[row, : len(ids)].tolist() for row, ids in enumerate(batch)]


def decode_spans(
    text: str,
    offsets: list[tuple[int, int]],
    label_ids: list[int],
    id2label: dict[int, str],
) -> list[dict]:
    """Turn per-token BIO labels into character spans over ``text``.

    The model card's own decoding rule, made lenient in the one way a
    reader needs: an ``I-x`` that follows nothing, or follows a
    different class, opens a span (the model dropped the ``B``), rather
    than being discarded. A span's edges are trimmed to the text they
    cover -- a token's offset often starts at the space before it --
    and an empty span is dropped.

    Pure: no model, no torch, so it is tested directly.

    :param text: The input string the offsets index.
    :param offsets: One ``(start, end)`` per token. Special tokens
        carry an empty range and are skipped.
    :param label_ids: One label id per token, same length.
    :param id2label: The model's label table, integer-keyed.
    :returns: ``[{"start", "end", "label", "text"}, ...]`` in text
        order. ``end`` is exclusive; ``label`` is the class without its
        BIO prefix.
    :rtype: list[dict]
    """
    spans: list[dict] = []
    current: dict | None = None
    # Markup predictions were not supervised during training. Keep
    # markup as context for inference, but never let its labels open
    # or split an entity. Mixed text/markup tokens snap to text edges.
    is_text = bytearray(not ch.isspace() for ch in text)
    for match in re.finditer(r"<[^>]*>", text):
        is_text[match.start() : match.end()] = bytes(len(match.group()))

    def close() -> None:
        nonlocal current
        if current is None:
            return
        start, end = current["start"], current["end"]
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if end > start:
            spans.append(
                {
                    "start": start,
                    "end": end,
                    "label": current["label"],
                    "text": text[start:end],
                }
            )
        current = None

    for (start, end), label_id in zip(offsets, label_ids, strict=True):
        while start < end and not is_text[start]:
            start += 1
        while end > start and not is_text[end - 1]:
            end -= 1
        if end <= start:
            # A special token ([CLS], [SEP], padding) covers no text
            # and must neither open nor split a span.
            continue
        if current is not None and start < current["end"]:
            # A later byte of one multi-byte character: byte-level BPE
            # splits a symbol such as the West key (☞) into several
            # tokens that all carry the character's offsets. One
            # character cannot hold two labels or end a span in its
            # middle, so whatever the label says, the token extends
            # the span it shares a character with.
            current["end"] = max(current["end"], end)
            continue
        label = id2label.get(int(label_id), "O")
        if label == "O":
            close()
            continue
        prefix, _, name = label.partition("-")
        if prefix == "I" and current is not None and current["label"] == name:
            current["end"] = end
            continue
        close()
        current = {"start": start, "end": end, "label": name}
    close()
    return spans


def _action_tag(job: dict, inputs: dict, tmp_dir: Path) -> dict:
    """Tag every sequence and return their spans.

    :param job: RunPod job dict (used for the progress update).
    :param inputs: Handler input payload. Required: ``input_url`` or
        ``sequences`` (see :func:`_read_sequences`). Result delivery
        (handled by :func:`_deliver`, not here): ``result_url`` and
        ``result_key``. Optional: ``batch_size`` (default
        ``HANDLER_BATCH_SIZE``).
    :param tmp_dir: Per-job scratch directory.
    :returns: ``{"sequences": [...], "sequence_count": int,
        "failed_sequences": [ids], "model": str, "max_tokens": int,
        "duration_ms": int}``. Each sequence entry carries its ``id``,
        its ``token_count``, ``window_count`` and merged ``spans`` (see
        :func:`decode_spans`). Entries keep the input order. Long cases
        are windowed internally; ``failed_sequences`` stays empty.
    :rtype: dict
    :raises BadInputError: On any bad input.
    """
    _ensure_model()
    sequences = _read_sequences(inputs, tmp_dir)
    batch_size = coerce_input(
        "batch_size", inputs.get("batch_size", DEFAULT_BATCH_SIZE), int
    )
    if batch_size < 1:
        raise BadInputError(f"'batch_size' must be >= 1, got {batch_size!r}")
    max_tokens = _max_tokens()
    id2label = _id2label()

    try:
        runpod.serverless.progress_update(
            job, f"tagging {len(sequences)} sequences"
        )
    except Exception:
        # Progress is best-effort; never fail a job over it.
        pass

    t0 = time.monotonic()
    results: dict[int, dict] = {}
    encoded = []
    case_tokens = []
    for index, (sid, text) in enumerate(sequences):
        ids, offsets = _tokenize(text)
        windows, owned = _plan_windows(text, ids, offsets, max_tokens)
        case_tokens.append((offsets, [None] * len(ids)))
        results[index] = {
            "id": sid,
            "token_count": len(ids),
            "window_count": len(windows),
        }
        for window, ranges in zip(windows, owned, strict=True):
            window_ids, start, _, prefix_length = window
            encoded.append((index, window_ids, start, prefix_length, ranges))

    # Sorted by length so each batch pads to a near neighbour, not to
    # the longest case of the volume.
    encoded.sort(key=lambda item: len(item[1]))
    for offset in range(0, len(encoded), batch_size):
        chunk = encoded[offset : offset + batch_size]
        predictions = _predict_batch([item[1] for item in chunk])
        for (index, ids, start, prefix_length, ranges), labels in zip(
            chunk, predictions, strict=True
        ):
            if len(labels) != len(ids):
                raise RuntimeError(
                    "model returned an incomplete token sequence"
                )
            _, selected = case_tokens[index]
            for a, b in ranges:
                lo = prefix_length + a - start
                selected[a:b] = labels[lo : lo + b - a]

    for index, (_, text) in enumerate(sequences):
        offsets, selected = case_tokens[index]
        # Only special tokens may be unowned; a missing text token is
        # an implementation failure, never a silently omitted passage.
        text_tokens = [
            (pair, label)
            for pair, label in zip(offsets, selected, strict=True)
            if pair[1] > pair[0]
        ]
        if any(label is None for _, label in text_tokens):
            raise RuntimeError(
                "window merge left text tokens without predictions"
            )
        results[index]["spans"] = decode_spans(
            text,
            [pair for pair, _ in text_tokens],
            [label for _, label in text_tokens],
            id2label,
        )

    duration_ms = int((time.monotonic() - t0) * 1000)
    ordered = [results[index] for index in range(len(sequences))]
    span_count = sum(len(entry.get("spans") or []) for entry in ordered)
    logger.info(
        "tag OK: %d sequences, %d spans, %d windows, in %d ms "
        "(batch_size=%d, device=%s)",
        len(sequences),
        span_count,
        len(encoded),
        duration_ms,
        batch_size,
        _DEVICE,
    )
    return {
        "sequences": ordered,
        "sequence_count": len(sequences),
        "failed_sequences": [],
        "model": MODEL_NAME,
        "max_tokens": max_tokens,
        "duration_ms": duration_ms,
    }


# ── Result delivery ─────────────────────────────────────────────────
# Summary fields kept in the job response when the payload goes to S3.
# Everything else -- above all ``sequences`` -- is deliberately dropped:
# the response is capped at about 20 MB and discarded with the job
# record, which is the whole reason the payload travels through S3.
_SUMMARY_FIELDS = (
    "sequence_count",
    "failed_sequences",
    "model",
    "max_tokens",
    "duration_ms",
)


def _span_count(result: dict) -> dict:
    """Return the extra summary field this worker keeps.

    Kept because it is small and it is what a caller checks first.

    :param result: The action's own return value.
    :returns: ``{"span_count": int}``.
    :rtype: dict
    """
    return {
        "span_count": sum(
            len(entry.get("spans") or [])
            for entry in result.get("sequences") or []
        )
    }


def _deliver(result: dict, inputs: dict, scan_pk: Any) -> dict:
    """See :func:`runpod_common.deliver_result`."""
    return runpod_common.deliver_result(
        result,
        inputs,
        scan_pk,
        summary_fields=_SUMMARY_FIELDS,
        upload=upload_result,
        extra_summary=_span_count,
    )


_ACTIONS = {
    "tag": _action_tag,
}


# ── Entry point ─────────────────────────────────────────────────────
def handler(job: dict) -> dict:
    """RunPod Serverless entry point.

    :param job: RunPod job dict. ``job["input"]`` must carry an
        ``action`` ("tag") and action-specific args. An optional
        ``scan_pk`` is used to tag Sentry events.
    :returns: Action-specific result dict. Every successful return
        (and every structured error) also carries ``worker_boot_ms``
        (cold-start cost of this worker process, constant per worker),
        ``worker_uptime_ms`` (ms since preload finished, at job start),
        and ``gpu_available`` (whether torch.cuda saw a device). On bad
        or unknown input an ``{"error": ..., "error_code": ...}`` dict
        is returned; the SDK moves ``error`` to the top level and
        RunPod marks the job ``FAILED``. The caller reads
        ``error_code`` from ``output`` to tell a transient failure
        (retry) from a terminal one.

        When the input carries ``result_url``, the payload is PUT to S3
        and the response holds only a summary (``result_key``,
        ``bytes``, ``span_count``, ``sequence_count``,
        ``failed_sequences``, ``model``, ``max_tokens``,
        ``duration_ms``). Without it the payload comes back inline.
    :rtype: dict
    """
    inputs = job.get("input") or {}
    action = inputs.get("action")
    scan_pk = inputs.get("scan_pk")

    if not isinstance(action, str):
        return _with_worker_meta(
            {
                "error": f"missing or invalid 'action' in input: {action!r}",
                "error_code": "BAD_INPUT",
            }
        )

    _tag_sentry(job, action, scan_pk)

    # Belt-and-suspenders: the fitness check should keep CPU-only
    # workers away from jobs, but handle it defensively in case CUDA
    # becomes unavailable after startup.
    if _DEVICE is None:
        # NO_GPU is transient from the caller's perspective: retry and
        # the next attempt lands on a different worker. Logged at
        # warning, not error: expected and self-healing, so no Sentry
        # event.
        logger.warning(
            "rejecting %s job for scan %s: no GPU on this worker; "
            "the caller should retry.",
            action,
            scan_pk,
        )
        # refresh_worker: the pinned runpod SDK pops this from the
        # return dict, delivers the result, then terminates the worker
        # (stopPod). A CPU-only worker never grows a GPU, so keeping it
        # warm would let it keep swallowing retried jobs.
        return _with_worker_meta(
            {
                "error": "GPU unavailable on this worker",
                "error_code": "NO_GPU",
                "refresh_worker": True,
            }
        )

    fn = _ACTIONS.get(action)
    if fn is None:
        return _with_worker_meta(
            {
                "error": f"unknown action: {action!r}. Expected one of {sorted(_ACTIONS)}",
                "error_code": "UNKNOWN_ACTION",
            }
        )

    # The except arms that map exceptions to error codes live in the
    # runner: they are the daemon's contract, shared by every worker.
    return runpod_common.execute_action(
        fn,
        job,
        inputs,
        action=action,
        scan_pk=scan_pk,
        deliver=_deliver,
        meta=_with_worker_meta,
        sentry=sentry_sdk,
        handler_logger=logger,
    )


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
