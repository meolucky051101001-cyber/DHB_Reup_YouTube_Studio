import csv
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api_security
import database
import render_engine
import youtube_uploader


class ApiSecurityTests(unittest.TestCase):
    def test_allowed_hosts(self):
        for host in ("127.0.0.1:8686", "localhost:8686", "[::1]:8686", "localhost"):
            self.assertTrue(api_security.is_allowed_host(host), host)
        for host in ("", "evil.com", "evil.com:8686", "127.0.0.1.evil.com", "192.168.1.10:8686"):
            self.assertFalse(api_security.is_allowed_host(host), host)

    def test_allowed_origins(self):
        self.assertTrue(api_security.is_allowed_origin(""))
        self.assertTrue(api_security.is_allowed_origin("http://127.0.0.1:8686"))
        self.assertTrue(api_security.is_allowed_origin("http://localhost:8686"))
        for origin in ("http://evil.com", "null", "https://127.0.0.1.evil.com", "file://"):
            self.assertFalse(api_security.is_allowed_origin(origin), origin)

    def test_token_matches(self):
        token = api_security.generate_api_token()
        self.assertTrue(api_security.token_matches(token, token))
        self.assertFalse(api_security.token_matches("", token))
        self.assertFalse(api_security.token_matches(token + "x", token))


class SecretMaskingTests(unittest.TestCase):
    def test_public_settings_masks_secrets(self):
        public = database.public_settings(
            {
                "gemini_api_key": "AIza-secret-value",
                "openai_api_key": "",
                "gemini_model": "gemini-2.5-flash",
                "last_upload_started_epoch": 123,
            }
        )
        self.assertNotIn("gemini_api_key", public)
        self.assertNotIn("openai_api_key", public)
        self.assertTrue(public["gemini_api_key_configured"])
        self.assertFalse(public["openai_api_key_configured"])
        self.assertEqual(public["gemini_model"], "gemini-2.5-flash")
        self.assertEqual(public["last_upload_started_epoch"], 123)

    def test_public_channel_removes_token_and_cookie(self):
        row = {
            "id": 1,
            "name": "Kênh A",
            "oauth_token_json": '{"refresh_token":"secret"}',
            "cookie": "SID=secret",
            "proxy": "",
        }
        public = database.public_channel(row)
        self.assertNotIn("oauth_token_json", public)
        self.assertNotIn("cookie", public)
        self.assertTrue(public["has_oauth_token"])
        self.assertTrue(public["has_cookie"])
        self.assertEqual(public["name"], "Kênh A")

    def test_profile_channels_masks_secrets(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE youtube_channels ("
            "id INTEGER, name TEXT, profile_name TEXT, oauth_token_json TEXT, cookie TEXT, proxy TEXT)"
        )
        conn.execute(
            "INSERT INTO youtube_channels VALUES (?, ?, ?, ?, ?, ?)",
            (9, "Kênh B", "Profile A", '{"refresh_token":"secret"}', "SID=secret", ""),
        )
        with mock.patch.object(database, "get_conn", return_value=conn):
            rows = database.get_channels_by_profile("Profile A")
        self.assertEqual(len(rows), 1)
        self.assertNotIn("oauth_token_json", rows[0])
        self.assertNotIn("cookie", rows[0])
        self.assertTrue(rows[0]["has_oauth_token"])
        self.assertTrue(rows[0]["has_cookie"])

    def test_redact_secrets_text(self):
        secrets = ["AIzaVerySecretKey123", "cookie-value-123"]
        text = (
            "key=AIzaVerySecretKey123 cookie cookie-value-123 "
            "https://example.test/x?key=AIzaVerySecretKey123 "
            "Authorization: Bearer abc.def.ghi"
        )
        redacted = database.redact_secrets(text, secrets)
        self.assertNotIn("AIzaVerySecretKey123", redacted)
        self.assertNotIn("cookie-value-123", redacted)
        self.assertNotIn("abc.def.ghi", redacted)
        self.assertIn("***", redacted)

    def test_csv_export_redacts_secret_columns(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE app_settings (key TEXT, value TEXT)")
        conn.execute("CREATE TABLE youtube_channels (id INTEGER, name TEXT, oauth_token_json TEXT, cookie TEXT)")
        conn.execute("INSERT INTO app_settings VALUES ('gemini_api_key', 'AIza-secret-value')")
        conn.execute("INSERT INTO app_settings VALUES ('gemini_model', 'gemini-2.5-flash')")
        conn.execute(
            "INSERT INTO youtube_channels VALUES (1, 'Kênh A', '{\"refresh_token\":\"secret\"}', 'SID=secret')"
        )
        conn.commit()

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(database, "get_conn", return_value=conn):
                result = database.export_all_tables_csv(tmp, keep=2)
            export_dir = Path(result["path"])

            settings_rows = list(
                csv.reader((export_dir / "app_settings.csv").read_text(encoding="utf-8-sig").splitlines())
            )
            values = {row[0]: row[1] for row in settings_rows[1:]}
            self.assertEqual(values["gemini_api_key"], "")
            self.assertEqual(values["gemini_model"], "gemini-2.5-flash")

            channel_text = (export_dir / "youtube_channels.csv").read_text(encoding="utf-8-sig")
            self.assertNotIn("refresh_token", channel_text)
            self.assertNotIn("SID=secret", channel_text)
            self.assertIn("Kênh A", channel_text)


class RenderSafetyTests(unittest.TestCase):
    def test_custom_ffmpeg_args_quoted_filter(self):
        args = render_engine._build_custom_ffmpeg_args(
            'ffmpeg -i {input} -vf "scale=1080:1920" -c:v libx264 {output}',
            r"C:\videos\in.mp4",
            r"D:\out\out.mp4",
        )
        self.assertEqual(args[0], render_engine.FFMPEG_EXE)
        self.assertIn(r"C:\videos\in.mp4", args)
        self.assertIn(r"D:\out\out.mp4", args)
        self.assertIn("scale=1080:1920", args)
        # args[0] có thể là "ffmpeg" khi máy không có bản FFmpeg kế thừa; token thô ở đầu
        # command gốc phải bị bóc, không được lặp lại trong phần tham số.
        self.assertNotIn("ffmpeg", args[1:])

    def test_custom_ffmpeg_injection_stays_literal_argument(self):
        args = render_engine._build_custom_ffmpeg_args(
            "-i {input} & calc.exe {output}",
            r"C:\videos\in.mp4",
            r"D:\out\out.mp4",
        )
        self.assertIn("&", args)
        self.assertIn("calc.exe", args)
        self.assertNotIn("cmd", " ".join(args).lower())

    def test_custom_ffmpeg_rejects_empty(self):
        with self.assertRaises(ValueError):
            render_engine._build_custom_ffmpeg_args("   ", "in.mp4", "out.mp4")

    def test_output_filenames_are_unique(self):
        names = {render_engine._make_output_filename("yt", "clip", ".mp4") for _ in range(1000)}
        self.assertEqual(len(names), 1000)
        self.assertTrue(all(name.startswith("yt_") and name.endswith("_clip.mp4") for name in names))


class ApiUploadProxyTests(unittest.TestCase):
    def test_proxy_url_formats(self):
        self.assertEqual(youtube_uploader._proxy_url_from_channel({"proxy": ""}), "")
        self.assertEqual(
            youtube_uploader._proxy_url_from_channel({"proxy": "1.2.3.4:8080"}),
            "http://1.2.3.4:8080",
        )
        self.assertEqual(
            youtube_uploader._proxy_url_from_channel({"proxy": "1.2.3.4:8080:user:p@ss"}),
            "http://user:p%40ss@1.2.3.4:8080",
        )
        with self.assertRaises(ValueError):
            youtube_uploader._proxy_url_from_channel({"proxy": "1.2.3.4:8080:onlyuser"})

    def test_session_uses_channel_proxy(self):
        session = youtube_uploader._http_session_for_channel({"proxy": "1.2.3.4:8080:user:pass"})
        try:
            self.assertEqual(session.proxies["http"], "http://user:pass@1.2.3.4:8080")
            self.assertEqual(session.proxies["https"], "http://user:pass@1.2.3.4:8080")
        finally:
            session.close()


class AiKeyTests(unittest.TestCase):
    def test_gemini_key_uses_header_not_url(self):
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers") or {}
            return FakeResponse()

        import ai_engine

        with mock.patch.object(
            ai_engine,
            "get_settings",
            return_value={"gemini_api_key": "AIzaVerySecretKey123", "gemini_model": "gemini-2.5-flash"},
        ), mock.patch.object(ai_engine.requests, "post", side_effect=fake_post), mock.patch.object(
            ai_engine, "add_log"
        ):
            result = ai_engine.call_llm("hello", provider="gemini")

        self.assertEqual(result, "ok")
        self.assertNotIn("AIzaVerySecretKey123", captured["url"])
        self.assertNotIn("key=", captured["url"])
        self.assertEqual(captured["headers"].get("x-goog-api-key"), "AIzaVerySecretKey123")


class FrontendWiringTests(unittest.TestCase):
    def test_no_unauthenticated_window_exports(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("window.open(url", source)
        self.assertNotIn("window.open(`/api/", source)
        self.assertNotIn("window.location = '/api/", source)

    def test_api_token_header_wired(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("X-DHB-Token", source)
        self.assertIn("/api/auth/token", source)
        self.assertIn("/stream?token=", source)

    def test_xhr_upload_sends_token(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        marker = "xhr.open('POST', '/api/upload-video-files', true);"
        self.assertIn(marker, source)
        body = source.split(marker, 1)[1]
        self.assertIn("xhr.setRequestHeader('X-DHB-Token', token);", body[:400])


if __name__ == "__main__":
    unittest.main()
