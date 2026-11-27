"""Channel workspace tests replace all YouTube calls."""

import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import TestCase, mock

from fastapi import HTTPException

import channel_workspace as workspace
import database


CHANNEL = "UC" + "m" * 22
VIDEO = "dQw4w9WgXcQ"
COMMENT = "moderation-fixture"
PLAYLIST = "PL_fixture_playlist"


class ChannelWorkspaceTests(TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        path = Path(root.name)
        db = mock.patch.multiple(database, APP_DB_PATH=path / "test.sqlite", APP_DATA_DIR=path,
                                 sync_from_dhb_facebook=mock.DEFAULT)
        db.start()
        self.addCleanup(db.stop)
        guard = mock.patch("requests.sessions.Session.request", side_effect=AssertionError("Network unavailable in test"))
        guard.start()
        self.addCleanup(guard.stop)
        database.init_db()

        @contextmanager
        def channel(_):
            yield CHANNEL, {"snippet": {"title": "Fixture"}, "statistics": {"subscriberCount": "0"}}, None, "fixture"

        mock_channel = mock.patch.object(workspace, "_channel_session", channel)
        mock_channel.start()
        self.addCleanup(mock_channel.stop)

    def test_analytics_reports_unavailable_fields_separately_from_zero(self):
        with mock.patch.object(workspace, "_request", return_value={
            "columnHeaders": [{"name": "views"}, {"name": "estimatedMinutesWatched"}], "rows": [[12, 90]]
        }) as request:
            result = workspace.channel_analytics(1)
        self.assertEqual((result["subscribers"], result["views_28d"], result["watch_hours_28d"]), (0, 12, 1.5))
        self.assertIsNone(result["views_48h"])
        self.assertEqual(request.call_args.kwargs["params"]["ids"], "channel==MINE")

    def test_reply_success_and_retries_only_make_one_post(self):
        def respond(_http, _token, endpoint, *, params, payload=None):
            if endpoint == "videos":
                return {"items": [{"id": VIDEO, "snippet": {"channelId": CHANNEL}}]}
            if payload is None:
                return {"items": [{"id": COMMENT, "snippet": {"videoId": VIDEO}}]}
            return {"id": "reply-fixture", "snippet": {"parentId": COMMENT}}

        data = workspace.Reply(video_id=VIDEO, comment_id=COMMENT, text="Cảm ơn")
        with mock.patch.object(workspace, "_request", side_effect=respond) as request:
            self.assertEqual(workspace.reply_to_comment(1, data)["state"], "completed")
            with self.assertRaises(HTTPException) as denied:
                workspace.reply_to_comment(1, data)
            self.assertEqual(denied.exception.status_code, 409)
        self.assertEqual(sum(bool(call.kwargs.get("payload")) for call in request.call_args_list), 1)
        with closing(database.get_conn()) as conn:
            self.assertEqual(conn.execute("SELECT reply_id FROM channel_comment_replies").fetchone()[0], "reply-fixture")

    def test_reply_rejects_another_channels_video(self):
        data = workspace.Reply(video_id=VIDEO, comment_id=COMMENT, text="Cảm ơn")
        with mock.patch.object(workspace, "_request", return_value={
            "items": [{"id": VIDEO, "snippet": {"channelId": "another"}}]
        }) as request:
            with self.assertRaises(HTTPException) as denied:
                workspace.reply_to_comment(1, data)
        self.assertEqual(denied.exception.status_code, 409)
        self.assertIsNone(request.call_args.kwargs.get("payload"))

    def test_playlist_existing_video_never_posts_again(self):
        data = workspace.PlaylistItem(video_id=VIDEO, playlist_id=PLAYLIST)
        with mock.patch.object(workspace, "_request", side_effect=[
            {"items": [{"id": VIDEO, "snippet": {"channelId": CHANNEL}}]},
            {"items": [{"id": PLAYLIST, "snippet": {"channelId": CHANNEL}}]},
            {"items": [{"id": "playlist-item-fixture"}]},
        ]) as request:
            self.assertEqual(workspace.add_to_playlist(1, data)["state"], "already_added")
        self.assertEqual(request.call_count, 3)
        self.assertFalse(any(call.kwargs.get("payload") for call in request.call_args_list))
