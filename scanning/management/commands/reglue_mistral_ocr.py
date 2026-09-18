"""Glue every read Mistral run again, volume and corrected volume.

Issue #245. The glue is the one transform of a Mistral result: the
harvest stores what Mistral wrote, line for line, and
``mistral_ocr.parse_payload`` is what turns it into pages. So a better
transform -- a new block field, a cleaner text rule, a fault nobody had
seen -- must reach the volumes already read, and this command is how.
It costs one small download per shard and no API payment, because the
per-shard results are kept for good.

It writes the same two objects the collect tick writes: the volume
document of every glued run, and, for a scan whose corrected volume
(#224) stands, that run's own document. Nothing else moves. No scan
status is written, no review state is read, no row is created and no
job is started, so the command is safe on a corpus in any state.

The walk is ``reglue_extract.ReglueExtractCommand``, which the Surya
twin shares (#368); this module is the Mistral entry of it.

Examples:

    # Say what would be written, and change nothing.
    docker exec scanning-daemon python manage.py reglue_mistral_ocr \\
        --dry-run

    # Glue every read volume again.
    docker exec scanning-daemon python manage.py reglue_mistral_ocr

    # Two named volumes only.
    docker exec scanning-daemon python manage.py reglue_mistral_ocr \\
        2726 2702
"""

from scanning import mistral_ocr
from scanning.models import JobEngine
from scanning.reglue_extract import EngineReglue, ReglueExtractCommand


class Command(ReglueExtractCommand):
    help = (
        "Glue every read Mistral run again, so a changed transform "
        "reaches the volumes already read. Writes the volume document "
        "and, where a corrected volume stands, its document too."
    )

    spec = EngineReglue(
        label="Mistral",
        engine=JobEngine.MISTRAL_OCR,
        key_field="extract_key",
        run_field="extract_run",
        live_rows=mistral_ocr.live_extract_jobs,
        merge=mistral_ocr.merge_extract_results,
        apply_rows=mistral_ocr.apply_jobs,
        apply_due=mistral_ocr.apply_glue_due,
        glue_apply=mistral_ocr.glue_apply_run,
    )
