"""Versioned, fail-closed one-video Private upload schedules.

Importing this module performs no IO. Operational JSON and logs belong in an
external state directory; they are deliberately not application source files.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

import requests


CONTRACT_VERSION = 1
MANIFEST_VERSION = 2
STATE_VERSION = 2
APPLICATION = "dhb-reup-youtube-studio"
JSON_LIMIT = 512 * 1024
PUBLIC_STATES = frozenset(("ready", "waiting", "submitted", "running", "completed", "needs_review", "error", "missed", "finished"))
MESSAGES = {
    "ready": "Lịch đã sẵn sàng.", "waiting": "Đang chờ đủ khoảng cách giữa hai lượt đăng.",
    "submitted": "Đã gửi lệnh; đang chờ kết quả video.", "running": "Đang theo dõi kết quả video.",
    "completed": "Video gần nhất đã hoàn tất.", "needs_review": "Lịch đã dừng để đối soát video.",
    "error": "Lịch gặp lỗi; kiểm tra trạng thái trước khi tiếp tục.",
    "missed": "Chưa nhận được kết quả hoặc lượt kiểm tra đúng hạn.", "finished": "Đã hoàn tất toàn bộ lô video.",
}


class ScheduleError(RuntimeError):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


def utc_now():
    return datetime.now(timezone.utc)


def parse_time(value):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ScheduleError("TIME_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ScheduleError("TIME_INVALID") from None
    if parsed.tzinfo is None:
        raise ScheduleError("TIMEZONE_REQUIRED")
    return parsed.astimezone(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat()


def load_json(path):
    path = Path(path)
    if path.stat().st_size > JSON_LIMIT:
        raise ScheduleError("JSON_TOO_LARGE")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError):
        raise ScheduleError("JSON_INVALID") from None
    if not isinstance(value, dict):
        raise ScheduleError("JSON_INVALID")
    return value


def save_json(path, value):
    """Durable replace; temporary names never collide across interrupted runs."""
    path = Path(path)
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(data.encode("utf-8")) > JSON_LIMIT:
        raise ScheduleError("JSON_TOO_LARGE")
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_digest(manifest):
    return hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _integer(value, minimum=1):
    return type(value) is int and value >= minimum


def validate_manifest(manifest):
    if manifest.get("version") != MANIFEST_VERSION or manifest.get("runner_contract") != CONTRACT_VERSION:
        raise ScheduleError("MANIFEST_VERSION")
    if not isinstance(manifest.get("project"), str) or not 1 <= len(manifest["project"]) <= 255:
        raise ScheduleError("MANIFEST_PROJECT")
    if not _integer(manifest.get("cadence_hours"), 72) or manifest["cadence_hours"] > 8760:
        raise ScheduleError("MANIFEST_CADENCE")
    parse_time(manifest.get("start_at"))
    channel = manifest.get("channel")
    if (not isinstance(channel, dict) or not _integer(channel.get("id"))
            or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", str(channel.get("channel_id", "")))
            or not isinstance(channel.get("name"), str) or not 1 <= len(channel["name"]) <= 255):
        raise ScheduleError("MANIFEST_CHANNEL")
    root = Path(str(manifest.get("source_directory", "")))
    if not root.is_absolute():
        raise ScheduleError("MANIFEST_SOURCE")
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= 1000:
        raise ScheduleError("MANIFEST_JOBS")
    ids, paths = set(), set()
    for job in jobs:
        if (not isinstance(job, dict) or not _integer(job.get("queue_id"))
                or not re.fullmatch(r"[a-fA-F0-9]{64}", str(job.get("sha256", "")))):
            raise ScheduleError("MANIFEST_JOB")
        path = Path(str(job.get("path", "")))
        try:
            path.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            raise ScheduleError("MANIFEST_SOURCE") from None
        if not path.is_absolute() or path.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
            raise ScheduleError("MANIFEST_SOURCE")
        canonical = os.path.normcase(str(path.resolve()))
        if job["queue_id"] in ids or canonical in paths:
            raise ScheduleError("MANIFEST_DUPLICATE")
        ids.add(job["queue_id"])
        paths.add(canonical)
    return jobs


def validate_release(release, runtime_dir):
    if (release.get("version") != 1 or release.get("runner_contract") != CONTRACT_VERSION
            or not re.fullmatch(r"[a-f0-9]{40}", str(release.get("revision", "")))
            or not isinstance(release.get("build"), str) or not 1 <= len(release["build"]) <= 100
            or Path(str(release.get("runtime_root", ""))).resolve() != Path(runtime_dir).resolve()):
        raise ScheduleError("RELEASE_PIN_INVALID")
    hashes = release.get("runner_hashes")
    expected_names = {"scheduled_uploads.py", "scheduled_upload_runner.py", "scheduled_upload_task.py"}
    if not isinstance(hashes, dict) or set(hashes) != expected_names:
        raise ScheduleError("RUNNER_PIN_INVALID")
    package = Path(__file__).resolve().parent
    if any(not re.fullmatch(r"[a-f0-9]{64}", str(value)) or sha256(package / name) != value
           for name, value in hashes.items()):
        raise ScheduleError("RUNNER_PIN_MISMATCH")


def new_state(manifest):
    return {"version": STATE_VERSION, "manifest_sha256": manifest_digest(manifest), "next_index": 0,
            "last_started_at": None, "inflight_queue_id": None, "submission_state": "ready",
            "submitted_at": None, "status": "ready", "last_checked_at": None,
            "last_completed_at": None, "last_result": None, "error_code": ""}


def validate_state(state, manifest):
    jobs = manifest["jobs"]
    if (state.get("version") != STATE_VERSION or state.get("manifest_sha256") != manifest_digest(manifest)
            or not _integer(state.get("next_index"), 0) or state["next_index"] > len(jobs)
            or state.get("status") not in PUBLIC_STATES
            or state.get("submission_state") not in {"ready", "submitting", "started", "halted"}):
        raise ScheduleError("STATE_INVALID")
    index, inflight = state["next_index"], state.get("inflight_queue_id")
    if inflight is not None and (index >= len(jobs) or inflight != jobs[index]["queue_id"]):
        raise ScheduleError("STATE_INFLIGHT_INVALID")
    if state["submission_state"] in {"submitting", "started"} and not inflight:
        raise ScheduleError("STATE_INFLIGHT_INVALID")
    if inflight and not state.get("submitted_at"):
        raise ScheduleError("STATE_SUBMISSION_MISSING")
    for key in ("last_started_at", "submitted_at", "last_checked_at", "last_completed_at"):
        if state.get(key) is not None:
            parse_time(state[key])
    if index and not state.get("last_started_at"):
        raise ScheduleError("STATE_CADENCE_MISSING")
    return state


def migrate_legacy(manifest, state, *, start_at):
    """Pure conversion. No live changes, resets, new uploads or inferred success."""
    if state.get("version") != 1 or "version" in manifest:
        raise ScheduleError("LEGACY_VERSION_INVALID")
    converted_manifest = {key: value for key, value in manifest.items() if key not in {"revision", "build"}}
    converted_manifest.update(version=MANIFEST_VERSION, runner_contract=CONTRACT_VERSION, start_at=start_at)
    validate_manifest(converted_manifest)
    converted_state = new_state(converted_manifest)
    for key in ("next_index", "last_started_at", "inflight_queue_id", "submission_state", "submitted_at"):
        if key not in state:
            raise ScheduleError("LEGACY_STATE_INCOMPLETE")
        converted_state[key] = state[key]
    converted_state["status"] = ("needs_review" if state["submission_state"] == "halted" else
                                 "submitted" if state["inflight_queue_id"] else "ready")
    validate_state(converted_state, converted_manifest)
    return converted_manifest, converted_state


@contextmanager
def single_instance(state_dir):
    path = Path(state_dir) / "schedule.lock"
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ScheduleError("RUNNER_ALREADY_ACTIVE") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def next_due(manifest, state):
    first = parse_time(manifest["start_at"])
    if state.get("last_started_at"):
        first = max(first, parse_time(state["last_started_at"]) + timedelta(hours=manifest["cadence_hours"]))
    return first


def read_schedule_status(state_dir, *, now=None):
    """Safe UI view. Never contacts a runtime, writes state or exposes file paths."""
    if not state_dir or not (Path(state_dir) / "manifest.json").is_file():
        return {"configured": False, "status": "unconfigured", "message": "Chưa cấu hình lịch upload Private."}
    try:
        manifest = load_json(Path(state_dir) / "manifest.json")
        validate_manifest(manifest)
        state = load_json(Path(state_dir) / "state.json")
        validate_state(state, manifest)
        current = now or utc_now()
        due = next_due(manifest, state)
        status = state["status"]
        checked = parse_time(state["last_checked_at"]) if state.get("last_checked_at") else None
        if status in {"submitted", "running"} and (not checked or current - checked > timedelta(minutes=15)):
            status = "missed"
        elif status in {"ready", "waiting", "completed"} and state["next_index"] < len(manifest["jobs"]) and current > due + timedelta(minutes=15):
            status = "missed"
        result = state.get("last_result")
        public_result = None
        if isinstance(result, dict) and result.get("status") in PUBLIC_STATES and _integer(result.get("queue_id")):
            public_result = {"queue_id": result["queue_id"], "status": result["status"]}
            if result.get("finished_at"):
                public_result["finished_at"] = stamp(parse_time(result["finished_at"]))
        index = state["next_index"]
        return {"configured": True, "status": status, "message": MESSAGES[status],
                "last_checked_at": state.get("last_checked_at"), "last_started_at": state.get("last_started_at"),
                "last_result": public_result, "next_due_at": stamp(due) if index < len(manifest["jobs"]) else None,
                "inflight_queue_id": state.get("inflight_queue_id"),
                "next_queue_id": manifest["jobs"][index]["queue_id"] if index < len(manifest["jobs"]) else None,
                "completed_count": index, "total_count": len(manifest["jobs"]),
                "task_session_required": True,
                "action_required": status in {"needs_review", "error", "missed"},
                "task_disable_recommended": status == "finished"}
    except (OSError, ScheduleError, TypeError, ValueError, KeyError):
        return {"configured": True, "status": "error", "message": "Cấu hình hoặc trạng thái lịch không hợp lệ; cần kiểm tra trước khi đăng.", "action_required": True}


class LocalAPI:
    """Loopback-only client. Credentials and provider bodies never leave this class."""
    def __init__(self, base_url, runtime_dir, release, *, session=None):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ScheduleError("LOOPBACK_REQUIRED")
        self.base = base_url.rstrip("/")
        self.runtime_dir = Path(runtime_dir)
        self.release = release
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"Origin": self.base})

    def request(self, method, path, **kwargs):
        try:
            response = self.session.request(method, self.base + path, timeout=kwargs.pop("timeout", 20), **kwargs)
            if response.status_code >= 400:
                raise ScheduleError("LOCAL_API_REJECTED")
            result = response.json()
            if not isinstance(result, (dict, list)):
                raise ScheduleError("LOCAL_API_INVALID")
            return result
        except (requests.RequestException, ValueError):
            raise ScheduleError("LOCAL_API_UNAVAILABLE") from None

    def connect(self):
        health = self.request("GET", "/health/dhb")
        if (health.get("application") != APPLICATION
                or Path(str(health.get("source_root", ""))).resolve() != self.runtime_dir.resolve()):
            raise ScheduleError("RUNTIME_IDENTITY_CHANGED")
        token = self.request("GET", "/api/auth/token").get("token")
        if not isinstance(token, str) or not token:
            raise ScheduleError("LOCAL_AUTH_UNAVAILABLE")
        self.session.headers["X-DHB-Token"] = token

    def diagnostics(self, *, idle=False):
        result = self.request("GET", "/api/diagnostics")
        runtime = result.get("runtime") or {}
        if (result.get("version") != self.release["build"] or runtime.get("revision") != self.release["revision"]
                or runtime.get("restart_required") is not False):
            raise ScheduleError("RUNTIME_RELEASE_CHANGED")
        workers = result.get("workers") or {}
        if idle and any(workers.get(key) for key in ("pipeline_running", "active_batch_threads", "monitor_workers")):
            raise ScheduleError("WORKER_BUSY")
        return result

    def verify_channel(self, manifest):
        expected = manifest["channel"]
        channels = self.request("GET", "/api/channels")
        channel = next((row for row in channels if row.get("id") == expected["id"]), None)
        if (not channel or any(channel.get(key) != expected[key] for key in ("channel_id", "name"))
                or channel.get("upload_mode") != "youtube_api"):
            raise ScheduleError("CHANNEL_CHANGED")

    def queue(self, manifest):
        rows = self.request("GET", "/api/queue", params={"project": manifest["project"]})
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ScheduleError("QUEUE_INVALID")
        found = {row.get("id"): row for row in rows}
        if len(found) != len(rows) or set(found) != {job["queue_id"] for job in manifest["jobs"]}:
            raise ScheduleError("QUEUE_SCOPE_CHANGED")
        for job in manifest["jobs"]:
            row = found[job["queue_id"]]
            if (os.path.normcase(str(Path(str(row.get("input_video_path") or "")).resolve())) != os.path.normcase(str(Path(job["path"]).resolve()))
                    or row.get("visibility") != "private" or row.get("schedule_time")
                    or row.get("target_channel_id") != manifest["channel"]["id"]):
                raise ScheduleError("QUEUE_BINDING_CHANGED")
        return found

    def preview(self, manifest, queue_id):
        payload = {"ids": [queue_id], "do_download": False, "do_render": False, "do_upload": True}
        result = self.request("POST", "/api/queue/preview-distribution", json=payload)
        items = result.get("items") or []
        if not result.get("ok") or len(items) != 1 or not result.get("preview_id"):
            raise ScheduleError("PREVIEW_BLOCKED")
        row = items[0]
        if (row.get("id") != queue_id or row.get("blocked") or row.get("channel_id") != manifest["channel"]["id"]
                or row.get("channel_name") != manifest["channel"]["name"] or row.get("upload_mode") != "youtube_api"
                or row.get("visibility") != "private" or row.get("schedule_time")):
            raise ScheduleError("PREVIEW_CHANGED")
        return {**payload, "preview_id": result["preview_id"]}

    def start(self, payload):
        return self.request("POST", "/api/pipeline/start", json=payload, timeout=30)


class ScheduleRunner:
    def __init__(self, state_dir, runtime_dir, *, api_factory=LocalAPI, base_url="http://127.0.0.1:8686",
                 now=utc_now, monotonic=time.monotonic, sleep=time.sleep, observer_seconds=3600, poll_seconds=5):
        self.directory, self.runtime = Path(state_dir), Path(runtime_dir)
        self.api_factory, self.base_url = api_factory, base_url
        self.now, self.monotonic, self.sleep = now, monotonic, sleep
        self.observer_seconds = max(1, min(5400, int(observer_seconds)))
        self.poll_seconds = max(.1, min(30, float(poll_seconds)))
        self.state = self.manifest = self.api = None

    def _save(self, status=None, code=""):
        if status:
            self.state["status"] = status
        self.state["last_checked_at"] = stamp(self.now())
        self.state["error_code"] = code
        save_json(self.directory / "state.json", self.state)

    def _check_rows(self, rows):
        index, inflight = self.state["next_index"], self.state.get("inflight_queue_id")
        for offset, job in enumerate(self.manifest["jobs"]):
            row = rows[job["queue_id"]]
            if offset < index:
                if row.get("status") != "Completed" or not row.get("published_url") or row.get("is_claimed") or row.get("requires_review"):
                    raise ScheduleError("COMPLETED_ITEM_CHANGED")
            elif job["queue_id"] != inflight and (row.get("status") != "Pending" or row.get("published_url")
                    or row.get("is_claimed") or row.get("requires_review")):
                raise ScheduleError("PENDING_ITEM_CHANGED")

    def _observe(self):
        """Read-only after submission. A resumed run returns before any new start."""
        queue_id = self.state["inflight_queue_id"]
        deadline = self.monotonic() + self.observer_seconds
        while True:
            self.api.diagnostics()
            self.api.verify_channel(self.manifest)
            rows = self.api.queue(self.manifest)
            self._check_rows(rows)
            row = rows[queue_id]
            if row.get("status") == "Completed" and row.get("published_url") and not row.get("is_claimed") and not row.get("requires_review"):
                submitted = parse_time(self.state["submitted_at"])
                prior = parse_time(self.state["last_started_at"]) if self.state.get("last_started_at") else submitted
                self.state.update(next_index=self.state["next_index"] + 1, inflight_queue_id=None,
                                  submission_state="ready", submitted_at=None,
                                  last_started_at=stamp(max(submitted, prior)), last_completed_at=stamp(self.now()),
                                  last_result={"queue_id": queue_id, "status": "completed", "finished_at": stamp(self.now())})
                final = "finished" if self.state["next_index"] == len(self.manifest["jobs"]) else "completed"
                self._save(final)
                return 0
            if row.get("requires_review") or row.get("status") == "NeedsReview":
                self.state.update(submission_state="halted", last_result={"queue_id": queue_id, "status": "needs_review", "finished_at": stamp(self.now())})
                self._save("needs_review", "UPLOAD_REVIEW_REQUIRED")
                return 2
            status = str(row.get("status") or "")
            active = bool(row.get("is_claimed")) or status.startswith(("Uploading", "Downloading", "Rendering", "Verifying", "Đang đăng", "Chờ"))
            if not active:
                self.state.update(submission_state="halted", last_result={"queue_id": queue_id, "status": "error", "finished_at": stamp(self.now())})
                self._save("error", "UPLOAD_RESULT_UNCERTAIN")
                return 2
            self.state["submission_state"] = "started"
            self._save("running")
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                self._save("missed", "OBSERVER_TIMEOUT")
                return 3
            self.sleep(min(self.poll_seconds, remaining))

    def run(self, *, dry_run=False, observe_only=False):
        if not self.directory.is_dir():
            raise ScheduleError("SCHEDULE_UNINITIALIZED")
        with single_instance(self.directory):
            self.manifest = load_json(self.directory / "manifest.json")
            jobs = validate_manifest(self.manifest)
            # Missing state is a hard stop, never permission to restart from job 0.
            self.state = load_json(self.directory / "state.json")
            validate_state(self.state, self.manifest)
            release = load_json(self.directory / "release.json")
            validate_release(release, self.runtime)
            self.api = self.api_factory(self.base_url, self.runtime, release)
            try:
                self.api.connect()
                self.api.diagnostics()
                self.api.verify_channel(self.manifest)
                rows = self.api.queue(self.manifest)
                self._check_rows(rows)
                if self.state.get("inflight_queue_id"):
                    if dry_run:
                        return 0
                    return self._observe()
                if self.state["submission_state"] == "halted":
                    if not dry_run:
                        self._save("needs_review", "SCHEDULE_HALTED")
                    return 2
                if self.state["next_index"] >= len(jobs):
                    if not dry_run:
                        self._save("finished")
                    return 0
                if observe_only:
                    return 0
                if dry_run:
                    diagnostics = self.api.diagnostics(idle=True)
                    slot = diagnostics.get("upload_slot") or {}
                    if not slot.get("available") or slot.get("state") == "recovery_required":
                        raise ScheduleError("UPLOAD_SLOT_UNAVAILABLE")
                    for pending_job in jobs[self.state["next_index"]:]:
                        if not Path(pending_job["path"]).is_file() or sha256(pending_job["path"]) != pending_job["sha256"].lower():
                            raise ScheduleError("SOURCE_CHANGED")
                    self.api.preview(self.manifest, jobs[self.state["next_index"]]["queue_id"])
                    return 0
                due = next_due(self.manifest, self.state)
                remaining = (due - self.now()).total_seconds()
                if remaining > 0:
                    if remaining > 600:
                        self._save("waiting")
                        return 0
                    deadline = self.monotonic() + 600
                    while self.now() < due:
                        seconds = min(30, (due - self.now()).total_seconds(), deadline - self.monotonic())
                        if seconds <= 0:
                            raise ScheduleError("CADENCE_CLOCK_DRIFT")
                        self.sleep(seconds)
                diagnostics = self.api.diagnostics(idle=True)
                self.api.verify_channel(self.manifest)
                rows = self.api.queue(self.manifest)
                self._check_rows(rows)
                slot = diagnostics.get("upload_slot") or {}
                if not slot.get("available") or slot.get("state") == "recovery_required":
                    raise ScheduleError("UPLOAD_SLOT_UNAVAILABLE")
                if slot.get("state") == "active" or (slot.get("remaining_seconds") or 0) > 0:
                    raise ScheduleError("UPLOAD_SLOT_BUSY")
                job = jobs[self.state["next_index"]]
                if not Path(job["path"]).is_file() or sha256(job["path"]) != job["sha256"].lower():
                    raise ScheduleError("SOURCE_CHANGED")
                payload = self.api.preview(self.manifest, job["queue_id"])
                submitted = stamp(self.now())
                self.state.update(inflight_queue_id=job["queue_id"], submission_state="submitting", submitted_at=submitted,
                                  last_started_at=submitted)
                self._save("submitted")  # This must be durable before the only mutating start request.
                result = self.api.start(payload)
                if not result.get("ok"):
                    self.state["submission_state"] = "halted"
                    self._save("needs_review", "START_REJECTED")
                    return 2
                self.state["submission_state"] = "started"
                self._save("running")
                return self._observe()
            except (ScheduleError, OSError, ValueError, TypeError, KeyError):
                # Keep inflight/submitting intact: an uncertain response cannot authorize resend.
                if not dry_run:
                    self._save("error", "SCHEDULE_CHECK_FAILED")
                raise
            finally:
                session = getattr(self.api, "session", None)
                if session is not None:
                    session.close()
