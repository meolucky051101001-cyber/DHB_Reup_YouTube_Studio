import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database
import pipeline_manager as pm
import youtube_uploader as yu

UC_A = "UCaaaaaaaaaaaaaaaaaaaaaa"
UC_B = "UCbbbbbbbbbbbbbbbbbbbbbb"


def _channel_rows():
    return [
        (1, "Kênh A", UC_A, "@kenha", "Mặc định", ""),
        (2, "Kênh B", UC_B, "@kenhb", "Mặc định", ""),
    ]


class ChannelGroupResolutionTests(unittest.TestCase):
    def _manager(self, rows):
        created = []

        def make_conn():
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE youtube_channels ("
                "id INTEGER PRIMARY KEY, name TEXT, channel_id TEXT, handle TEXT, "
                "group_name TEXT, profile_name TEXT)"
            )
            conn.executemany(
                "INSERT INTO youtube_channels VALUES (?, ?, ?, ?, ?, ?)", rows
            )
            created.append(conn)
            return conn

        patcher = mock.patch.object(pm, "get_conn", side_effect=make_conn)
        patcher.start()
        self.addCleanup(patcher.stop)
        log_patcher = mock.patch.object(pm, "add_log")
        self.mock_add_log = log_patcher.start()
        self.addCleanup(log_patcher.stop)
        self.addCleanup(lambda: [c.close() for c in created])
        return pm.PipelineManager()

    def test_group_rotation_uses_video_id_not_batch_position(self):
        manager = self._manager(_channel_rows())
        item = {"id": 4, "target_type": "GROUP", "target_group_name": "Mặc định", "title": "V4"}
        first = manager._resolve_target_channel(item, 0)
        second = manager._resolve_target_channel(item, 999)
        self.assertEqual(first["id"], 1)
        self.assertEqual(second["id"], 1)

        next_item = {"id": 5, "target_type": "GROUP", "target_group_name": "Mặc định", "title": "V5"}
        self.assertEqual(manager._resolve_target_channel(next_item, 0)["id"], 2)

    def test_rotation_logs_the_selected_channel(self):
        manager = self._manager(_channel_rows())
        item = {"id": 6, "target_type": "GROUP", "target_group_name": "Mặc định", "title": "V6"}
        selected = manager._resolve_target_channel(item, 0)
        self.assertEqual(selected["name"], "Kênh A")
        args, kwargs = self.mock_add_log.call_args
        self.assertIn("video #6", args[1])
        self.assertIn("Kênh A", args[1])
        self.assertEqual(kwargs.get("video_title"), "V6")

    def test_legacy_default_group_no_longer_falls_back(self):
        manager = self._manager(_channel_rows())
        item = {"id": 1, "target_type": "GROUP", "target_group_name": "Default"}
        with self.assertRaises(RuntimeError) as ctx:
            manager._resolve_target_channel(item, 0)
        self.assertIn("Default", str(ctx.exception))
        self.assertIn("chưa có kênh nào", str(ctx.exception))

    def test_missing_group_fails_closed(self):
        manager = self._manager(_channel_rows())
        item = {"id": 1, "target_type": "GROUP", "target_group_name": "Nhóm lạ"}
        with self.assertRaises(RuntimeError):
            manager._resolve_target_channel(item, 0)


class GroupDefaultNormalizationTests(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE channel_groups (name TEXT PRIMARY KEY, description TEXT DEFAULT '');
            CREATE TABLE youtube_channels (id INTEGER PRIMARY KEY, group_name TEXT);
            CREATE TABLE video_queue (id INTEGER PRIMARY KEY, target_group_name TEXT);
            CREATE TABLE monitors (id INTEGER PRIMARY KEY, target_group_name TEXT);
            """
        )
        return conn

    def test_merges_legacy_default_into_canonical_group(self):
        conn = self._conn()
        conn.execute("INSERT INTO channel_groups (name) VALUES ('Mặc định')")
        conn.execute("INSERT INTO channel_groups (name) VALUES ('Default')")
        conn.execute("INSERT INTO youtube_channels (group_name) VALUES ('Default')")
        conn.execute("INSERT INTO video_queue (target_group_name) VALUES ('Default')")
        conn.execute("INSERT INTO monitors (target_group_name) VALUES ('Default')")

        changed = database.normalize_legacy_group_defaults(conn)

        self.assertGreater(changed, 0)
        groups = [r[0] for r in conn.execute("SELECT name FROM channel_groups")]
        self.assertEqual(groups, ["Mặc định"])
        for table, column in (
            ("youtube_channels", "group_name"),
            ("video_queue", "target_group_name"),
            ("monitors", "target_group_name"),
        ):
            values = [r[0] for r in conn.execute(f"SELECT {column} FROM {table}")]
            self.assertEqual(values, ["Mặc định"])

    def test_renames_group_when_no_canonical_group_exists(self):
        conn = self._conn()
        conn.execute("INSERT INTO channel_groups (name) VALUES ('Default')")
        database.normalize_legacy_group_defaults(conn)
        groups = [r[0] for r in conn.execute("SELECT name FROM channel_groups")]
        self.assertEqual(groups, ["Mặc định"])


class ChecksProbeErrorTests(unittest.TestCase):
    def test_probe_error_is_reported_instead_of_swallowed(self):
        class BoomPage:
            def locator(self, selector):
                raise RuntimeError("dom boom")

        state = yu.check_copyright_and_violations(BoomPage())
        self.assertFalse(state["has_violation"])
        self.assertIn("dom boom", state["probe_error"])


class FrontendTokenRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_api_fetch_retries_once_after_401(self):
        self.assertIn("function resetApiToken()", self.source)
        self.assertIn("if (res.status === 401)", self.source)
        self.assertIn("const safeFetch = async () =>", self.source)
        self.assertIn("resetApiToken();\n    res = await safeFetch();", self.source)
        self.assertIn("ensureApiToken(true)", self.source)

    def test_excel_import_uses_retrying_fetch(self):
        start = self.source.index("const sendImport = (previewOnly)")
        body = self.source[start:start + 400]
        self.assertIn("return apiFetch('/api/queue/import-excel'", body)
        self.assertNotIn("return fetch('/api/queue/import-excel'", body)


if __name__ == "__main__":
    unittest.main()
