"""The type of a blocking place (``blocking_types``)."""

from django.test import SimpleTestCase

from scanning import blocking_types


class TestTypeOf(SimpleTestCase):
    def assertType(self, readings, expected):
        self.assertEqual(blocking_types.type_of(readings), expected, readings)

    def test_two_engines_that_meet_without_the_space_settle_it(self):
        """Mistral dropped a digit; dots and Surya read the same number,
        one with a space: the place is spacing."""
        self.assertType(
            ["¶10 Petitioner", "¶0 Petitioner", "¶ 10 Petitioner"],
            "spacing",
        )

    def test_the_lightest_fold_that_two_readings_meet_under_names_it(self):
        self.assertType(['"rule"', "“rule”", "mle"], "quotes")
        self.assertType(["anti-trust", "antitrust", "anti trust"], "spacing")
        self.assertType(["held,", "held.", "heId"], "punctuation")
        self.assertType(["APPELLANT", "Appellant", "Appellnat"], "capitals")

    def test_two_alike_and_one_off_is_what_the_odd_one_differs_by(self):
        """Two engines read the same; the type is the third's slip."""
        self.assertType(["", "", ","], "punctuation")
        self.assertType(["the", "the", "tbe"], "characters")

    def test_the_words_a_person_must_read(self):
        self.assertType(["$10,000", "$70,000", "$76,000"], "numbers")
        self.assertType(["not", "now", "nor"], "meaning_word")
        self.assertType(["Matousek", "Mattousek", "Matnusek"], "name")
        self.assertType(["court", "house", "table"], "different_word")

    def test_a_gap(self):
        self.assertType(["", "Smith", ""], "words_short")
        self.assertType(["", "*412", ""], "star_mark")
        self.assertType(["", "the court held that", ""], "words_long")
        self.assertType(["", "not", ""], "meaning_word")

    def test_one_engine_alone(self):
        self.assertType(["v."], "single_short")
        self.assertType(["Smith v. Jones was decided here"], "single_long")

    def test_blocks_that_share_little_are_different_text(self):
        self.assertType(
            [
                "the court held that the statute applies",
                "swollen antiswollen and stiff in the joints",
                "appellant filed a timely notice of appeal",
            ],
            "different_text",
        )

    def test_every_type_has_a_rank_and_hardest_reads_it(self):
        self.assertEqual(len(blocking_types.RANK), len(blocking_types.TYPES))
        self.assertEqual(
            blocking_types.hardest(["quotes", "numbers", "spacing"]), "numbers"
        )
        self.assertIsNone(blocking_types.hardest([]))
