"""Name the apply run each measured detection run was measured against.

Issue #269. The redaction compute reads the corrected volume of the
standing ``ApplyRun`` from here on, so its ``applied_at`` stamp on the
detect run (``provider_meta["apply"]``) names that run in ``apply_run``,
and a stamp that names no run is not current
(``yolo.redactions_current``). Every stamp written before this
migration names none.

Two cases. A scan whose standing run has no structural edit (an
identity map) was measured in the same page space the run's outputs
describe, so its stamp is given the run's pk and its result stands. A
scan whose standing run deletes, inserts, replaces or rotates a page
was measured in the original's space, and its rows are wrong for the
volume review 2 now shows: its stamp is left alone, so the collect tick
re-queues the compute and the volume is measured again in the final
space by itself. Nothing moves a status here.

Historical models only, and nothing imported from ``scanning.apply``:
the identity test is inlined.
"""

from django.db import migrations

DETECT = "detect"
BLACKLETTER = "blackletter"
RUNPOD = "runpod"


def _is_identity(page_map):
    """Return whether a stored map keeps every original page in place.

    :param page_map: The run's stored map.
    :returns: Whether the final PDF is the original, page for page.
    """
    entries = (page_map or {}).get("pages") or []
    return len(entries) == (page_map or {}).get("source_page_count") and all(
        (entry.get("source") or {}).get("kind") == "original"
        for entry in entries
    )


def stamp_apply_runs(apps, schema_editor):
    """Give every measured detect run the pk of its identity apply run.

    :param apps: The historical app registry.
    :param schema_editor: Unused.
    :return: None.
    """
    ExternalJob = apps.get_model("scanning", "ExternalJob")
    ApplyRun = apps.get_model("scanning", "ApplyRun")
    Scan = apps.get_model("scanning", "Scan")

    heads = ExternalJob.objects.filter(
        stage=DETECT,
        engine=BLACKLETTER,
        provider=RUNPOD,
        shard_index=0,
        apply_run__isnull=True,
        provider_meta__apply__applied_at__isnull=False,
    )
    for head in heads:
        state = dict((head.provider_meta or {}).get("apply") or {})
        if state.get("apply_run") is not None:
            continue
        run = (
            ApplyRun.objects.filter(scan_id=head.scan_id, superseded_at__isnull=True)
            .order_by("-number")
            .first()
        )
        if run is None or not (
            run.bitonal_key
            and run.ocr_key
            and run.printed_pages_key
            and run.detections_key
        ):
            continue
        scan = Scan.objects.filter(pk=head.scan_id).only("source_fingerprint").first()
        theirs = scan.source_fingerprint if scan else ""
        if run.source_fingerprint and theirs and run.source_fingerprint != theirs:
            continue
        if not _is_identity(run.page_map):
            continue
        state["apply_run"] = run.pk
        meta = dict(head.provider_meta or {})
        meta["apply"] = state
        head.provider_meta = meta
        head.save(update_fields=["provider_meta"])


class Migration(migrations.Migration):
    dependencies = [
        ("scanning", "0023_apply_runs"),
    ]

    operations = [
        migrations.RunPython(stamp_apply_runs, migrations.RunPython.noop),
    ]
