"""Campaign tests use a temporary database and a mocked batch worker."""

from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase, mock

from fastapi import HTTPException

import campaign_sources
import campaigns as c
import database
import scanner_downloader
from validate_review_release import rehearse


class PublishingCampaignTests(TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.path = Path(workspace.name)
        db = mock.patch.multiple(
            database, APP_DB_PATH=self.path / "fixture.sqlite", APP_DATA_DIR=self.path,
            sync_from_dhb_facebook=mock.DEFAULT,
        )
        db.start()
        self.addCleanup(db.stop)
        network = mock.patch("requests.sessions.Session.request", side_effect=AssertionError("Offline fixture"))
        network.start()
        self.addCleanup(network.stop)
        database.init_db()
        database.init_db()
        self.clock = datetime(2026, 11, 26, 1, 59, tzinfo=timezone.utc)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO chrome_profiles(name,path) VALUES ('profile-fixture','fixture')")
            conn.execute(
                "INSERT INTO youtube_channels(id,name,channel_id,profile_name,studio_category_mode) "
                "VALUES (1,'Fixture',?,'profile-fixture','keep')",
                ("UC" + "x" * 22,),
            )
            for item_id in (1, 2):
                source = self.path / f"{item_id}.mp4"
                source.write_bytes(b"synthetic source")
                conn.execute(
                    "INSERT INTO video_queue(id,title,source_type,platform,input_video_path,target_channel_id,"
                    "visibility,render_mode,upload_to,group_schema_version,target_group_names_json) "
                    "VALUES (?,?,'File','LocalFile',?,1,'private','none','Video',1,'[]')",
                    (item_id, f"Video {item_id}", str(source)),
                )
        database.init_db()
        list_id = c.create_list(c.ContentList(name="Nguồn", queue_ids=[1, 2]))["id"]
        self.campaign_id = c.create_campaign(c.Campaign(
            name="Fixture", list_id=list_id, channel_id=1, times=["09:00", "16:00"]
        ))["id"]

    def enable(self):
        preview = c.preview_campaign(self.campaign_id)
        self.assertEqual(preview["problems"], [])
        with mock.patch.object(c, "datetime", wraps=datetime) as clock:
            clock.now.return_value = self.clock
            c.enable_campaign(self.campaign_id, c.Confirmation(revision=preview["revision"]))

    def test_two_slots_one_dispatch_each(self):
        self.enable()
        done = Event()
        sent = []

        def fake_worker(ids, *_args):
            sent.extend(ids)
            with closing(database.get_conn()) as conn, conn:
                conn.execute("UPDATE video_queue SET status='Completed',published_url=? WHERE id=?",
                             (f"https://youtu.be/fixture000{ids[0]}", ids[0]))
            done.set()

        class InlineThread:
            def __init__(self, *, target, args, daemon):
                self.target = target
                self.args = args

            def start(self):
                # A synchronous worker would deadlock the lifecycle lock. This stub runs outside it.
                self.__class__.pending.append((self.target, self.args))

        InlineThread.pending = []
        with (
            mock.patch("pipeline_manager.threading.Thread", InlineThread),
            mock.patch.object(c.pipeline_manager, "_batch_worker", side_effect=fake_worker),
        ):
            for offset in (60, 7 * 3600 + 60):
                due = self.clock.timestamp() + offset
                c.run_campaign_tick(due)
                self.assertEqual(len(InlineThread.pending), 1)
                target, args = InlineThread.pending.pop()
                target(*args)
                self.assertTrue(done.is_set())
                done.clear()
                c.run_campaign_tick(due + 15)
                self.assertFalse(InlineThread.pending)
        self.assertEqual(sent, [1, 2])
        self.assertEqual([run["state"] for run in c.get_campaigns()["runs"]], ["completed", "completed"])

    def test_approved_source_change_blocks_batch(self):
        self.enable()
        source = self.path / "1.mp4"
        stamp = source.stat()
        source.write_bytes(b"x" * stamp.st_size)
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        with mock.patch.object(c.pipeline_manager, "start_batch") as upload:
            c.run_campaign_tick(self.clock.timestamp() + 60)
            upload.assert_not_called()
        data = c.get_campaigns()
        self.assertEqual(data["campaigns"][0]["state"], "paused")
        self.assertEqual(data["runs"][0]["state"], "blocked")
        self.assertEqual(data["runs"][0]["queue_id"], 1)
        self.assertIn("SHA-256", data["runs"][0]["message"])

    def test_stale_confirmation_cannot_enable(self):
        preview = c.preview_campaign(self.campaign_id)
        with closing(database.get_conn()) as conn, conn:
            conn.execute("UPDATE video_queue SET title='Edited' WHERE id=1")
        with self.assertRaises(HTTPException) as result:
            c.enable_campaign(self.campaign_id, c.Confirmation(revision=preview["revision"]))
        self.assertEqual(result.exception.status_code, 409)

    def test_http_api_requires_token_and_preserves_draft_confirmation(self):
        from fastapi.testclient import TestClient

        with mock.patch.dict(os.environ, {"DHB_DISABLE_BACKGROUND_TASKS": "1"}):
            import app

        base = "http://127.0.0.1:8686"
        route = "/api/campaigns/" + str(self.campaign_id)
        with TestClient(app.app, base_url=base) as client:
            self.assertEqual(client.get(route + "/preview").status_code, 401)
            headers = {"X-DHB-Token": app.API_TOKEN, "Origin": base}
            preview = client.get(route + "/preview", headers=headers)
            self.assertEqual(preview.status_code, 200)
            self.assertTrue(preview.json()["revision"])
            self.assertEqual(preview.json()["items"][0]["youtube_channel_id"], "UC" + "x" * 22)
            result = client.post(route + "/enable", headers=headers, json={"revision": preview.json()["revision"]})
            self.assertEqual(result.status_code, 200)
            listing = client.get("/api/campaigns", headers=headers).json()
            self.assertEqual(listing["campaigns"][0]["state"], "enabled")
            self.assertNotIn("snapshots_json", listing["campaigns"][0])
            self.assertEqual(client.post(route + "/pause", headers=headers).json()["state"], "paused")

    def test_resume_after_sleep_marks_missed_slot(self):
        self.enable()
        with mock.patch.object(c.pipeline_manager, "start_batch") as upload:
            c.run_campaign_tick(self.clock.timestamp() + 121)
            upload.assert_not_called()
        self.assertEqual(c.get_campaigns()["runs"][0]["state"], "missed")

    def test_recovers_unaccepted_submission_without_reusing_the_token(self):
        self.enable()
        due = self.clock.timestamp() + 60
        with mock.patch.object(c.pipeline_manager, "start_batch", return_value={"ok": False}) as upload:
            c.run_campaign_tick(due)
            c.run_campaign_tick(due + 15)
            self.assertEqual(upload.call_count, 2)
            self.assertNotEqual(upload.call_args_list[0].kwargs["preview_token"], upload.call_args.kwargs["preview_token"])
        self.assertEqual(len(c.get_campaigns()["runs"]), 1)

    def test_interrupted_accepted_upload_is_not_started_again(self):
        self.enable()
        due = self.clock.timestamp() + 60
        with mock.patch.object(c.pipeline_manager, "start_batch", return_value={"ok": True}) as upload:
            c.run_campaign_tick(due)
            with closing(database.get_conn()) as conn, conn:
                token = upload.call_args.kwargs["preview_token"]
                conn.execute("UPDATE upload_approvals SET state='interrupted',run_token='old-run' WHERE token=?", (token,))
                conn.execute("UPDATE video_queue SET status='NeedsReview' WHERE id=1")
            c.run_campaign_tick(due + 15)
            self.assertEqual(upload.call_count, 1)
        result = c.get_campaigns()
        self.assertEqual(result["campaigns"][0]["state"], "paused")
        self.assertEqual(result["runs"][0]["state"], "needs_review")

    def test_two_campaigns_sharing_a_slot_wait_for_global_cooldown(self):
        self.enable()
        with closing(database.get_conn()) as conn, conn:
            first = conn.execute("SELECT config_json,snapshots_json,checked_at FROM publishing_campaigns").fetchone()
            conn.execute("INSERT INTO publishing_campaigns(config_json,state,snapshots_json,checked_at) "
                         "VALUES (?,'enabled',?,?)", tuple(first))
            due = self.clock.timestamp() + 60
            conn.execute("UPDATE upload_slot SET state='cooldown',cooldown_until=? WHERE id=1", (due + 1800,))
        with mock.patch.object(c.pipeline_manager, "start_batch") as upload:
            c.run_campaign_tick(due)
            upload.assert_not_called()
        self.assertEqual([run["state"] for run in c.get_campaigns()["runs"]], ["queued", "queued"])

    def test_sheet_import_checks_revision_and_preserves_order_and_metadata(self):
        one, two = ("https://drive.google.com/file/d/" + letter * 30 + "/view" for letter in "ab")
        fixture = (["Drive", "Title", "Description"], [
            (2, [one, "Tiêu đề đầu", "Tiếng Việt,\ndòng hai"]),
            (3, [two, "Tiêu đề sau", "Mô tả khác"]),
            (4, [one, "Trùng", ""]),
            (5, ["https://example.invalid", "Sai nguồn", ""]),
        ])
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO projects(name) VALUES ('Sheet fixture')")
        payload = c.SheetImport(
            url="https://docs.google.com/spreadsheets/d/" + "x" * 30 + "/edit#gid=42",
            name="Sheet", project="Sheet fixture", channel_id=1,
            source_column=0, title_column=1, description_column=2,
        )
        with mock.patch.object(c, "read_sheet", return_value=fixture):
            preview = c.sheet_preview(payload)
            self.assertEqual((preview["ready"], preview["duplicates"], preview["errors"][0]["line"]), (2, 1, 5))
            with self.assertRaises(HTTPException):
                c.sheet_import(payload.model_copy(update={"revision": "stale"}))
            result = c.sheet_import(payload.model_copy(update={"revision": preview["revision"]}))
            self.assertEqual(c.sheet_preview(payload)["ready"], 0)
        self.assertEqual(c.get_campaigns()["lists"][0]["queue_ids"], result["queue_ids"])
        with closing(database.get_conn()) as conn:
            rows = conn.execute(
                "SELECT title,description,visibility,target_channel_id FROM video_queue "
                "WHERE project_name='Sheet fixture' ORDER BY id"
            ).fetchall()
        self.assertEqual([tuple(row) for row in rows],
                         [("Tiêu đề đầu", "Tiếng Việt,\ndòng hai", "private", 1),
                          ("Tiêu đề sau", "Mô tả khác", "private", 1)])

    def test_drive_links_import_rechecks_revision_and_deduplicates_by_file_id(self):
        file_id = "a" * 30
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO projects(name) VALUES ('Drive fixture')")
        data = c.DriveLinksImport(
            name="Drive", project="Drive fixture", channel_id=1,
            links=f"https://drive.google.com/open?id={file_id}\n"
                  f"https://drive.google.com/file/d/{file_id}/view\nhttps://example.invalid/video",
        )
        with mock.patch.object(c, "read_sheet") as load:
            preview = c.links_preview(data)
            self.assertEqual((preview["ready"], preview["duplicates"], preview["errors"][0]["line"]), (1, 1, 3))
            stale = data.model_copy(update={"revision": preview["revision"], "visibility": "unlisted"})
            with self.assertRaises(HTTPException):
                c.links_import(stale)
            result = c.links_import(data.model_copy(update={"revision": preview["revision"]}))
            load.assert_not_called()
        self.assertEqual(c.get_campaigns()["lists"][0]["queue_ids"], result["queue_ids"])

    def test_playlist_failure_can_be_retried_independently_of_video_upload(self):
        with closing(database.get_conn()) as conn, conn:
            conn.execute("INSERT INTO campaign_playlist_jobs(run_id,channel_id,video_id,playlist_id,state) "
                         "VALUES (11,1,'fixture0001','PL_fixture_playlist','queued')")
        with mock.patch.object(c, "add_to_playlist", side_effect=HTTPException(502, "Mock network")):
            c._run_playlist_job()
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute("SELECT state FROM campaign_playlist_jobs").fetchone()[0], "error")
        c.retry_playlist(11)
        with mock.patch.object(c, "add_to_playlist", return_value={"state": "already_added"}) as playlist:
            c._run_playlist_job()
        playlist.assert_called_once()
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute("SELECT state FROM campaign_playlist_jobs").fetchone()[0], "completed")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM upload_attempts").fetchone()[0], 0)

    def test_release_rehearsal_preserves_campaigns_and_pending_lists(self):
        report = rehearse(database.APP_DB_PATH)
        self.assertTrue(report["restore_verified"])
        self.assertTrue(report["migration_idempotent"])
        self.assertEqual(report["campaign_counts"]["publishing_campaigns"], 1)
        self.assertEqual(report["campaign_counts"]["campaign_content_lists"], 1)


class SheetTransportTests(TestCase):
    def setUp(self):
        self.url = "https://docs.google.com/spreadsheets/d/" + ("z" * 30) + "/edit#gid=6"

    @staticmethod
    def response(status, headers, body=b""):
        response = mock.MagicMock(status_code=status, headers=headers)
        response.__enter__.return_value = response
        response.iter_content.return_value = iter([body])
        return response

    def test_google_redirect_and_quoted_csv(self):
        redirect = "https://doc-ab-sheets.googleusercontent.com/export"
        replies = [self.response(302, {"Location": redirect}),
                   self.response(200, {"Content-Type": "text/csv"}, 'Tên,Mô tả\r\n"Clip, 1","a\nb"\r\n'.encode())]
        with mock.patch("requests.sessions.Session.get", side_effect=replies) as get:
            self.assertEqual(campaign_sources.read_sheet(self.url),
                             (["Tên", "Mô tả"], [(2, ["Clip, 1", "a\nb"])]))
        self.assertTrue(get.call_args_list[0].args[0].endswith("format=csv&gid=6"))
        self.assertEqual(get.call_args.args[0], redirect)

    def test_rejects_a_login_redirect_and_html_response(self):
        for reply in (self.response(302, {"Location": "https://accounts.google.com/"}),
                      self.response(200, {"Content-Type": "text/html"}, b"<html>Log in</html>")):
            with self.subTest(status=reply.status_code):
                with mock.patch("requests.sessions.Session.get", return_value=reply) as get:
                    with self.assertRaises(ValueError):
                        campaign_sources.read_sheet(self.url)
                    get.assert_called_once()

    def test_drive_html_is_rejected_and_owned_partial_is_removed(self):
        def worker(options, *_args):
            output = Path(options["out_template"].replace("%(ext)s", "mp4"))
            output.write_text("<html>Not found</html>", encoding="utf-8")
            return {"video_path": str(output)}

        with TemporaryDirectory() as folder:
            local = Path(folder)
            fallback = mock.Mock(returncode=1)
            fallback.poll.return_value = 1
            with (
                mock.patch.object(scanner_downloader, "APP_TEMP_DIR", local),
                mock.patch.object(scanner_downloader, "get_settings", return_value={}),
                mock.patch.object(scanner_downloader, "_run_yt_dlp_worker", side_effect=worker),
                mock.patch.object(scanner_downloader, "probe_video_info",
                                  side_effect=scanner_downloader.VideoProbeError("not a video")) as probe,
                mock.patch.object(scanner_downloader.subprocess, "Popen", return_value=fallback),
            ):
                with self.assertRaisesRegex(RuntimeError, "not a video"):
                    scanner_downloader.download_video("https://drive.google.com/file/d/" + "a" * 30 + "/view")
                probe.assert_called_once()
            self.assertEqual(list(local.iterdir()), [])
