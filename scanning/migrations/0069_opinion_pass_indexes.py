from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    """The indexes of the opinion passes' "owes work" questions (#604).

    Each condition is the pass's ``Q`` of ``scanning.models``, so the
    planner matches it to the pass's query. Built concurrently: the
    passes write the opinion table, and a plain CREATE INDEX holds those
    writes for the build.
    """

    atomic = False

    dependencies = [
        ("scanning", "0068_opinion_list_indexes"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="opinion",
            index=models.Index(
                condition=models.Q(
                    ("pdf_attempts__lt", 3),
                    models.Q(("status", "error"), _negated=True),
                    models.Q(
                        ("redacted_pdf_revision", models.F("glue_revision")),
                        _negated=True,
                    ),
                ),
                fields=["-scan_id", "first_printed_page", "index_in_page"],
                name="idx_opinion_pdf_owed",
            ),
        ),
        AddIndexConcurrently(
            model_name="opinion",
            index=models.Index(
                condition=models.Q(
                    ("ensemble_revision", models.F("glue_revision")),
                    ("figure_attempts__lt", 3),
                    ("ocr_glue_revision", models.F("glue_revision")),
                    models.Q(("figure_digest", ""), _negated=True),
                    models.Q(
                        ("figures_cut_digest", models.F("figure_digest")),
                        _negated=True,
                    ),
                    models.Q(
                        ("status__in", ("text_review_done", "error")),
                        _negated=True,
                    ),
                ),
                fields=["-scan_id", "first_printed_page", "index_in_page"],
                name="idx_opinion_figures_owed",
            ),
        ),
        AddIndexConcurrently(
            model_name="opinion",
            index=models.Index(
                condition=models.Q(
                    ("ocr_glue_attempts__lt", 3),
                    models.Q(("status", "error"), _negated=True),
                    models.Q(
                        ("ocr_glue_revision__isnull", True),
                        models.Q(
                            ("ocr_glue_revision", models.F("glue_revision")),
                            _negated=True,
                        ),
                        _connector="OR",
                    ),
                ),
                fields=["-scan_id", "first_printed_page", "index_in_page"],
                name="idx_opinion_ocr_owed",
            ),
        ),
        AddIndexConcurrently(
            model_name="opinion",
            index=models.Index(
                condition=models.Q(
                    ("ensemble_attempts__lt", 3),
                    ("ocr_glue_revision", models.F("glue_revision")),
                    ("ocr_glue_revision__isnull", False),
                    models.Q(
                        ("status__in", ("error", "text_review_done")),
                        _negated=True,
                    ),
                    models.Q(
                        ("ensemble_revision__isnull", True),
                        models.Q(
                            (
                                "ensemble_revision",
                                models.F("ocr_glue_revision"),
                            ),
                            _negated=True,
                        ),
                        models.Q(
                            (
                                "ensemble_edit_revision",
                                models.F("edit_revision"),
                            ),
                            _negated=True,
                        ),
                        _connector="OR",
                    ),
                ),
                fields=["-scan_id", "first_printed_page", "index_in_page"],
                name="idx_opinion_ensemble_owed",
            ),
        ),
        AddIndexConcurrently(
            model_name="opinion",
            index=models.Index(
                condition=models.Q(
                    models.Q(("final_xml_tag_key", ""), _negated=True),
                    models.Q(("status", "text_review_done"), _negated=True),
                ),
                fields=["id"],
                name="idx_opinion_xml_withdrawn",
            ),
        ),
    ]
