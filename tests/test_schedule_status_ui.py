"""Schedule source warnings against UI assets only; never starts a runtime/server/provider.

Opt in with DHB_BROWSER_TESTS=1. All requests are intercepted by UIPage at a
kernel-assigned, reserved loopback port; the socket never listens or accepts.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

from ui_browser_fixture import UIPage


class ScheduleStatusRendererFixtureTests(unittest.TestCase):
    def test_renderer_states_and_text_sinks_without_browser_or_network(self):
        """Execute the actual renderer in a small DOM fixture, with no HTTP client."""
        node = os.environ.get("DHB_TEST_NODE") or shutil.which("node")
        if not node:
            spec = importlib.util.find_spec("playwright")
            if spec and spec.origin:
                bundled = Path(spec.origin).parent / "driver" / ("node.exe" if os.name == "nt" else "node")
                if bundled.is_file():
                    node = str(bundled)
        if not node:
            self.skipTest("Node is needed for the isolated JavaScript renderer fixture")
        program = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const adminSource = fs.readFileSync(process.argv[2], 'utf8');
const factoryStart = source.indexOf('  const node = ');
const factoryEnd = source.indexOf('  function queueItems()', factoryStart);
const rendererStart = source.indexOf('    const renderScheduleBanner = ');
const rendererEnd = source.indexOf("\n    if (typeof apiFetch === 'function')", rendererStart);
assert(factoryStart >= 0 && factoryEnd > factoryStart && rendererStart >= 0 && rendererEnd > rendererStart);
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.hidden = true; this.listeners = {}; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(' '); }
  set innerHTML(_) { throw new Error('API text must not reach an HTML sink'); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  find(id) { return this.id === id ? this : this.children.map(child => child.find(id)).find(Boolean); }
}
const drift = {detected: true, missing_future_count: 7, new_files_count: 7, next_source_exists: false};
const waiting = {configured: true, status: 'waiting', message: 'Đang chờ', folder_drift: drift, action_required: true};
function fixture(payload, syncResult = {}) {
  const box = new Element('section'); const calls = []; const toasts = [];
  const context = {
    document: {createElement: tag => new Element(tag)}, dialog: {isConnected: true},
    byId: id => { assert.equal(id, 'publish-schedule-status'); return box; }, state: {},
    capture: () => {}, refreshValidity: () => {}, showToast: (text, type) => toasts.push({text, type}),
    apiFetch: async (url, options) => {
      calls.push({url, options}); assert.equal(url, '/api/scheduled-uploads/sync');
      return {ok: true, added_count: 2, removed_pending_count: 1, schedule: {configured: true,
        status: 'waiting', message: 'Đang chờ', completed_count: 1, total_count: 9,
        action_required: false, folder_drift: {detected: false, missing_future_count: 0, new_files_count: 0, next_source_exists: true}}, ...syncResult};
    },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(factoryStart, factoryEnd) + source.slice(rendererStart, rendererEnd)
    + '\nglobalThis.render = renderScheduleBanner;', context);
  context.render(payload);
  return {box, calls, toasts};
}
async function main() {
  let cases = 0;
  let f = fixture(waiting);
  assert.equal(f.box.dataset.state, 'error');
  assert.match(f.box.children[0].children[0].textContent, /Cần xử lý nguồn trước lượt đăng tiếp.*file kế tiếp không còn trên đĩa/);
  assert.doesNotMatch(f.box.children[0].children[0].textContent, /Đang chờ/);
  for (const text of ['7 file chờ không còn trên đĩa', '7 file mới chưa nạp vào lịch', 'File mới không mặc nhiên là file đổi tên', 'Kiểm tra đúng thư mục/file nguồn']) assert(f.box.textContent.includes(text));
  assert.equal(f.calls.length, 0); cases++;
  await f.box.find('publish-schedule-sync').listeners.click();
  assert.equal(f.calls.length, 1); assert.equal(f.calls[0].options.method, 'POST');
  assert.equal(f.box.dataset.state, 'ready');
  assert.deepEqual(f.toasts.map(toast => toast.type), ['success']);
  assert.match(f.toasts[0].text, /\+2 video mới, -1 mục mất file/); cases++;
  const hostileWarning = '<img src=x onerror=window.scheduleXss=1>';
  for (const [warning_code, expected] of [
    ['STALE_CLEANUP_INCOMPLETE', 'một số mục cũ vẫn còn trong hàng đợi'],
    [hostileWarning, 'Lịch đã cập nhật có cảnh báo'],
    [{code: hostileWarning}, 'Lịch đã cập nhật có cảnh báo'],
  ]) {
    f = fixture(waiting, {warning_code});
    assert.equal(f.calls.length, 0);
    assert.equal(f.toasts.length, 0);
    await f.box.find('publish-schedule-sync').listeners.click();
    assert.equal(f.calls.length, 1);
    assert.equal(f.calls[0].options.method, 'POST');
    assert.equal(f.box.dataset.state, 'warning');
    assert(f.box.textContent.includes('1/9 video'));
    assert(f.box.textContent.includes(expected));
    assert(!f.box.textContent.includes(hostileWarning));
    assert.deepEqual(f.toasts.map(toast => toast.type), ['warning']);
    assert(f.toasts[0].text.includes(expected));
    assert(!f.toasts[0].text.includes(hostileWarning)); cases++;
  }
  for (const status of ['running', 'submitted']) {
    f = fixture({...waiting, status, inflight_queue_id: 239});
    assert.equal(f.box.find('publish-schedule-sync'), undefined);
    assert(f.box.textContent.includes('Chờ lượt đang xử lý kết thúc')); assert.equal(f.calls.length, 0); cases++;
  }
  f = fixture({configured: true, status: 'waiting', folder_drift: {next_source_exists: false}});
  assert.equal(f.box.dataset.state, 'error'); assert.doesNotMatch(f.box.textContent, /undefined|0 file|\(\)/); cases++;
  f = fixture({...waiting, action_required: false, folder_drift: {detected: false, new_files_count: 7}});
  assert.equal(f.box.dataset.state, 'warning'); assert(f.box.textContent.includes('7 file mới chưa nạp vào lịch')); cases++;
  for (const payload of [null, [], 'broken', {}, {configured: false}, {configured: 'true'}]) {
    f = fixture(payload); assert.equal(f.box.hidden, true); assert.equal(f.calls.length, 0); cases++;
  }
  for (const broken of ['broken', [], {detected: 'false'}, {next_source_exists: 'false'}, {new_files_count: '7'}, {missing_future_count: -1}]) {
    f = fixture({...waiting, action_required: false, folder_drift: broken});
    assert.equal(f.box.dataset.state, 'error'); assert(f.box.textContent.includes('dữ liệu kiểm tra thư mục không hợp lệ'));
    assert.equal(f.box.find('publish-schedule-sync'), undefined); assert.equal(f.calls.length, 0); cases++;
  }
  const attack = '<img src=x onerror="window.scheduleXss=1"> & <svg onload="window.scheduleXss=2">';
  f = fixture({...waiting, message: attack, next_queue_id: attack, next_due_at: attack, last_result: {queue_id: attack, status: attack}});
  assert(f.box.textContent.includes(attack)); assert.equal(f.calls.length, 0); cases++;
  f = fixture({configured: true, status: 'waiting'});
  assert.equal(f.box.dataset.state, 'ready'); assert.doesNotMatch(f.box.textContent, /undefined/); cases++;
  const adminStart = adminSource.indexOf('  function onDiagnosticsLoaded(data) {');
  const adminEnd = adminSource.indexOf('  function onDiagnosticsError(error)', adminStart);
  assert(adminStart >= 0 && adminEnd > adminStart);
  function diagnostics(payload) {
    const host = new Element('div');
    const context = {state: {}, window: {}, document: {createElement: tag => new Element(tag)},
      byId: id => { assert.equal(id, 'admin-diagnostics-cards'); return host; }};
    vm.createContext(context);
    vm.runInContext(source.slice(factoryStart, factoryEnd) + adminSource.slice(adminStart, adminEnd)
      + '\nglobalThis.render = onDiagnosticsLoaded;', context);
    context.render({scheduled_uploads: payload});
    return host.children.find(card => card.children[0].textContent === 'Lịch đăng Private');
  }
  let card = diagnostics(waiting);
  assert(card.children[1].textContent.includes('Cần xử lý nguồn'));
  assert.doesNotMatch(card.children[1].textContent, /waiting|Đang chờ/);
  assert(card.textContent.includes('7 file chờ không còn trên đĩa'));
  assert(card.textContent.includes('7 file mới chưa nạp vào lịch')); cases++;
  card = diagnostics({...waiting, action_required: false, folder_drift: {new_files_count: 7, next_source_exists: true}});
  assert(card.children[1].textContent.includes('Cần kiểm tra nguồn'));
  assert(card.textContent.includes('File mới không mặc nhiên là file đổi tên')); cases++;
  card = diagnostics({...waiting, folder_drift: {next_source_exists: 'false'}});
  assert(card.children[1].textContent.includes('Cần kiểm tra nguồn'));
  assert(card.textContent.includes('tải lại trạng thái')); cases++;
  card = diagnostics({configured: true, status: attack, message: attack, action_required: true});
  assert(card.textContent.includes(attack)); cases++;
  assert.equal(diagnostics({configured: 'true'}), undefined); cases++;
  console.log(JSON.stringify({renderer_fixture_cases: cases, network_clients: 0, passed: true}));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
"""
        source_path = Path(__file__).resolve().parents[1] / "static" / "ui-publish.js"
        # File-backed stdio avoids Windows named pipes, which confined runners deny.
        with tempfile.TemporaryDirectory(prefix="dhb-schedule-renderer-") as temp_dir:
            output = Path(temp_dir) / "renderer.log"
            with output.open("w", encoding="utf-8") as stream:
                completed = subprocess.run(
                    [node, "-e", program, str(source_path), str(source_path.with_name("ui-admin.js"))],
                    stdin=subprocess.DEVNULL,
                    stdout=stream, stderr=subprocess.STDOUT, timeout=30, check=False,
                )
            evidence = output.read_text(encoding="utf-8")
            self.assertEqual(completed.returncode, 0, evidence)
            print(evidence.strip())


@unittest.skipUnless(os.environ.get("DHB_BROWSER_TESTS") == "1", "Run in the browser CI job")
class ScheduleStatusUIBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        # Reserve an ephemeral port without starting even a fixture HTTP server.
        # UIPage intercepts every request, including assets, before networking.
        self.port_reservation = socket.socket()
        self.port_reservation.bind(("127.0.0.1", 0))
        self.addCleanup(self.port_reservation.close)
        self.base = f"http://127.0.0.1:{self.port_reservation.getsockname()[1]}"
        base_patch = mock.patch("ui_browser_fixture.BASE", self.base)
        base_patch.start()
        self.addCleanup(base_patch.stop)
        self.fixture = UIPage(self.browser)
        self.addCleanup(self.fixture.close)
        self.page = self.fixture.page
        self.fixture.seed_queue([])

    def tearDown(self):
        self.assertEqual(self.fixture.errors, [])
        self.assertEqual(self.fixture.external, [])

    @staticmethod
    def waiting(**changes):
        payload = {
            "configured": True,
            "status": "waiting",
            "message": "Đang chờ đủ khoảng cách giữa hai lượt đăng.",
            "completed_count": 1,
            "total_count": 8,
            "next_queue_id": 240,
            "next_due_at": "2026-11-23T16:15:00+00:00",
            "folder_drift": {
                "detected": True,
                "missing_future_count": 7,
                "new_files_count": 7,
                "next_source_exists": False,
            },
            "action_required": True,
            "task_session_required": True,
        }
        payload.update(changes)
        return payload

    def open_schedule(self, payload):
        dialog = self.page.locator("#publish-workflow-dialog")
        if dialog.count():
            dialog.locator("#publish-close").click()
            dialog.wait_for(state="detached")
        self.fixture.responses["/api/scheduled-uploads/status"] = payload
        with self.page.expect_response("**/api/scheduled-uploads/status") as response:
            self.fixture.open_more_actions()
            self.page.locator("#publish-workflow-open").click()
        response.value.body()
        # Flush the rendering frame, also for unconfigured/malformed top levels.
        self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
        return self.page.locator("#publish-schedule-status")

    def mutations(self):
        return [row for row in self.fixture.requests if row["method"] not in ("GET", "HEAD")]

    def capture(self, name, banner):
        target = os.environ.get("DHB_SCHEDULE_UI_EVIDENCE")
        if not target:
            return
        directory = Path(target)
        directory.mkdir(parents=True, exist_ok=True)
        banner.screenshot(path=str(directory / f"{name}-banner.png"))
        self.page.screenshot(path=str(directory / f"{name}-page.png"), full_page=True)
        (directory / f"{name}.json").write_text(json.dumps({
            "fixture_base": self.base,
            "banner_state": banner.get_attribute("data-state"),
            "headline": banner.locator(".publish-scope strong").inner_text(),
            "text": banner.inner_text(),
            "requests": self.fixture.requests,
            "external_requests": self.fixture.external,
            "page_errors": self.fixture.errors,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_waiting_missing_next_source_is_prominent_and_readonly(self):
        banner = self.open_schedule(self.waiting())
        banner.wait_for(state="visible")
        self.assertEqual(banner.get_attribute("data-state"), "error")
        headline = banner.locator(".publish-scope strong").inner_text()
        self.assertIn("Cần xử lý nguồn trước lượt đăng tiếp", headline)
        self.assertIn("file kế tiếp không còn trên đĩa", headline)
        self.assertNotIn("Đang chờ", headline)
        text = banner.inner_text()
        self.assertIn("Trạng thái lịch: Đang chờ", text)
        self.assertIn("7 file chờ không còn trên đĩa", text)
        self.assertIn("7 file mới chưa nạp vào lịch", text)
        self.assertIn("Kiểm tra đúng thư mục/file nguồn", text)
        self.assertIn("File mới không mặc nhiên là file đổi tên", text)
        self.assertIn("Đối chiếu nội dung", text)
        self.assertTrue(banner.locator("#publish-schedule-sync").is_enabled())
        self.assertEqual(self.mutations(), [])
        self.capture("waiting-missing-next-source", banner)

    def test_sync_is_explicit_and_clears_only_after_returned_status(self):
        payload = self.waiting(action_required=False, folder_drift={
            "detected": True, "missing_future_count": 1,
            "new_files_count": 2, "next_source_exists": True,
        })
        banner = self.open_schedule(payload)
        self.assertEqual(banner.get_attribute("data-state"), "warning")
        self.assertIn("Cần kiểm tra nguồn", banner.locator("strong").inner_text())
        self.assertEqual(self.mutations(), [])
        self.fixture.responses["/api/scheduled-uploads/sync"] = {
            "ok": True, "removed_pending_count": 1, "added_count": 2,
            "schedule": self.waiting(action_required=False, total_count=9, folder_drift={
                "detected": False, "missing_future_count": 0,
                "new_files_count": 0, "next_source_exists": True,
            }),
        }
        with self.page.expect_response("**/api/scheduled-uploads/sync"):
            banner.locator("#publish-schedule-sync").click()
        self.page.wait_for_function("document.getElementById('publish-schedule-status').dataset.state === 'ready'")
        self.assertIn("1/9 video", banner.inner_text())
        self.assertNotIn("Cần xử lý nguồn", banner.inner_text())
        self.assertEqual([(row["method"], row["path"]) for row in self.mutations()], [
            ("POST", "/api/scheduled-uploads/sync"),
        ])
        self.assertEqual(json.loads(self.mutations()[0]["body"]), {})

    def test_active_upload_has_warning_without_sync_button(self):
        for status in ("running", "submitted"):
            with self.subTest(status=status):
                banner = self.open_schedule(self.waiting(status=status, inflight_queue_id=239))
                self.assertEqual(banner.get_attribute("data-state"), "error")
                self.assertEqual(banner.locator("#publish-schedule-sync").count(), 0)
                self.assertIn("Chờ lượt đang xử lý kết thúc", banner.inner_text())
        self.assertEqual(self.mutations(), [])

    def test_missing_optional_fields_still_honor_explicit_missing_source(self):
        banner = self.open_schedule({
            "configured": True, "status": "waiting",
            "folder_drift": {"next_source_exists": False},
        })
        self.assertEqual(banner.get_attribute("data-state"), "error")
        self.assertIn("Cần xử lý nguồn", banner.locator("strong").inner_text())
        self.assertNotIn("0 file", banner.inner_text())
        self.assertNotIn("undefined", banner.inner_text())
        self.assertNotIn("()", banner.inner_text())
        self.assertEqual(self.mutations(), [])

    def test_counts_warn_even_when_detected_flag_is_missing_or_false(self):
        for flag in (None, False):
            with self.subTest(detected=flag):
                drift = {"new_files_count": 7, "next_source_exists": True}
                if flag is not None:
                    drift["detected"] = flag
                banner = self.open_schedule(self.waiting(action_required=False, folder_drift=drift))
                self.assertEqual(banner.get_attribute("data-state"), "warning")
                self.assertIn("7 file mới chưa nạp vào lịch", banner.inner_text())
        self.assertEqual(self.mutations(), [])

    def test_absent_drift_and_unconfigured_payloads_are_safe(self):
        banner = self.open_schedule({"configured": True, "status": "waiting"})
        self.assertEqual(banner.get_attribute("data-state"), "ready")
        self.assertNotIn("undefined", banner.inner_text())
        for payload in (None, [], "broken", {}, {"configured": False}, {"configured": "true"}):
            with self.subTest(payload=payload):
                banner = self.open_schedule(payload)
                self.assertTrue(banner.is_hidden())
                self.assertEqual(banner.locator("#publish-schedule-sync").count(), 0)
        self.assertEqual(self.mutations(), [])

    def test_malformed_drift_never_claims_ready_or_offers_sync(self):
        malformed = [
            "broken", [], {"detected": "false"}, {"next_source_exists": "false"},
            {"new_files_count": "7"}, {"missing_future_count": -1},
            {"detected": True, "new_files_count": '<img src=x onerror="window.scheduleXss=1">'},
        ]
        for drift in malformed:
            with self.subTest(drift=drift):
                banner = self.open_schedule(self.waiting(action_required=False, folder_drift=drift))
                self.assertEqual(banner.get_attribute("data-state"), "error")
                self.assertIn("dữ liệu kiểm tra thư mục không hợp lệ", banner.inner_text())
                self.assertIn("Tải lại trạng thái", banner.inner_text())
                self.assertEqual(banner.locator("#publish-schedule-sync").count(), 0)
                self.assertEqual(banner.locator("img,svg,script").count(), 0)
                self.assertFalse(self.page.evaluate("Boolean(window.scheduleXss)"))
        self.assertEqual(self.mutations(), [])

    def test_api_strings_in_warning_are_rendered_as_text(self):
        attack = '<img src=x onerror="window.scheduleXss=1"> & <svg onload="window.scheduleXss=2">'
        banner = self.open_schedule(self.waiting(
            message=attack, next_queue_id=attack, next_due_at=attack,
            last_result={"queue_id": attack, "status": attack},
        ))
        self.assertIn(attack, banner.inner_text())
        self.assertEqual(banner.locator("img,svg,script").count(), 0)
        self.assertFalse(self.page.evaluate("Boolean(window.scheduleXss)"))
        self.assertEqual(self.mutations(), [])

    def test_diagnostics_prioritize_source_warning_and_file_counts(self):
        for next_exists, label in ((False, "Cần xử lý nguồn"), (True, "Cần kiểm tra nguồn")):
            with self.subTest(next_source_exists=next_exists):
                payload = self.waiting(action_required=not next_exists, folder_drift={
                    "detected": True, "missing_future_count": 7,
                    "new_files_count": 7, "next_source_exists": next_exists,
                })
                self.page.evaluate(
                    "payload => StudioAdmin.onDiagnosticsLoaded({scheduled_uploads: payload})", payload,
                )
                card = self.page.locator("#admin-diagnostics-cards .admin-metric").filter(has_text="Lịch đăng Private")
                self.assertIn(label, card.locator("strong").inner_text())
                self.assertNotIn("waiting", card.locator("strong").inner_text())
                self.assertIn("7 file chờ không còn trên đĩa", card.inner_text())
                self.assertIn("7 file mới chưa nạp vào lịch", card.inner_text())
                self.assertIn("File mới không mặc nhiên là file đổi tên", card.inner_text())
                self.assertIn("kiểm tra nguồn trước khi đồng bộ", card.inner_text())
        self.assertEqual(self.mutations(), [])

    def test_diagnostics_malformed_source_and_api_text_are_safe(self):
        attack = '<img src=x onerror="window.scheduleXss=1">'
        cases = [
            (self.waiting(action_required=False, folder_drift={"next_source_exists": "false"}), "tải lại trạng thái"),
            ({"configured": True, "status": attack, "message": attack, "action_required": True}, attack),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.page.evaluate(
                    "payload => StudioAdmin.onDiagnosticsLoaded({scheduled_uploads: payload})", payload,
                )
                card = self.page.locator("#admin-diagnostics-cards .admin-metric").filter(has_text="Lịch đăng Private")
                self.assertIn(expected, card.inner_text())
                self.assertEqual(card.locator("img,svg,script").count(), 0)
                self.assertFalse(self.page.evaluate("Boolean(window.scheduleXss)"))
        self.assertEqual(self.mutations(), [])

    def test_source_warning_preserves_manual_preview_confirmation(self):
        self.fixture.seed_queue([{
            "id": 240, "title": "Fixture video", "status": "Rendered",
            "target_channel_id": 1, "visibility": "private",
        }])
        self.page.evaluate("gSelectedIds = new Set([240]); updateQueueSelectionSummary()")
        self.fixture.responses["/api/queue/preview-distribution"] = {
            "items": [{
                "id": 240, "title": "Fixture video", "visibility": "private",
                "channel_name": "Kênh mẫu", "profile_name": "Profile mẫu",
                "youtube_channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa", "upload_mode": "playwright",
                "file_name": "fixture.mp4", "file_kind": "File có sẵn",
                "blocked": False, "reasons": [], "warnings": [],
            }],
            "blocked": 0, "preview_id": "fixture-preview-token",
        }
        self.open_schedule(self.waiting())
        self.assertEqual(self.mutations(), [])
        self.page.locator("#publish-continue").click()
        preview = self.page.locator(".upload-preview-dialog")
        preview.wait_for(state="visible")
        self.assertTrue(preview.get_by_role("button", name="Xác nhận và chạy", exact=True).is_visible())
        self.assertEqual([row["path"] for row in self.mutations()], ["/api/queue/preview-distribution"])
        preview.get_by_role("button", name="Đóng", exact=True).click()
        preview.wait_for(state="detached")
        self.assertEqual([row["path"] for row in self.mutations()], ["/api/queue/preview-distribution"])


if __name__ == "__main__":
    unittest.main()
