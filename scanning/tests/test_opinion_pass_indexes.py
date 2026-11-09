"""The indexes of the opinion passes' "owes work" questions (#604).

Each pass asks every tick which opinions still owe its output, and the
question compares two columns of the row. Each is one ``Q`` of
``scanning.models``, read by the pass's query and by a partial index
whose condition is that ``Q``. These tests pin the index and that the
planner can read it for the pass's query: with sequential scans off, a
query whose filter does not imply the index condition cannot use it.
"""

from django.db import connection

from scanning import (
    ensemble,
    final_xml,
    opinion_figures,
    opinion_ocr,
    opinion_pdf,
)
from scanning.tests.test_views import ScanningTestCase

#: The columns the four passes order by: newest scan, reading order.
PASS_COLUMNS = "(scan_id DESC, first_printed_page, index_in_page)"


def _queries():
    """Return ``{index: [(label, queryset)]}``, every pass query an index
    serves, in the shape the pass runs it."""
    return {
        "idx_opinion_pdf_owed": [
            ("owed", opinion_pdf.owed()),
            ("due", opinion_pdf.due()),
        ],
        "idx_opinion_figures_owed": [
            ("owed", opinion_figures.owed()),
            ("due", opinion_figures.due()),
        ],
        "idx_opinion_ocr_owed": [
            (
                "scans",
                opinion_ocr.due()
                .order_by("-scan_id")
                .values_list("scan_id", flat=True)
                .distinct(),
            ),
            (
                "rows",
                opinion_ocr.due()
                .filter(scan_id=1)
                .order_by("first_printed_page", "index_in_page"),
            ),
        ],
        "idx_opinion_ensemble_owed": [("due", ensemble.due(5))],
        "idx_opinion_xml_withdrawn": [
            ("withdrawn", final_xml.withdrawn_rows()[:50])
        ],
    }


class TestOpinionPassIndexes(ScanningTestCase):
    def _definition(self, name):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexdef FROM pg_indexes WHERE indexname = %s", [name]
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row, name)
        return row[0]

    def test_each_pass_index_is_partial_on_the_pass_order(self):
        for name in (
            "idx_opinion_pdf_owed",
            "idx_opinion_figures_owed",
            "idx_opinion_ocr_owed",
            "idx_opinion_ensemble_owed",
        ):
            with self.subTest(name):
                definition = self._definition(name)
                self.assertIn(PASS_COLUMNS, definition)
                self.assertIn(" WHERE ", definition)

    def test_the_withdrawn_final_xml_index(self):
        withdrawn = self._definition("idx_opinion_xml_withdrawn")
        self.assertIn("(id) WHERE", withdrawn)
        self.assertIn("final_xml_tag_key", withdrawn)

    def test_the_pass_columns_compare_inside_the_condition(self):
        """The two-column comparison that no plain index serves is in
        the condition, so the index holds the owed rows alone."""
        for name, comparison in (
            ("idx_opinion_pdf_owed", "redacted_pdf_revision = glue_revision"),
            ("idx_opinion_figures_owed", "figures_cut_digest)::text = "),
            ("idx_opinion_ocr_owed", "ocr_glue_revision = glue_revision"),
            (
                "idx_opinion_ensemble_owed",
                "ensemble_edit_revision = edit_revision",
            ),
        ):
            with self.subTest(name):
                self.assertIn(comparison, self._definition(name))

    def test_each_pass_query_can_read_its_index(self):
        """The planner matches the query's filter to the condition only
        when the filter implies it: the model's ``Q`` in both."""
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
            for name, queries in _queries().items():
                for label, queryset in queries:
                    with self.subTest(f"{name} {label}"):
                        sql, params = queryset.query.sql_with_params()
                        cursor.execute("EXPLAIN " + sql, params)
                        plan = "\n".join(row[0] for row in cursor.fetchall())
                        self.assertIn(name, plan)
