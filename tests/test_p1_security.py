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

    def test_mutation_origin_must_match_host_and_port(self):
        self.assertTrue(api_security.is_same_local_origin("http://127.0.0.1:8686", "127.0.0.1:8686"))
        self.assertTrue(api_security.is_same_local_origin("http://localhost:8686", "localhost:8686"))
        self.assertFalse(api_security.is_same_local_origin("", "127.0.0.1:8686"))
        self.assertFalse(api_security.is_same_local_origin("http://localhost:8686", "127.0.0.1:8686"))
        self.assertFalse(api_security.is_same_local_origin("http://localhost:9000", "localhost:8686"))

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
        self.assertNotIn("last_upload_started_epoch", public)

    def test_settings_whitelist_and_type_validation(self):
        self.assertEqual(
            database.validate_public_settings({"max_concurrent_threads": 3, "hide_browser": True}),
            {"max_concurrent_threads": 3, "hide_browser": True},
        )
        with self.assertRaises(ValueError):
            database.validate_public_settings({"unknown_secret": "do-not-store"})
        with self.assertRaises(ValueError):
            database.validate_public_settings({"max_concurrent_threads": "3"})
        with self.assertRaises(ValueError):
            database.validate_public_settings({"max_uploads_per_run": 1000})

    def test_secrets_require_separate_payload(self):
        with self.assertRaises(ValueError):
            database.validate_public_settings({"gemini_api_key": "new-key"})
        self.assertEqual(database.validate_secret_settings({"gemini_api_key": " new-key "}), {
            "gemini_api_key": "new-key",
        })

    def test_proxy_password_is_masked_in_public_channel(self):
        public = database.public_channel({"id": 2, "name": "Kênh", "proxy": "1.2.3.4:8080:user:pass123"})
        self.assertEqual(public["proxy"], "1.2.3.4:8080:user:***")

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

    def test_redact_query_json_and_proxy_credentials(self):
        text = (
            "https://service.test/cb?access_token=oauth-secret "
            '{"refresh_token":"refresh-secret"} '
            "http://user:proxy-secret@example.test:8080 "
            "1.2.3.4:8080:user:colon-secret "
            "Authorization: Basic dXNlcjpwYXNz"
        )
        redacted = database.redact_secrets(text, [])
        for secret in ("oauth-secret", "refresh-secret", "proxy-secret", "colon-secret", "dXNlcjpwYXNz"):
            self.assertNotIn(secret, redacted)

    def test_redact_fails_closed_when_secret_source_unreadable(self):
        with mock.patch.object(
            database, "collect_secret_values",
            side_effect=database.SecretCollectionError("db down"),
        ):
            self.assertEqual(database.redact_secrets("token=abc123"), "***")
        self.assertEqual(database.redact_secrets("token=abc123", []), "token=***")

    def test_collect_secret_values_raises_when_settings_unreadable(self):
        database._invalidate_secret_cache()
        try:
            with mock.patch.object(database, "get_settings", side_effect=RuntimeError("db down")):
                with self.assertRaises(database.SecretCollectionError):
                    database.collect_secret_values()
        finally:
            database._invalidate_secret_cache()

    def test_collect_secret_values_raises_when_channel_store_unreadable(self):
        with mock.patch.object(database, "get_conn", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(database.SecretCollectionError):
                database.collect_secret_values(settings={"gemini_api_key": "abcdef"})

    def test_add_log_persists_structured_run_context_and_safe_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "logs.sqlite"

            def connect():
                result = sqlite3.connect(db_path)
                result.row_factory = sqlite3.Row
                return result

            conn = connect()
            conn.execute(
                "CREATE TABLE system_logs (id INTEGER PRIMARY KEY, module TEXT, channel_name TEXT, video_title TEXT, "
                "status TEXT, message TEXT, created_at TEXT, run_id TEXT, queue_id INTEGER, attempt_id INTEGER, "
                "phase TEXT, error_code TEXT)"
            )
            # Redaction đọc bí mật từ các bảng này; thiếu bảng sẽ fail-closed thành "***".
            conn.execute("CREATE TABLE app_settings (key TEXT, value TEXT)")
            conn.execute("CREATE TABLE youtube_channels (oauth_token_json TEXT, cookie TEXT, proxy TEXT)")
            conn.execute("CREATE TABLE chrome_profiles (proxy TEXT)")
            conn.commit()
            conn.close()
            database.clear_log_context()
            database.set_log_context(run_id="run-1", queue_id=7, attempt_id=12, phase="Uploading")
            database._invalidate_secret_cache()
            try:
                with mock.patch.object(database, "get_conn", side_effect=connect):
                    database.add_log("Test", "Authorization: Bearer secret-token", "ERROR")
                conn = connect()
                row = conn.execute("SELECT * FROM system_logs").fetchone()
                self.assertEqual(row["run_id"], "run-1")
                self.assertEqual(row["queue_id"], 7)
                self.assertEqual(row["attempt_id"], 12)
                self.assertEqual(row["phase"], "Uploading")
                self.assertEqual(row["error_code"], "ERR_UNCLASSIFIED")
                self.assertNotIn("secret-token", row["message"])
                conn.close()
            finally:
                database.clear_log_context()

    def test_existing_log_table_receives_phase6_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "data" / "app.sqlite"
            db_path.parent.mkdir(parents=True)
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE system_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, module TEXT DEFAULT 'System', "
                "channel_name TEXT DEFAULT '', video_title TEXT DEFAULT '', status TEXT DEFAULT 'INFO', "
                "message TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.commit()
            conn.close()
            original = {
                "APP_DB_PATH": database.APP_DB_PATH,
                "APP_DATA_DIR": database.APP_DATA_DIR,
                "APP_TEMP_DIR": database.APP_TEMP_DIR,
                "APP_PROFILES_DIR": database.APP_PROFILES_DIR,
            }
            database.APP_DB_PATH = db_path
            database.APP_DATA_DIR = root / "data"
            database.APP_TEMP_DIR = root / "temp"
            database.APP_PROFILES_DIR = root / "profiles"
            for path in (database.APP_TEMP_DIR, database.APP_PROFILES_DIR):
                path.mkdir(parents=True, exist_ok=True)
            try:
                with mock.patch.object(database, "backup_database_on_startup"), \
                     mock.patch.object(database, "sync_from_dhb_facebook"):
                    database.init_db()
                conn = database.get_conn()
                columns = {row[1] for row in conn.execute("PRAGMA table_info(system_logs)")}
                conn.close()
                self.assertTrue({"run_id", "queue_id", "attempt_id", "phase", "error_code"}.issubset(columns))
            finally:
                for name, value in original.items():
                    setattr(database, name, value)

    def test_csv_export_redacts_secret_columns(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE app_settings (key TEXT, value TEXT)")
        conn.execute("CREATE TABLE youtube_channels (id INTEGER, name TEXT, oauth_token_json TEXT, cookie TEXT, proxy TEXT)")
        conn.execute("CREATE TABLE chrome_profiles (name TEXT, path TEXT, proxy TEXT)")
        conn.execute("INSERT INTO app_settings VALUES ('gemini_api_key', 'AIza-secret-value')")
        conn.execute("INSERT INTO app_settings VALUES ('gemini_model', 'gemini-2.5-flash')")
        conn.execute(
            "INSERT INTO youtube_channels VALUES (1, 'Kênh A', '{\"refresh_token\":\"secret\"}', 'SID=secret', '')"
        )
        conn.execute("INSERT INTO chrome_profiles VALUES ('Profile A', 'C:/Profiles/A', '1.2.3.4:8080:user:proxy-pass-987')")
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
            profile_text = (export_dir / "chrome_profiles.csv").read_text(encoding="utf-8-sig")
            self.assertNotIn("proxy-pass-987", profile_text)


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
