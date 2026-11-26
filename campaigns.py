"""Local publishing campaigns backed by DHB's existing upload pipeline."""

import asyncio
import csv
import json
import logging
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from threading import Event, Thread
from typing import Literal

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from database import collect_secret_values, get_conn, queue_requires_review, redact_secrets
from campaign_sources import drive_url, read_sheet
from channel_workspace import PlaylistItem, add_to_playlist
from content_identity import sha256_file
from phase4_rules import GROUP_SCHEMA_VERSION, validate_metadata_fields
from pipeline_manager import UPLOAD_MIN_GAP_SECONDS, _log_run_id, pipeline_manager
from upload_approval import asset_stamps, digest, encode
from youtube_uploader import normalize_published_video_url


router = APIRouter(prefix="/api/campaigns")
logger = logging.getLogger(__name__)


class ContentList(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    queue_ids: list[int] = Field(min_length=1, max_length=1000)


class Campaign(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    list_id: int = Field(gt=0)
    channel_id: int = Field(gt=0)
    visibility: Literal["private", "unlisted", "public"] = "private"
    content_order: Literal["sequential", "random"] = "sequential"
    start_date: date | None = None
    utc_offset_minutes: int = Field(default=420, ge=-720, le=840)
    times: list[str] = Field(min_length=1, max_length=15)
    render: bool = False
    playlist_id: str = Field(default="", pattern=r"^(?:[A-Za-z0-9_-]{10,100})?$")


class Confirmation(BaseModel):
    revision: str


class SheetAddress(BaseModel):
    url: str = Field(max_length=2048)


class DriveImport(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    project: str = Field(min_length=1, max_length=150)
    channel_id: int = Field(gt=0)
    visibility: Literal["private", "unlisted", "public"] = "private"
    revision: str = ""


class DriveLinksImport(DriveImport):
    links: str = Field(min_length=1, max_length=262144)


class SheetImport(DriveImport, SheetAddress):
    source_column: int = Field(ge=0, lt=100)
    title_column: int = Field(ge=0, lt=100)
    description_column: int | None = Field(default=None, ge=0, lt=100)
    tags_column: int | None = Field(default=None, ge=0, lt=100)


def _sheet_data(url):
    try:
        return read_sheet(url)
    except requests.RequestException as exc:
        raise HTTPException(502, "Không tải được CSV Google Sheets. Kiểm tra mạng và quyền chia sẻ.") from exc
    except (ValueError, csv.Error) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/sources/sheet-columns")
def sheet_columns(data: SheetAddress):
    header, rows = _sheet_data(data.url)
    return {"columns": header, "row_count": len(rows)}


def _preview_sheet(data):
    header, rows = _sheet_data(data.url)
    columns = (data.source_column, data.title_column, data.description_column, data.tags_column)
    if any(column is not None and column >= len(header) for column in columns):
        raise HTTPException(422, "Chọn các cột tương ứng trong bảng.")
    return _preview_drive_items(data, (
        (number, row[data.source_column], {
            "title": row[data.title_column],
            "description": "" if data.description_column is None else row[data.description_column],
            "tags": "" if data.tags_column is None else row[data.tags_column],
        }) for number, row in rows
    ))


def _preview_links(data):
    rows = [(number, line.strip()) for number, line in enumerate(data.links.splitlines(), 1) if line.strip()]
    if not 1 <= len(rows) <= 1000:
        raise HTTPException(422, "Dán từ 1 đến 1.000 liên kết Drive.")
    return _preview_drive_items(data, ((number, link, {}) for number, link in rows))


def _preview_drive_items(data, rows):
    if not data.name.strip():
        raise HTTPException(422, "Nhập tên danh sách.")
    eligible, errors = [], []
    with closing(get_conn()) as conn:
        if not conn.execute("SELECT 1 FROM projects WHERE name=?", (data.project,)).fetchone():
            raise HTTPException(422, "Dự án không còn tồn tại.")
        channel = conn.execute("SELECT name,channel_id FROM youtube_channels WHERE id=?", (data.channel_id,)).fetchone()
        if not channel or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel["channel_id"] or ""):
            raise HTTPException(422, "Chọn kênh đích đã có YouTube Channel ID.")
        existing = {row[0] for row in conn.execute(
            "SELECT post_id FROM video_queue WHERE project_name=? AND platform='GoogleDrive'", (data.project,)
        )}
    duplicates = 0
    for number, link, fields in rows:
        try:
            file_id, url = drive_url(link)
            metadata = validate_metadata_fields(fields or {"title": f"Drive {file_id}"}, require_title=True)
        except ValueError as exc:
            errors.append({"line": number, "message": str(exc)})
            continue
        if file_id in existing:
            duplicates += 1
            continue
        existing.add(file_id)
        eligible.append({"line": number, "file_id": file_id, "url": url, **metadata})
    revision = digest([data.model_dump(exclude={"revision"}), dict(channel), eligible, errors, duplicates])
    return eligible, dict(channel), {
        "revision": revision if eligible else "", "ready": len(eligible), "duplicates": duplicates,
        "errors": errors, "samples": [{"line": item["line"], "title": item["title"]} for item in eligible[:10]],
        "channel": channel["name"], "youtube_channel_id": channel["channel_id"], "visibility": data.visibility,
    }


@router.post("/sources/sheet-preview")
def sheet_preview(data: SheetImport):
    return _preview_sheet(data)[-1]


@router.post("/sources/sheet-import")
def sheet_import(data: SheetImport):
    return _import_drive_items(data, *_preview_sheet(data))


@router.post("/sources/links-preview")
def links_preview(data: DriveLinksImport):
    return _preview_links(data)[-1]


@router.post("/sources/links-import")
def links_import(data: DriveLinksImport):
    return _import_drive_items(data, *_preview_links(data))


def _import_drive_items(data, eligible, channel, preview):
    if not preview["revision"] or data.revision != preview["revision"]:
        raise HTTPException(409, "Danh sách hoặc dữ liệu đích đã thay đổi. Xem trước lại trước khi nhập.")
    with closing(get_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute("SELECT name,channel_id FROM youtube_channels WHERE id=?", (data.channel_id,)).fetchone()
        project = conn.execute("SELECT 1 FROM projects WHERE name=?", (data.project,)).fetchone()
        if not project or not target or dict(target) != channel:
            raise HTTPException(409, "Dự án hoặc kênh đích đã thay đổi.")
        ids = []
        for item in eligible:
            if conn.execute("SELECT 1 FROM video_queue WHERE project_name=? AND platform='GoogleDrive' AND post_id=?",
                            (data.project, item["file_id"])).fetchone():
                raise HTTPException(409, "Có tệp Drive vừa được thêm vào dự án. Hãy xem trước lại.")
            ids.append(conn.execute(
                """INSERT INTO video_queue(project_name,source_type,platform,post_id,source_url,title,description,tags,
                       target_type,target_channel_id,visibility,upload_to,render_mode,group_schema_version,target_group_names_json)
                   VALUES (?,'URL','GoogleDrive',?,?,?,?,?,'CHANNEL',?,?,'Video','none',?,'[]')""",
                (data.project, item["file_id"], item["url"], item["title"], item["description"], item["tags"],
                 data.channel_id, data.visibility, GROUP_SCHEMA_VERSION),
            ).lastrowid)
        list_id = conn.execute(
            "INSERT INTO campaign_content_lists(name,queue_ids_json) VALUES (?,?)", (data.name.strip(), encode(ids))
        ).lastrowid
    return {"id": list_id, "queue_ids": ids}


def _campaign(conn, campaign_id):
    row = conn.execute("SELECT * FROM publishing_campaigns WHERE id=?", (campaign_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Không tìm thấy chiến dịch.")
    return row, Campaign.model_validate_json(row["config_json"])


def _content(conn, list_id):
    row = conn.execute("SELECT * FROM campaign_content_lists WHERE id=?", (list_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Không tìm thấy danh sách nội dung.")
    return row, json.loads(row["queue_ids_json"])


def _list_rows(conn, queue_ids):
    placeholders = ",".join("?" for _ in queue_ids)
    return {row["id"]: row for row in conn.execute(
        f"SELECT id, title, status, published_url, target_channel_id, visibility, schedule_time, "
        f"manual_reupload_authorized, claim_token FROM video_queue WHERE id IN ({placeholders})", queue_ids
    )}


def _save_list(data, list_id=None):
    queue_ids = list(dict.fromkeys(data.queue_ids))
    if not data.name.strip() or any(item_id < 1 for item_id in queue_ids):
        raise HTTPException(422, "Tên danh sách hoặc ID video không hợp lệ.")
    with closing(get_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if set(_list_rows(conn, queue_ids)) != set(queue_ids):
            raise HTTPException(422, "Có video đã bị xóa khỏi hàng đợi DHB.")
        if list_id is None:
            list_id = conn.execute(
                "INSERT INTO campaign_content_lists(name,queue_ids_json) VALUES (?,?)",
                (data.name.strip(), encode(queue_ids)),
            ).lastrowid
        else:
            _content(conn, list_id)
            active = conn.execute(
                "SELECT 1 FROM publishing_campaigns "
                "WHERE state='enabled' AND json_extract(config_json,'$.list_id')=?", (list_id,)
            ).fetchone()
            if active:
                raise HTTPException(409, "Tạm dừng chiến dịch dùng danh sách này trước khi sửa.")
            conn.execute("UPDATE campaign_content_lists SET name=?,queue_ids_json=? WHERE id=?",
                         (data.name.strip(), encode(queue_ids), list_id))
    return {"id": list_id}


@router.post("/lists")
def create_list(data: ContentList):
    return _save_list(data)


@router.put("/lists/{list_id}")
def edit_list(list_id: int, data: ContentList):
    return _save_list(data, list_id)


def _save_campaign(data, campaign_id=None):
    if not data.name.strip() or not all(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", slot)
                                        for slot in data.times):
        raise HTTPException(422, "Nhập tên và các mốc giờ theo định dạng HH:mm.")
    if len(set(data.times)) != len(data.times):
        raise HTTPException(422, "Mỗi mốc giờ chỉ được xuất hiện một lần.")
    data = data.model_copy(update={"name": data.name.strip(), "times": sorted(data.times)})
    config = encode(data.model_dump(mode="json"))
    with closing(get_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _content(conn, data.list_id)
        if not conn.execute("SELECT 1 FROM youtube_channels WHERE id=?", (data.channel_id,)).fetchone():
            raise HTTPException(422, "Kênh đích không còn tồn tại.")
        if campaign_id is None:
            campaign_id = conn.execute(
                "INSERT INTO publishing_campaigns(config_json) VALUES (?)", (config,)
            ).lastrowid
        else:
            row, _ = _campaign(conn, campaign_id)
            active = conn.execute(
                "SELECT 1 FROM publishing_campaign_runs WHERE campaign_id=? AND state='submitting'", (campaign_id,)
            ).fetchone()
            if row["state"] == "enabled" or active:
                raise HTTPException(409, "Tạm dừng chiến dịch và đợi lượt đang gửi kết thúc trước khi sửa.")
            conn.execute("UPDATE publishing_campaigns SET config_json=?,state='draft',snapshots_json='{}' WHERE id=?",
                         (config, campaign_id))
    return {"id": campaign_id}


@router.post("")
def create_campaign(data: Campaign):
    return _save_campaign(data)


@router.put("/{campaign_id}")
def edit_campaign(campaign_id: int, data: Campaign):
    return _save_campaign(data, campaign_id)


def _clock_zone(data):
    return timezone(timedelta(minutes=data.utc_offset_minutes))


def _scheduled_slots(data, start, end):
    """Return local slots once across a lower-exclusive and upper-inclusive interval."""
    zone = _clock_zone(data)
    first, last = (datetime.fromtimestamp(epoch, zone).date() for epoch in (start, end))
    if data.start_date is not None:
        first = max(first, data.start_date)
    day = first
    while day <= last:
        for slot in data.times:
            due = datetime.combine(day, time.fromisoformat(slot), zone).timestamp()
            if start < due <= end:
                yield day.isoformat(), slot, due
        if day == last:
            break
        day += timedelta(days=1)


@router.get("")
def get_campaigns():
    now = datetime.now(timezone.utc).timestamp()
    with closing(get_conn()) as conn:
        campaigns = []
        for row in conn.execute("SELECT * FROM publishing_campaigns ORDER BY id DESC").fetchall():
            data = Campaign.model_validate_json(row["config_json"])
            zone = _clock_zone(data)
            local_now = datetime.fromtimestamp(now, zone)
            next_day = max(local_now.date(), data.start_date or local_now.date())
            next_at = next((datetime.combine(next_day, time.fromisoformat(slot), zone) for slot in data.times
                            if next_day > local_now.date() or time.fromisoformat(slot) > local_now.time()), None)
            if next_at is None and next_day < date.max:
                next_at = datetime.combine(next_day + timedelta(days=1), time.fromisoformat(data.times[0]), zone)
            campaigns.append({
                "id": row["id"], "state": row["state"], **data.model_dump(mode="json"),
                "next_at": next_at.isoformat() if row["state"] == "enabled" and next_at else None,
            })
        lists = [{"id": row["id"], "name": row["name"], "queue_ids": json.loads(row["queue_ids_json"])}
                 for row in conn.execute("SELECT * FROM campaign_content_lists ORDER BY id DESC")]
        runs = [dict(row) for row in conn.execute(
            "SELECT r.id,r.campaign_id,r.local_day,r.local_time,r.state,r.queue_id,r.started_at,r.finished_at,"
            "r.message,q.status AS queue_status,p.state AS playlist_state,p.message AS playlist_message "
            "FROM publishing_campaign_runs r LEFT JOIN video_queue q ON q.id=r.queue_id "
            "LEFT JOIN campaign_playlist_jobs p ON p.run_id=r.id ORDER BY r.due_at DESC,r.id DESC LIMIT 100"
        )]
        slot = conn.execute("SELECT state,cooldown_until FROM upload_slot WHERE id=1").fetchone()
    return {
        "lists": lists, "campaigns": campaigns, "runs": runs, "upload_gap_minutes": UPLOAD_MIN_GAP_SECONDS // 60,
        "cooldown_until": slot["cooldown_until"] if slot and (slot["cooldown_until"] or 0) > now else None,
    }


def _run_logs(run_id, after):
    with closing(get_conn()) as conn:
        run = conn.execute(
            "SELECT a.run_token FROM publishing_campaign_runs r "
            "LEFT JOIN upload_approvals a ON r.preview_token=a.token WHERE r.id=?", (run_id,)
        ).fetchone()
        if not run or not run["run_token"]:
            return []
        log_id = _log_run_id(run["run_token"])
        if after == 0:
            rows = list(reversed(conn.execute(
                "SELECT id,created_at,status,message FROM system_logs WHERE run_id=? ORDER BY id DESC LIMIT 100",
                (log_id,),
            ).fetchall()))
        else:
            rows = conn.execute(
                "SELECT id,created_at,status,message FROM system_logs "
                "WHERE run_id=? AND id>? ORDER BY id LIMIT 100", (log_id, after)
            ).fetchall()
    if not rows:
        return []
    secrets_to_mask = collect_secret_values()
    return [{**dict(row), "message": redact_secrets(row["message"], secrets_to_mask)} for row in rows]


@router.get("/runs/{run_id}/logs")
def stream_run_logs(run_id: int, request: Request, after: int = 0):
    with closing(get_conn()) as conn:
        if not conn.execute("SELECT 1 FROM publishing_campaign_runs WHERE id=?", (run_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy lượt chạy.")

    async def stream():
        cursor = max(0, after)
        while not await request.is_disconnected():
            rows = await run_in_threadpool(_run_logs, run_id, cursor)
            for row in rows:
                cursor = row["id"]
                yield f"id: {cursor}\ndata: {encode(row)}\n\n"
            if not rows:
                yield ": keepalive\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


def _options(data):
    return [True, data.render, True]


def _source_hashes(approval):
    source = json.loads(approval)
    paths = source["paths"]
    stamps = source["asset_stamps"]
    if asset_stamps(paths) != stamps:
        raise OSError("Tệp nguồn đã đổi trong lúc kiểm tra.")
    hashes = [sha256_file(path) if stamp is not None else None for path, stamp in zip(paths, stamps)]
    if asset_stamps(paths) != stamps:
        raise OSError("Tệp nguồn đã đổi trong lúc kiểm tra.")
    return hashes


def _inspect(campaign_id):
    with closing(get_conn()) as conn:
        row, data = _campaign(conn, campaign_id)
        _, ids = _content(conn, data.list_id)
        rows = _list_rows(conn, ids)
    problems = []
    pending = []
    for item_id in ids:
        item = rows.get(item_id)
        if item is None:
            problems.append(f"Video #{item_id} đã bị xóa khỏi hàng đợi.")
        elif item["status"] != "Completed" and not item["published_url"]:
            if (item["target_channel_id"] != data.channel_id or item["visibility"] != data.visibility
                    or item["schedule_time"] or item["manual_reupload_authorized"]):
                problems.append(f"Video #{item_id} chưa có đúng kênh đích, chế độ đăng hoặc đang chờ đăng lại thủ công.")
            pending.append(item_id)
    if not pending:
        problems.append("Danh sách đã hết video chờ đăng.")
    preview = pipeline_manager.preview_distribution(pending, _options(data), persist=False) if not problems else {}
    for item in preview.get("items", []):
        if item["blocked"]:
            problems.extend(f"Video #{item['id']}: {message}" for message in item["reasons"])
        elif item["channel_id"] != data.channel_id or item["visibility"] != data.visibility or item["groups"]:
            problems.append(f"Video #{item['id']} phải được gán trực tiếp vào kênh và chế độ đăng của chiến dịch.")
    snapshots = {}
    if not problems:
        for item_id, approval in preview.get("snapshots", {}).items():
            try:
                snapshots[item_id] = {"approval": approval, "source_hashes": _source_hashes(approval)}
            except OSError:
                problems.append(f"Video #{item_id} có tệp không đọc được hoặc đang thay đổi.")
    revision = digest([row["config_json"], ids, snapshots]) if not problems else ""
    return row["config_json"], ids, snapshots, {
        "revision": revision, "problems": problems, "items": preview.get("items", []),
        "upload_gap_minutes": UPLOAD_MIN_GAP_SECONDS // 60,
    }


@router.get("/{campaign_id}/preview")
def preview_campaign(campaign_id: int):
    return _inspect(campaign_id)[-1]


@router.post("/{campaign_id}/enable")
def enable_campaign(campaign_id: int, confirmation: Confirmation):
    config, ids, snapshots, preview = _inspect(campaign_id)
    if not preview["revision"] or confirmation.revision != preview["revision"]:
        raise HTTPException(409, "Dữ liệu hoặc cấu hình đã thay đổi. Hãy xem trước chiến dịch lại.")
    now = datetime.now(timezone.utc).timestamp()
    with closing(get_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row, data = _campaign(conn, campaign_id)
        _, current_ids = _content(conn, data.list_id)
        if row["config_json"] != config or current_ids != ids:
            raise HTTPException(409, "Chiến dịch hoặc thứ tự video đã thay đổi.")
        if row["state"] == "enabled":
            return {"id": campaign_id, "state": "enabled"}
        if conn.execute("SELECT 1 FROM publishing_campaign_runs WHERE campaign_id=? AND state='submitting'",
                        (campaign_id,)).fetchone():
            raise HTTPException(409, "Đợi lượt gửi hiện tại hoàn tất trước khi bật lại.")
        conn.execute(
            "UPDATE publishing_campaigns SET state='enabled',snapshots_json=?,checked_at=? WHERE id=?",
            (encode(snapshots), now, campaign_id),
        )
    return {"id": campaign_id, "state": "enabled"}


@router.post("/{campaign_id}/pause")
def pause_campaign(campaign_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _campaign(conn, campaign_id)
        conn.execute("UPDATE publishing_campaigns SET state='paused' WHERE id=?", (campaign_id,))
        conn.execute(
            "UPDATE publishing_campaign_runs SET state='canceled',message='Chiến dịch đã tạm dừng.' "
            "WHERE campaign_id=? AND state='queued'", (campaign_id,)
        )
    return {"id": campaign_id, "state": "paused"}


@router.post("/runs/{run_id}/playlist-retry")
def retry_playlist(run_id: int):
    with closing(get_conn()) as conn, conn:
        updated = conn.execute(
            "UPDATE campaign_playlist_jobs SET state='queued',message='' WHERE run_id=? AND state='error'", (run_id,)
        ).rowcount
    if not updated:
        raise HTTPException(409, "Lượt này không có bước playlist cần thử lại.")
    return {"state": "queued"}


@router.post("/runs/{run_id}/catch-up")
def catch_up(run_id: int):
    with closing(get_conn()) as conn, conn:
        updated = conn.execute(
            "UPDATE publishing_campaign_runs SET state='queued',message='Lượt chạy bù đã được chọn.' "
            "WHERE id=? AND state='missed' AND campaign_id IN "
            "(SELECT id FROM publishing_campaigns WHERE state='enabled')", (run_id,)
        ).rowcount
    if not updated:
        raise HTTPException(409, "Chỉ có thể chạy bù một lượt bị lỡ của chiến dịch đang bật.")
    return {"state": "queued"}


def _materialize(conn, now):
    rows = conn.execute("SELECT * FROM publishing_campaigns WHERE state='enabled'").fetchall()
    for row in rows:
        data = Campaign.model_validate_json(row["config_json"])
        start = row["checked_at"] if row["checked_at"] is not None else now
        if now <= start:
            continue
        conn.executemany(
            "INSERT INTO publishing_campaign_runs(campaign_id,local_day,local_time,due_at,state,message) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(campaign_id,local_day,local_time) DO NOTHING",
            ((row["id"], day, slot, due, "queued" if now - due < 60 else "missed",
              "" if now - due < 60 else "Backend chưa xử lý trong phút đã hẹn.")
             for day, slot, due in _scheduled_slots(data, start, now)),
        )
        conn.execute("UPDATE publishing_campaigns SET checked_at=? WHERE id=?", (now, row["id"]))


def _finish(conn, run_id, now, state, message=""):
    conn.execute("UPDATE publishing_campaign_runs SET state=?,message=?,finished_at=? WHERE id=?",
                 (state, message, now, run_id))


def _hold_campaign(conn, run, now, state, message):
    _finish(conn, run["id"], now, state, message)
    conn.execute("UPDATE publishing_campaigns SET state='paused' WHERE id=?", (run["campaign_id"],))
    conn.execute(
        "UPDATE publishing_campaign_runs SET state='canceled',message='Chiến dịch cần kiểm tra.' "
        "WHERE campaign_id=? AND state='queued'", (run["campaign_id"],)
    )


def _reconcile(conn, now, run_id=None):
    runs = conn.execute(
        "SELECT * FROM publishing_campaign_runs WHERE state='submitting' AND (? IS NULL OR id=?)", (run_id, run_id)
    ).fetchall()
    for run in runs:
        approval = conn.execute("SELECT state FROM upload_approvals WHERE token=?", (run["preview_token"],)).fetchone()
        if approval and approval["state"] == "accepted":
            continue
        if approval is None or approval["state"] in ("preview", "launch_failed"):
            # The previous process never accepted the approval, so it never started an uploader.
            campaign, _ = _campaign(conn, run["campaign_id"])
            if campaign["state"] == "enabled":
                conn.execute("UPDATE publishing_campaign_runs SET state='queued',preview_token=NULL,started_at=NULL "
                             "WHERE id=?", (run["id"],))
            else:
                _finish(conn, run["id"], now, "canceled", "Chiến dịch đã tạm dừng.")
            continue
        item = _list_rows(conn, [run["queue_id"]]).get(run["queue_id"])
        if item and item["status"] == "Completed" and item["published_url"]:
            _finish(conn, run["id"], now, "completed", item["published_url"])
            _, data = _campaign(conn, run["campaign_id"])
            url = normalize_published_video_url(item["published_url"])
            if data.playlist_id and url:
                conn.execute(
                    "INSERT OR IGNORE INTO campaign_playlist_jobs(run_id,channel_id,video_id,playlist_id,state) "
                    "VALUES (?,?,?,?,'queued')", (run["id"], data.channel_id, url.rsplit("/", 1)[-1], data.playlist_id),
                )
        elif item and queue_requires_review(conn, run["queue_id"]):
            _hold_campaign(conn, run, now, "needs_review", "Kết quả gửi cần được đối soát trong hàng đợi DHB.")
        else:
            _hold_campaign(conn, run, now, "error", "Pipeline chưa xác nhận video đã đăng. Xem hàng đợi và bằng chứng.")


def _run_playlist_job():
    with closing(get_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT * FROM campaign_playlist_jobs WHERE state='queued' ORDER BY run_id LIMIT 1").fetchone()
        if not job:
            return
        conn.execute("UPDATE campaign_playlist_jobs SET state='submitting' WHERE run_id=?", (job["run_id"],))
    try:
        add_to_playlist(job["channel_id"], PlaylistItem(video_id=job["video_id"], playlist_id=job["playlist_id"]))
    except (HTTPException, ValueError) as exc:
        state, message = "error", str(getattr(exc, "detail", exc))
    else:
        state, message = "completed", ""
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE campaign_playlist_jobs SET state=?,message=? WHERE run_id=?", (state, message, job["run_id"]))


def _complete_run(run_id):
    with closing(get_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _reconcile(conn, datetime.now(timezone.utc).timestamp(), run_id)


def run_campaign_tick(now=None, pipeline=pipeline_manager):
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    with closing(get_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _materialize(conn, now)
        _reconcile(conn, now)
        if pipeline.is_running:
            return
        slot = conn.execute("SELECT state, cooldown_until FROM upload_slot WHERE id=1").fetchone()
        if slot and (slot["state"] == "active" or (slot["cooldown_until"] or 0) > now):
            return
        run = conn.execute(
            "SELECT r.* FROM publishing_campaign_runs r JOIN publishing_campaigns c ON r.campaign_id=c.id "
            "WHERE r.state='queued' AND c.state='enabled' ORDER BY r.due_at,r.id LIMIT 1"
        ).fetchone()
        if run is None:
            return
        row, data = _campaign(conn, run["campaign_id"])
        approved = json.loads(row["snapshots_json"])
        _, ids = _content(conn, data.list_id)
        rows = _list_rows(conn, ids)
        available = [i for i in ids if str(i) in approved and (i not in rows
                     or (rows[i]["status"] != "Completed" and not rows[i]["published_url"]))]
        if not available:
            _hold_campaign(conn, run, now, "empty", "Danh sách đã hết video chờ đăng.")
            return
        selected = secrets.choice(available) if data.content_order == "random" else available[0]
        if selected in rows and rows[selected]["claim_token"]:
            return
    expectation = approved[str(selected)]
    approval = expectation["approval"] if isinstance(expectation, dict) else expectation
    try:
        matches_source = isinstance(expectation, dict) and _source_hashes(approval) == expectation["source_hashes"]
    except OSError:
        matches_source = False
    if matches_source:
        preview = pipeline.preview_distribution([selected], _options(data), expected={selected: approval})
    else:
        preview = {"preview_token": "", "items": [{"reasons": [
            "Tệp nguồn thiếu, không đọc được hoặc sai SHA-256. Kiểm tra video rồi xem trước lại chiến dịch."
        ]}]}
    token = preview["preview_token"]
    with closing(get_conn()) as conn, conn:
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("BEGIN IMMEDIATE")
        current, _ = _campaign(conn, run["campaign_id"])
        active = conn.execute(
            "SELECT 1 FROM publishing_campaign_runs WHERE id=? AND state='queued'", (run["id"],)
        ).fetchone()
        if not active or current["state"] != "enabled" or current["snapshots_json"] != row["snapshots_json"]:
            return
        if not token:
            reasons = preview["items"][0].get("reasons") or ["Không xác minh được video."]
            conn.execute("UPDATE publishing_campaign_runs SET queue_id=? WHERE id=?", (selected, run["id"]))
            _hold_campaign(conn, run, now, "blocked", " ".join(reasons))
            return
        # Commit before handing off to the uploader. Its approval is the durable record of acceptance.
        conn.execute(
            "UPDATE publishing_campaign_runs SET state='submitting',queue_id=?,preview_token=?,started_at=? WHERE id=?",
            (selected, token, now, run["id"]),
        )
    result = pipeline.start_batch(
        [selected], *_options(data), preview_token=token, on_finished=lambda: _complete_run(run["id"]),
    )
    if not result["ok"] and result.get("code") in ("PREVIEW_STALE", "REVIEW_REQUIRED"):
        with closing(get_conn()) as conn, conn:
            _hold_campaign(conn, run, now, "blocked", result["message"])


def start_campaign_worker():
    stop = Event()
    # Startup has already recovered pipeline claims. Reconcile before HTTP can start another batch.
    with closing(get_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _reconcile(conn, datetime.now(timezone.utc).timestamp())
        conn.execute(
            "UPDATE campaign_playlist_jobs SET state='error',message='Lượt kiểm tra playlist đã bị gián đoạn.' "
            "WHERE state='submitting'"
        )

    def work():
        while not stop.is_set():
            try:
                run_campaign_tick()
                _run_playlist_job()
            except (sqlite3.Error, HTTPException, ValueError):
                logger.exception("Campaign scheduler tick failed")
            stop.wait(15)

    thread = Thread(target=work, daemon=True, name="DHB-Campaigns")
    thread.start()
    return stop, thread
