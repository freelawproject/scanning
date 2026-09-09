"""Tests for the standalone RunPod case-law tagger worker handler.

``scanning/runpod-caselaw-tagger/handler.py`` is a separate deploy
artifact: only that one file is copied into the worker image, and it
imports the worker stack (``runpod``/``torch``/``transformers``) that
isn't installed in the scanning test environment. :func:`_load_handler`
injects lightweight stubs into ``sys.modules`` before importing the
module from its file path, and gives ``torch`` a CUDA-less answer so
``_preload()`` returns before it tries to open the snapshot.

The handler imports the shared transfer code as a top-level
``runpod_common`` module (the Dockerfile copies it next to handler.py),
so the loader aliases the real ``scanning.runpod_common`` under that
name; its download/upload behaviour is covered in
``test_runpod_common.py``. These tests cover what is specific to this
worker: the dispatch and error-code surface, the GPU gating, the input
validation of the ``tag`` action in both its shapes, the BIO decoding
into character spans, long-case windowing and merging, and both delivery
shapes.

Only ``_predict_batch`` is stubbed: it is the one function that needs
torch. The tokenizer is a whitespace fake installed at
``handler._TOKENIZER``, so ``_tokenize`` and the offsets it returns run
for real.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from scanning import runpod_common

_HANDLER_PATH = (
    Path(__file__).resolve().parent.parent
    / "runpod-caselaw-tagger"
    / "handler.py"
)

#: The checkpoint's classes, in its own order; ``ID2LABEL`` is the
#: 25-entry BIO table its config carries.
CLASSES = (
    "party",
    "separator",
    "docketnumber",
    "court",
    "attorneys",
    "judges",
    "datefiled",
    "otherdate",
    "history",
    "disposition",
    "author",
    "heading",
)
ID2LABEL = {0: "O"}
for _index, _name in enumerate(CLASSES):
    ID2LABEL[1 + 2 * _index] = f"B-{_name}"
    ID2LABEL[2 + 2 * _index] = f"I-{_name}"
LABEL2ID = {label: label_id for label_id, label in ID2LABEL.items()}

#: Token ids the fake tokenizer hands out: ``WORD_BASE + word index``.
#: Special tokens sit below it, so a predict stub can tell them apart.
CLS_ID, SEP_ID, PAD_ID = 1, 2, 0
WORD_BASE = 100


def _load_handler(*, cuda=False):
    """Import the handler module with its worker-only deps stubbed."""
    stub_runpod = mock.MagicMock()
    # The fitness-check decorator must return the function it wraps so
    # the tests can call the real ``_require_gpu``.
    stub_runpod.serverless.register_fitness_check.side_effect = lambda f: f
    # ``torch.cuda.is_available()`` -> False makes ``_preload()`` take
    # the no-GPU path regardless of the machine running the tests, so
    # the import opens no snapshot.
    stub_torch = mock.MagicMock()
    stub_torch.__version__ = "2.14.0+cu126"
    stub_torch.cuda.is_available.return_value = cuda
    stubs = {
        "runpod": stub_runpod,
        "torch": stub_torch,
        "transformers": mock.MagicMock(),
        # The real shared module, under the top-level name the worker
        # image gives it.
        "runpod_common": runpod_common,
    }
    with mock.patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            "_runpod_caselaw_tagger_handler_under_test", _HANDLER_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


handler = _load_handler()


class FakeTokenizer:
    """A whitespace tokenizer with the interface ``_tokenize`` uses.

    One token per ``\\S+`` run, ``[CLS]`` first and ``[SEP]`` last with
    empty offsets, the shape a fast Hugging Face tokenizer returns.
    """

    pad_token_id = PAD_ID

    def __call__(self, text, **kwargs):
        ids = [CLS_ID]
        offsets = [(0, 0)]
        for index, match in enumerate(re.finditer(r"\S+", text)):
            ids.append(WORD_BASE + index)
            offsets.append((match.start(), match.end()))
        ids.append(SEP_ID)
        offsets.append((0, 0))
        return {"input_ids": ids, "offset_mapping": offsets}


def _fake_model(max_tokens=8192):
    """Return a model stand-in with the two config fields the handler
    reads. String keys, as a config loaded from JSON carries them."""
    return SimpleNamespace(
        config=SimpleNamespace(
            id2label={str(k): v for k, v in ID2LABEL.items()},
            max_position_embeddings=max_tokens,
        )
    )


def _predict_first_two_words_as_party(batch):
    """A predict stub: the first two words of every sequence are one
    ``party`` span, everything else is ``O``."""
    out = []
    for ids in batch:
        labels = []
        for token_id in ids:
            if token_id == WORD_BASE:
                labels.append(LABEL2ID["B-party"])
            elif token_id == WORD_BASE + 1:
                labels.append(LABEL2ID["I-party"])
            else:
                labels.append(LABEL2ID["O"])
        out.append(labels)
    return out


class _TagCase(SimpleTestCase):
    """Shared plumbing: a device, a loaded fake model, and no real IO."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(handler, "_DEVICE", "cuda"))
        self.enterContext(mock.patch.object(handler, "_CUDA_AVAILABLE", True))
        self.enterContext(mock.patch.object(handler, "_MODEL", _fake_model()))
        self.enterContext(
            mock.patch.object(handler, "_TOKENIZER", FakeTokenizer())
        )
        self.predict = self.enterContext(
            mock.patch.object(
                handler,
                "_predict_batch",
                side_effect=_predict_first_two_words_as_party,
            )
        )
        self.enterContext(mock.patch.object(handler, "download_pdf"))

    def run_tag(self, **inputs):
        """Dispatch one tag job, with a default inline sequence list."""
        payload = {
            "action": "tag",
            "sequences": [
                {"id": "c1", "text": "<p>Jane ROE, Appellant,</p>\n<p>v.</p>"}
            ],
        }
        payload.update(inputs)
        return handler.handler({"id": "job-1", "input": payload})


class TestDispatch(SimpleTestCase):
    """The handler's error-code surface, before any real work."""

    def test_missing_action_returns_bad_input(self):
        out = handler.handler({"input": {}})
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("worker_boot_ms", out)
        self.assertIn("worker_uptime_ms", out)
        self.assertFalse(out["gpu_available"])

    def test_non_string_action_returns_bad_input(self):
        out = handler.handler({"input": {"action": 42}})
        self.assertEqual(out["error_code"], "BAD_INPUT")

    def test_no_device_returns_no_gpu(self):
        # The module-level preload ran with a CUDA-less torch and no
        # HANDLER_ALLOW_CPU, so there is no device.
        self.assertIsNone(handler._DEVICE)
        out = handler.handler(
            {"input": {"action": "tag", "sequences": [{"id": 1, "text": "x"}]}}
        )
        self.assertEqual(out["error_code"], "NO_GPU")
        # A CPU-only worker never grows a GPU: the SDK must terminate
        # it after the response instead of keeping it warm.
        self.assertIs(out["refresh_worker"], True)

    def test_unknown_action_returns_unknown_action(self):
        with mock.patch.object(handler, "_DEVICE", "cuda"):
            out = handler.handler({"input": {"action": "parse"}})
        self.assertEqual(out["error_code"], "UNKNOWN_ACTION")
        # The message names the action this worker does have.
        self.assertIn("tag", out["error"])

    def test_fitness_check_rejects_a_worker_with_no_device(self):
        with self.assertRaises(RuntimeError):
            handler._require_gpu()

    def test_fitness_check_accepts_a_cpu_device_when_allowed(self):
        # HANDLER_ALLOW_CPU=1 puts the worker on the CPU on purpose, and
        # the fitness check must let it serve.
        with mock.patch.object(handler, "_DEVICE", "cpu"):
            handler._require_gpu()


class TestTagValidation(_TagCase):
    """Every bad input comes back as a structured BAD_INPUT."""

    def test_neither_source_is_refused(self):
        out = handler.handler({"input": {"action": "tag"}})
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("input_url", out["error"])
        self.assertIn("sequences", out["error"])
        self.predict.assert_not_called()

    def test_both_sources_are_refused(self):
        out = self.run_tag(input_url="https://s3/in.json")
        self.assertEqual(out["error_code"], "BAD_INPUT")
        # Refused before any download: the two would disagree.
        handler.download_pdf.assert_not_called()

    def test_empty_list_is_refused(self):
        out = self.run_tag(sequences=[])
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("non-empty", out["error"])

    def test_non_object_entry_is_refused(self):
        out = self.run_tag(sequences=["<p>text</p>"])
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("sequences[0]", out["error"])

    def test_missing_or_blank_text_is_refused(self):
        for entry in (
            {"id": 1},
            {"id": 1, "text": "   "},
            {"id": 1, "text": 7},
        ):
            with self.subTest(entry=entry):
                out = self.run_tag(sequences=[entry])
                self.assertEqual(out["error_code"], "BAD_INPUT")
                self.assertIn("text", out["error"])

    def test_bad_id_is_refused(self):
        # ``True`` is an ``int`` in Python and must not pass as one; a
        # float or a missing id is a caller that cannot place the
        # answer back.
        for sid in (None, True, 1.5, ["a"]):
            with self.subTest(id=sid):
                out = self.run_tag(sequences=[{"id": sid, "text": "x"}])
                self.assertEqual(out["error_code"], "BAD_INPUT")
                self.assertIn(".id", out["error"])

    def test_duplicate_id_is_refused(self):
        out = self.run_tag(
            sequences=[{"id": "a", "text": "x"}, {"id": "a", "text": "y"}]
        )
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("duplicate", out["error"])

    def test_integer_and_string_ids_both_pass(self):
        out = self.run_tag(
            sequences=[
                {"id": 7, "text": "x y"},
                {"id": "seven", "text": "x y"},
            ]
        )
        self.assertEqual([s["id"] for s in out["sequences"]], [7, "seven"])

    def test_over_max_sequences_is_refused(self):
        with mock.patch.object(handler, "MAX_SEQUENCES", 1):
            out = self.run_tag(
                sequences=[{"id": 1, "text": "x"}, {"id": 2, "text": "y"}]
            )
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("MAX_SEQUENCES", out["error"])
        self.predict.assert_not_called()

    def test_bad_batch_size_is_refused(self):
        # JSON null and a non-number must come back as BAD_INPUT, not as
        # a TypeError traceback with no error_code.
        for value in (0, -1, None, "four", [4]):
            with self.subTest(batch_size=value):
                out = self.run_tag(batch_size=value)
                self.assertEqual(out["error_code"], "BAD_INPUT")
                self.assertIn("batch_size", out["error"])
                self.predict.assert_not_called()

    def _download_writes(self, body: str):
        """Make the patched downloader write ``body`` to its dest."""

        def write(url, dest):
            Path(dest).write_text(body, encoding="utf-8")

        handler.download_pdf.side_effect = write

    def test_input_url_document_is_read(self):
        self._download_writes(
            json.dumps(
                {"sequences": [{"id": "c9", "text": "Jane ROE v. STATE"}]}
            )
        )
        out = handler.handler(
            {"input": {"action": "tag", "input_url": "https://s3/in.json"}}
        )
        self.assertEqual(out["sequence_count"], 1)
        self.assertEqual(out["sequences"][0]["id"], "c9")
        url, dest = handler.download_pdf.call_args[0]
        self.assertEqual(url, "https://s3/in.json")
        self.assertEqual(Path(dest).name, "input.json")

    def test_input_url_may_be_a_bare_list(self):
        self._download_writes(json.dumps([{"id": 1, "text": "Jane ROE"}]))
        out = handler.handler(
            {"input": {"action": "tag", "input_url": "https://s3/in.json"}}
        )
        self.assertEqual(out["sequence_count"], 1)

    def test_input_url_that_is_not_json_is_bad_input(self):
        # The downloader checked the byte count, so a document that
        # will not parse is the caller's, not a truncated copy.
        self._download_writes("%PDF-1.4 not json")
        out = handler.handler(
            {"input": {"action": "tag", "input_url": "https://s3/in.json"}}
        )
        self.assertEqual(out["error_code"], "BAD_INPUT")
        self.assertIn("not JSON", out["error"])

    def test_corrupt_download_is_transient_not_bad_input(self):
        handler.download_pdf.side_effect = runpod_common.CorruptDownloadError(
            "truncated"
        )
        out = handler.handler(
            {"input": {"action": "tag", "input_url": "https://s3/in.json"}}
        )
        self.assertEqual(out["error_code"], "INPUT_DOWNLOAD_CORRUPT")


class TestTagging(_TagCase):
    """What the action computes, with only the forward pass stubbed."""

    def test_spans_are_decoded_over_the_input_text(self):
        text = "<p>Jane ROE, Appellant,</p>\n<p>v.</p>"
        out = self.run_tag(sequences=[{"id": "c1", "text": text}])
        entry = out["sequences"][0]
        self.assertEqual(entry["id"], "c1")
        # [CLS] + 4 whitespace-delimited words + [SEP]
        self.assertEqual(entry["token_count"], 6)
        self.assertEqual(
            entry["spans"],
            [
                {
                    "start": 3,
                    "end": len("<p>Jane ROE,"),
                    "label": "party",
                    "text": "Jane ROE,",
                }
            ],
        )
        self.assertEqual(out["failed_sequences"], [])
        self.assertEqual(out["sequence_count"], 1)
        self.assertEqual(out["model"], handler.MODEL_NAME)
        self.assertEqual(out["max_tokens"], 8192)
        self.assertIn("duration_ms", out)

    def test_long_case_is_chunked_and_returned_as_one_case(self):
        with mock.patch.object(handler, "_MODEL", _fake_model(max_tokens=6)):
            out = self.run_tag(
                sequences=[
                    {"id": "long", "text": "a b c d e f"},
                    {"id": "short", "text": "a b"},
                ]
            )
        self.assertEqual(out["failed_sequences"], [])
        long_entry, short_entry = out["sequences"]
        self.assertEqual(long_entry["id"], "long")
        self.assertEqual(long_entry["token_count"], 8)
        self.assertIn("spans", long_entry)
        self.assertGreater(long_entry["window_count"], 1)
        self.assertEqual(short_entry["id"], "short")
        self.assertEqual(len(short_entry["spans"]), 1)
        for call in self.predict.call_args_list:
            self.assertTrue(all(len(ids) <= 6 for ids in call.args[0]))

    def test_hundred_page_case_has_no_missing_or_duplicated_text(self):
        # All content is one entity, including across chunk boundaries.
        # The merge must emit it once and retain the final disposition.
        text = "\n".join(
            f"<p>Page {i}: Café — " + "body " * 30 + "Affirmed.</p>"
            for i in range(100)
        )
        self.predict.side_effect = lambda batch: [
            [LABEL2ID["I-party"]] * len(ids) for ids in batch
        ]
        with mock.patch.object(handler, "_MODEL", _fake_model(max_tokens=42)):
            out = self.run_tag(sequences=[{"id": "book", "text": text}])
        entry = out["sequences"][0]
        self.assertGreater(entry["window_count"], 1)
        self.assertEqual(len(entry["spans"]), 1)
        self.assertEqual(entry["spans"][0]["start"], 3)
        self.assertEqual(entry["spans"][0]["end"], len(text) - 4)
        self.assertEqual(entry["spans"][0]["text"], text[3:-4])

    def test_overlapping_predictions_use_the_more_interior_paragraph(self):
        text = "\n".join(f"<p>word{i} end{i}</p>" for i in range(14))

        def predict(batch):
            # First window votes party, second votes court. The ninth
            # paragraph is more interior in the first; the tenth is
            # more interior in the second.
            return [
                [LABEL2ID["I-party" if ids[1] == WORD_BASE else "I-court"]]
                * len(ids)
                for ids in batch
            ]

        self.predict.side_effect = predict
        with mock.patch.object(handler, "WINDOW_TOKENS", 22):
            out = self.run_tag(sequences=[{"id": "overlap", "text": text}])
        spans = out["sequences"][0]["spans"]
        self.assertEqual([s["label"] for s in spans], ["party", "court"])
        self.assertTrue(spans[0]["text"].endswith("end8"))
        self.assertTrue(spans[1]["text"].startswith("word9"))

    def test_oversized_single_paragraph_overlaps_without_losing_tokens(self):
        text = "<p>" + " ".join(f"word{i}" for i in range(35)) + "</p>"
        ids, offsets = handler._tokenize(text)
        windows, owners = handler._plan_windows(text, ids, offsets, 12)
        self.assertGreater(len(windows), 1)
        self.assertTrue(all(len(w[0]) <= 12 for w in windows))
        self.assertTrue(all(w[0][0] == CLS_ID for w in windows))
        self.assertTrue(all(w[0][-1] == SEP_ID for w in windows))
        owned = [
            i for ranges in owners for a, b in ranges for i in range(a, b)
        ]
        self.assertEqual(sorted(owned), list(range(1, len(ids) - 1)))
        self.assertGreater(sum(w[2] - w[1] for w in windows), len(ids) - 2)

    def test_windows_keep_paragraphs_whole_when_they_fit(self):
        text = "\n".join(f"<p>a{i} b{i}</p>" for i in range(15))
        ids, offsets = handler._tokenize(text)
        windows, _ = handler._plan_windows(text, ids, offsets, 12)
        for _, a, b, _ in windows:
            self.assertTrue(text[offsets[a][0] :].startswith("<p>"))
            self.assertTrue(text[: offsets[b - 1][1]].endswith("</p>"))

    def test_trailing_plain_text_is_not_lost_after_the_last_paragraph(self):
        text = "<p>first paragraph</p> trailing final words"
        self.predict.side_effect = lambda batch: [
            [LABEL2ID["I-party"]] * len(ids) for ids in batch
        ]
        with mock.patch.object(handler, "_MODEL", _fake_model(max_tokens=5)):
            out = self.run_tag(sequences=[{"id": "tail", "text": text}])
        self.assertEqual(out["sequences"][0]["spans"][0]["end"], len(text))

    def test_short_model_output_fails_instead_of_silently_losing_text(self):
        self.predict.side_effect = lambda batch: [[0] for _ in batch]
        with self.assertRaisesRegex(RuntimeError, "incomplete token"):
            self.run_tag()

    def test_output_keeps_input_order_despite_length_sorting(self):
        out = self.run_tag(
            sequences=[
                {"id": "longest", "text": "a b c d e"},
                {"id": "shortest", "text": "a"},
                {"id": "middle", "text": "a b c"},
            ],
            batch_size=1,
        )
        self.assertEqual(
            [s["id"] for s in out["sequences"]],
            ["longest", "shortest", "middle"],
        )
        # Batched shortest first.
        lengths = [
            len(call.args[0][0]) for call in self.predict.call_args_list
        ]
        self.assertEqual(lengths, sorted(lengths))

    def test_batch_size_bounds_the_forward_pass(self):
        sequences = [{"id": i, "text": "a b"} for i in range(5)]
        self.run_tag(sequences=sequences, batch_size=2)
        self.assertEqual(self.predict.call_count, 3)
        self.assertEqual(
            [len(call.args[0]) for call in self.predict.call_args_list],
            [2, 2, 1],
        )

    def test_default_batch_size_is_the_env_value(self):
        sequences = [{"id": i, "text": "a b"} for i in range(5)]
        with mock.patch.object(handler, "DEFAULT_BATCH_SIZE", 5):
            self.run_tag(sequences=sequences)
        self.assertEqual(self.predict.call_count, 1)

    def test_model_is_loaded_lazily_when_the_preload_failed(self):
        # A boot-time load failure is swallowed so the worker starts;
        # the first job must retry it rather than answer with nothing.
        def load():
            handler._MODEL = _fake_model()
            handler._TOKENIZER = FakeTokenizer()

        with (
            mock.patch.object(handler, "_MODEL", None),
            mock.patch.object(handler, "_TOKENIZER", None),
            mock.patch.object(handler, "_load_model", side_effect=load) as lm,
        ):
            out = self.run_tag()
        lm.assert_called_once_with()
        self.assertEqual(out["sequence_count"], 1)

    def test_progress_failure_does_not_fail_the_job(self):
        handler.runpod.serverless.progress_update.side_effect = RuntimeError(
            "no job context"
        )
        try:
            out = self.run_tag()
        finally:
            handler.runpod.serverless.progress_update.side_effect = None
        self.assertEqual(out["sequence_count"], 1)


class TestDecodeSpans(SimpleTestCase):
    """The BIO decoder, a pure function over offsets and labels."""

    def decode(self, text, labels):
        """Tokenize ``text`` with the fake and decode ``labels`` (one
        per word, by name) over it."""
        encoded = FakeTokenizer()(text)
        label_ids = (
            [LABEL2ID["O"]]
            + [LABEL2ID[label] for label in labels]
            + [LABEL2ID["O"]]
        )
        return handler.decode_spans(
            text, encoded["offset_mapping"], label_ids, ID2LABEL
        )

    def test_b_then_i_is_one_span(self):
        spans = self.decode(
            "Jane ROE v. STATE", ["B-party", "I-party", "O", "O"]
        )
        self.assertEqual(
            spans,
            [{"start": 0, "end": 8, "label": "party", "text": "Jane ROE"}],
        )

    def test_two_b_labels_are_two_spans(self):
        spans = self.decode("Jane ROE", ["B-party", "B-party"])
        self.assertEqual([s["text"] for s in spans], ["Jane", "ROE"])

    def test_i_without_b_opens_a_span(self):
        # The model dropped the B. Discarding the run would lose the
        # span; the caller wants the text, not the prefix.
        spans = self.decode("Jane ROE", ["I-party", "I-party"])
        self.assertEqual([s["text"] for s in spans], ["Jane ROE"])

    def test_i_of_another_class_closes_the_span(self):
        spans = self.decode(
            "Jane ROE No. 24-1",
            ["B-party", "I-party", "I-docketnumber", "I-docketnumber"],
        )
        self.assertEqual(
            [(s["label"], s["text"]) for s in spans],
            [("party", "Jane ROE"), ("docketnumber", "No. 24-1")],
        )

    def test_span_edges_are_trimmed_to_the_text(self):
        # A real tokenizer's offsets often start at the space before the
        # word; the span must cover the words only.
        text = "  Jane   ROE  "
        offsets = [(0, 0), (0, 6), (6, 14), (0, 0)]
        label_ids = [0, LABEL2ID["B-party"], LABEL2ID["I-party"], 0]
        spans = handler.decode_spans(text, offsets, label_ids, ID2LABEL)
        self.assertEqual(
            spans,
            [{"start": 2, "end": 12, "label": "party", "text": "Jane   ROE"}],
        )

    def test_special_tokens_neither_open_nor_split(self):
        # A label on [CLS] or [SEP] (empty offsets) is noise, and an
        # empty-offset token in the middle must not end a run.
        text = "Jane ROE"
        offsets = [(0, 0), (0, 4), (0, 0), (5, 8), (0, 0)]
        label_ids = [
            LABEL2ID["B-party"],
            LABEL2ID["B-party"],
            LABEL2ID["O"],
            LABEL2ID["I-party"],
            LABEL2ID["B-court"],
        ]
        spans = handler.decode_spans(text, offsets, label_ids, ID2LABEL)
        self.assertEqual([s["text"] for s in spans], ["Jane ROE"])

    def test_all_outside_is_no_span(self):
        self.assertEqual(self.decode("Jane ROE", ["O", "O"]), [])

    def test_markup_noise_cannot_open_or_split_entities(self):
        text = "<p>Jane <em>ROE</em></p>"
        parts = ["<p>", "Jane", " ", "<em>", "ROE", "</em>", "</p>"]
        offsets, pos = [], 0
        for part in parts:
            offsets.append((pos, pos + len(part)))
            pos += len(part)
        labels = ["B-court", "B-party", "O", "O", "I-party", "B-court", "O"]
        spans = handler.decode_spans(
            text, offsets, [LABEL2ID[label] for label in labels], ID2LABEL
        )
        self.assertEqual(
            spans,
            [
                {
                    "start": 3,
                    "end": 15,
                    "label": "party",
                    "text": "Jane <em>ROE",
                }
            ],
        )

    def test_mixed_text_and_markup_tokens_are_trimmed(self):
        self.assertEqual(
            self.decode("<p>Jane ROE</p>", ["B-party", "I-party"]),
            [{"start": 3, "end": 11, "label": "party", "text": "Jane ROE"}],
        )

    def test_bytes_of_one_character_cannot_split_a_span(self):
        # Byte-level BPE gives every byte of a multi-byte symbol the same
        # character offsets. On real volume 2574 the model labelled the
        # second byte of a West key symbol ``B-heading``, and the
        # decoder opened a second span one character inside the first.
        text = "2. Appeal and Error \u261e"
        mark = (len(text) - 1, len(text))
        offsets = [
            (0, 0),
            (0, 2),
            (3, 9),
            (10, 13),
            (14, 19),
            mark,
            mark,
            mark,
            (0, 0),
        ]
        labels = [
            "O",
            "B-heading",
            "I-heading",
            "I-heading",
            "I-heading",
            "I-heading",
            "B-heading",
            "I-heading",
            "O",
        ]
        spans = handler.decode_spans(
            text, offsets, [LABEL2ID[label] for label in labels], ID2LABEL
        )
        self.assertEqual(
            spans,
            [{"start": 0, "end": len(text), "label": "heading", "text": text}],
        )

    def test_an_outside_byte_inside_a_character_does_not_close_the_span(self):
        text = "Error \u261e 26"
        mark = (6, 7)
        offsets = [(0, 0), (0, 5), mark, mark, mark, (8, 10), (0, 0)]
        labels = [
            "O",
            "B-heading",
            "I-heading",
            "O",
            "I-heading",
            "I-heading",
            "O",
        ]
        spans = handler.decode_spans(
            text, offsets, [LABEL2ID[label] for label in labels], ID2LABEL
        )
        self.assertEqual([s["text"] for s in spans], [text])

    def test_unknown_label_id_reads_as_outside(self):
        spans = handler.decode_spans(
            "Jane", [(0, 0), (0, 4), (0, 0)], [0, 99, 0], ID2LABEL
        )
        self.assertEqual(spans, [])

    def test_mismatched_lengths_raise(self):
        with self.assertRaises(ValueError):
            handler.decode_spans("Jane", [(0, 4)], [1, 2], ID2LABEL)


class TestDelivery(_TagCase):
    """Inline and S3 delivery, and what each response carries."""

    def test_inline_when_no_result_url(self):
        out = self.run_tag()
        self.assertEqual(len(out["sequences"]), 1)
        self.assertEqual(out["sequence_count"], 1)
        self.assertTrue(out["gpu_available"])

    def test_whole_volume_with_long_cases_uploads_one_result(self):
        sequences = [
            {"id": "long", "text": "<p>" + "word " * 40 + "end</p>"},
            {"id": "short", "text": "<p>Jane ROE</p>"},
        ]
        with (
            mock.patch.object(handler, "_MODEL", _fake_model(max_tokens=12)),
            mock.patch.object(
                handler, "upload_result", return_value=4096
            ) as upload,
        ):
            out = self.run_tag(
                sequences=sequences,
                result_url="https://s3/put",
                result_key="volume/results.json",
            )
        upload.assert_called_once()
        entries = upload.call_args.args[1]["payload"]["sequences"]
        self.assertEqual([entry["id"] for entry in entries], ["long", "short"])
        self.assertGreater(entries[0]["window_count"], 1)
        self.assertEqual(entries[1]["window_count"], 1)
        self.assertEqual(out["failed_sequences"], [])
        self.assertNotIn("sequences", out)

    def test_s3_delivery_returns_a_summary_only(self):
        with mock.patch.object(
            handler, "upload_result", return_value=4096
        ) as upload:
            out = self.run_tag(
                result_url="https://s3/put",
                result_key="jobs/tag/r1/t.json",
                scan_pk=123,
            )
        # The whole point of the presigned PUT: the spans do not ride
        # back through a response capped at about 20 MB.
        self.assertNotIn("sequences", out)
        self.assertEqual(out["span_count"], 1)
        self.assertEqual(out["sequence_count"], 1)
        self.assertEqual(out["failed_sequences"], [])
        self.assertEqual(out["model"], handler.MODEL_NAME)
        self.assertEqual(out["result_key"], "jobs/tag/r1/t.json")
        self.assertEqual(out["bytes"], 4096)

        url, envelope, content_type = upload.call_args[0]
        self.assertEqual(url, "https://s3/put")
        self.assertEqual(content_type, "application/json")
        self.assertEqual(
            envelope["schema_version"], handler.RESULT_SCHEMA_VERSION
        )
        self.assertEqual(envelope["action"], "tag")
        self.assertEqual(envelope["scan_pk"], 123)
        self.assertEqual(envelope["result_key"], "jobs/tag/r1/t.json")
        self.assertEqual(envelope["payload"]["sequences"][0]["id"], "c1")
        # The envelope must survive a round trip as JSON, since that is
        # how the caller reads it back out of the bucket.
        json.dumps(envelope)

    def test_upload_failure_returns_its_own_code(self):
        error = runpod_common.ResultUploadError(
            "expired", "RESULT_URL_EXPIRED"
        )
        with mock.patch.object(handler, "upload_result", side_effect=error):
            out = self.run_tag(
                result_url="https://s3/put", result_key="jobs/tag/r1/t.json"
            )
        self.assertEqual(out["error_code"], "RESULT_URL_EXPIRED")


class TestPreload(SimpleTestCase):
    """The cold-start path, which must never stop the worker starting."""

    def test_no_cuda_and_no_cpu_override_leaves_no_device(self):
        # The module-level preload already ran this path; assert the
        # state it leaves behind, which every response reports and the
        # fitness check reads.
        self.assertFalse(handler._CUDA_AVAILABLE)
        self.assertIsNone(handler._DEVICE)
        self.assertIsNone(handler._MODEL)

    def test_successful_boot_does_not_call_helpers_defined_later(self):
        # Exercise the actual module-level _load_model call. Previously
        # its final log called _max_tokens before that function existed.
        with mock.patch.object(handler.logger, "exception") as log_error:
            loaded = _load_handler(cuda=True)
        self.assertIsNotNone(loaded._MODEL)
        self.assertEqual(loaded._DEVICE, "cuda")
        log_error.assert_not_called()

    def _run_preload(self, *, cuda: bool, allow_cpu: bool, load_error=None):
        """Run ``_preload`` under a given torch answer and CPU policy.

        :returns: The ``_load_model`` mock, for call assertions.
        """
        stub_torch = mock.MagicMock()
        stub_torch.__version__ = "2.14.0+cu126"
        stub_torch.cuda.is_available.return_value = cuda
        load = mock.MagicMock(side_effect=load_error)
        try:
            with (
                mock.patch.dict(sys.modules, {"torch": stub_torch}),
                mock.patch.object(handler, "ALLOW_CPU", allow_cpu),
                mock.patch.object(handler, "_load_model", load),
            ):
                handler._preload()
                self.device = handler._DEVICE
                self.cuda = handler._CUDA_AVAILABLE
        finally:
            # Leave the module as the other tests expect to find it.
            handler._CUDA_AVAILABLE = False
            handler._DEVICE = None
        return load

    def test_a_gpu_loads_the_model_onto_cuda(self):
        load = self._run_preload(cuda=True, allow_cpu=False)
        self.assertEqual(self.device, "cuda")
        self.assertTrue(self.cuda)
        load.assert_called_once_with()

    def test_cpu_override_loads_the_model_onto_the_cpu(self):
        load = self._run_preload(cuda=False, allow_cpu=True)
        self.assertEqual(self.device, "cpu")
        self.assertFalse(self.cuda)
        load.assert_called_once_with()

    def test_no_gpu_without_the_override_loads_nothing(self):
        load = self._run_preload(cuda=False, allow_cpu=False)
        self.assertIsNone(self.device)
        load.assert_not_called()

    def test_a_broken_load_is_swallowed(self):
        # The first job retries the load and surfaces the real error;
        # the boot must still finish so the worker can answer it.
        load = self._run_preload(
            cuda=True, allow_cpu=False, load_error=RuntimeError("bad snapshot")
        )
        self.assertEqual(self.device, "cuda")
        load.assert_called_once_with()
