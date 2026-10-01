"""Score every page of a volume for the look of a bad scan.

The bad-page model (``badpage-v2.joblib``) is a gradient-boosted
classifier trained on the volunteers' own repair requests: for every
page a reviewer asked to have scanned again, and for the pages a
reviewer then judged fine, it learned the pixel statistics of
:mod:`scanning.badpage.features`. Its score is the probability that a
reviewer with the page in front of them would ask for a rescan. On the
held-out volumes of its training run it ranked a flagged page above a
clean one 99 times in 100 (``auc_oof``).

The score is a **suspicion for a person to judge**, like the model
reading of a page number. So it is stored on the scan
(``Scan.page_scores``) and read into review 1 as a card per page at or
above :data:`THRESHOLD`, which a curator dismisses or answers with a
rescan request. The cards are derived by :func:`issues` on every
rebuild of the review-1 issues, so a dismissal matches them the way it
matches every other card (check name plus page), a deletion answers
them (``CHECKS_A_DELETION_ANSWERS``), and an open rescan request
answers them too. The card exists to get a page looked at fast, and
it goes once that has happened.

The pages are the **original's**: the score is measured on the review-1
bitonal copy, which is page for page the original as uploaded, and the
stamp carries the scan's ``source_fingerprint`` so a re-upload reads as
unscored, never as scored wrong (:func:`scores_of`).

The pass is a daemon task (``score_bad_pages`` with no argument, the
sixth task of ``run_daemon``): a fact on the row and no hand-off, the
shape of the opinion PDF pass (#336). A scan owes a score when it is
past the bitonal merge and carries no stamp (:func:`owed_scans`), the
tick takes the newest one, pulls its bitonal copy, scores it with
``settings.BADPAGE_JOBS`` worker processes and stamps it. No scan status moves. A
fault spends one of :data:`MAX_ATTEMPTS` on the row's own stamp, loud
then quiet, and at the cap the row is left alone until a person runs
the command on it. The daemon container does the work, so the web
pods never carry it; one volume per tick and a small pool keep the
daemon's cores for the job waves.
"""

import functools
import logging
from pathlib import Path

import fitz
import joblib
import numpy as np
import pandas as pd
from django.conf import settings
from django.utils import timezone

from scanning.badpage import features

logger = logging.getLogger(__name__)

#: The bundle the command and the tests read.
MODEL_PATH = Path(__file__).with_name("badpage-v2.joblib")

#: A page at or above this score gets a card. The model's own review
#: tool used 0.25 for "about 1 to 2 percent of clean pages"; review 1
#: asks a little wider, because a page it misses is a page nobody looks
#: at again.
THRESHOLD = 0.2

#: The columns of a feature frame that are not measures.
NON_FEATURES = {"scan", "page", "label", "weight"}

#: Faults a row may spend before the pass leaves it alone.
MAX_ATTEMPTS = 3

#: Volumes scored per tick. The tick blocks the serial scheduler for
#: the whole score, a minute or two for a volume, so one is the rule.
SCANS_PER_TICK = 1


@functools.lru_cache(maxsize=1)
def load_model() -> dict:
    """Return the model bundle, read once per process.

    :returns: The joblib bundle: ``model``, ``features`` (the column
        order the model expects), and its training record.
    :rtype: dict
    """
    return joblib.load(MODEL_PATH)


def add_relative(data: pd.DataFrame) -> pd.DataFrame:
    """Add every measure relative to the median of its volume and parity.

    Page numbers sit left on even pages and right on odd ones, so the
    median is taken over the pages of the same parity. Typeface, layout
    and scanner differences between volumes cancel out, and "bolder,
    blurrier or emptier than the rest of this book" is what remains.
    Line for line the training step of the model (``train.add_relative``).

    :param data: One row per page, with a ``scan`` and a ``page`` column.
    :returns: The frame plus one ``*_rel`` column per measure.
    :rtype: pandas.DataFrame
    """
    base = [c for c in data.columns if c not in NON_FEATURES]
    parity = data["page"] % 2
    med = data.groupby([data["scan"], parity])[base].transform("median")
    rel = (data[base] - med).add_suffix("_rel")
    return pd.concat([data, rel], axis=1)


def score_pdf(path, jobs: int | None = None) -> dict[int, float]:
    """Return the bad-page score of every page of a PDF, keyed 1-based.

    Renders every page at 200 dpi in grey, measures it, expresses the
    measures relative to the volume, and asks the model. The whole
    volume is needed for the relative step, so there is no per-page
    entry point.

    :param path: The PDF to score, the review-1 bitonal copy.
    :param jobs: Worker processes for the render; None is the CPU count.
    :returns: ``{page: score}``, scores rounded to four places.
    :rtype: dict[int, float]
    """
    rows = features.features_of_pdf(path, jobs)
    if not rows:
        return {}
    frame = pd.DataFrame([dict(page=p, **rows[p]) for p in sorted(rows)])
    frame.insert(0, "scan", 0)
    frame["label"] = 0
    frame["weight"] = 1.0
    frame = add_relative(frame)
    bundle = load_model()
    for col in bundle["features"]:
        # Tolerate a model trained on a slightly different column set.
        if col not in frame.columns:
            frame[col] = np.nan
    matrix = frame[bundle["features"]].to_numpy(dtype=float)
    scores = bundle["model"].predict_proba(matrix)[:, 1]
    return {int(p): round(float(s), 4) for p, s in zip(frame["page"], scores)}


def stamp(scan, scores: dict[int, float]) -> None:
    """Write a volume's scores onto the scan, one field and nothing else.

    :param scan: The scan the scores belong to.
    :param scores: ``{page: score}`` from :func:`score_pdf`.
    """
    from scanning.models import Scan

    bundle = load_model()
    payload = {
        "model": bundle.get("version", ""),
        "trained": bundle.get("trained", ""),
        "scored_at": timezone.now().isoformat(),
        "source_fingerprint": scan.source_fingerprint or "",
        "scores": {str(p): s for p, s in sorted(scores.items())},
    }
    Scan.objects.filter(pk=scan.pk).update(page_scores=payload)
    scan.page_scores = payload


def scores_of(scan) -> dict[int, float]:
    """Return the scores stamped on a scan, or none when they are stale.

    A stamp made against another upload of the volume is not read: the
    pages it names are not these pages. A blank fingerprint on either
    side matches anything, the rule every row that addresses the
    original follows.

    :param scan: The scan.
    :returns: ``{page: score}``, empty when the scan is unscored or its
        stamp is for another original.
    :rtype: dict[int, float]
    """
    payload = scan.page_scores or {}
    stamped = payload.get("source_fingerprint") or ""
    current = scan.source_fingerprint or ""
    if stamped and current and stamped != current:
        return {}
    return {int(p): float(s) for p, s in (payload.get("scores") or {}).items()}


def flagged(scan, threshold: float = THRESHOLD) -> list[tuple[int, float]]:
    """Return the pages at or above the threshold, worst first.

    :param scan: The scan.
    :param threshold: The score a page needs to be listed.
    :returns: ``[(page, score), ...]`` by descending score, then page.
    :rtype: list[tuple[int, float]]
    """
    return sorted(
        ((p, s) for p, s in scores_of(scan).items() if s >= threshold),
        key=lambda item: (-item[1], item[0]),
    )


#: The states of a scan's score, for the note on step 1.
PENDING = "pending"
FAILED = "failed"
SCORED = "scored"


def state(scan) -> str:
    """Return where a scan's bad-page score stands.

    :data:`PENDING` is a scan the pass has not scored, or scored under
    another upload; :data:`FAILED` one the pass gave up on at
    :data:`MAX_ATTEMPTS`, which the command rescores; :data:`SCORED`
    one whose scores stand, flagged pages or none. Step 1 says the
    first and is silent on the other two.

    :param scan: The scan.
    :returns: One of the three states.
    :rtype: str
    """
    if scores_of(scan) or (scan.page_scores or {}).get("scores") == {}:
        return SCORED
    attempts = int((scan.page_scores or {}).get("attempts") or 0)
    return FAILED if attempts >= MAX_ATTEMPTS else PENDING


def issues(scan) -> list[dict]:
    """Return the review-1 cards of a scan's flagged pages.

    One card per page at or above :data:`THRESHOLD`, addressed by the
    physical page like every ``PHYSICAL_PAGE_CHECKS`` card, with the
    score in ``metadata`` for the ranking and in the message for the
    reader. Called by ``services.recalculate_issues`` before the
    deletion and dismissal filters.

    :param scan: The scan.
    :returns: Issue dicts in the shape ``Issue(**d)`` takes.
    :rtype: list[dict]
    """
    from scanning import repairs
    from scanning.models import CheckName, PageRepairRequest

    # A page a scanner is asked for needs no card: the card's job was
    # to get the page looked at, and the request is the answer. A
    # fulfilled request still counts until it is dismissed, like the
    # open row it is; a dismissed one frees the page, and the card
    # comes back for the curator to dismiss in its turn.
    asked = {
        row.pdf_page
        for row in repairs.open_requests(scan)
        if row.action == PageRepairRequest.Action.REPLACE
    }
    return [
        {
            "page_number": page,
            "check_name": CheckName.BAD_PAGE,
            "severity": "warning",
            "message": (
                f"PDF page {page}: the bad-page model scores this page "
                f"{score:.2f}. Look for a blurry, faint, cropped or warped "
                f"scan, and ask for a rescan or dismiss."
            ),
            "metadata": {"score": score},
        }
        for page, score in flagged(scan)
        if page not in asked
    ]


def jobs_setting() -> int:
    """Return the worker processes the daemon pass renders with.

    :returns: ``settings.BADPAGE_JOBS``.
    :rtype: int
    """
    return int(getattr(settings, "BADPAGE_JOBS", 2))


def owed_scans():
    """Return the scans that owe a score, newest first.

    A scan owes one when it is past the bitonal merge, which every
    status outside the busy, uploaded, error and cancelled ones is,
    and carries no stamp, or a failed stamp under the attempt cap. A
    stamp made against another upload is not found here: a re-upload is
    rare, and the command rescores it by hand.

    :returns: A queryset of ``Scan`` rows.
    """
    from django.db.models import Q

    from scanning.models import BUSY_STATUSES, Scan, Status

    skipped = set(BUSY_STATUSES) | {
        Status.UPLOADED,
        Status.ERROR,
        Status.ERROR_MAX_RETRIES,
        Status.ERROR_INTERRUPTED,
        Status.CANCELLED,
    }
    return (
        Scan.objects.exclude(status__in=skipped)
        .exclude(source_fingerprint="")
        .filter(
            Q(page_scores__isnull=True)
            | Q(page_scores__attempts__lt=MAX_ATTEMPTS)
        )
        .order_by("-pk")
    )


def record_failure(scan, reason: str) -> int:
    """Count a fault on the scan's stamp and return the attempts so far.

    The failed stamp holds no ``scores``, so :func:`scores_of` reads it
    as unscored, and :func:`owed_scans` takes the row again until the
    cap. Loud on the last attempt, quiet before it.

    :param scan: The scan.
    :param reason: What went wrong, for the stamp and the log.
    :returns: The attempts spent, this one included.
    :rtype: int
    """
    from scanning.models import Scan

    previous = scan.page_scores or {}
    attempts = int(previous.get("attempts") or 0) + 1
    payload = {
        "attempts": attempts,
        "error": reason[:500],
        "failed_at": timezone.now().isoformat(),
    }
    Scan.objects.filter(pk=scan.pk).update(page_scores=payload)
    scan.page_scores = payload
    log = logger.error if attempts >= MAX_ATTEMPTS else logger.warning
    log(
        "bad-page score: scan %s failed (attempt %d of %d): %s",
        scan.pk,
        attempts,
        MAX_ATTEMPTS,
        reason,
    )
    return attempts


def score_scan(scan, pdf, jobs: int | None = None) -> dict[int, float]:
    """Score a scan's bitonal copy, stamp it and rebuild its cards.

    The one path the command and the daemon pass share. The cards are
    rebuilt only for a scan in a review state, which the rebuild keeps;
    a scan still in the pipeline gets them from the pipeline's own
    rebuild once the page numbers land.

    :param scan: The scan.
    :param pdf: The bitonal copy to score, page for page the original.
    :param jobs: Worker processes for the render.
    :returns: ``{page: score}``.
    :rtype: dict[int, float]
    :raises ValueError: If the file's page count is not the scan's.
    """
    from scanning import services
    from scanning.models import REVIEW_STATUSES

    with fitz.open(str(pdf)) as doc:
        page_count = len(doc)
    if scan.page_count and scan.page_count != page_count:
        raise ValueError(
            f"scan {scan.pk} has {scan.page_count} pages and {pdf} has "
            f"{page_count}: the scores would address the wrong pages"
        )
    scores = score_pdf(pdf, jobs=jobs)
    stamp(scan, scores)
    scan.refresh_from_db()
    if scan.ocr_results and scan.status in REVIEW_STATUSES:
        services.recalculate_issues(scan)
    return scores


def bitonal_copy(scan) -> tuple[Path, bool]:
    """Return the scan's review-1 bitonal copy on disk, and whether it was pulled.

    The merge leaves the copy on this disk for a while, and a
    development tree keeps it, so the local file is read before the
    bucket is asked where the copy is (``apply.volume_bitonal_key``,
    which also stands the original in for a volume that skipped the
    conversion). A pulled copy is the caller's to remove.

    :param scan: The scan.
    :returns: The path, and True when this call pulled it.
    :rtype: tuple[pathlib.Path, bool]
    :raises apply.ApplyError: If the copy is absent and cannot be pulled.
    """
    from scanning import apply
    from scanning.s3_sync import PIPELINE_INPUT_NAME

    local = Path(scan.output_dir) / PIPELINE_INPUT_NAME
    if local.is_file():
        return local, False
    return apply.local_copy(scan, apply.volume_bitonal_key(scan)), True


def run_tick(jobs: int | None = None) -> int:
    """Score up to :data:`SCANS_PER_TICK` owed scans, newest first.

    The bitonal copy is pulled when it is not on disk and removed again
    when this tick pulled it: the daemon's tree is ephemeral, and the
    next pass that needs the file pulls its own.

    :param jobs: Worker processes for the render; None reads the setting.
    :returns: How many scans were scored.
    :rtype: int
    """
    scored = 0
    for scan in owed_scans()[:SCANS_PER_TICK]:
        pulled = None
        try:
            pdf, was_pulled = bitonal_copy(scan)
            if was_pulled:
                pulled = pdf
            score_scan(scan, pdf, jobs or jobs_setting())
            scored += 1
            logger.info(
                "bad-page score: scan %s scored, %d page(s) flagged",
                scan.pk,
                len(flagged(scan)),
            )
        except Exception as exc:
            record_failure(scan, f"{type(exc).__name__}: {exc}")
        finally:
            if pulled is not None:
                pulled.unlink(missing_ok=True)
    return scored
