"""The indexes of the opinion passes' "owes work" questions (#604).

Each pass asks every tick which opinions still owe its output, and the
question compares two columns of the row. Each is one ``Q`` of
``scanning.models``, read by the pass's query and by a partial index
whose condition is that ``Q``. These tests pin the index and that the
planner can read it for the pass's query: with sequential scans off and
the table's other indexes dropped, a query whose filter does not imply
the index condition cannot use it. The drop is what makes the answer
"usable", never "cheapest": on the near-empty test table a plain index
of the same columns costs about the same, and the stats earlier tests
leave behind decided which one won.
"""

from django.db import connection, transaction

from scanning import (
    ensemble,
    final_xml,
    opinion_figures,
    opinion_ocr,
    opinion_pdf,
)
from scanning.models import Opinion
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


def _drop_other_indexes(cursor, keep):
    """Drop every index of the opinion table but ``keep``, a constraint's
    with its constraint. Run it inside a savepoint that rolls back.

    The deferred foreign-key checks of the rows a test wrote fire first:
    a table with pending trigger events refuses ``ALTER TABLE``.
    """
    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    cursor.execute(
        """
        SELECT index.relname, con.conname
        FROM pg_index AS x
        JOIN pg_class AS index ON index.oid = x.indexrelid
        LEFT JOIN pg_constraint AS con
            ON con.conindid = x.indexrelid AND con.conrelid = x.indrelid
        WHERE x.indrelid = 'scanning_opinion'::regclass
            AND index.relname <> %s
        """,
        [keep],
    )
    quote = connection.ops.quote_name
    for index, constraint in cursor.fetchall():
        if constraint:
            cursor.execute(
                f"ALTER TABLE scanning_opinion DROP CONSTRAINT"
                f" {quote(constraint)} CASCADE"
            )
        else:
            cursor.execute(f"DROP INDEX {quote(index)}")


def _plan(name, queryset):
    """Return the plan of ``queryset`` with ``name`` the one index of the
    opinion table and sequential scans off; nothing outlives the call."""
    sql, params = queryset.query.sql_with_params()
    with transaction.atomic(), connection.cursor() as cursor:
        _drop_other_indexes(cursor, name)
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("EXPLAIN " + sql, params)
        plan = "\n".join(row[0] for row in cursor.fetchall())
        transaction.set_rollback(True)
    return plan


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
        for name, queries in _queries().items():
            for label, queryset in queries:
                with self.subTest(f"{name} {label}"):
                    self.assertIn(name, _plan(name, queryset))

    def test_a_filter_that_does_not_imply_the_condition_misses(self):
        """The check above can fail: a filter short of the ``Q`` reads
        the table, never the index."""
        queryset = Opinion.objects.filter(ocr_glue_attempts__lt=3)
        self.assertNotIn(
            "idx_opinion_ocr_owed", _plan("idx_opinion_ocr_owed", queryset)
        )
