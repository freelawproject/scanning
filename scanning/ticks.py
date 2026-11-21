"""The daemon's pass groups, shared by the tick commands and the tasks (#503).

Each function runs one group of the passes ``collect_external_jobs``
used to run in one serial tick, in the order that tick ran them, and
returns the counts the command prints. The command calls every group
in order; a Celery tick task calls one group on its own clock and its
own queue, so a slow group (the bitonal merge) no longer holds a fast
one (the polls). Nothing here is new behaviour: the module exists so
the two callers never hold two copies of the list.

Every pass is a poll over the rows, so the order between groups is a
courtesy and not a rule: what one group leaves, the next tick of the
group after it finds.
"""

from __future__ import annotations

from scanning import (
    apply,
    bitonal,
    dots_mocr,
    ensemble,
    final_xml,
    jobs,
    mistral_ocr,
    opinion_ocr,
    opinions,
    review_states,
    surya,
    tagger,
    yolo,
)


def sweep(providers: tuple[str, ...] | None = None) -> jobs.SweepSummary:
    """Ask after every in-flight external job (pass 1).

    :param providers: The providers whose rows are swept, or ``None``
        for every one: the daemon's own pass. A Celery tick names one.
    :returns: What the sweep did.
    :rtype: jobs.SweepSummary
    """
    return jobs.sweep_jobs(providers=providers)


def merge_conversions() -> dict[str, int]:
    """Merge the bitonal shards of every converted scan (pass 2).

    The one group of the collect tick that pulls PDFs, so it has a
    queue of its own under Celery.

    :returns: The count, keyed as the command prints it.
    :rtype: dict[str, int]
    """
    return {"finished": bitonal.finish_ready_scans()}


def glue_runs() -> dict[str, int]:
    """Glue the volume runs and move the review states (passes 3 to 11).

    Seconds of JSON over S3 and compare-and-swap status writes, in the
    order the serial tick ran them: the dots.mocr glue, the page-number
    apply, the detection merge, the tagger glue, the apply trigger, the
    compute queueing, the review-2 promotion, then the Mistral and Surya
    glues and their corrected-volume documents.

    :returns: The counts, keyed as the command prints them.
    :rtype: dict[str, int]
    """
    return {
        "glued": dots_mocr.finish_ready_runs(),
        "applied": dots_mocr.apply_ready_runs(),
        "detected": yolo.finish_ready_runs(),
        "tagged": tagger.finish_ready_runs(),
        "applied_edits": apply.queue_ready_scans(),
        "queued": yolo.queue_ready_runs(),
        "promoted": review_states.promote_ready_scans(),
        "extracted": mistral_ocr.finish_ready_runs(),
        "extracted_applies": mistral_ocr.finish_ready_applies(),
        "read_surya": surya.finish_ready_runs(),
        "surya_applies": surya.finish_ready_applies(),
    }


def prepare_opinions() -> dict[str, int]:
    """Write the opinions' documents and open their reviews (passes 12 to 15).

    The OCR glue, the ensemble, the text-review promotion and the final
    XML export, in that order.

    :returns: The counts, keyed as the command prints them.
    :rtype: dict[str, int]
    """
    return {
        "glued_opinions": opinion_ocr.glue_due(),
        "read_opinions": ensemble.run_tick(),
        "ready_opinions": opinions.promote_ready_opinions(),
        "exported": final_xml.export_due(),
    }


def collect() -> tuple[jobs.SweepSummary, dict[str, int]]:
    """Run every group in order: the whole collect tick.

    :returns: The sweep summary and the merged counts of the groups.
    :rtype: tuple[jobs.SweepSummary, dict[str, int]]
    """
    summary = sweep()
    counts: dict[str, int] = {}
    counts.update(merge_conversions())
    counts.update(glue_runs())
    counts.update(prepare_opinions())
    return summary, counts
