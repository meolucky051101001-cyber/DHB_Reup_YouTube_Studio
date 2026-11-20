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
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import requests

from app_instance_lock import AppInstanceAlreadyRunning, AppInstanceLock


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
def runtime_operation_lock(runtime_dir):
    """Exclude runner and rollout operations before they take a schedule lock."""
    lock = AppInstanceLock(Path(runtime_dir).resolve() / "data" / "schedule_operation.lock")
    try:
        lock.acquire()
    except AppInstanceAlreadyRunning:
        raise ScheduleError("RUNTIME_OPERATION_ACTIVE") from None
    try:
        yield
    finally:
        lock.release()


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


VIDEO_SUFFIXES = frozenset((".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"))


def detect_folder_drift(manifest, state):
    """Safe metadata-only check; never hashes files or exposes filesystem paths."""
    jobs = manifest["jobs"]
    index = state["next_index"]
    pending = jobs[index:]
    next_exists = True if not pending else Path(pending[0]["path"]).is_file()
    missing_future = sum(1 for job in pending if not Path(job["path"]).is_file())
    known = {os.path.normcase(str(Path(job["path"]).resolve())) for job in jobs}
    source_root = Path(manifest["source_directory"])
    new_files = 0
    if source_root.is_dir():
        for entry in source_root.iterdir():
            if entry.is_file() and entry.suffix.lower() in VIDEO_SUFFIXES:
                if os.path.normcase(str(entry.resolve())) not in known:
                    new_files += 1
    return {
        "detected": bool(missing_future or new_files),
        "missing_future_count": missing_future,
        "new_files_count": new_files,
        "next_source_exists": next_exists,
    }


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
        drift = detect_folder_drift(manifest, state)
        message = MESSAGES[status]
        if status == "error" and state.get("error_code") == "SOURCE_CHANGED":
            message = "Tệp nguồn của lượt kế tiếp đã thay đổi, không còn hoặc không đọc được. Cần kiểm tra trước khi đăng."
        return {"configured": True, "status": status, "message": message,
                "last_checked_at": state.get("last_checked_at"), "last_started_at": state.get("last_started_at"),
                "last_result": public_result, "next_due_at": stamp(due) if index < len(manifest["jobs"]) else None,
                "inflight_queue_id": state.get("inflight_queue_id"),
                "next_queue_id": manifest["jobs"][index]["queue_id"] if index < len(manifest["jobs"]) else None,
                "completed_count": index, "total_count": len(manifest["jobs"]),
                "folder_drift": drift,
                "task_session_required": True,
                "action_required": status in {"needs_review", "error", "missed"} or not drift["next_source_exists"],
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

    def _ensure_local_server(self, port: int) -> dict:
        try:
            return self.request("GET", "/health/dhb", timeout=4)
        except ScheduleError:
            if not (self.runtime_dir / "app.py").is_file() or not Path(sys.executable).is_file():
                raise
            log_dir = self.runtime_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            with (log_dir / "scheduled-server.log").open("ab") as log_handle:
                subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
                    cwd=self.runtime_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=log_handle,
                    creationflags=flags,
                    close_fds=True,
                )
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                time.sleep(1)
                try:
                    return self.request("GET", "/health/dhb", timeout=3)
                except ScheduleError:
                    continue
            raise ScheduleError("LOCAL_API_UNAVAILABLE")

    def connect(self, *, auto_start=False):
        parsed_port = urlsplit(self.base).port or 8686
        health = self._ensure_local_server(parsed_port) if auto_start else self.request("GET", "/health/dhb")
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

    def project_rows(self, project):
        rows = self.request("GET", "/api/queue", params={"project": project})
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ScheduleError("QUEUE_INVALID")
        return rows

    def delete_rows(self, ids):
        if not ids:
            return {"ok": True, "deleted": 0}
        result = self.request("POST", "/api/queue/delete", json={"ids": list(ids)}, timeout=30)
        if not isinstance(result, dict) or not result.get("ok"):
            raise ScheduleError("QUEUE_DELETE_FAILED")
        return result

    def scan_folder(self, manifest):
        result = self.request(
            "POST",
            "/api/queue/scan",
            json={
                "project_name": manifest["project"],
                "urls": manifest["source_directory"],
                "max_items": 500,
                "recursive": False,
                "target_channel_id": manifest["channel"]["id"],
                "upload_to": "Shorts",
                "render_mode": "shorts_9_16",
                "resolution": "1080p",
                "visibility": "private",
                "schedule_time": "",
                "made_for_kids": 0,
                "age_restriction": 0,
                "altered_content": 0,
                "emulate_mobile": False,
            },
            timeout=90,
        )
        if not isinstance(result, dict) or not result.get("ok"):
            raise ScheduleError("QUEUE_SCAN_FAILED")
        return result

    def preview_batch(self, manifest, queue_ids):
        if not queue_ids:
            return
        payload = {"ids": list(queue_ids), "do_download": False, "do_render": False, "do_upload": True}
        result = self.request("POST", "/api/queue/preview-distribution", json=payload, timeout=45)
        items = result.get("items") or []
        if not result.get("ok") or len(items) != len(queue_ids):
            raise ScheduleError("PREVIEW_BLOCKED")
        expected = manifest["channel"]
        for row in items:
            if (row.get("blocked") or row.get("channel_id") != expected["id"]
                    or row.get("channel_name") != expected["name"] or row.get("upload_mode") != "youtube_api"
                    or row.get("visibility") != "private" or row.get("schedule_time")):
                raise ScheduleError("PREVIEW_CHANGED")


def sync_schedule_folder(state_dir, runtime_dir, *, api_factory=LocalAPI, base_url="http://127.0.0.1:8686", check_release=True):
    """Synchronize source_directory drift safely without resetting next_index or cadence."""
    directory, runtime = Path(state_dir), Path(runtime_dir)
    if not directory.is_dir():
        raise ScheduleError("SCHEDULE_UNINITIALIZED")
    with single_instance(directory):
        manifest = load_json(directory / "manifest.json")
        validate_manifest(manifest)
        state = load_json(directory / "state.json")
        validate_state(state, manifest)
        if state.get("inflight_queue_id") is not None or state.get("submission_state") != "ready":
            raise ScheduleError("SCHEDULE_NOT_IDLE")
        release = load_json(directory / "release.json") if check_release else {"version": 1}
        if check_release:
            validate_release(release, runtime)
        api = api_factory(base_url, runtime, release)
        try:
            if api_factory is LocalAPI:
                api.connect(auto_start=True)
            else:
                api.connect()
            api.diagnostics(idle=True)
            api.verify_channel(manifest)

            index = state["next_index"]
            completed_jobs = list(manifest["jobs"][:index])
            completed_ids = {job["queue_id"] for job in completed_jobs}
            source_root = Path(manifest["source_directory"]).resolve()

            rows = api.project_rows(manifest["project"])
            by_id = {int(row["id"]): row for row in rows if _integer(row.get("id"))}
            for job in completed_jobs:
                row = by_id.get(job["queue_id"])
                if not row or row.get("status") != "Completed" or not row.get("published_url") or row.get("is_claimed") or row.get("requires_review"):
                    raise ScheduleError("COMPLETED_ITEM_CHANGED")

            stale_ids = []
            for qid, row in sorted(by_id.items()):
                if qid in completed_ids:
                    continue
                if row.get("status") != "Pending" or row.get("published_url") or row.get("is_claimed") or row.get("requires_review"):
                    raise ScheduleError("PENDING_ITEM_CHANGED")
                candidate = Path(str(row.get("input_video_path") or ""))
                in_root = False
                if candidate.is_file():
                    try:
                        candidate.resolve().relative_to(source_root)
                        in_root = True
                    except (ValueError, OSError):
                        in_root = False
                if not in_root:
                    stale_ids.append(qid)

            if stale_ids:
                api.delete_rows(stale_ids)

            scan_res = api.scan_folder(manifest)
            updated_rows = api.project_rows(manifest["project"])
            updated_by_id = {int(row["id"]): row for row in updated_rows if _integer(row.get("id"))}

            prior_pending_order = [job["queue_id"] for job in manifest["jobs"][index:] if job["queue_id"] in updated_by_id]
            seen_pending = set(prior_pending_order)
            extra_pending = sorted(
                qid for qid in updated_by_id
                if qid not in completed_ids and qid not in seen_pending
            )
            ordered_pending_ids = prior_pending_order + extra_pending
            if not completed_jobs and not ordered_pending_ids:
                raise ScheduleError("MANIFEST_JOBS")

            pending_jobs = []
            for qid in ordered_pending_ids:
                row = updated_by_id[qid]
                file_path = Path(str(row.get("input_video_path") or ""))
                if (not file_path.is_file() or row.get("status") != "Pending" or row.get("published_url")
                        or row.get("visibility") != "private" or row.get("schedule_time")
                        or row.get("target_channel_id") != manifest["channel"]["id"]):
                    raise ScheduleError("QUEUE_BINDING_CHANGED")
                pending_jobs.append({"queue_id": qid, "path": str(file_path), "sha256": sha256(file_path)})

            api.preview_batch(manifest, ordered_pending_ids)
            new_jobs = completed_jobs + pending_jobs
            new_manifest = {**manifest, "jobs": new_jobs}
            validate_manifest(new_manifest)

            new_state_obj = {**state, "manifest_sha256": manifest_digest(new_manifest), "last_checked_at": stamp(utc_now()), "error_code": ""}
            if new_state_obj["next_index"] >= len(new_jobs):
                new_state_obj["status"] = "finished"
            elif new_state_obj["status"] in {"finished", "error"}:
                new_state_obj["status"] = "waiting" if new_state_obj.get("last_started_at") else "ready"
            validate_state(new_state_obj, new_manifest)

            save_json(directory / "manifest.json", new_manifest)
            save_json(directory / "state.json", new_state_obj)
            return {
                "ok": True,
                "removed_pending_count": len(stale_ids),
                "added_count": int(scan_res.get("added") or 0),
                "schedule": read_schedule_status(directory),
            }
        finally:
            session = getattr(api, "session", None)
            if session is not None:
                session.close()


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

    @staticmethod
    def _check_source(job):
        path = Path(job["path"])
        try:
            if path.is_file() and sha256(path) == job["sha256"].lower():
                return
        except OSError:
            pass  # The source can disappear or become unreadable during the hash.
        raise ScheduleError("SOURCE_CHANGED")

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
        with runtime_operation_lock(self.runtime), single_instance(self.directory):
            self.manifest = load_json(self.directory / "manifest.json")
            jobs = validate_manifest(self.manifest)
            # Missing state is a hard stop, never permission to restart from job 0.
            self.state = load_json(self.directory / "state.json")
            validate_state(self.state, self.manifest)
            release = load_json(self.directory / "release.json")
            validate_release(release, self.runtime)
            index = self.state["next_index"]
            if (not observe_only and index < len(jobs) and not self.state.get("inflight_queue_id")
                    and self.state["submission_state"] == "ready"):
                # Reject bad input before creating the API client or starting its server.
                try:
                    self._check_source(jobs[index])
                except ScheduleError as error:
                    if not dry_run:
                        self._save("error", error.code)
                    raise
            self.api = self.api_factory(self.base_url, self.runtime, release)
            try:
                if self.api_factory is LocalAPI and not dry_run:
                    self.api.connect(auto_start=True)
                else:
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
                    for pending_job in jobs[index:]:
                        self._check_source(pending_job)
                    # The HTTP preview persists an approval in the runtime database.
                    # Only injected adapters may supply a read-only preview here.
                    if not isinstance(self.api, LocalAPI):
                        self.api.preview(self.manifest, jobs[index]["queue_id"])
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
                job = jobs[index]
                self._check_source(job)
                payload = self.api.preview(self.manifest, job["queue_id"])
                # The file can change during preview. Check before recording a submission.
                self._check_source(job)
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
            except (ScheduleError, OSError, ValueError, TypeError, KeyError) as error:
                # Keep inflight/submitting intact: an uncertain response cannot authorize resend.
                if not dry_run:
                    code = "SCHEDULE_CHECK_FAILED"
                    if isinstance(error, ScheduleError) and error.code == "SOURCE_CHANGED":
                        code = error.code
                    self._save("error", code)
                raise
            finally:
                session = getattr(self.api, "session", None)
                if session is not None:
                    session.close()
