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


class _StudioLink:
    def __init__(self, href):
        self.href = href

    def get_attribute(self, name):
        return self.href if name == "href" else None


class _StudioList:
    def __init__(self, items=()):
        self.items = list(items)

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]

    @property
    def first(self):
        return self.items[0] if self.items else self


class _StudioText:
    def __init__(self, text):
        self.text = text

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def is_visible(self):
        return True

    def inner_text(self, **_kwargs):
        return self.text


class _StudioRow:
    def __init__(self, title, video_id, status="Công khai"):
        self.title = title
        self.video_id = video_id
        self.status = status

    def locator(self, selector):
        if selector in ("#video-title", "a#video-title", "ytcp-video-title"):
            return _StudioText(self.title)
        if selector == "a[href*='/video/'][href*='/edit']":
            return _StudioList([_StudioLink(f"/video/{self.video_id}/edit")] if self.video_id else [])
        return _StudioList()

    def inner_text(self):
        return f"{self.title}\n{self.status}"


class _StudioPage:
    def __init__(self, rows_by_section):
        self.rows_by_section = rows_by_section
        self.section = ""
        self.visited = []
        self.url = "https://studio.youtube.com/"
        self.footer_override = {}

    def goto(self, url, **_kwargs):
        self.visited.append(url)
        self.url = url
        self.section = "short" if url.endswith("/videos/short") else "upload"

    def wait_for_selector(self, *_args, **_kwargs):
        return None

    def wait_for_timeout(self, _ms):
        return None

    def locator(self, selector):
        if selector == "ytcp-video-row":
            return _StudioList(self.rows_by_section.get(self.section, []))
        count = len(self.rows_by_section.get(self.section, []))
        if selector == "ytcp-video-section-content#video-list ytcp-table-footer":
            return _StudioText(self.footer_override.get(self.section, f"Rows per page:\n30\n1 – {count}/{count}"))
        if selector == "ytcp-video-section-content#video-list .no-content > .no-content-header" and not count:
            return _StudioText("Video của bạn sẽ xuất hiện ở đây")
        return _StudioList()


class UrlAndChecksTests(unittest.TestCase):
    def test_snapshot_retries_transient_read_and_accepts_verified_empty_tabs(self):
        page = mock.Mock(url="https://studio.youtube.com/")
        with mock.patch.object(yu, "_collect_studio_title_candidates", side_effect=[RuntimeError("loading"), []]) as read:
            result = yu._snapshot_studio_exact_title_ids(page, CHANNEL_ID, "Pilot", attempts=3)
        self.assertEqual(result, set())
        self.assertEqual(read.call_count, 2)
        page.wait_for_timeout.assert_called_once_with(2000)
        page.goto.assert_called_once_with(page.url, wait_until="domcontentloaded", timeout=30000)

    def test_unreadable_snapshot_blocks_normal_uploader_before_file_dispatch(self):
        page = mock.Mock(url="https://studio.youtube.com/")
        page.locator.return_value.first.count.return_value = 0
        session = mock.Mock(page=page)
        metadata = {"title": "Pilot", "upload_to": "Video"}
        with mock.patch.object(yu, "get_settings", return_value={}), \
                mock.patch.object(yu, "_channel_antidetect_id", return_value="test-profile"), \
                mock.patch.object(yu, "_StudioBrowserSession") as browser, \
                mock.patch.object(yu, "select_studio_channel", return_value=(page, {"channel_id": CHANNEL_ID})), \
                mock.patch.object(yu, "_collect_studio_title_candidates", side_effect=RuntimeError("loading")) as read, \
                mock.patch.object(yu, "attach_upload_file") as attach, \
                mock.patch.object(yu, "human_move_and_click") as click, \
                mock.patch.object(yu, "discard_and_cancel_upload") as discard, \
                mock.patch.object(yu, "add_log") as log:
            browser.return_value.__enter__.return_value = session
            with self.assertRaises(yu.UploadNotSent):
                yu.upload_via_playwright_studio("fixture.mp4", metadata, {"id": 1, "name": "Test"})
        self.assertEqual(read.call_count, 3)
        attach.assert_not_called()
        click.assert_not_called()
        discard.assert_not_called()
        self.assertEqual(sum(c.kwargs.get("phase") == "PreUploadSnapshot" for c in log.call_args_list), 3)

    def test_paginated_result_waits_for_new_rows_and_checks_all_matches(self):
        class PaginatedPage(_StudioPage):
            def __init__(self, first_title, approximate=False):
                super().__init__({"short": [_StudioRow(first_title, VIDEO_ID)], "upload": []})
                self.second_page = False
                self.refresh_ticks = 0
                self.approximate = approximate

            def click(self):
                self.second_page = True

            def evaluate(self, _script):
                return self.second_page and self.refresh_ticks >= 2

            def count(self):
                return 1

            def wait_for_timeout(self, _ms):
                self.refresh_ticks += 1
                if self.refresh_ticks == 2:
                    self.rows_by_section["short"] = [_StudioRow("New short", "13t572zFqlw", "Riêng tư")]

            def locator(self, selector):
                if self.section == "short" and selector == yu._STUDIO_TABLE_FOOTER:
                    start = 2 if self.second_page else 1
                    total = "/khoảng 1" if self.approximate else " of 2"
                    return _StudioText(f"Rows per page:\n30\n{start} – {start}{total}")
                if self.section == "short" and selector.endswith("#navigate-after"):
                    return _StudioList([self])
                return super().locator(selector)

        page = PaginatedPage("Other title")
        self.assertEqual(yu._find_published_video_in_studio_content(
            page, CHANNEL_ID, "New short", "Shorts", "private", set()
        ), "https://youtu.be/13t572zFqlw")
        self.assertEqual(page.refresh_ticks, 2)
        estimated = PaginatedPage("Other title", approximate=True)
        self.assertEqual(yu._find_published_video_in_studio_content(
            estimated, CHANNEL_ID, "New short", "Shorts", "private", set()
        ), "https://youtu.be/13t572zFqlw")
        self.assertEqual(estimated.refresh_ticks, 2)
        self.assertEqual(yu._find_published_video_in_studio_content(
            PaginatedPage("New short"), CHANNEL_ID, "New short", "Shorts", "private", set()
        ), "")

    def test_approximate_total_or_truncated_page_cannot_confirm_result(self):
        page = _StudioPage({"short": [_StudioRow("New short", VIDEO_ID)], "upload": []})
        page.footer_override["short"] = "Số hàng mỗi trang:\n30\n1 – 1/khoảng 1"
        self.assertIsNone(yu._snapshot_studio_exact_title_ids(page, CHANNEL_ID, "New short", "Shorts"))
        page = _StudioPage({"short": [_StudioRow("Other title", VIDEO_ID)] * 101, "upload": []})
        self.assertIsNone(yu._snapshot_studio_exact_title_ids(page, CHANNEL_ID, "New short", "Shorts"))

    def test_studio_empty_content_label_observed_in_vietnamese(self):
        class EmptyContentPage(_StudioPage):
            def locator(self, selector):
                if selector == yu._STUDIO_EMPTY_HEADER:
                    return _StudioText("Không có nội dung")
                return super().locator(selector)

        page = EmptyContentPage({"short": [], "upload": []})
        self.assertEqual(yu._snapshot_studio_exact_title_ids(page, CHANNEL_ID, "New short", "Shorts"), set())

    def test_fresh_tab_retries_unreadable_and_pending_private_result(self):
        context = mock.Mock()
        observations = [
            RuntimeError("footer still loading"),
            [{"id": VIDEO_ID, "visibility": ""}],
            [{"id": VIDEO_ID, "visibility": "private"}],
        ]
        with mock.patch.object(yu, "_collect_studio_title_candidates", side_effect=observations) as read, \
                mock.patch.object(yu, "add_log") as log:
            url = yu._verify_published_video_on_fresh_tab(
                context, CHANNEL_ID, {"title": "Pilot", "privacy_status": "private"}, set()
            )
        self.assertEqual(url, f"https://youtu.be/{VIDEO_ID}")
        self.assertEqual(read.call_count, 3)
        self.assertEqual(log.call_count, 3)
        context.new_page.assert_called_once()
        context.new_page.return_value.close.assert_called_once()

    def test_retry_never_replaces_a_previous_candidate_with_a_different_id(self):
        observations = [
            [{"id": VIDEO_ID, "visibility": ""}],
            [{"id": "13t572zFqlw", "visibility": "private"}],
        ]
        with mock.patch.object(yu, "_collect_studio_title_candidates", side_effect=observations) as read:
            self.assertEqual(yu._find_published_video_in_studio_content(
                mock.Mock(), CHANNEL_ID, "Pilot", "Video", "private", set(), attempts=3
            ), "")
        self.assertEqual(read.call_count, 2)

    def test_empty_video_tab_can_confirm_a_new_private_video(self):
        class EmptyStudioPage(_StudioPage):
            def wait_for_selector(self, selector, **_kwargs):
                if not self.rows_by_section[self.section] and ".no-content" not in selector:
                    raise TimeoutError("Empty tab has no video rows")

        page = EmptyStudioPage({"upload": [], "short": [_StudioRow("Old short", VIDEO_ID)]})
        known = yu._snapshot_studio_exact_title_ids(page, CHANNEL_ID, "Pilot clip", "Video")
        self.assertEqual(known, set())
        page.rows_by_section["upload"] = [_StudioRow("Pilot clip", "13t572zFqlw", "Riêng tư")]
        self.assertEqual(yu._find_published_video_in_studio_content(
            page, CHANNEL_ID, "Pilot clip", "Video", "private", known
        ), "https://youtu.be/13t572zFqlw")

    def test_private_confirmation_rejects_incomplete_footer_or_other_channel(self):
        page = _StudioPage({"upload": [_StudioRow("Pilot clip", VIDEO_ID, "Riêng tư")], "short": []})
        page.footer_override["upload"] = "Số hàng mỗi trang:\n30\n1 – 1/2"
        self.assertEqual(yu._find_published_video_in_studio_content(
            page, CHANNEL_ID, "Pilot clip", "Video", "private", set()
        ), "")
        self.assertIsNone(yu._snapshot_studio_exact_title_ids(page, CHANNEL_ID, "Pilot clip", "Video"))

        class RedirectedStudioPage(_StudioPage):
            def goto(self, url, **kwargs):
                super().goto(url.replace(CHANNEL_ID, "UCbbbbbbbbbbbbbbbbbbbbbb"), **kwargs)

        redirected = RedirectedStudioPage(page.rows_by_section)
        self.assertEqual(yu._find_published_video_in_studio_content(
            redirected, CHANNEL_ID, "Pilot clip", "Video", "private", set()
        ), "")
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

    def test_route_change_url_is_publish_evidence_only_on_youtube_hosts(self):
        class EmptyLocator:
            def count(self):
                return 0

            @property
            def first(self):
                return self

        class Page:
            def __init__(self, url):
                self.url = url

            def locator(self, _selector):
                return EmptyLocator()

        upload_url = "https://studio.youtube.com/channel/UC123/videos/upload?d=ud"
        self.assertEqual(
            yu.extract_published_video_url(
                Page("https://studio.youtube.com/video/13t572zFqlw/edit"), previous_url=upload_url
            ),
            "https://youtu.be/13t572zFqlw",
        )
        self.assertEqual(
            yu.extract_published_video_url(
                Page("https://www.youtube.com/watch?v=13t572zFqlw"), previous_url=upload_url
            ),
            "https://youtu.be/13t572zFqlw",
        )
        self.assertEqual(
            yu.extract_published_video_url(
                Page("https://evil.example/video/13t572zFqlw/edit"), previous_url=upload_url
            ),
            "",
        )

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

    def test_share_dialog_text_url_is_accepted_as_publish_evidence(self):
        class EmptyLocator:
            def count(self):
                return 0

            @property
            def first(self):
                return self

        class ShareDialog(EmptyLocator):
            def count(self):
                return 1

            def is_visible(self):
                return True

            def inner_text(self):
                return (
                    "Đã đăng video 0:05 Tiêu đề video\n"
                    "Đường liên kết của video https://youtu.be/13t572zFqlw Đóng"
                )

            def locator(self, _selector):
                return EmptyLocator()

        class Page:
            url = "https://studio.youtube.com/channel/UC123/videos/upload?d=ud"

            def locator(self, selector):
                return ShareDialog() if selector == "ytcp-video-share-dialog" else EmptyLocator()

        self.assertEqual(
            yu.extract_published_video_url(Page(), previous_url=Page.url),
            "https://youtu.be/13t572zFqlw",
        )

    def test_content_list_fallback_checks_shorts_tab_and_requires_single_new_public_video(self):
        page = _StudioPage({
            "short": [_StudioRow("Unrelated short", VIDEO_ID)],
            "upload": [_StudioRow("Fresh short #shorts", "13t572zFqlw")],
        })
        url = yu._find_published_video_in_studio_content(
            page, CHANNEL_ID, "Fresh short #shorts", upload_to="Shorts",
            expected_visibility="public", known_video_ids=set(),
        )

        self.assertEqual(url, "https://youtu.be/13t572zFqlw")
        # Studio dùng tab Shorts ở /videos/short (số ít); /videos/shorts trả danh sách rỗng.
        self.assertEqual(
            page.visited,
            [
                f"https://studio.youtube.com/channel/{CHANNEL_ID}/videos/short",
                f"https://studio.youtube.com/channel/{CHANNEL_ID}/videos/upload",
            ],
        )

        # Video cùng tiêu đề đã tồn tại từ trước upload không được dùng làm bằng chứng.
        page = _StudioPage({
            "short": [_StudioRow("Fresh short #shorts", "13t572zFqlw")],
            "upload": [],
        })
        self.assertEqual(
            yu._find_published_video_in_studio_content(
                page, CHANNEL_ID, "Fresh short #shorts", upload_to="Shorts",
                expected_visibility="public", known_video_ids={"13t572zFqlw"},
            ),
            "",
        )

    def test_content_list_fallback_rejects_draft_wrong_visibility_and_ambiguous_ids(self):
        for status in ("Bản nháp", "Đã tải được 34%", "Đang xử lý"):
            with self.subTest(status=status):
                page = _StudioPage({
                    "short": [_StudioRow("Fresh short #shorts", "13t572zFqlw", status)],
                    "upload": [],
                })
                self.assertEqual(
                    yu._find_published_video_in_studio_content(
                        page, CHANNEL_ID, "Fresh short #shorts", upload_to="Shorts",
                        expected_visibility="public", known_video_ids=set(),
                    ),
                    "",
                )

        page = _StudioPage({
            "short": [_StudioRow("Fresh short #shorts", "13t572zFqlw", "Riêng tư")],
            "upload": [],
        })
        self.assertEqual(
            yu._find_published_video_in_studio_content(
                page, CHANNEL_ID, "Fresh short #shorts", upload_to="Shorts",
                expected_visibility="public", known_video_ids=set(),
            ),
            "",
        )

        page = _StudioPage({
            "short": [
                _StudioRow("Fresh short #shorts", "13t572zFqlw"),
                _StudioRow("Fresh short #shorts", "aaaaaaaaaaa"),
            ],
            "upload": [],
        })
        self.assertEqual(
            yu._find_published_video_in_studio_content(
                page, CHANNEL_ID, "Fresh short #shorts", upload_to="Shorts",
                expected_visibility="public", known_video_ids=set(),
            ),
            "",
        )

        # Public + Draft cùng tiêu đề: đếm ID trước khi lọc visibility nên vẫn là 2 ứng viên mới.
        page = _StudioPage({
            "short": [
                _StudioRow("Fresh short #shorts", "13t572zFqlw", "Công khai"),
                _StudioRow("Fresh short #shorts", "aaaaaaaaaaa", "Bản nháp"),
            ],
            "upload": [],
        })
        self.assertEqual(
            yu._find_published_video_in_studio_content(
                page, CHANNEL_ID, "Fresh short #shorts", upload_to="Shorts",
                expected_visibility="public", known_video_ids=set(),
            ),
            "",
        )

        page = _StudioPage({
            "short": [_StudioRow("Riêng tư là gì #shorts", "13t572zFqlw", "Công khai")],
            "upload": [],
        })
        self.assertEqual(
            yu._find_published_video_in_studio_content(
                page, CHANNEL_ID, "Riêng tư là gì #shorts", upload_to="Shorts",
                expected_visibility="public", known_video_ids=set(),
            ),
            "https://youtu.be/13t572zFqlw",
        )
        page = _StudioPage({
            "short": [_StudioRow("Public speaking #shorts", "13t572zFqlw", "Riêng tư")],
            "upload": [],
        })
        self.assertEqual(
            yu._find_published_video_in_studio_content(
                page, CHANNEL_ID, "Public speaking #shorts", upload_to="Shorts",
                expected_visibility="public", known_video_ids=set(),
            ),
            "",
        )

        page = _StudioPage({
            "short": [],
            "upload": [_StudioRow("Video hẹn lịch", "13t572zFqlw", "Đã lên lịch")],
        })
        self.assertEqual(
            yu._find_published_video_in_studio_content(
                page, CHANNEL_ID, "Video hẹn lịch", upload_to="Video",
                expected_visibility="schedule", known_video_ids=set(),
            ),
            "https://youtu.be/13t572zFqlw",
        )

    def test_content_list_fallback_rejects_title_substrings_missing_snapshot_and_invalid_channel_id(self):
        row = mock.Mock()
        row.locator.return_value.first.count.return_value = 0
        row.inner_text.return_value = "Fresh short #shorts\nCông khai"
        self.assertFalse(yu._studio_row_matches_title(row, "Fresh short"))

        page = mock.Mock()
        self.assertEqual(
            yu._find_published_video_in_studio_content(
                page, "bad-id", "Fresh short", known_video_ids=set()
            ),
            "",
        )
        page.goto.assert_not_called()

        self.assertEqual(
            yu._find_published_video_in_studio_content(
                mock.Mock(), CHANNEL_ID, "Fresh short", known_video_ids=None
            ),
            "",
        )

    def test_snapshot_exact_title_ids_restores_original_page(self):
        page = _StudioPage({
            "short": [_StudioRow("Fresh short #shorts", "13t572zFqlw")],
            "upload": [],
        })
        page.url = f"https://studio.youtube.com/channel/{CHANNEL_ID}/videos/upload"

        known = yu._snapshot_studio_exact_title_ids(
            page, CHANNEL_ID, "Fresh short #shorts", upload_to="Shorts"
        )

        self.assertEqual(known, {"13t572zFqlw"})
        self.assertEqual(page.visited[-1], f"https://studio.youtube.com/channel/{CHANNEL_ID}/videos/upload")

        class UnreadablePage(_StudioPage):
            def wait_for_selector(self, *_args, **_kwargs):
                raise TimeoutError("danh sách chưa tải")

        unreadable = UnreadablePage({"short": [], "upload": []})
        self.assertIsNone(
            yu._snapshot_studio_exact_title_ids(unreadable, CHANNEL_ID, "Fresh short #shorts", "Shorts")
        )

    def test_snapshot_rejects_rows_with_unreadable_id_and_incomplete_pagination(self):
        # Hàng trùng tiêu đề nhưng không đọc được ID: không được coi là "không có video nào".
        broken = _StudioPage({
            "short": [_StudioRow("Fresh short #shorts", "", "Công khai")],
            "upload": [],
        })
        self.assertIsNone(
            yu._snapshot_studio_exact_title_ids(broken, CHANNEL_ID, "Fresh short #shorts", "Shorts")
        )
        self.assertEqual(
            yu._find_published_video_in_studio_content(
                broken, CHANNEL_ID, "Fresh short #shorts", upload_to="Shorts",
                expected_visibility="public", known_video_ids=set(),
            ),
            "",
        )

        # Mới đọc trang 1/4: thiếu nút chuyển trang thì snapshot phải fail closed.
        many_rows = [_StudioRow(f"Video cũ {index}", f"{index:011d}") for index in range(30)]
        page = _StudioPage({"short": many_rows, "upload": []})
        page.footer_override["short"] = "Rows per page:\n30\n1 – 30/100"
        self.assertIsNone(
            yu._snapshot_studio_exact_title_ids(page, CHANNEL_ID, "Fresh short #shorts", "Shorts")
        )
        self.assertEqual(
            yu._find_published_video_in_studio_content(
                page, CHANNEL_ID, "Fresh short #shorts", upload_to="Shorts",
                expected_visibility="public", known_video_ids=set(),
            ),
            "",
        )

    def test_content_list_fallback_fails_closed_when_one_tab_times_out(self):
        class OneTabTimeoutPage(_StudioPage):
            def wait_for_selector(self, *_args, **_kwargs):
                if self.section == "short":
                    raise TimeoutError("tab Shorts chưa tải")
                return None

        page = OneTabTimeoutPage({
            "short": [],
            "upload": [_StudioRow("Fresh short #shorts", "13t572zFqlw", "Công khai")],
        })
        self.assertEqual(
            yu._find_published_video_in_studio_content(
                page, CHANNEL_ID, "Fresh short #shorts", upload_to="Shorts",
                expected_visibility="public", known_video_ids=set(),
            ),
            "",
        )

    def test_studio_channel_id_regex_keeps_capture_group(self):
        self.assertTrue(yu._STUDIO_CHANNEL_ID_RE.groups)
        page = type("Page", (), {
            "url": f"https://studio.youtube.com/channel/{CHANNEL_ID}/videos/upload",
            "evaluate": mock.Mock(side_effect=AssertionError("URL đã đủ, không được gọi evaluate")),
        })()
        self.assertEqual(yu._read_studio_channel_id(page), CHANNEL_ID)

    def test_wait_for_publish_completion_survives_still_processing(self):
        calls = {"n": 0}

        def fake_extract(_page, _previous_url):
            calls["n"] += 1
            return f"https://youtu.be/{VIDEO_ID}" if calls["n"] >= 3 else ""

        class EmptyLocator:
            def count(self):
                return 0

            @property
            def first(self):
                return self

        class Page:
            def locator(self, _selector):
                return EmptyLocator()

            def wait_for_timeout(self, _ms):
                return None

        ticks = iter(range(10000))
        with mock.patch.object(yu, "extract_published_video_url", side_effect=fake_extract), \
                mock.patch.object(yu.time, "monotonic", side_effect=lambda: next(ticks)):
            url = yu._wait_for_publish_completion(Page(), previous_url="", timeout=30)

        self.assertEqual(url, f"https://youtu.be/{VIDEO_ID}")
        self.assertGreaterEqual(calls["n"], 3)


class _DiscardClick:
    def __init__(self, page, kind):
        self._page = page
        self.kind = kind

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def is_visible(self):
        return True

    def nth(self, _index):
        return self

    def inner_text(self):
        return ""

    def locator(self, selector):
        return self._page.dialog_locator(selector)


class _DiscardDialog(_DiscardClick):
    def inner_text(self):
        return "Xóa vĩnh viễn video nháp này?"


class _DiscardDialogList:
    def __init__(self, page):
        self._page = page

    def count(self):
        return 1 if self._page.dialog_open else 0

    def nth(self, _index):
        return _DiscardDialog(self._page, "dialog")


class _DiscardPage:
    def __init__(self):
        self.dialog_open = True

    def dialog_locator(self, selector):
        if "checkbox" in selector:
            return _DiscardClick(self, "confirm-checkbox")
        if "#confirm-button" in selector:
            return _DiscardClick(self, "confirm")
        return _DiscardClick(self, "discard")

    def locator(self, selector):
        if "paper-dialog" in selector or "confirmation-dialog" in selector:
            return _DiscardDialogList(self)
        return _DiscardClick(self, "close")

    def wait_for_timeout(self, _ms):
        return None


class MetadataAndCleanupTests(unittest.TestCase):
    def test_description_over_limit_fails_instead_of_truncating_url(self):
        url = "https://shopee.vn/product/123456"
        desc = "a" * (5000 - len(url)) + url
        meta = yu.build_final_metadata(
            {"title": "Tiêu đề", "description": desc, "visibility": "public", "upload_to": "Video"}, {}
        )
        self.assertEqual(meta["description"], desc)
        self.assertTrue(meta["description"].endswith(url))
        self.assertEqual(meta["upload_to"], "Video")

        with self.assertRaises(ValueError):
            yu.build_final_metadata(
                {"title": "Tiêu đề", "description": "a" * 5001, "visibility": "public", "upload_to": "Video"},
                {},
            )

        with self.assertRaises(ValueError):
            yu.build_final_metadata(
                {"title": "Tiêu đề", "description": "a" * 4950, "visibility": "public", "upload_to": "Video"},
                {},
                {"affiliate_enabled": True, "affiliate_url": "https://shopee.vn/x"},
            )

    def test_checks_elapsed_uses_monotonic_clock(self):
        source = Path(yu.__file__).read_text(encoding="utf-8")
        self.assertNotIn("int(time.time() - start_wait)", source)

    def test_discard_upload_reports_not_found_without_success_log(self):
        class EmptyLocator:
            def count(self):
                return 0

            @property
            def first(self):
                return self

            def nth(self, _index):
                return self

            def is_visible(self):
                return False

        class Page:
            def locator(self, _selector):
                return EmptyLocator()

            def wait_for_timeout(self, _ms):
                return None

        with mock.patch.object(yu, "DISCARD_DIALOG_WAIT_TIMEOUT_MS", 0), \
                mock.patch.object(yu, "add_log") as log_mock:
            result = yu.discard_and_cancel_upload(Page(), video_title="Tiêu đề")

        self.assertEqual(result["status"], "not_found")
        messages = [str(call.args[1]) for call in log_mock.call_args_list if len(call.args) > 1]
        self.assertFalse(any("✅" in message for message in messages))

    def test_discard_upload_never_claims_deleted_without_destructive_dialog(self):
        page = _DiscardPage()
        page.dialog_open = False

        with mock.patch.object(yu, "DISCARD_DIALOG_WAIT_TIMEOUT_MS", 0), \
                mock.patch.object(yu, "human_move_and_click"), \
                mock.patch.object(yu, "add_log") as log_mock:
            result = yu.discard_and_cancel_upload(page, video_title="Tiêu đề")

        self.assertEqual(result["status"], "not_found")
        self.assertNotIn("confirm-delete", result["clicked"])
        messages = [str(call.args[1]) for call in log_mock.call_args_list if len(call.args) > 1]
        self.assertFalse(any("✅" in message for message in messages))

    def test_discard_upload_dialog_read_error_is_uncertain_not_deleted(self):
        class ReadErrorDialogList:
            def count(self):
                raise RuntimeError("DOM hộp thoại không đọc được")

        class ReadErrorPage(_DiscardPage):
            def locator(self, selector):
                if "paper-dialog" in selector or "confirmation-dialog" in selector:
                    return ReadErrorDialogList()
                return super().locator(selector)

        page = ReadErrorPage()
        page.dialog_open = False
        with mock.patch.object(yu, "DISCARD_DIALOG_WAIT_TIMEOUT_MS", 0), \
                mock.patch.object(yu, "human_move_and_click"), \
                mock.patch.object(yu, "add_log") as log_mock:
            result = yu.discard_and_cancel_upload(page, video_title="Tiêu đề")

        self.assertEqual(result["status"], "uncertain")
        self.assertNotIn("confirm-delete", result["clicked"])
        messages = [str(call.args[1]) for call in log_mock.call_args_list if len(call.args) > 1]
        self.assertFalse(any("✅" in message for message in messages))

    def test_discard_upload_reports_deleted_only_after_dialog_closes(self):
        page = _DiscardPage()

        def fake_click(_page, locator, delay_after=None):
            if getattr(locator, "kind", "") == "confirm":
                page.dialog_open = False

        with mock.patch.object(yu, "human_move_and_click", side_effect=fake_click), \
                mock.patch.object(yu, "add_log") as log_mock:
            result = yu.discard_and_cancel_upload(page, video_title="Tiêu đề")

        self.assertEqual(result["status"], "deleted")
        self.assertFalse(result["dialog_open"])
        messages = [str(call.args[1]) for call in log_mock.call_args_list if len(call.args) > 1]
        self.assertTrue(any("✅" in message for message in messages))


class PreflightAndProxyTests(unittest.TestCase):
    def test_declared_antidetect_profile_never_falls_back_to_local_chrome(self):
        with self.assertRaises(RuntimeError):
            yu._channel_antidetect_id({"profile_path": "ANTIDETECT://", "profile_name": ""})

    def test_malformed_proxy_fails_closed(self):
        for value in ("proxy:not-a-port", "proxy:70000", "proxy:8080:user", "proxy:8080:user:"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                yu._parse_channel_proxy({"proxy": value})

    def test_studio_upload_rejects_age_restriction_before_browser_launch(self):
        with mock.patch.object(yu, "get_settings") as get_settings, \
                mock.patch("playwright.sync_api.sync_playwright") as sync_playwright:
            with self.assertRaises(RuntimeError) as raised:
                yu.upload_via_playwright_studio(
                    "C:/khong-ton-tai.mp4",
                    {"title": "Video", "age_restriction": True},
                    {"name": "Kênh", "channel_id": CHANNEL_ID},
                )
        self.assertIn("giới hạn độ tuổi", str(raised.exception))
        get_settings.assert_not_called()
        sync_playwright.assert_not_called()

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


class _MenuLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    @property
    def first(self):
        return self

    def nth(self, _index):
        return self

    def count(self):
        if "avatar" in self._selector:
            return 1 if self._page.avatar_ready else 0
        if "ytd-active-account-header-renderer" in self._selector:
            return 1 if self._page.menu_open else 0
        return 0

    def is_visible(self):
        return self.count() > 0

    def bounding_box(self):
        return {"x": 1833, "y": 15, "width": 60, "height": 34}

    def scroll_into_view_if_needed(self, timeout=None):
        return None

    def click(self):
        self._page.menu_open = True


class _MenuMouse:
    def __init__(self, page):
        self._page = page

    def move(self, *_args, **_kwargs):
        self._page.mouse_calls.append("move")

    def down(self):
        self._page.mouse_calls.append("down")
        self._page.menu_open = True

    def up(self):
        self._page.mouse_calls.append("up")


class _MenuPage:
    """Studio chỉ render #avatar-btn sau một khoảng chờ; hành vi đó quyết định nhánh bấm."""

    def __init__(self, *, avatar_ready=False, render_after_wait=False):
        self.avatar_ready = avatar_ready
        self._render_after_wait = render_after_wait
        self.menu_open = False
        self.wait_selectors = []
        self.mouse_calls = []
        self.mouse = _MenuMouse(self)
        self.keyboard = type("Keyboard", (), {"press": lambda *_: None})()

    def locator(self, selector):
        return _MenuLocator(self, selector)

    def wait_for_selector(self, selector, timeout=None):
        self.wait_selectors.append(selector)
        if "avatar" in selector:
            if self.avatar_ready:
                return True
            if self._render_after_wait:
                self.avatar_ready = True
                return True
            raise TimeoutError("avatar không xuất hiện")
        if "ytd-active-account-header-renderer" in selector and not self.menu_open:
            raise TimeoutError("menu tài khoản không mở")
        return True

    def wait_for_timeout(self, _ms):
        return None


class StudioAccountMenuTimingTests(unittest.TestCase):
    def test_waits_for_avatar_before_clicking_instead_of_giving_up(self):
        page = _MenuPage(render_after_wait=True)
        self.assertTrue(yu._open_studio_account_menu(page))
        self.assertTrue(any("avatar" in selector for selector in page.wait_selectors))
        self.assertIn("down", page.mouse_calls)

    def test_missing_avatar_still_fails_closed_without_clicking(self):
        page = _MenuPage()
        self.assertFalse(yu._open_studio_account_menu(page))
        self.assertEqual(page.mouse_calls, [])
        self.assertFalse(page.menu_open)


class StudioCreateButtonSelectorTests(unittest.TestCase):
    def test_create_button_uses_exact_label_not_substring(self):
        # Dạng [aria-label*='Tạo' i] khớp nhầm "Trò chuyện với Bộ phận hỗ trợ nhà sáng tạo"
        # và mở Trung tâm trợ giúp thay vì menu Tạo.
        selector = yu.STUDIO_CREATE_BUTTON_SELECTOR
        self.assertIn("[aria-label='Tạo']", selector)
        self.assertIn("[aria-label='Create']", selector)
        self.assertNotIn("aria-label*='Tạo'", selector)
        self.assertNotIn("aria-label*='Create'", selector)

    def test_create_button_selector_still_covers_legacy_ids(self):
        self.assertIn("#create-icon", yu.STUDIO_CREATE_BUTTON_SELECTOR)


class AttachUploadFileCdpTests(unittest.TestCase):
    """Playwright chặn set_input_files >50MB với browser gắn qua CDP; phải đi qua DOM.setFileInputFiles."""

    class _Input:
        def __init__(self):
            self.markers = []
            self.files_via_playwright = []
            self.evaluate_error = None

        def evaluate(self, expression, arg=None):
            if self.evaluate_error:
                raise self.evaluate_error
            self.markers.append((expression, arg))

        def set_input_files(self, path, timeout=None):
            self.files_via_playwright.append(path)

    class _Session:
        def __init__(self, calls, object_id="node-1"):
            self.calls = calls
            self.object_id = object_id
            self.detached = False

        def send(self, method, params=None):
            self.calls.append((method, params))
            if method == "Runtime.evaluate":
                if not self.object_id:
                    return {"result": {"type": "undefined"}}
                return {"result": {"type": "object", "objectId": self.object_id}}
            return {}

        def detach(self):
            self.detached = True

    class _Context:
        def __init__(self, session=None, session_error=None):
            self.session = session
            self.session_error = session_error

        def new_cdp_session(self, _page):
            if self.session_error:
                raise self.session_error
            return self.session

    def _page(self, session=None, session_error=None):
        return type("Page", (), {"context": self._Context(session, session_error)})()

    def test_uses_cdp_and_skips_playwright_transfer(self):
        calls = []
        session = self._Session(calls)
        upload_input = self._Input()

        yu.attach_upload_file(self._page(session=session), upload_input, "D:\\video.mp4")

        set_files = [params for method, params in calls if method == "DOM.setFileInputFiles"]
        self.assertEqual(len(set_files), 1)
        self.assertEqual(set_files[0]["objectId"], "node-1")
        self.assertEqual(set_files[0]["files"], [str(Path("D:\\video.mp4").resolve())])
        self.assertEqual(upload_input.files_via_playwright, [])
        self.assertTrue(session.detached)
        self.assertEqual(len(upload_input.markers), 2)  # gắn rồi dọn con trỏ tạm

    def test_falls_back_to_set_input_files_without_cdp_session(self):
        upload_input = self._Input()

        yu.attach_upload_file(
            self._page(session_error=RuntimeError("no cdp")), upload_input, "D:\\small.mp4"
        )

        self.assertEqual(
            upload_input.files_via_playwright, [str(Path("D:\\small.mp4").resolve())]
        )

    def test_raises_when_both_transfer_paths_fail(self):
        upload_input = self._Input()
        upload_input.evaluate_error = RuntimeError("evaluate blocked")
        upload_input.set_input_files = lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("Cannot transfer files larger than 50Mb")
        )

        with self.assertRaises(RuntimeError) as raised:
            yu.attach_upload_file(
                self._page(session=self._Session([], object_id=None)),
                upload_input,
                "D:\\video.mp4",
            )
        self.assertIn("Không nạp được file tải lên", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
