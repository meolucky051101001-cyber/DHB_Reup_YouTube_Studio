import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, Any, Optional, Tuple

from config import (
    APP_OUTPUT_DIR,
    APP_TEMP_DIR,
    DHB_RENDER_DIR,
    FFMPEG_EXE,
)
from scanner_downloader import probe_video_info

TIME_REGEX = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

# Quản lý toàn bộ tiến trình FFmpeg đang chạy để có thể dừng ngay lập tức khi người dùng bấm '⏹️ Dừng'.
# Mỗi PID gắn owner ('batch' / 'monitor') để Dừng batch không giết render của Monitor.
_ACTIVE_FFMPEG_LOCK = threading.Lock()
_ACTIVE_FFMPEG_PROCS: Dict[int, Tuple[subprocess.Popen, str]] = {}


def _kill_stray_tool_ffmpeg():
    """
    Dọn tiến trình FFmpeg rơi rớt CHỈ khi command line của nó thuộc thư mục temp/ hoặc output/ của tool,
    để không vô tình giết các tiến trình ffmpeg của người dùng đang chạy ngoài phần mềm.
    """
    try:
        temp_root = os.path.realpath(str(APP_TEMP_DIR)).lower()
        out_root = os.path.realpath(str(APP_OUTPUT_DIR)).lower()
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        stdout = (res.stdout or "").strip()
        if not stdout:
            return
        import json as _json
        data = _json.loads(stdout)
        if isinstance(data, dict):
            data = [data]
        for item in data or []:
            cmdline = (item.get("CommandLine") or "").lower()
            pid = item.get("ProcessId")
            if pid and (temp_root in cmdline or out_root in cmdline):
                try:
                    os.kill(int(pid), 9)
                except Exception:
                    pass
    except Exception:
        pass


def _terminate_ffmpeg_proc(proc: subprocess.Popen, timeout: float = 5.0):
    """Kill và reap tiến trình FFmpeg còn sống — tránh tiến trình mồ côi giữ file output."""
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=timeout)
    except Exception:
        pass


def kill_all_render_processes(owner: Optional[str] = None):
    """Hủy các tiến trình FFmpeg do tool khởi chạy.

    owner=None  → dừng tất cả (kể cả dọn tiến trình rơi rớt).
    owner='batch' / 'monitor' → chỉ dừng đúng làn đó, không đụng làn còn lại.
    """
    with _ACTIVE_FFMPEG_LOCK:
        if owner is None:
            procs = [proc for proc, _ in _ACTIVE_FFMPEG_PROCS.values()]
            _ACTIVE_FFMPEG_PROCS.clear()
        else:
            procs = []
            for pid, (proc, proc_owner) in list(_ACTIVE_FFMPEG_PROCS.items()):
                if proc_owner == owner:
                    procs.append(proc)
                    _ACTIVE_FFMPEG_PROCS.pop(pid, None)

    for proc in procs:
        _terminate_ffmpeg_proc(proc)

    # Dọn các tiến trình rơi rớt (mất PID theo dõi) nhưng CHỈ trong phạm vi thư mục của tool.
    # Khi dừng theo làn thì không quét rơi rớt vì không xác định được chúng thuộc làn nào.
    if owner is None and os.name == "nt":
        _kill_stray_tool_ffmpeg()


def _ff_path(p) -> str:
    """Chuẩn hóa đường dẫn cho filter FFmpeg (drawtext textfile/fontfile):
    bỏ backslash + escape dấu hai chấm ổ đĩa Windows thành '\\:' (2 cấp escaping của filtergraph)."""
    s = str(p).replace("\\", "/")
    return s.replace(":", "\\:")


def _resolve_font(name: str) -> str:
    """Tìm font: kho _Font của DHB -> font hệ thống Windows -> arial mặc định."""
    name = (name or "arial").strip()
    if not name.lower().endswith((".ttf", ".otf")):
        name = name + ".ttf"
    cand = DHB_RENDER_DIR / "_Font" / name
    if cand.exists():
        return str(cand)
    sys_font = Path("C:/Windows/Fonts") / name
    if sys_font.exists():
        return str(sys_font)
    return str(Path("C:/Windows/Fonts/arial.ttf"))


def _write_textfile(text: str) -> str:
    """Ghi text watermark ra file tạm để drawtext đọc qua textfile (tránh lỗi escape ký tự đặc biệt)."""
    APP_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    p = APP_TEMP_DIR / f"wmtext_{int(time.time() * 1000)}.txt"
    p.write_text(text, encoding="utf-8")
    return str(p)


def _pick_media(folder: Path, name: str, use_random: bool, exts) -> Optional[Path]:
    """Chọn file từ kho tài nguyên: theo tên, hoặc chọn ngẫu nhiên nếu bật random (học videobgr_random của DHB)."""
    try:
        if use_random and folder.exists():
            files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in exts]
            if files:
                import random
                return files[random.randint(0, len(files) - 1)]
        if name:
            p = folder / name
            if p.exists():
                return p
    except Exception:
        pass
    return None


def _build_branding(cfg, base_label: str, input_count: int, extra_inputs: list, frame_h: int):
    """
    Xây dựng lớp phủ branding kế thừa DHB Reup: watermark ảnh PNG (kho _Image),
    logo video chroma-key nền xanh (kho _Video), watermark chữ và chữ chạy ngang.
    Returns (overlay_segments, drawtext_chain, final_label, input_count):
    - overlay_segments: các chuỗi filter_complex có nhãn (overlay 2 đầu vào).
    - drawtext_chain: các filter đơn đầu vào nối tiếp bằng dấu phẩy sau final_label.
    """
    overlay_segments = []
    draw_chain = []
    label = base_label

    # 1) Watermark ảnh từ kho _Image
    img_name = (cfg.get("watermark_image_name") or "").strip()
    if img_name:
        img_path = _pick_media(DHB_RENDER_DIR / "_Image", img_name, False, (".png", ".jpg", ".jpeg", ".webp"))
        if img_path:
            try:
                size = str(cfg.get("watermark_image_size") or "240:120").replace("x", ":").replace(",", ":")
                pos = str(cfg.get("watermark_image_pos") or "10:10").replace(",", ":")
                op = max(0.0, min(1.0, float(cfg.get("watermark_image_opacity", 1.0) or 1.0)))
                idx = input_count
                input_count += 1
                extra_inputs.extend(["-i", str(img_path)])
                wm_lbl = f"wmi{idx}"
                overlay_segments.append(
                    f"[{idx}:v]scale={size},format=rgba,colorchannelmixer=aa={op:.2f}[{wm_lbl}];"
                    f"[{label}][{wm_lbl}]overlay={pos}:eof_action=repeat[{label}w]"
                )
                label = f"{label}w"
            except Exception:
                pass

    # 2) Logo video chroma-key (tự tách nền xanh — tính năng đặc trưng của DHB)
    logo_name = (cfg.get("logo_video_name") or "").strip()
    if logo_name:
        logo_path = _pick_media(DHB_RENDER_DIR / "_Video", logo_name, False, (".mp4", ".mov", ".webm"))
        if logo_path:
            try:
                size = str(cfg.get("logo_video_size") or "300:150").replace("x", ":").replace(",", ":")
                pos = str(cfg.get("logo_video_pos") or "10:10").replace(",", ":")
                chroma = str(cfg.get("logo_video_chroma") or "13FE06").lstrip("#").replace("0x", "")
                sim = max(0.01, min(1.0, float(cfg.get("logo_video_similarity", 0.4) or 0.4)))
                blend = max(0.0, min(1.0, float(cfg.get("logo_video_blend", 0.1) or 0.1)))
                idx = input_count
                input_count += 1
                extra_inputs.extend(["-stream_loop", "-1", "-i", str(logo_path)])
                lg_lbl = f"lgo{idx}"
                overlay_segments.append(
                    f"[{idx}:v]scale={size},chromakey=0x{chroma}:{sim:.2f}:{blend:.2f}[{lg_lbl}];"
                    f"[{label}][{lg_lbl}]overlay={pos}:shortest=1[{label}l]"
                )
                label = f"{label}l"
            except Exception:
                pass

    # 3) Watermark chữ
    txt = str(cfg.get("watermark_text") or "").strip()
    if txt:
        try:
            tf = _write_textfile(txt)
            font = _resolve_font(str(cfg.get("watermark_text_font") or "arial"))
            tsize = int(float(cfg.get("watermark_text_size", 50) or 50))
            pos_parts = str(cfg.get("watermark_text_pos") or "350:80").replace(",", ":").split(":")
            tx = pos_parts[0]
            ty = pos_parts[1] if len(pos_parts) > 1 else "10"
            color = str(cfg.get("watermark_text_color") or "white")
            draw_chain.append(
                f"drawtext=textfile='{_ff_path(tf)}':fontfile='{_ff_path(font)}':"
                f"fontsize={tsize}:fontcolor={color}:x={tx}:y={ty}"
            )
        except Exception:
            pass

    # 4) Chữ chạy ngang sát đáy khung (text scroll — học từ preset RenderDoc của DHB)
    stxt = str(cfg.get("text_scroll") or "").strip()
    if stxt:
        try:
            tf = _write_textfile(stxt)
            font = _resolve_font(str(cfg.get("text_scroll_font") or "arial"))
            ssize = int(float(cfg.get("text_scroll_size", 40) or 40))
            speed = max(1, int(float(cfg.get("text_scroll_speed", 10) or 10)))
            color = str(cfg.get("text_scroll_color") or "yellow")
            y_pos = max(0, int(frame_h) - max(30, int(float(cfg.get("text_scroll_height", 60) or 60))))
            draw_chain.append(
                f"drawtext=textfile='{_ff_path(tf)}':fontfile='{_ff_path(font)}':"
                f"fontsize={ssize}:fontcolor={color}:x='w-mod(t*{speed}\\,w+tw)':y={y_pos}"
            )
        except Exception:
            pass

    return overlay_segments, draw_chain, label, input_count


def _apply_intro_outro(main_path: str, cfg, target_w: int, target_h: int, fps: int,
                       progress_callback=None, stop_event=None, owner: str = "") -> str:
    """
    Ghép Intro / Outro từ kho tài nguyên (_Intro / _Outro) vào đầu / cuối video đã render,
    hỗ trợ chọn ngẫu nhiên (intro_random / outro_random) — kế thừa preset Render của DHB.
    """
    intro = _pick_media(
        DHB_RENDER_DIR / "_Intro", cfg.get("intro_name") or "", bool(cfg.get("intro_random")),
        (".mp4", ".mov", ".webm"),
    ) if cfg.get("intro_enabled") else None
    outro = _pick_media(
        DHB_RENDER_DIR / "_Outro", cfg.get("outro_name") or "", bool(cfg.get("outro_random")),
        (".mp4", ".mov", ".webm"),
    ) if cfg.get("outro_enabled") else None

    if not intro and not outro:
        return main_path

    main_probe = probe_video_info(main_path)
    media = [m for m in (intro, Path(main_path), outro) if m]
    inputs = []
    for m in media:
        inputs.extend(["-i", str(m)])

    parts, vids, auds = [], [], []
    f = int(fps or 30)
    # Main nằm ngay sau intro khi có intro, nếu không thì đứng đầu — KHÔNG giả định i == 1,
    # vì khi chỉ có outro thì main là i=0 và outro mới là segment cần chuẩn hóa khung hình.
    main_index = 1 if intro else 0
    for i, m in enumerate(media):
        # Concat filter yêu cầu các segment cùng timebase/pix_fmt/sample_rate/channel_layout —
        # chuẩn hóa triệt để (settb=AVTB + aformat) để tránh "Error linking filters"
        if i == main_index:
            parts.append(f"[{i}:v]fps={f},setsar=1,format=yuv420p,settb=AVTB[p{i}]")
        else:
            parts.append(
                f"[{i}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,fps={f},setsar=1,format=yuv420p,settb=AVTB[p{i}]"
            )
        vids.append(f"[p{i}]")
        seg_probe = probe_video_info(str(m))
        if seg_probe.get("has_audio"):
            parts.append(
                f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo,asettb=AVTB[a{i}]"
            )
        else:
            seg_dur = max(0.5, float(seg_probe.get("seconds") or 3.0))
            parts.append(
                f"anullsrc=channel_layout=stereo:sample_rate=44100,atrim=0:{seg_dur:.2f},asettb=AVTB[a{i}]"
            )
        auds.append(f"[a{i}]")

    n = len(media)
    # concat filter nhận input XEN KẼ theo từng segment: [v0][a0][v1][a1]... (KHÔNG phải video hết rồi audio)
    fc = ";".join(parts) + ";" + "".join(f"[p{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"

    out_path = str(APP_OUTPUT_DIR / f"yt_io_{int(time.time())}_{Path(main_path).stem}.mp4")
    cmd = (
        [FFMPEG_EXE, "-y"] + inputs
        + ["-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
        + ["-c:v", "libx264", "-r", str(f), "-crf", "20", "-preset", "superfast"]
        + ["-c:a", "aac", "-b:a", "192k"]
        + _clear_metadata_args()
        + [out_path]
    )
    _run_ffmpeg_with_progress(
        cmd, max(1.0, main_probe.get("seconds") or 10.0),
        progress_callback, stop_event=stop_event, partial_output=out_path, owner=owner,
    )
    try:
        if out_path != main_path and os.path.exists(main_path):
            os.remove(main_path)
    except Exception:
        pass
    return out_path


def time_str_to_seconds(t_str: str) -> float:
    if not t_str:
        return 0.0
    parts = t_str.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except Exception:
        return 0.0


def _clamp_int(value, minimum: int, maximum: int, default: int = 0) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _parse_pair(value, default):
    match = re.fullmatch(r"\s*(\d+)\s*[:xX]\s*(\d+)\s*", str(value or ""))
    if not match:
        return default
    return int(match.group(1)), int(match.group(2))


def _normalize_pad_color(value) -> str:
    raw = str(value or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", raw):
        return "0x" + raw.upper()
    if re.fullmatch(r"[a-zA-Z]+", raw):
        return raw
    return "black"


def _build_pre_edit_chain(cfg: Dict[str, Any], src_w: int, src_h: int, fps: int) -> str:
    """Chuỗi filter trung lập: xoay → crop → viền màu → move camera (zoom chậm)."""
    parts = []
    width = max(2, int(src_w or 0))
    height = max(2, int(src_h or 0))

    rotation = _clamp_int(cfg.get("rotation_angle", 0), 0, 270, 0)
    if rotation == 90:
        parts.append("transpose=1")
        width, height = height, width
    elif rotation == 180:
        parts.append("hflip,vflip")
    elif rotation == 270:
        parts.append("transpose=2")
        width, height = height, width

    if cfg.get("crop_enabled"):
        crop_w, crop_h = _parse_pair(cfg.get("crop_size"), (width, height))
        crop_w = max(2, min(width, crop_w))
        crop_h = max(2, min(height, crop_h))
        if cfg.get("crop_center", True):
            pos_x = (width - crop_w) // 2
            pos_y = (height - crop_h) // 2
        else:
            pos_x, pos_y = _parse_pair(cfg.get("crop_location"), (0, 0))
            pos_x = max(0, min(width - crop_w, pos_x))
            pos_y = max(0, min(height - crop_h, pos_y))
        parts.append(f"crop={crop_w}:{crop_h}:{pos_x}:{pos_y}")
        width, height = crop_w, crop_h

    pad_size = _clamp_int(cfg.get("pad_size", 0), 0, 300, 0)
    if cfg.get("pad_enabled") and pad_size > 0:
        pad_color = _normalize_pad_color(cfg.get("pad_color", "black"))
        parts.append(
            f"pad=iw+{pad_size * 2}:ih+{pad_size * 2}:{pad_size}:{pad_size}:color={pad_color}"
        )
        width += pad_size * 2
        height += pad_size * 2

    if cfg.get("move_camera"):
        try:
            cam_speed = max(0.1, min(3.0, float(cfg.get("move_camera_speed", 1.0) or 1.0)))
        except (TypeError, ValueError):
            cam_speed = 1.0
        zoom_w = max(2, width - (width % 2))
        zoom_h = max(2, height - (height % 2))
        step = 0.0004 * cam_speed
        parts.append(
            f"zoompan=z='min(zoom+{step:.5f},1.12)':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={zoom_w}x{zoom_h}:fps={fps}"
        )

    return ",".join(parts)


def _clear_metadata_args() -> list:
    return [
        "-metadata", "title=",
        "-metadata", "artist=",
        "-metadata", "album_artist=",
        "-metadata", "album=",
        "-metadata", "date=",
        "-metadata", "track=",
        "-metadata", "genre=",
        "-metadata", "publisher=",
        "-metadata", "encoded_by=",
        "-metadata", "copyright=",
        "-metadata", "composer=",
        "-metadata", "performer=",
        "-metadata", "comment=DHB_YT_" + str(int(time.time() * 1000)),
        "-metadata", "encoder=",
    ]


def _make_output_filename(prefix: str, stem: str, suffix: str) -> str:
    return f"{prefix}_{time.time_ns()}_{uuid.uuid4().hex[:6]}_{stem}{suffix}"


def _split_custom_command(cmd: str) -> list:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
        shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        argc = ctypes.c_int()
        argv = shell32.CommandLineToArgvW(cmd, ctypes.byref(argc))
        if not argv:
            raise ValueError("Không phân tích được Custom FFmpeg command.")
        try:
            return [argv[i] for i in range(argc.value)]
        finally:
            kernel32.LocalFree(argv)
    return shlex.split(cmd, posix=True)


def _build_custom_ffmpeg_args(raw_cmd: str, input_path: str, output_path: str) -> list:
    """Tách lệnh custom thành argv để chạy FFmpeg không qua shell (chặn command injection)."""
    cmd = str(raw_cmd or "").strip()
    if not cmd:
        raise ValueError("Custom FFmpeg command đang trống.")
    if cmd.lower().startswith("ffmpeg"):
        cmd = cmd[6:].strip()
    args = _split_custom_command(cmd)
    if not args:
        raise ValueError("Custom FFmpeg command không có tham số hợp lệ.")
    cleaned_args = []
    for arg in args:
        cleaned_args.append(arg.replace("{input}", input_path).replace("{output}", output_path))
    return [FFMPEG_EXE] + cleaned_args


def render_video(
    input_path: str,
    render_mode: str = "shorts_9_16",
    render_config: Optional[Dict[str, Any]] = None,
    cut_start: str = "00:00:00",
    cut_end: str = "00:00:00",
    progress_callback: Optional[Callable[[int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
    owner: str = "",
) -> str:
    """
    Thực thi Render & Lách bản quyền video bằng FFmpeg kế thừa tài nguyên từ C:\\DHB Reup Facebook\\Data\\Render.
    Trả về đường dẫn video thành phẩm trong thư mục output/.
    """
    if not input_path or not os.path.exists(input_path):
        raise FileNotFoundError(f"Không tìm thấy file video đầu vào: {input_path}")

    if stop_event and stop_event.is_set():
        raise InterruptedError("Tiến trình render đã bị dừng trước khi bắt đầu.")

    cfg = render_config or {}
    in_file = Path(input_path)
    out_filename = _make_output_filename("yt", in_file.stem, ".mp4")
    output_path = str(APP_OUTPUT_DIR / out_filename)

    if render_mode == "none":
        shutil.copy2(input_path, output_path)
        if progress_callback:
            progress_callback(100, "Giữ nguyên video gốc")
        return output_path

    probe = probe_video_info(input_path)
    has_audio = bool(probe.get("has_audio", True))
    # Video câm (không có track âm thanh): chèn nguồn im lặng anullsrc để FFmpeg không lỗi "Stream specifier ':a' matches no streams"
    silent_audio_input = [] if has_audio else [
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
    ]
    total_sec = max(1.0, probe.get("seconds", 10.0))
    start_sec = time_str_to_seconds(cut_start or cfg.get("cut_start", "00:00:00"))
    end_cut_sec = time_str_to_seconds(cut_end or cfg.get("cut_end", "00:00:00"))
    effective_dur = max(1.0, total_sec - start_sec - end_cut_sec)

    speed = float(cfg.get("speed", 1.0) or 1.0)
    volume = float(cfg.get("volume", 1.0) or 1.0)
    # Trộn % nhạc gốc (học OriginalAudioPercent của DHB): 0-100% -> quy đổi thành volume
    aop = cfg.get("audio_original_percent")
    if aop is not None:
        try:
            volume = max(0.0, min(3.0, float(aop) / 100.0))
        except Exception:
            pass
    hflip = bool(cfg.get("hflip", False))
    brightness = float(cfg.get("brightness", 0.0) or 0.0)
    preset = str(cfg.get("preset", "superfast") or "superfast")
    fps = int(cfg.get("framerate", 30) or 30)

    # Tối ưu hóa thời lượng chuẩn YouTube Shorts (luôn <= 59s để 100% được xếp vào Shorts Feed)
    enforce_shorts = cfg.get("enforce_shorts_duration", True)
    if render_mode == "shorts_9_16" and enforce_shorts:
        max_shorts_dur = float(cfg.get("max_shorts_duration", 58.5) or 58.5)
        cur_dur = effective_dur / speed
        if cur_dur > max_shorts_dur:
            fit_mode = cfg.get("shorts_fit_mode", "speedup")
            if fit_mode == "speedup" and cur_dur <= 72.0:
                speed = round(effective_dur / max_shorts_dur, 2)
            else:
                effective_dur = max_shorts_dur * speed

    # Cấu hình chất lượng xuất xưởng: Full HD (1080p) đến 4K Ultra HD (2160p)
    res_mode = str(cfg.get("resolution", "1080p") or "1080p").lower()
    if res_mode == "4k":
        w_shorts, h_shorts = 2160, 3840
        w_16_9, h_16_9 = 3840, 2160
        default_bitrate = "28000k"
        crf_val = "18"
    elif res_mode == "2k":
        w_shorts, h_shorts = 1440, 2560
        w_16_9, h_16_9 = 2560, 1440
        default_bitrate = "16000k"
        crf_val = "19"
    elif res_mode == "original":
        orig_w = int(probe.get("width") or 1080)
        orig_h = int(probe.get("height") or 1920)
        w_shorts, h_shorts = orig_w, orig_h
        w_16_9, h_16_9 = orig_w, orig_h
        default_bitrate = "12000k"
        crf_val = "19"
    else:  # '1080p' Full HD
        w_shorts, h_shorts = 1080, 1920
        w_16_9, h_16_9 = 1920, 1080
        default_bitrate = "8000k"
        crf_val = "20"

    bitrate = str(cfg.get("bitrate") or default_bitrate)

    # Các chế độ copy stream/custom command không có filter graph để áp crop/xoay/viền/zoom
    pre_edit_chain = ""
    if render_mode not in ("md5_cut", "custom_ffmpeg"):
        pre_edit_chain = _build_pre_edit_chain(
            cfg,
            _clamp_int(probe.get("width"), 0, 100000, 0),
            _clamp_int(probe.get("height"), 0, 100000, 0),
            fps,
        )
    src_prefix = f"[0:v]{pre_edit_chain}," if pre_edit_chain else "[0:v]"

    # Kích thước khung xuất cuối (dùng để chuẩn hóa Intro/Outro) — cập nhật theo từng chế độ
    out_w, out_h = w_16_9, h_16_9
    rendered = False

    trim_args = []
    if start_sec > 0:
        trim_args.extend(["-ss", f"{start_sec:.2f}"])
    if (end_cut_sec > 0 or (render_mode == "shorts_9_16" and enforce_shorts)) and effective_dur > 0:
        trim_args.extend(["-t", f"{effective_dur:.2f}"])

    # Chế độ 1: Cắt đầu/cuối + Đổi MD5 + Xóa sạch ID3 Tags (Siêu tốc - copy stream)
    if render_mode == "md5_cut":
        cmd = (
            [FFMPEG_EXE, "-y"]
            + trim_args
            + ["-i", input_path, "-codec", "copy"]
            + _clear_metadata_args()
            + [output_path]
        )
        _run_ffmpeg_with_progress(cmd, effective_dur, progress_callback, stop_event=stop_event, partial_output=output_path, owner=owner)
        return output_path

    # Chế độ 7: Custom FFmpeg Command
    if render_mode == "custom_ffmpeg" and cfg.get("custom_command"):
        cmd = _build_custom_ffmpeg_args(str(cfg["custom_command"]), input_path, output_path)
        _run_ffmpeg_with_progress(cmd, effective_dur, progress_callback, stop_event=stop_event, partial_output=output_path, owner=owner)
        return output_path

    # Xây dựng bộ lọc cơ bản (speed, flip, brightness, audio tempo/volume)
    v_mods = []
    if hflip:
        v_mods.append("hflip")
    if abs(brightness) > 0.001:
        v_mods.append(f"eq=brightness={brightness:.2f}")
    if render_mode == "shorts_9_16" and cfg.get("shorts_visual_pop", cfg.get("shorts_vibrant_colors", True)):
        v_mods.append("eq=contrast=1.04:saturation=1.12")
        v_mods.append("unsharp=3:3:0.35:3:3:0.0")
    if abs(speed - 1.0) > 0.01:
        v_mods.append(f"setpts=PTS/{speed:.2f}")
    v_mod_chain = ("," + ",".join(v_mods)) if v_mods else ""

    a_mods = []
    if abs(speed - 1.0) > 0.01:
        clamped_speed = max(0.5, min(2.0, speed))
        a_mods.append(f"atempo={clamped_speed:.2f}")
    if abs(volume - 1.0) > 0.01:
        a_mods.append(f"volume={volume:.2f}")
    if cfg.get("audio_pitch_shift") or render_mode in ("audio_effect", "dhb_audio_master"):
        # Công thức lách âm thanh đỉnh cao kế thừa nguyên bản từ DHB Reup:
        # 1. Định dạng âm thanh nổi stereo 44.1kHz chuẩn studio
        # 2. Bộ cân bằng âm sắc 10 băng tần (Equalizer) triệt tiêu tần số trầm 31Hz (-10dB), tăng dải rõ 500Hz-1kHz (+9dB)
        # 3. Nén dải động Compand làm sai lệch thuật toán nhận dạng Content ID
        # 4. Hiệu ứng Chorus + Phaser làm mờ phổ âm thanh
        # 5. Dịch tần số mẫu asetrate + atempo
        a_mods.extend([
            "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo",
            "equalizer=f=31:width_type=o:w=1:g=-10,equalizer=f=63:width_type=o:w=1:g=3,equalizer=f=125:width_type=o:w=1:g=8,equalizer=f=500:width_type=o:w=1:g=9,equalizer=f=1000:width_type=o:w=1:g=9,equalizer=f=2000:width_type=o:w=1:g=6,equalizer=f=4000:width_type=o:w=1:g=4,equalizer=f=8000:width_type=o:w=1:g=3",
            "compand=.3|.3:1|1:-90/-60|-60/-40|-40/-30|-20/-20:6:0:-90:0.2",
            "chorus=0.2:0.4:20|20:0.4|0.12:0.12|0.4:2|1.2",
            "aphaser=in_gain=0.9:out_gain=0.9:delay=2.9:decay=0.3:speed=0.33",
            "asetrate=44100*0.97,atempo=1/0.97",
            "highpass=f=100,lowpass=f=14000",
        ])
    a_filter = ",".join(a_mods) if a_mods else "anull"

    # Chế độ 2: Render Dọc 9:16 Chuẩn YouTube Shorts có nền Blur mờ (Hỗ trợ 1080p đến 4K)
    if render_mode == "shorts_9_16":
        blur_rad = 40 if res_mode == "4k" else (30 if res_mode == "2k" else 20)
        base_fc = (
            f"{src_prefix}scale={w_shorts}:{h_shorts}:force_original_aspect_ratio=increase,crop={w_shorts}:{h_shorts},boxblur={blur_rad}:10[bg];"
            f"{src_prefix}scale={w_shorts}:{h_shorts}:force_original_aspect_ratio=decrease{v_mod_chain}[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[vpre]"
        )

        bg_music_name = cfg.get("bg_music_name", "")
        bg_music_path = DHB_RENDER_DIR / "_Backgroud Music" / bg_music_name if bg_music_name else None

        extra_inputs: list = []
        if bg_music_path and bg_music_path.exists():
            brand_segments, draw_chain, brand_label, input_count = _build_branding(cfg, "vpre", 2, extra_inputs, h_shorts)
        else:
            brand_segments, draw_chain, brand_label, input_count = _build_branding(
                cfg, "vpre", 1 + (0 if has_audio else 1), extra_inputs, h_shorts
            )
        tail_filters = ",".join(draw_chain + ["format=yuv420p"])
        filter_complex = base_fc + (";" + ";".join(brand_segments) if brand_segments else "") + ";" + f"[{brand_label}]{tail_filters}[outv]"

        if bg_music_path and bg_music_path.exists():
            bg_pct = cfg.get("bg_music_percent")
            music_vol = (float(bg_pct) / 100.0) if bg_pct is not None else float(cfg.get("bg_music_volume", 0.25) or 0.25)
            if has_audio:
                audio_fc = f";[0:a]{a_filter}[a0];[1:a]volume={music_vol:.2f}[a1];[a0][a1]amix=inputs=2:duration=first:dropout_transition=2[outa]"
            else:
                # Video câm: chỉ còn nhạc nền làm âm thanh
                audio_fc = f";[1:a]volume={music_vol:.2f}[outa]"
            cmd = (
                [FFMPEG_EXE, "-y"]
                + trim_args
                + ["-i", input_path, "-stream_loop", "-1", "-i", str(bg_music_path)]
                + extra_inputs
                + ["-filter_complex", filter_complex + audio_fc, "-map", "[outv]", "-map", "[outa]"]
                + ["-c:v", "libx264", "-r", str(fps), "-crf", crf_val, "-b:v", bitrate, "-preset", preset]
                + ["-c:a", "aac", "-b:a", "320k", "-ar", "48000"]
                + _clear_metadata_args()
                + [output_path]
            )
        elif has_audio:
            cmd = (
                [FFMPEG_EXE, "-y"]
                + trim_args
                + ["-i", input_path]
                + extra_inputs
                + ["-filter_complex", filter_complex, "-map", "[outv]", "-map", "0:a?"]
                + ["-af", a_filter, "-c:v", "libx264", "-r", str(fps), "-crf", crf_val, "-b:v", bitrate, "-preset", preset]
                + ["-c:a", "aac", "-b:a", "320k", "-ar", "48000"]
                + _clear_metadata_args()
                + [output_path]
            )
        else:
            # Video câm không nhạc nền: chèn tiếng im lặng giữ chuẩn output
            cmd = (
                [FFMPEG_EXE, "-y"]
                + trim_args
                + ["-i", input_path]
                + silent_audio_input
                + extra_inputs
                + ["-filter_complex", filter_complex, "-map", "[outv]", "-map", "1:a", "-shortest"]
                + ["-c:v", "libx264", "-r", str(fps), "-crf", crf_val, "-b:v", bitrate, "-preset", preset]
                + ["-c:a", "aac", "-b:a", "320k", "-ar", "48000"]
                + _clear_metadata_args()
                + [output_path]
            )
        _run_ffmpeg_with_progress(cmd, effective_dur / speed, progress_callback, stop_event=stop_event, partial_output=output_path, owner=owner)
        out_w, out_h = w_shorts, h_shorts
        rendered = True

    # Chế độ 3: Lách Thu Nhỏ (PiP trên Video Nền từ C:\DHB Reup Facebook\Data\Render\_Backgroud Video)
    if render_mode == "pip_thu_nho":
        bg_name = cfg.get("bg_video_name") or "Bubble HD.mp4"
        bg_path = _pick_media(
            DHB_RENDER_DIR / "_Backgroud Video", bg_name, bool(cfg.get("bg_video_random")),
            (".mp4", ".mov", ".webm"),
        )
        if not bg_path:
            # Lấy video nền đầu tiên trong thư mục nếu không tìm thấy
            bgs = list((DHB_RENDER_DIR / "_Backgroud Video").glob("*.mp4")) if (DHB_RENDER_DIR / "_Backgroud Video").exists() else []
            bg_path = bgs[0] if bgs else None

        if bg_path and bg_path.exists():
            base_fc = (
                f"[1:v]scale=1280:720,boxblur=4:1[bg];"
                f"{src_prefix}scale=960:540:force_original_aspect_ratio=decrease,pad=iw+8:ih+8:4:4:color=white{v_mod_chain}[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1[vpre]"
            )
            extra_inputs = []
            brand_segments, draw_chain, brand_label, input_count = _build_branding(
                cfg, "vpre", 2 + (0 if has_audio else 1), extra_inputs, 720
            )
            tail_filters = ",".join(draw_chain + ["format=yuv420p"])
            filter_complex = base_fc + (";" + ";".join(brand_segments) if brand_segments else "") + ";" + f"[{brand_label}]{tail_filters}[outv]"

            if has_audio:
                audio_map_args = ["-map", "[outv]", "-map", "0:a?"]
                audio_fx_args = ["-af", a_filter]
            else:
                # Video câm: anullsrc là input thứ 3 (index 2)
                audio_map_args = ["-map", "[outv]", "-map", "2:a", "-shortest"]
                audio_fx_args = []
            cmd = (
                [FFMPEG_EXE, "-y"]
                + trim_args
                + ["-i", input_path, "-stream_loop", "-1", "-i", str(bg_path)]
                + silent_audio_input
                + extra_inputs
                + ["-filter_complex", filter_complex]
                + audio_map_args + audio_fx_args
                + ["-c:v", "libx264", "-r", str(fps), "-b:v", bitrate, "-preset", preset]
                + ["-c:a", "aac", "-b:a", "192k", "-ar", "44100"]
                + _clear_metadata_args()
                + [output_path]
            )
            _run_ffmpeg_with_progress(cmd, effective_dur / speed, progress_callback, stop_event=stop_event, partial_output=output_path, owner=owner)
            out_w, out_h = 1280, 720
            rendered = True

    # Chế độ 4: Render Vuông 1:1 (1080x1080)
    if not rendered and render_mode == "square_1_1":
        base_fc = (
            f"{src_prefix}scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080,boxblur=20:10[bg];"
            f"{src_prefix}scale=1080:1080:force_original_aspect_ratio=decrease{v_mod_chain}[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[vpre]"
        )
        extra_inputs = []
        brand_segments, draw_chain, brand_label, input_count = _build_branding(
            cfg, "vpre", 1 + (0 if has_audio else 1), extra_inputs, 1080
        )
        tail_filters = ",".join(draw_chain + ["format=yuv420p"])
        filter_complex = base_fc + (";" + ";".join(brand_segments) if brand_segments else "") + ";" + f"[{brand_label}]{tail_filters}[outv]"

        if has_audio:
            audio_map_args = ["-map", "[outv]", "-map", "0:a?"]
            audio_fx_args = ["-af", a_filter]
        else:
            audio_map_args = ["-map", "[outv]", "-map", "1:a", "-shortest"]
            audio_fx_args = []
        cmd = (
            [FFMPEG_EXE, "-y"]
            + trim_args
            + ["-i", input_path]
            + silent_audio_input
            + extra_inputs
            + ["-filter_complex", filter_complex]
            + audio_map_args + audio_fx_args
            + ["-c:v", "libx264", "-r", str(fps), "-b:v", bitrate, "-preset", preset]
            + ["-c:a", "aac", "-b:a", "192k"]
            + _clear_metadata_args()
            + [output_path]
        )
        _run_ffmpeg_with_progress(cmd, effective_dur / speed, progress_callback, stop_event=stop_event, partial_output=output_path, owner=owner)
        out_w, out_h = 1080, 1080
        rendered = True

    # Mặc định / Chế độ 5 & 6: Lách Full Màn 16:9 (Hỗ trợ Full HD 1080p đến 4K 3840x2160)
    # Guard: chỉ chạy khi chưa có chế độ nào phía trên render (shorts/pip/square) — tránh render đè
    if not rendered:
        target_res = cfg.get("size")
        if target_res:
            w_res, h_res = (target_res.split(":") + ["1920", "1080"])[:2]
        else:
            w_res, h_res = w_16_9, h_16_9

        vf_core = (
            f"{src_prefix}scale={w_res}:{h_res}:force_original_aspect_ratio=decrease,"
            f"pad={w_res}:{h_res}:(ow-iw)/2:(oh-ih)/2:black"
            f"{v_mod_chain}"
        )
        bg_music_name = cfg.get("bg_music_name", "")
        bg_music_path = DHB_RENDER_DIR / "_Backgroud Music" / bg_music_name if bg_music_name else None

        extra_inputs = []
        if bg_music_path and bg_music_path.exists():
            brand_segments, draw_chain, brand_label, input_count = _build_branding(cfg, "vpre", 2, extra_inputs, h_res)
        else:
            brand_segments, draw_chain, brand_label, input_count = _build_branding(
                cfg, "vpre", 1 + (0 if has_audio else 1), extra_inputs, h_res
            )
        tail_filters = ",".join(draw_chain + ["format=yuv420p"])
        video_fc = f"{vf_core}[vpre]" + (";" + ";".join(brand_segments) if brand_segments else "") + ";" + f"[{brand_label}]{tail_filters}[outv]"

        # Nếu có chọn nhạc nền MP3 từ C:\DHB Reup Facebook\Data\Render\_Backgroud Music
        if bg_music_path and bg_music_path.exists():
            bg_pct = cfg.get("bg_music_percent")
            music_vol = (float(bg_pct) / 100.0) if bg_pct is not None else float(cfg.get("bg_music_volume", 0.3) or 0.3)
            if has_audio:
                audio_fc = f"[0:a]{a_filter}[a0];[1:a]volume={music_vol:.2f}[a1];[a0][a1]amix=inputs=2:duration=first:dropout_transition=2[outa]"
            else:
                # Video câm: chỉ còn nhạc nền làm âm thanh
                audio_fc = f"[1:a]volume={music_vol:.2f}[outa]"
            filter_complex = video_fc + ";" + audio_fc
            cmd = (
                [FFMPEG_EXE, "-y"]
                + trim_args
                + ["-i", input_path, "-stream_loop", "-1", "-i", str(bg_music_path)]
                + extra_inputs
                + ["-filter_complex", filter_complex, "-map", "[outv]", "-map", "[outa]"]
                + ["-c:v", "libx264", "-r", str(fps), "-b:v", bitrate, "-preset", preset]
                + ["-c:a", "aac", "-b:a", "192k"]
                + _clear_metadata_args()
                + [output_path]
            )
        elif has_audio:
            cmd = (
                [FFMPEG_EXE, "-y"]
                + trim_args
                + ["-i", input_path]
                + extra_inputs
                + ["-filter_complex", video_fc, "-map", "[outv]", "-map", "0:a?"]
                + ["-af", a_filter, "-c:v", "libx264", "-r", str(fps), "-b:v", bitrate, "-preset", preset]
                + ["-c:a", "aac", "-b:a", "192k"]
                + _clear_metadata_args()
                + [output_path]
            )
        else:
            # Video câm không nhạc nền: chèn tiếng im lặng, -shortest bám theo chiều dài video
            cmd = (
                [FFMPEG_EXE, "-y"]
                + trim_args
                + ["-i", input_path]
                + silent_audio_input
                + extra_inputs
                + ["-filter_complex", video_fc, "-map", "[outv]", "-map", "1:a", "-shortest"]
                + ["-c:v", "libx264", "-r", str(fps), "-b:v", bitrate, "-preset", preset]
                + ["-c:a", "aac", "-b:a", "192k"]
                + _clear_metadata_args()
                + [output_path]
            )

        _run_ffmpeg_with_progress(cmd, effective_dur / speed, progress_callback, stop_event=stop_event, partial_output=output_path, owner=owner)
        out_w, out_h = w_res, h_res
        rendered = True

    # GHÉP INTRO/OUTRO TỰ ĐỘNG (kế thừa preset Render của DHB) — áp cho các chế độ có encode lại video
    if rendered and (cfg.get("intro_enabled") or cfg.get("outro_enabled")):
        output_path = _apply_intro_outro(
            output_path, cfg, out_w, out_h, fps,
            progress_callback=progress_callback, stop_event=stop_event, owner=owner,
        )
    return output_path


def _cleanup_partial_output(partial_output: Optional[str]):
    """Xóa file video xuất dở dang khi render thất bại hoặc bị người dùng bấm Dừng."""
    if partial_output:
        try:
            if os.path.exists(partial_output):
                os.remove(partial_output)
        except Exception:
            pass


def _run_ffmpeg_with_progress(
    cmd: list,
    expected_duration: float,
    progress_callback: Optional[Callable[[int, str], None]],
    stop_event: Optional[threading.Event] = None,
    partial_output: Optional[str] = None,
    owner: str = "",
):
    if progress_callback:
        progress_callback(5, "Khởi chạy FFmpeg render...")
    if stop_event and stop_event.is_set():
        raise InterruptedError("Tiến trình render đã bị dừng trước khi bắt đầu.")

    proc = subprocess.Popen(
        cmd,
        # FFmpeg chỉ ghi progress ra stderr; stdout để DEVNULL tránh pipe đầy làm treo render.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="ignore",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    with _ACTIVE_FFMPEG_LOCK:
        _ACTIVE_FFMPEG_PROCS[proc.pid] = (proc, owner)

    stderr_lines = []
    try:
        if proc.stderr:
            while True:
                if stop_event and stop_event.is_set():
                    _terminate_ffmpeg_proc(proc)
                    raise InterruptedError("Người dùng đã bấm Dừng Render.")

                line = proc.stderr.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.04)
                    continue

                stderr_lines.append(line)
                m = TIME_REGEX.search(line)
                if m and progress_callback and expected_duration > 0:
                    if stop_event and stop_event.is_set():
                        _terminate_ffmpeg_proc(proc)
                        raise InterruptedError("Người dùng đã bấm Dừng Render.")
                    cur_sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    pct = min(99, max(5, int(cur_sec * 100 / expected_duration)))
                    progress_callback(pct, f"Đang Render... {pct}%")

        proc.wait()
        if stop_event and stop_event.is_set():
            raise InterruptedError("Người dùng đã bấm Dừng Render.")
        if proc.returncode != 0:
            err_tail = "".join(stderr_lines[-10:])
            raise RuntimeError(f"FFmpeg render thất bại (code {proc.returncode}): {err_tail}")
        if progress_callback:
            progress_callback(100, "Render hoàn tất")
    except BaseException:
        # Mọi lỗi (callback, dừng, FFmpeg fail) phải kill + reap TRƯỚC khi xóa file,
        # nếu không FFmpeg sẽ mồ côi và tiếp tục giữ/ghi đè file output.
        _terminate_ffmpeg_proc(proc)
        _cleanup_partial_output(partial_output)
        raise
    finally:
        with _ACTIVE_FFMPEG_LOCK:
            _ACTIVE_FFMPEG_PROCS.pop(proc.pid, None)
        try:
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass


def generate_thumbnail_from_video(video_path: str, output_path: Optional[str] = None) -> Optional[str]:
    """
    Tự động trích xuất ảnh bìa Thumbnail chất lượng cao từ video nguồn bằng FFmpeg.
    Lấy cảm hứng từ ReupThumbnail của DHB Reup Facebook.
    Tối ưu cho Shorts: lấy khung hình tại điểm vàng 20% - 30% thời lượng để bắt trọn khoảnh khắc hấp dẫn nhất.
    """
    if not video_path or not os.path.exists(video_path):
        return None
    if not output_path:
        stem = Path(video_path).stem
        output_path = str(APP_OUTPUT_DIR / _make_output_filename("thumb", stem, ".jpg"))
    try:
        probe = probe_video_info(video_path)
        total_sec = float(probe.get("seconds", 10.0) or 10.0)
        capture_sec = max(0.5, min(total_sec * 0.25, 3.5)) if total_sec > 1.5 else 0.5
        time_str = f"{capture_sec:.2f}"

        cmd = [
            FFMPEG_EXE, "-y",
            "-ss", time_str,
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            output_path,
        ]
        res = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if res.returncode == 0 and os.path.exists(output_path):
            return output_path
    except Exception as e:
        from database import add_log
        add_log("Render", f"Lỗi tạo thumbnail cho '{os.path.basename(video_path)}': {e}", "WARNING")
    return None
