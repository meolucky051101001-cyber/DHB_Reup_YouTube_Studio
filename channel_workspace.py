"""Read channel data and perform explicit actions on the authenticated channel."""

import re
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from threading import Lock

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from database import get_conn
from studio_realtime import read_realtime_views
from youtube_uploader import (
    _http_session_for_channel, _parse_oauth_payload, _refresh_google_access_token, _safe_video_id,
)


router = APIRouter(prefix="/api/channel-workspace")
DATA_API = "https://www.googleapis.com/youtube/v3/"
ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
_playlist_mutation = Lock()


def _request(http, token, endpoint, *, params=None, payload=None):
    url = ANALYTICS_URL if endpoint == "analytics" else DATA_API + endpoint
    try:
        response = http.request(
            "GET" if payload is None else "POST", url, params=params, json=payload,
            headers={"Authorization": "Bearer " + token}, timeout=(8, 20), allow_redirects=False,
        )
        if response.status_code in (401, 403):
            raise HTTPException(403, "Google chưa cấp quyền cho thao tác này hoặc API chưa được bật.")
        if response.status_code not in (200, 201):
            raise HTTPException(502, f"YouTube trả mã HTTP {response.status_code}.")
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(502, "Không đọc được phản hồi từ YouTube.") from exc
    if not isinstance(body, dict):
        raise HTTPException(502, "Phản hồi YouTube sai định dạng.")
    return body


@contextmanager
def _channel_session(channel_id):
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM youtube_channels WHERE id=?", (channel_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Không tìm thấy kênh.")
    channel = dict(row)
    target = channel["channel_id"] or ""
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", target):
        raise HTTPException(409, "Kênh chưa có YouTube Channel ID hợp lệ.")
    try:
        credentials = _parse_oauth_payload(channel["oauth_token_json"] or "")
        if not credentials:
            raise HTTPException(409, "Kết nối OAuth cho kênh trước khi đọc dữ liệu.")
        with _http_session_for_channel(channel) as http:
            token = _refresh_google_access_token(credentials, session=http)
            data = _request(http, token, "channels", params={"part": "snippet,statistics", "mine": "true"})
            items = data.get("items")
            if not isinstance(items, list) or len(items) != 1 or items[0].get("id") != target:
                raise HTTPException(409, "OAuth không đại diện cho đúng kênh đã chọn.")
            yield target, data["items"][0], http, token
    except RuntimeError as exc:
        raise HTTPException(409, "Không xác minh được OAuth. Hãy kiểm tra lại kết nối kênh.") from exc


def _nonnegative_integer(value):
    return int(value) if re.fullmatch(r"[0-9]{1,18}", str(value)) else None


@router.get("/{channel_id}/analytics")
def channel_analytics(channel_id: int):
    with _channel_session(channel_id) as (target, channel, http, token):
        stats = channel.get("statistics") or {}
        result = {
            "channel_id": target, "name": (channel.get("snippet") or {}).get("title") or "",
            "subscribers": None if stats.get("hiddenSubscriberCount") else _nonnegative_integer(stats.get("subscriberCount")),
            "channel_views": _nonnegative_integer(stats.get("viewCount")),
            "views_28d": None, "watch_hours_28d": None, "views_48h": None,
            "updated_at": datetime.now(timezone.utc).isoformat(), "warning": "",
        }
        end = date.today() - timedelta(days=1)
        try:
            report = _request(http, token, "analytics", params={
                "ids": "channel==MINE", "metrics": "views,estimatedMinutesWatched",
                "startDate": (end - timedelta(days=27)).isoformat(), "endDate": end.isoformat(),
            })
        except HTTPException as exc:
            result["warning"] = exc.detail
        else:
            rows = report.get("rows") or []
            columns = [column.get("name") for column in report.get("columnHeaders", [])]
            if rows and isinstance(rows[0], list):
                metrics = dict(zip(columns, rows[0]))
                result["views_28d"] = _nonnegative_integer(metrics.get("views"))
                watch_minutes = _nonnegative_integer(metrics.get("estimatedMinutesWatched"))
                if watch_minutes is not None:
                    result["watch_hours_28d"] = round(watch_minutes / 60, 2)
    return result


@router.post("/{channel_id}/analytics/realtime")
def channel_realtime(channel_id: int):
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM youtube_channels WHERE id=?", (channel_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Không tìm thấy kênh.")
    try:
        views = read_realtime_views(dict(row))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"channel_id": row["channel_id"], "views_48h": views,
            "updated_at": datetime.now(timezone.utc).isoformat()}


class Reply(BaseModel):
    comment_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,180}$")
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    text: str = Field(min_length=1, max_length=10000)


def _owned_video(http, token, target, video_id):
    items = _request(http, token, "videos", params={"part": "snippet", "id": video_id}).get("items") or []
    if len(items) != 1 or items[0].get("id") != video_id or (items[0].get("snippet") or {}).get("channelId") != target:
        raise HTTPException(409, "Video không thuộc kênh đã chọn.")


@router.get("/{channel_id}/comments")
def channel_comments(channel_id: int, video_id: str, cursor: str = ""):
    if not _safe_video_id(video_id) or len(cursor) > 512:
        raise HTTPException(422, "Video hoặc mã trang không hợp lệ.")
    with _channel_session(channel_id) as (target, _, http, token):
        _owned_video(http, token, target, video_id)
        params = {"part": "snippet", "videoId": video_id, "maxResults": 30, "order": "time", "textFormat": "plainText"}
        if cursor:
            params["pageToken"] = cursor
        threads = _request(http, token, "commentThreads", params=params)
    with closing(get_conn()) as conn:
        statuses = dict(conn.execute(
            "SELECT comment_id,state FROM channel_comment_replies WHERE channel_id=? AND video_id=?",
            (channel_id, video_id),
        ))
    comments = []
    for entry in threads.get("items") or []:
        top = (entry.get("snippet") or {}).get("topLevelComment") or {}
        snippet = top.get("snippet") or {}
        comment_id = top.get("id")
        if comment_id and snippet.get("videoId") == video_id:
            comments.append({
                "id": comment_id, "author": snippet.get("authorDisplayName") or "",
                "text": snippet.get("textDisplay") or "", "date": snippet.get("publishedAt"),
                "reply_state": statuses.get(comment_id, ""),
            })
    return {"items": comments, "next": threads.get("nextPageToken") or ""}


@router.post("/{channel_id}/comments/reply")
def reply_to_comment(channel_id: int, data: Reply):
    body = data.text.strip()
    if not body:
        raise HTTPException(422, "Nhập nội dung trả lời.")
    key = (channel_id, data.video_id, data.comment_id)
    with _channel_session(channel_id) as (target, _, http, token):
        _owned_video(http, token, target, data.video_id)
        comments = _request(http, token, "comments", params={"part": "snippet", "id": data.comment_id}).get("items") or []
        if (len(comments) != 1 or comments[0].get("id") != data.comment_id
                or (comments[0].get("snippet") or {}).get("videoId") != data.video_id
                or (comments[0].get("snippet") or {}).get("parentId")):
            raise HTTPException(409, "Không xác minh được bình luận gốc của video.")
        with closing(get_conn()) as conn, conn:
            conn.execute("PRAGMA synchronous=FULL")
            inserted = conn.execute(
                "INSERT INTO channel_comment_replies(channel_id,video_id,comment_id,state) VALUES (?,?,?,'submitting') "
                "ON CONFLICT(channel_id,video_id,comment_id) DO NOTHING", key
            ).rowcount
            if not inserted:
                raise HTTPException(409, "Bình luận này đã có lượt trả lời. Hãy kiểm tra lại trong Studio.")
        try:
            reply = _request(http, token, "comments", params={"part": "snippet"}, payload={
                "snippet": {"parentId": data.comment_id, "textOriginal": body}
            })
            if not reply.get("id") or (reply.get("snippet") or {}).get("parentId") != data.comment_id:
                raise HTTPException(502, "YouTube chưa xác nhận phản hồi. Kiểm tra Studio trước khi gửi lại.")
        except HTTPException:
            with closing(get_conn()) as conn, conn:
                conn.execute("UPDATE channel_comment_replies SET state='needs_review' "
                             "WHERE channel_id=? AND video_id=? AND comment_id=?", key)
            raise
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE channel_comment_replies SET state='completed',reply_id=? "
                     "WHERE channel_id=? AND video_id=? AND comment_id=?", (reply["id"], *key))
    return {"id": reply["id"], "state": "completed"}


class PlaylistItem(BaseModel):
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    playlist_id: str = Field(pattern=r"^[A-Za-z0-9_-]{10,100}$")


@router.post("/{channel_id}/playlist")
def add_to_playlist(channel_id: int, data: PlaylistItem):
    with _playlist_mutation, _channel_session(channel_id) as (target, _, http, token):
        _owned_video(http, token, target, data.video_id)
        playlists = _request(http, token, "playlists", params={"part": "snippet", "id": data.playlist_id}).get("items") or []
        if (len(playlists) != 1 or playlists[0].get("id") != data.playlist_id
                or (playlists[0].get("snippet") or {}).get("channelId") != target):
            raise HTTPException(409, "Playlist không thuộc kênh đã chọn.")
        found = _request(http, token, "playlistItems", params={
            "part": "id", "playlistId": data.playlist_id, "videoId": data.video_id, "maxResults": 1,
        }).get("items") or []
        if found:
            return {"id": found[0]["id"], "state": "already_added"}
        inserted = _request(http, token, "playlistItems", params={"part": "snippet"}, payload={"snippet": {
            "playlistId": data.playlist_id, "resourceId": {"kind": "youtube#video", "videoId": data.video_id}
        }})
        if not inserted.get("id"):
            raise HTTPException(502, "YouTube chưa xác nhận thêm playlist. Kiểm tra lại danh sách phát.")
    return {"id": inserted["id"], "state": "completed"}
