"""Tests for ``scanning.doctor_client``.

All HTTP is mocked at the ``requests`` layer; nothing here reaches
doctor. Worth pinning down: the wire format (form fields, not JSON or
query params), the terminal/transient split over doctor's error codes,
and the distinction the retry logic rests on -- whether doctor
*answered*, since an answered request never uploads afterwards and an
unanswered one still might.
"""

from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, override_settings

from scanning import doctor_client

DOCTOR = {
    "DOCTOR_ENABLED": True,
    "DOCTOR_HOST": "http://cl-doctor.example.svc:5050",
    "DOCTOR_BITONAL_DPI": 200,
    "DOCTOR_BITONAL_THRESHOLD": 160,
    "DOCTOR_CONNECT_TIMEOUT": 5,
    "DOCTOR_READ_TIMEOUT": 300,
}

SUCCESS_BODY = {
    "success": True,
    "pages": 100,
    "page_count": 100,
    "dpi": 200,
    "threshold": 160,
    "first_page": 1,
    "last_page": 100,
    "bytes": 5_000_000,
    "sha256": "a" * 64,
    "source_sha256": "b" * 64,
    "duration_ms": 31_200,
}


def _response(status_code: int, body=None, text: str = ""):
    """Build a stand-in for a ``requests.Response``.

    :param status_code: HTTP status.
    :param body: Parsed JSON body; ``None`` makes ``.json()`` raise, for
        the non-JSON-response tests.
    :param text: Raw body text, used when ``body`` is None.
    :returns: A MagicMock that behaves like a Response.
    :rtype: MagicMock
    """
    response = MagicMock()
    response.status_code = status_code
    response.ok = status_code < 400
    if body is None:
        response.json.side_effect = ValueError("no json body")
        response.text = text
    else:
        response.json.return_value = body
        response.text = str(body)
    return response


@override_settings(**DOCTOR)
class TestEnabled(SimpleTestCase):
    """The kill switch, and the half of it that is easy to forget."""

    def test_enabled_needs_both_the_switch_and_a_host(self):
        self.assertTrue(doctor_client.enabled())

        with override_settings(DOCTOR_HOST=""):
            self.assertFalse(doctor_client.enabled())
        with override_settings(DOCTOR_ENABLED=False):
            self.assertFalse(doctor_client.enabled())

    def test_disabled_refuses_rather_than_falling_back(self):
        """There is no in-process converter left to fall back to (#173)."""
        with override_settings(DOCTOR_ENABLED=False):
            with self.assertRaises(doctor_client.DoctorError):
                doctor_client.convert_bitonal("http://in", "http://out")


@override_settings(**DOCTOR)
class TestRequestShape(SimpleTestCase):
    """What actually goes on the wire."""

    def test_posts_form_fields_to_the_bitonal_path(self):
        with patch(
            "scanning.doctor_client.requests.post",
            return_value=_response(200, SUCCESS_BODY),
        ) as post:
            result = doctor_client.convert_bitonal(
                "https://s3/in?sig=1", "https://s3/out?sig=2"
            )

        self.assertEqual(result, SUCCESS_BODY)
        (url,) = post.call_args.args
        self.assertEqual(
            url, "http://cl-doctor.example.svc:5050/convert/pdf/bitonal/"
        )
        # data=, not json= or params=: doctor reads request.POST and
        # ignores query parameters entirely.
        self.assertEqual(
            post.call_args.kwargs["data"],
            {
                "input_url": "https://s3/in?sig=1",
                "output_url": "https://s3/out?sig=2",
                "dpi": 200,
                "threshold": 160,
            },
        )
        self.assertEqual(post.call_args.kwargs["timeout"], (5, 300))

    def test_a_trailing_slash_on_the_host_is_not_doubled(self):
        with override_settings(DOCTOR_HOST="http://doctor:5050/"):
            with patch(
                "scanning.doctor_client.requests.post",
                return_value=_response(200, SUCCESS_BODY),
            ) as post:
                doctor_client.convert_bitonal("in", "out")
        self.assertEqual(
            post.call_args.args[0], "http://doctor:5050/convert/pdf/bitonal/"
        )

    def test_explicit_parameters_override_the_settings(self):
        with patch(
            "scanning.doctor_client.requests.post",
            return_value=_response(200, SUCCESS_BODY),
        ) as post:
            doctor_client.convert_bitonal("in", "out", dpi=300, threshold=0)

        data = post.call_args.kwargs["data"]
        self.assertEqual(data["dpi"], 300)
        # 0 is a real threshold, not "unset".
        self.assertEqual(data["threshold"], 0)

    def test_signed_urls_are_masked_in_the_log(self):
        with self.assertLogs("scanning.doctor_client", level="INFO") as logs:
            with patch(
                "scanning.doctor_client.requests.post",
                return_value=_response(200, SUCCESS_BODY),
            ):
                doctor_client.convert_bitonal(
                    "https://s3/in?X-Amz-Signature=secret",
                    "https://s3/out?X-Amz-Signature=alsosecret",
                )

        logged = "\n".join(logs.output)
        self.assertNotIn("secret", logged)
        self.assertIn("https://s3/in?***", logged)


@override_settings(**DOCTOR)
class TestErrorClassification(SimpleTestCase):
    """Which failures get another attempt, and which are the end."""

    def _raises(self, status, body, expected):
        with patch(
            "scanning.doctor_client.requests.post",
            return_value=_response(status, body),
        ):
            with self.assertRaises(expected) as caught:
                doctor_client.convert_bitonal("in", "out")
        return caught.exception

    def test_retryable_codes_are_transient(self):
        for code, status in [
            ("INTERNAL_ERROR", 500),
            ("INPUT_DOWNLOAD_FAILED", 502),
            ("RESULT_UPLOAD_FAILED", 502),
            ("INPUT_URL_EXPIRED", 502),
            ("RESULT_URL_EXPIRED", 502),
        ]:
            with self.subTest(code=code):
                exc = self._raises(
                    status,
                    {"success": False, "error_code": code, "msg": "nope"},
                    doctor_client.DoctorTransientError,
                )
                self.assertEqual(exc.error_code, code)

    def test_terminal_codes_are_not_retried(self):
        for code, status in [
            ("VALIDATION_FAILED", 400),
            ("INVALID_PDF", 400),
            ("PAGE_RANGE_INVALID", 400),
            ("EGRESS_BLOCKED", 400),
            ("INPUT_TOO_LARGE", 400),
            ("CONVERSION_FAILED", 500),
            ("PAGE_COUNT_MISMATCH", 500),
            ("PAGE_GEOMETRY_MISMATCH", 500),
        ]:
            with self.subTest(code=code):
                exc = self._raises(
                    status,
                    {"success": False, "error_code": code, "msg": "nope"},
                    doctor_client.DoctorError,
                )
                self.assertNotIsInstance(
                    exc, doctor_client.DoctorTransientError
                )
                self.assertEqual(exc.error_code, code)

    def test_an_unknown_code_is_terminal(self):
        """Better a volume that stops than one that retries forever."""
        exc = self._raises(
            400,
            {"success": False, "error_code": "SOMETHING_NEW", "msg": "?"},
            doctor_client.DoctorError,
        )
        self.assertNotIsInstance(exc, doctor_client.DoctorTransientError)

    def test_a_5xx_with_no_code_is_infrastructure(self):
        exc = self._raises(
            503,
            {"success": False, "msg": "unavailable"},
            doctor_client.DoctorTransientError,
        )
        self.assertEqual(exc.error_code, "BAD_GATEWAY")

    def test_a_non_json_5xx_is_transient(self):
        """An ingress 502 is in front of doctor, not from it."""
        with patch(
            "scanning.doctor_client.requests.post",
            return_value=_response(502, None, text="<html>bad gateway"),
        ):
            with self.assertRaises(
                doctor_client.DoctorTransientError
            ) as caught:
                doctor_client.convert_bitonal("in", "out")
        self.assertEqual(caught.exception.error_code, "BAD_GATEWAY")

    def test_a_non_json_4xx_is_terminal(self):
        with patch(
            "scanning.doctor_client.requests.post",
            return_value=_response(404, None, text="not found"),
        ):
            with self.assertRaises(doctor_client.DoctorError) as caught:
                doctor_client.convert_bitonal("in", "out")
        self.assertEqual(caught.exception.error_code, "BAD_RESPONSE")

    def test_a_json_list_is_not_a_summary(self):
        with patch(
            "scanning.doctor_client.requests.post",
            return_value=_response(200, []),
        ):
            with self.assertRaises(doctor_client.DoctorError) as caught:
                doctor_client.convert_bitonal("in", "out")
        self.assertEqual(caught.exception.error_code, "BAD_RESPONSE")

    def test_a_200_without_success_is_still_a_failure(self):
        self._raises(
            200,
            {"success": False, "error_code": "CONVERSION_FAILED", "msg": "x"},
            doctor_client.DoctorError,
        )


@override_settings(**DOCTOR)
class TestUnansweredRequests(SimpleTestCase):
    """The distinction the retry logic is built on.

    A read timeout does not stop doctor: its sync view runs to
    completion and the PUT lands even though we stopped listening.
    Resubmitting would pay for the shard twice, so this case must be
    distinguishable from every failure doctor actually answered.
    """

    def test_a_read_timeout_reports_that_we_never_got_an_answer(self):
        with patch(
            "scanning.doctor_client.requests.post",
            side_effect=requests.ReadTimeout("too slow"),
        ):
            with self.assertRaises(
                doctor_client.DoctorTransientError
            ) as caught:
                doctor_client.convert_bitonal("in", "out")

        self.assertEqual(
            caught.exception.error_code, doctor_client.UNANSWERED_ERROR_CODE
        )

    def test_a_connect_timeout_is_safe_to_resubmit(self):
        """It provably never reached the application, so nothing runs."""
        with patch(
            "scanning.doctor_client.requests.post",
            side_effect=requests.ConnectTimeout("no route"),
        ):
            with self.assertRaises(
                doctor_client.DoctorTransientError
            ) as caught:
                doctor_client.convert_bitonal("in", "out")

        self.assertEqual(caught.exception.error_code, "CONNECT_TIMEOUT")
        self.assertNotEqual(
            caught.exception.error_code, doctor_client.UNANSWERED_ERROR_CODE
        )

    def test_an_answered_error_is_never_reported_as_unanswered(self):
        """Doctor answered, so it is done and will not upload later."""
        with patch(
            "scanning.doctor_client.requests.post",
            return_value=_response(
                502,
                {
                    "success": False,
                    "error_code": "RESULT_UPLOAD_FAILED",
                    "msg": "s3 said no",
                },
            ),
        ):
            with self.assertRaises(
                doctor_client.DoctorTransientError
            ) as caught:
                doctor_client.convert_bitonal("in", "out")

        self.assertNotEqual(
            caught.exception.error_code, doctor_client.UNANSWERED_ERROR_CODE
        )
