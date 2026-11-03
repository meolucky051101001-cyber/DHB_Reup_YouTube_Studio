import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import youtube_uploader as yu


CHANNEL_ID = "UC1234567890abcdefghijkl"
VIDEO_ID = "dQw4w9WgXcQ"


class _Response:
    def __init__(self, status_code, *, headers=None, body=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


class _ResumableSession:
    def __init__(self, put_steps):
        self.put_steps = list(put_steps)
        self.put_calls = []
        self.get_calls = []
        self.post_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return _Response(
            200,
            headers={"Location": "https://www.googleapis.com/upload/youtube/v3/videos/session"},
        )

    def put(self, url, **kwargs):
        self.put_calls.append((url, kwargs))
        step = self.put_steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return _Response(
            200,
            body={
                "items": [{
                    "id": VIDEO_ID,
                    "status": {
                        "uploadStatus": "processed",
                        "privacyStatus": "private",
                        "selfDeclaredMadeForKids": False,
                        "containsSyntheticMedia": False,
                    },
                    "processingDetails": {"processingStatus": "succeeded"},
                }]
            },
        )


class UrlAndChecksTests(unittest.TestCase):
    def test_video_url_parser_checks_host_route_and_id(self):
        self.assertEqual(
            yu.normalize_published_video_url(f"https://www.youtube.com/watch?v={VIDEO_ID}&t=4"),
            f"https://youtu.be/{VIDEO_ID}",
        )
        self.assertEqual(
            yu.normalize_published_video_url(f"https://youtube.com/shorts/{VIDEO_ID}"),
            f"https://youtu.be/{VIDEO_ID}",
        )
        self.assertEqual(yu.normalize_published_video_url(f"https://youtube.com.evil/watch?v={VIDEO_ID}"), "")
        self.assertEqual(
            yu.normalize_published_video_url(f"https://evil.example/?next=https://youtu.be/{VIDEO_ID}"),
            "",
        )
        self.assertEqual(yu.normalize_published_video_url("https://youtu.be/short"), "")

    def test_checks_state_is_explicit_and_missing_checks_panel_is_unknown(self):
        self.assertEqual(yu.classify_checks_text("No issues found")["result"], "safe")
        self.assertEqual(
            yu.classify_checks_text("No issues found; the video is still processing")["result"],
            "unknown",
        )
        self.assertEqual(
            yu.classify_checks_text("Copyright-protected content found")["result"],
            "violation",
        )

        class MissingLocator:
            count = lambda self: 0

        class MissingChecksPage:
            def locator(self, _selector):
                return type("FirstLocator", (), {"first": MissingLocator()})()

        result = yu.check_copyright_and_violations(MissingChecksPage())
        self.assertEqual(result["result"], "unknown")
        self.assertIn("Checks", result["probe_error"])

    def test_schedule_echo_must_match_requested_local_time(self):
        from datetime import datetime

        expected = datetime(2026, 11, 5, 19, 30)
        self.assertTrue(yu._schedule_values_match("05/11/2026", "19:30", expected))
        self.assertFalse(yu._schedule_values_match("06/11/2026", "19:30", expected))
        self.assertFalse(yu._schedule_values_match("05/11/2026", "20:30", expected))

    def test_previous_studio_video_url_is_not_used_as_publish_evidence(self):
        old_url = "https://studio.youtube.com/video/9bZkp7q19f0/edit"

        class EmptyLocator:
            first = None

            def count(self):
                return 0

        EmptyLocator.first = EmptyLocator()

        class Page:
            url = old_url

            def locator(self, _selector):
                return EmptyLocator()

        self.assertEqual(yu.extract_published_video_url(Page(), previous_url=old_url), "")


class PreflightAndProxyTests(unittest.TestCase):
    def test_declared_antidetect_profile_never_falls_back_to_local_chrome(self):
        with self.assertRaises(RuntimeError):
            yu._channel_antidetect_id({"profile_path": "ANTIDETECT://", "profile_name": ""})

    def test_malformed_proxy_fails_closed(self):
        for value in ("proxy:not-a-port", "proxy:70000", "proxy:8080:user", "proxy:8080:user:"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                yu._parse_channel_proxy({"proxy": value})

    def test_api_unsupported_metadata_is_rejected_before_file_exists(self):
        with self.assertRaises(RuntimeError) as raised:
            yu.validate_upload_preflight(
                {"title": "Video", "age_restriction": True},
                {"name": "Kênh", "channel_id": CHANNEL_ID, "upload_mode": "youtube_api"},
            )
        self.assertIn("giới hạn độ tuổi", str(raised.exception))
        with self.assertRaises(RuntimeError) as raised:
            yu.validate_upload_preflight(
                {"title": "Video", "is_premiere": True},
                {"name": "Kênh", "channel_id": CHANNEL_ID, "upload_mode": "youtube_api"},
            )
        self.assertIn("Công chiếu", str(raised.exception))


class ResumableUploadTests(unittest.TestCase):
    def _metadata(self):
        return {
            "title": "Clip",
            "description": "",
            "tags": [],
            "category_id": "22",
            "made_for_kids": False,
            "altered_content": False,
            "age_restriction": False,
            "privacy_status": "private",
            "schedule_time": "",
            "is_premiere": False,
            "pin_comment": "",
        }

    def _channel(self):
        return {
            "name": "Kênh thử",
            "channel_id": CHANNEL_ID,
            "oauth_token_json": '{"refresh_token":"refresh"}',
        }

    def _run(self, session, path):
        with mock.patch.object(yu, "_refresh_google_access_token", return_value="access"), \
                mock.patch.object(yu, "_fetch_oauth_channel_identity", return_value={
                    "channel_id": CHANNEL_ID, "name": "Kênh thử"
                }), \
                mock.patch.object(yu, "get_settings", return_value={}), \
                mock.patch.object(yu, "CHUNK_SIZE", 4), \
                mock.patch.object(yu.time, "sleep"):
            return yu._upload_via_youtube_api_with_session(
                str(path), self._metadata(), self._channel(), session=session
            )

    def test_partial_server_range_after_disconnect_resumes_at_confirmed_offset(self):
        session = _ResumableSession([
            _Response(308, headers={"Range": "bytes=0-3"}),
            requests.Timeout("response lost after server accepted part of chunk"),
            _Response(308, headers={"Range": "bytes=0-5"}),
            _Response(200, body={"id": VIDEO_ID}),
        ])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "clip.mp4"
            path.write_bytes(b"0123456789")
            result = self._run(session, path)

        self.assertEqual(result, f"https://youtu.be/{VIDEO_ID}")
        media_ranges = [
            kwargs["headers"].get("Content-Range")
            for _url, kwargs in session.put_calls
            if kwargs["headers"].get("Content-Range") != "bytes */10"
        ]
        self.assertEqual(media_ranges, ["bytes 0-3/10", "bytes 4-7/10", "bytes 6-9/10"])
        self.assertEqual(session.get_calls[0][1]["params"]["id"], VIDEO_ID)

    def test_lost_final_chunk_response_uses_status_query_without_resending(self):
        session = _ResumableSession([
            requests.ConnectionError("final response dropped"),
            _Response(201, body={"id": VIDEO_ID}),
        ])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "clip.mp4"
            path.write_bytes(b"data")
            result = self._run(session, path)

        self.assertEqual(result, f"https://youtu.be/{VIDEO_ID}")
        self.assertEqual(len(session.put_calls), 2)
        self.assertEqual(session.put_calls[0][1]["headers"]["Content-Range"], "bytes 0-3/4")
        self.assertEqual(session.put_calls[1][1]["headers"]["Content-Range"], "bytes */4")

    def test_range_header_is_validated(self):
        self.assertEqual(yu._read_resumable_offset(_Response(308, headers={"Range": "bytes=0-5"}), 10), 6)
        self.assertEqual(yu._read_resumable_offset(_Response(308), 10), 0)
        with self.assertRaises(RuntimeError):
            yu._read_resumable_offset(_Response(308, headers={"Range": "nonsense"}), 10)


if __name__ == "__main__":
    unittest.main()
