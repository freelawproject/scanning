"""Glue every read Surya run again, volume and corrected volume.

Issue #368, and the twin of ``reglue_mistral_ocr``. The glue is the one
transform of a Surya result: the worker's answer is stored per shard,
and ``surya.shard_pages`` is what turns it into the pages of a
document. So a better transform must reach the volumes already read,
and this command is how. It costs one small download per shard and no
GPU payment, because the per-shard results are kept for good.

Run it as well when a late read lands: a corrected volume whose Surya
document was written before a shard was re-read gets it again here.
The opinions are a second step, because their glue is stamped on the
row: run ``reglue_opinion_ocr`` after this command to write them again.

It writes the same two objects the collect tick writes: the volume
document of every glued run, and, for a scan whose corrected volume
(#224) stands, that run's own document. Nothing else moves. No scan
status is written, no review state is read, no row is created and no
job is started, so the command is safe on a corpus in any state.

Examples:

    # Say what would be written, and change nothing.
    docker exec scanning-daemon python manage.py reglue_surya_ocr \\
        --dry-run

    # Glue every read volume again.
    docker exec scanning-daemon python manage.py reglue_surya_ocr

    # One named volume only.
    docker exec scanning-daemon python manage.py reglue_surya_ocr 2845
"""

from scanning import surya
from scanning.models import JobEngine
from scanning.reglue_extract import EngineReglue, ReglueExtractCommand


class Command(ReglueExtractCommand):
    help = (
        "Glue every read Surya run again, so a changed transform "
        "reaches the volumes already read. Writes the volume document "
        "and, where a corrected volume stands, its document too."
    )

    spec = EngineReglue(
        label="Surya",
        engine=JobEngine.SURYA,
        key_field="surya_key",
        run_field="surya_run",
        live_rows=surya.live_extract_jobs,
        merge=surya.merge_surya_results,
        apply_rows=surya.apply_jobs,
        apply_due=surya.apply_glue_due,
        glue_apply=surya.glue_apply_run,
    )
