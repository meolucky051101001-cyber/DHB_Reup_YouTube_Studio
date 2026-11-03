import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import database
import pipeline_manager


def _connection_factory(db_path):
    def connect():
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    return connect


class ContentReservationTests(unittest.TestCase):
    def _make_db(self, td):
        db_path = os.path.join(td, "test.sqlite")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE video_queue (
                id INTEGER PRIMARY KEY,
                project_name TEXT DEFAULT 'Default',
                claim_token TEXT DEFAULT '',
                content_key TEXT DEFAULT '',
                post_id TEXT DEFAULT '',
                source_url TEXT DEFAULT '',
                platform TEXT DEFAULT 'Unknown',
                status TEXT DEFAULT 'Pending',
                error_message TEXT DEFAULT '',
                published_url TEXT DEFAULT '',
                last_attempt_id INTEGER DEFAULT NULL
            );
            CREATE TABLE published_history (
                id INTEGER PRIMARY KEY,
                source_post_id TEXT NOT NULL,
                source_url TEXT DEFAULT '',
                channel_id INTEGER DEFAULT NULL,
                content_key TEXT DEFAULT '',
                published_url TEXT DEFAULT '',
                UNIQUE(source_post_id, channel_id)
            );
            CREATE TABLE content_reservations (
                content_key TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                queue_id INTEGER NOT NULL,
                claim_token TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'Active',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(content_key, channel_id)
            );
            CREATE TABLE upload_attempts (
                id INTEGER PRIMARY KEY,
                queue_id INTEGER NOT NULL,
                phase TEXT DEFAULT 'Uploading',
                outcome TEXT DEFAULT 'InProgress',
                error_message TEXT DEFAULT '',
                published_url TEXT DEFAULT '',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO video_queue (id, claim_token, post_id) VALUES (1, 'token-1', 'POST-1');
            INSERT INTO video_queue (id, claim_token, post_id) VALUES (2, 'token-2', 'POST-2');
            INSERT INTO video_queue (id, claim_token, post_id) VALUES (3, 'token-3', 'POST-3');
            """
        )
        conn.commit()
        conn.close()
        return db_path

    def test_parallel_items_get_one_reservation_per_content_and_channel(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = self._make_db(td)
            barrier = threading.Barrier(2)
            item = {"content_key": "sha256:identical", "post_id": ""}
            factory = _connection_factory(db_path)

            def reserve(queue_id, token):
                barrier.wait(timeout=5)
                return pipeline_manager._acquire_content_reservation(
                    queue_id, token, item, 7
                )[1]

            with mock.patch.object(pipeline_manager, "get_conn", side_effect=factory):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = [
                        pool.submit(reserve, 1, "token-1"),
                        pool.submit(reserve, 2, "token-2"),
                    ]
                    states = [future.result(timeout=10) for future in results]

                conn = sqlite3.connect(db_path)
                reservations = conn.execute(
                    "SELECT queue_id, channel_id, state FROM content_reservations"
                ).fetchall()
                conn.close()

            self.assertCountEqual(states, ["acquired", "busy"])
            self.assertEqual(len(reservations), 1)
            self.assertEqual(reservations[0][1], 7)

    def test_reservation_is_per_channel_and_force_still_obeys_lock(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = self._make_db(td)
            item = {"content_key": "sha256:identical", "post_id": "POST-1"}
            with mock.patch.object(pipeline_manager, "get_conn", side_effect=_connection_factory(db_path)):
                key, first = pipeline_manager._acquire_content_reservation(1, "token-1", item, 7)
                other_channel = pipeline_manager._acquire_content_reservation(2, "token-2", item, 8)[1]
                second_same_channel = pipeline_manager._acquire_content_reservation(
                    3, "token-3", item, 7, force_reupload=True
                )[1]
            self.assertEqual((key, first), ("sha256:identical", "acquired"))
            self.assertEqual(other_channel, "acquired")
            self.assertEqual(second_same_channel, "busy")

    def test_history_blocks_same_channel_but_allows_other_channel(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = self._make_db(td)
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO published_history (source_post_id, channel_id, content_key) VALUES (?, ?, ?)",
                ("POST-1", 7, "sha256:already-published"),
            )
            conn.commit()
            conn.close()
            item = {"content_key": "sha256:already-published", "post_id": "POST-1"}

            with mock.patch.object(pipeline_manager, "get_conn", side_effect=_connection_factory(db_path)):
                _, same_channel = pipeline_manager._acquire_content_reservation(1, "token-1", item, 7)
                _, other_channel = pipeline_manager._acquire_content_reservation(2, "token-2", item, 8)
                _, explicit_retry = pipeline_manager._acquire_content_reservation(
                    3, "token-3", item, 7, force_reupload=True
                )

            self.assertEqual(same_channel, "published")
            self.assertEqual(other_channel, "acquired")
            self.assertEqual(explicit_retry, "acquired")

    def test_fingerprint_persistence_dedupes_renamed_files_in_one_project(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = self._make_db(td)
            with mock.patch.object(pipeline_manager, "get_conn", side_effect=_connection_factory(db_path)):
                first = pipeline_manager._store_content_fingerprint(1, "token-1", "sha256:same-bytes")
                second = pipeline_manager._store_content_fingerprint(2, "token-2", "sha256:same-bytes")

            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT status, error_message FROM video_queue WHERE id = 2").fetchone()
            conn.close()
            self.assertEqual(first, "stored")
            self.assertEqual(second, "duplicate")
            self.assertEqual(row[0], "SkippedDuplicate")
            self.assertIn("video #1", row[1])

    def test_init_db_adds_identity_schema_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "migration.sqlite")
            factory = _connection_factory(db_path)
            with mock.patch.object(database, "get_conn", side_effect=factory), \
                    mock.patch.object(database, "add_log"):
                database.init_db()
                database.init_db()

            conn = sqlite3.connect(db_path)
            queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(video_queue)")}
            history_columns = {row[1] for row in conn.execute("PRAGMA table_info(published_history)")}
            reservation = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_reservations'"
            ).fetchone()
            index = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_published_history_content_channel'"
            ).fetchone()
            conn.close()
            self.assertIn("content_key", queue_columns)
            self.assertIn("content_key", history_columns)
            self.assertIsNotNone(reservation)
            self.assertIsNotNone(index)

    def test_legacy_identity_report_is_read_only_and_counts_uncertain_rows(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "report.sqlite")
            file_path = os.path.join(td, "legacy.mp4")
            with open(file_path, "wb") as handle:
                handle.write(b"legacy")
            with mock.patch.object(database, "get_conn", side_effect=_connection_factory(db_path)), \
                    mock.patch.object(database, "add_log"):
                database.init_db()

            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO video_queue (project_name, source_type, platform, post_id, source_url, input_video_path) "
                "VALUES ('Legacy', 'File', 'LocalFile', '', '', ?)",
                (file_path,),
            )
            conn.execute(
                "INSERT INTO video_queue (project_name, source_type, platform, post_id, source_url) "
                "VALUES ('Legacy', 'URL', 'Unknown', '', 'not a valid URL')"
            )
            conn.execute(
                "INSERT INTO published_history (source_post_id, source_url, channel_id) "
                "VALUES ('legacy-post', '', NULL)"
            )
            conn.commit()
            conn.close()

            with mock.patch.object(database, "get_conn", side_effect=_connection_factory(db_path)):
                report = database.content_identity_migration_report()

            conn = sqlite3.connect(db_path)
            unchanged = conn.execute(
                "SELECT COUNT(*) FROM video_queue WHERE content_key = ''"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(report["local_file_hash_pending"], 1)
            self.assertEqual(report["invalid_source_url"], 1)
            self.assertEqual(report["history_missing_content_key"], 1)
            self.assertEqual(report["history_missing_channel"], 1)
            self.assertEqual(unchanged, 2)

    def test_restart_keeps_ambiguous_reservation_and_releases_safe_work(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = self._make_db(td)
            conn = sqlite3.connect(db_path)
            conn.execute("UPDATE video_queue SET status = 'Uploading -> Channel' WHERE id = 1")
            conn.execute("UPDATE video_queue SET status = 'Rendering...' WHERE id = 2")
            conn.execute("INSERT INTO upload_attempts (id, queue_id) VALUES (10, 1)")
            conn.execute("INSERT INTO upload_attempts (id, queue_id) VALUES (11, 2)")
            conn.execute("UPDATE video_queue SET last_attempt_id = 10 WHERE id = 1")
            conn.execute("UPDATE video_queue SET last_attempt_id = 11 WHERE id = 2")
            conn.execute(
                "INSERT INTO content_reservations (content_key, channel_id, queue_id, claim_token) "
                "VALUES ('sha256:ambiguous', 7, 1, 'token-1')"
            )
            conn.execute(
                "INSERT INTO content_reservations (content_key, channel_id, queue_id, claim_token) "
                "VALUES ('sha256:safe', 7, 2, 'token-2')"
            )
            conn.commit()
            conn.close()

            manager = pipeline_manager.PipelineManager()
            with mock.patch.object(pipeline_manager, "get_conn", side_effect=_connection_factory(db_path)), \
                    mock.patch.object(pipeline_manager, "add_log"):
                manager.clear_stale_claims()

            conn = sqlite3.connect(db_path)
            states = dict(conn.execute("SELECT id, status FROM video_queue"))
            reservations = conn.execute(
                "SELECT content_key, state FROM content_reservations"
            ).fetchall()
            attempts = dict(conn.execute("SELECT queue_id, outcome FROM upload_attempts"))
            conn.close()
            self.assertEqual(states[1], "NeedsReview")
            self.assertEqual(states[2], "Pending")
            self.assertEqual(reservations, [("sha256:ambiguous", "NeedsReview")])
            self.assertEqual(attempts, {1: "NeedsReview", 2: "NotSent"})


if __name__ == "__main__":
    unittest.main()
