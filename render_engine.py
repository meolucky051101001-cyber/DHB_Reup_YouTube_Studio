import os
import re
import shlex
import math
import signal
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
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

# Watchdog: FFmpeg không ghi ra stderr trong khoảng này (giây) thì coi như bị treo,
# tự hủy tiến trình để không kẹt vĩnh viễn worker của render queue (pool chỉ có 2 slot).
# Encode bình thường luôn ghi dòng progress ~0.5s/lần nên 120s là dư an toàn.
FFMPEG_SILENCE_TIMEOUT_SEC = 120.0
FFMPEG_MAX_RUNTIME_SEC = 4 * 60 * 60

# Quản lý toàn bộ tiến trình FFmpeg đang chạy để có thể dừng ngay lập tức khi người dùng bấm '⏹️ Dừng'.
# Mỗi PID gắn owner ('batch' / 'monitor') để Dừng batch không giết render của Monitor.
_ACTIVE_FFMPEG_LOCK = threading.Lock()
_ACTIVE_FFMPEG_PROCS: Dict[int, Tuple[subprocess.Popen, str]] = {}


def _path_within(path: Path, root: Path) -> bool:
    try:
        path_norm = os.path.normcase(str(path))
        root_norm = os.path.normcase(str(root))
        return os.path.commonpath([path_norm, root_norm]) == root_norm
    except (OSError, ValueError):
        return False


def _terminate_ffmpeg_proc(proc: subprocess.Popen, timeout: float = 5.0):
    """Kill cây tiến trình do lượt render này sở hữu và reap tiến trình gốc."""
    if proc.poll() is None and os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                shell=False,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
    elif proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    try:
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=timeout)
        except Exception:
            pass


def kill_all_render_processes(owner: Optional[str] = None):
    """Hủy các tiến trình FFmpeg do tool khởi chạy.

    owner=None  → dừng tất cả (kể cả dọn tiến trình rơi rớt).
    owner='batch' / 'monitor' → chỉ dừng đúng làn đó, không đụng làn còn lại.
    Chỉ dừng PID do tiến trình hiện tại theo dõi; không suy đoán ownership qua command-line.
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

def _ff_path(p) -> str:
    """Chuẩn hóa đường dẫn cho filter FFmpeg (drawtext textfile/fontfile):
    bỏ backslash + escape dấu hai chấm ổ đĩa Windows thành '\\:' (2 cấp escaping của filtergraph)."""
    s = str(p).replace("\\", "/")
    s = s.replace("'", "\\'")
    return s.replace(":", "\\:")


def _resolve_font(name: str) -> str:
    """Chỉ nhận tên font nằm trong kho font hoặc thư mục font hệ thống."""
    raw = str(name or "arial").strip()
    if not raw:
        raw = "arial"
    if Path(raw).name != raw or raw in (".", ".."):
        raise ValueError("Font chỉ được chọn bằng tên file, không nhận đường dẫn tùy ý.")
    if not raw.lower().endswith((".ttf", ".otf")):
        raw += ".ttf"
    dhb_font_folder = DHB_RENDER_DIR / "_Font"
    for folder in (dhb_font_folder, Path("C:/Windows/Fonts")):
        candidate = folder / raw
        try:
            resolved_folder = folder.resolve()
            if folder == dhb_font_folder and not _path_within(resolved_folder, DHB_RENDER_DIR.resolve()):
                continue
            resolved_candidate = candidate.resolve(strict=True)
            if _path_within(resolved_candidate, resolved_folder) and resolved_candidate.is_file():
                return str(resolved_candidate)
        except (OSError, ValueError):
            continue
    raise FileNotFoundError(f"Không tìm thấy font '{raw}' trong kho tài nguyên hoặc Windows Fonts.")


def _write_textfile(text: str) -> str:
    """Ghi text watermark ra file tạm để drawtext đọc qua textfile (tránh lỗi escape ký tự đặc biệt)."""
    APP_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    p = APP_TEMP_DIR / f"wmtext_{uuid.uuid4().hex}.txt"
    p.write_text(text, encoding="utf-8")
    try:
        from database import register_managed_artifact
        register_managed_artifact(str(p), "render_text", "render", None)
    except Exception:
        pass
    return str(p)


def _pick_media(folder: Path, name: str, use_random: bool, exts) -> Optional[Path]:
    """Chọn file hợp lệ nằm trong đúng thư mục tài nguyên; lựa chọn thiếu sẽ báo rõ."""
    root = folder.resolve()
    render_root = DHB_RENDER_DIR.resolve()
    try:
        within_render_root = _path_within(root, render_root)
    except ValueError as exc:
        raise ValueError("Thư mục tài nguyên đã resolve ra ngoài kho Render được cấu hình.") from exc
    if not within_render_root:
        raise ValueError("Thư mục tài nguyên đã resolve ra ngoài kho Render được cấu hình.")
    if use_random:
        if not root.is_dir():
            raise FileNotFoundError(f"Không có thư mục tài nguyên: {root}")
        files = []
        for candidate in root.iterdir():
            try:
                resolved = candidate.resolve(strict=True)
                if candidate.is_file() and candidate.suffix.lower() in exts and _path_within(resolved, root):
                    files.append(resolved)
            except (OSError, ValueError):
                continue
        if not files:
            raise FileNotFoundError(f"Không có tài nguyên phù hợp trong '{root}'.")
        import random
        return files[random.randrange(len(files))]
    if not name:
        return None
    if Path(str(name)).name != str(name) or str(name) in (".", ".."):
        raise ValueError("Tên tài nguyên không được chứa đường dẫn thư mục.")
    candidate = root / str(name)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FileNotFoundError(f"Không tìm thấy tài nguyên '{name}' trong '{root}'.") from exc
    if not _path_within(resolved, root) or not resolved.is_file():
        raise ValueError(f"Tài nguyên '{name}' nằm ngoài thư mục được phép hoặc không phải file.")
    if resolved.suffix.lower() not in exts:
        raise ValueError(f"Định dạng tài nguyên '{name}' không được hỗ trợ.")
    return resolved


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

    def pair_text(value, default, maximum=7680, minimum=0):
        pair = _parse_pair(str(value or "").replace(",", ":"), default)
        return tuple(max(minimum, min(maximum, int(v))) for v in pair)

    def opacity_value(value, default):
        number = float(default if value in (None, "") else value)
        if not math.isfinite(number):
            raise ValueError("Giá trị opacity phải là số hữu hạn.")
        return max(0.0, min(1.0, number))

    def color_value(value, default):
        color = str(value or default).strip()
        if not re.fullmatch(r"(?:[A-Za-z]{1,20}|#[0-9a-fA-F]{6}|0x[0-9a-fA-F]{6})", color):
            raise ValueError("Màu chữ không hợp lệ.")
        return color

    # Watermark ảnh từ kho _Image
    img_name = (cfg.get("watermark_image_name") or "").strip()
    if img_name:
        img_path = _pick_media(DHB_RENDER_DIR / "_Image", img_name, False, (".png", ".jpg", ".jpeg", ".webp"))
        size = pair_text(cfg.get("watermark_image_size"), (240, 120), minimum=2)
        pos = pair_text(cfg.get("watermark_image_pos"), (10, 10))
        op = opacity_value(cfg.get("watermark_image_opacity"), 1.0)
        idx = input_count
        input_count += 1
        extra_inputs.extend(["-i", str(img_path)])
        wm_lbl = f"wmi{idx}"
        overlay_segments.append(
            f"[{idx}:v]scale={size[0]}:{size[1]},format=rgba,colorchannelmixer=aa={op:.2f}[{wm_lbl}];"
            f"[{label}][{wm_lbl}]overlay={pos[0]}:{pos[1]}:eof_action=repeat[{label}w]"
        )
        label = f"{label}w"

    # Logo video chroma-key (tự tách nền xanh — tính năng đặc trưng của DHB)
    logo_name = (cfg.get("logo_video_name") or "").strip()
    if logo_name:
        logo_path = _pick_media(DHB_RENDER_DIR / "_Video", logo_name, False, (".mp4", ".mov", ".webm"))
        size = pair_text(cfg.get("logo_video_size"), (300, 150), minimum=2)
        pos = pair_text(cfg.get("logo_video_pos"), (10, 10))
        chroma = str(cfg.get("logo_video_chroma") or "13FE06").strip().lstrip("#").removeprefix("0x")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", chroma):
            raise ValueError("Màu chroma-key phải là mã RGB 6 ký tự.")
        sim = opacity_value(cfg.get("logo_video_similarity"), 0.4)
        blend = opacity_value(cfg.get("logo_video_blend"), 0.1)
        idx = input_count
        input_count += 1
        extra_inputs.extend(["-stream_loop", "-1", "-i", str(logo_path)])
        lg_lbl = f"lgo{idx}"
        overlay_segments.append(
            f"[{idx}:v]scale={size[0]}:{size[1]},chromakey=0x{chroma}:{max(0.01, sim):.2f}:{blend:.2f}[{lg_lbl}];"
            f"[{label}][{lg_lbl}]overlay={pos[0]}:{pos[1]}:shortest=1[{label}l]"
        )
        label = f"{label}l"

    txt = str(cfg.get("watermark_text") or "").strip()
    if txt:
        tf = _write_textfile(txt)
        font = _resolve_font(str(cfg.get("watermark_text_font") or "arial"))
        tsize = _clamp_int(cfg.get("watermark_text_size", 50), 8, 400, 50)
        tx, ty = pair_text(cfg.get("watermark_text_pos"), (350, 80))
        color = color_value(cfg.get("watermark_text_color"), "white")
        draw_chain.append(
            f"drawtext=textfile='{_ff_path(tf)}':fontfile='{_ff_path(font)}':"
            f"fontsize={tsize}:fontcolor={color}:x={tx}:y={ty}"
        )

    stxt = str(cfg.get("text_scroll") or "").strip()
    if stxt:
        tf = _write_textfile(stxt)
        font = _resolve_font(str(cfg.get("text_scroll_font") or "arial"))
        ssize = _clamp_int(cfg.get("text_scroll_size", 40), 8, 400, 40)
        speed = _clamp_int(cfg.get("text_scroll_speed", 10), 1, 1000, 10)
        color = color_value(cfg.get("text_scroll_color"), "yellow")
        y_height = _clamp_int(cfg.get("text_scroll_height", 60), 30, max(30, int(frame_h)), 60)
        y_pos = max(0, int(frame_h) - y_height)
        draw_chain.append(
            f"drawtext=textfile='{_ff_path(tf)}':fontfile='{_ff_path(font)}':"
            f"fontsize={ssize}:fontcolor={color}:x='w-mod(t*{speed}\\,w+tw)':y={y_pos}"
        )

    return overlay_segments, draw_chain, label, input_count


def _resolve_intro_outro(cfg) -> Tuple[Optional[Path], Optional[Path]]:
    """
    Chọn Intro / Outro từ kho tài nguyên (_Intro / _Outro), hỗ trợ chọn ngẫu nhiên
    (intro_random / outro_random) — kế thừa preset Render của DHB.
    """
    intro = _pick_media(
        DHB_RENDER_DIR / "_Intro", cfg.get("intro_name") or "", bool(cfg.get("intro_random")),
        (".mp4", ".mov", ".webm"),
    ) if cfg.get("intro_enabled") else None
    outro = _pick_media(
        DHB_RENDER_DIR / "_Outro", cfg.get("outro_name") or "", bool(cfg.get("outro_random")),
        (".mp4", ".mov", ".webm"),
    ) if cfg.get("outro_enabled") else None

    if cfg.get("intro_enabled") and intro is None:
        raise ValueError("Đã bật Intro nhưng chưa chọn tài nguyên Intro.")
    if cfg.get("outro_enabled") and outro is None:
        raise ValueError("Đã bật Outro nhưng chưa chọn tài nguyên Outro.")
    return intro, outro


def _inline_intro_outro(cmd: list, intro: Optional[Path], outro: Optional[Path], main_duration: float,
                        target_w: int, target_h: int, fps: int) -> Tuple[list, float]:
    """
    Ghép Intro / Outro ngay trong lệnh render chính để video chỉ encode một lần.
    `cmd` là lệnh render chính: map video [outv], audio qua [outa] hoặc stream + -af tùy chọn.
    """
    cmd = list(cmd)
    output_path = cmd.pop()
    if len(cmd) >= 2 and cmd[-2] == "-t":
        del cmd[-2:]
    cmd = [arg for arg in cmd if arg != "-shortest"]

    fc_index = cmd.index("-filter_complex") + 1
    maps = [i for i, arg in enumerate(cmd) if arg == "-map"]
    video_map, audio_map = cmd[maps[0] + 1], cmd[maps[1] + 1]
    if video_map != "[outv]":
        raise ValueError(f"Lệnh render không hỗ trợ ghép Intro/Outro (map video {video_map}).")
    audio_filter = "anull"
    if "-af" in cmd:
        af_index = cmd.index("-af")
        audio_filter = cmd[af_index + 1]
        del cmd[af_index:af_index + 2]
        maps = [i for i, arg in enumerate(cmd) if arg == "-map"]
        fc_index = cmd.index("-filter_complex") + 1
    sample_rate = cmd[cmd.index("-ar") + 1] if "-ar" in cmd else "44100"

    f = int(fps or 30)
    dur = float(main_duration)
    main_audio = audio_map if audio_map.startswith("[") else f"[{audio_map.rstrip('?')}]"
    audio_norm = f"aformat=sample_rates={sample_rate}:channel_layouts=stereo"
    parts = [
        cmd[fc_index],
        f"[outv]trim=duration={dur:.6f},setpts=PTS-STARTPTS,fps={f},setsar=1,format=yuv420p,settb=AVTB[io_main_v]",
        f"{main_audio}{audio_filter},{audio_norm},apad,atrim=duration={dur:.6f},asetpts=PTS-STARTPTS,asettb=AVTB[io_main_a]",
    ]
    first_extra = sum(1 for arg in cmd if arg == "-i")
    segments, new_inputs, expected = [], [], dur
    for name, media in (("intro", intro), ("outro", outro)):
        if not media:
            continue
        index = first_extra + len(new_inputs) // 2
        new_inputs.extend(["-i", str(media)])
        probe = probe_video_info(str(media))
        seg_dur = float(probe["seconds"])
        expected += seg_dur
        parts.append(
            f"[{index}:v]setpts=PTS-STARTPTS,scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,fps={f},setsar=1,format=yuv420p,settb=AVTB[io_{name}_v]"
        )
        if probe.get("has_audio"):
            parts.append(
                f"[{index}:a]{audio_norm},asetpts=PTS-STARTPTS,apad,atrim=duration={seg_dur:.6f},asettb=AVTB[io_{name}_a]"
            )
        else:
            parts.append(
                f"anullsrc=channel_layout=stereo:sample_rate={sample_rate},atrim=duration={seg_dur:.6f},asettb=AVTB[io_{name}_a]"
            )
        segments.append(name)
    order = (["intro"] if "intro" in segments else []) + ["main"] + (["outro"] if "outro" in segments else [])
    # concat filter nhận input XEN KẼ theo từng segment: [v0][a0][v1][a1]...
    parts.append("".join(f"[io_{n}_v][io_{n}_a]" for n in order) + f"concat=n={len(order)}:v=1:a=1[io_v][io_a]")

    cmd[fc_index] = ";".join(parts)
    cmd[maps[0] + 1], cmd[maps[1] + 1] = "[io_v]", "[io_a]"
    input_end = fc_index - 1
    cmd[input_end:input_end] = new_inputs
    return cmd + ["-t", f"{expected:.6f}", output_path], expected


def time_str_to_seconds(t_str: str) -> float:
    if not t_str:
        return 0.0
    parts = t_str.strip().split(":")
    try:
        if len(parts) == 3:
            result = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            result = int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 1:
            result = float(parts[0])
        else:
            raise ValueError
    except Exception:
        raise ValueError(f"Mốc thời gian cắt không hợp lệ: {t_str!r}.")
    if not math.isfinite(result):
        raise ValueError(f"Mốc thời gian cắt không hợp lệ: {t_str!r}.")
    return result


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


def normalize_render_speed(value: Any) -> float:
    """Một validator dùng chung cho API/preset và worker render."""
    try:
        speed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Tốc độ render phải là số từ 0.5 đến 2.0.") from exc
    if not math.isfinite(speed) or speed < 0.5 or speed > 2.0:
        raise ValueError("Tốc độ render phải nằm trong khoảng 0.5–2.0 để giữ đồng bộ âm thanh và video.")
    return speed


def _finalize_render_output(
    output_path: str,
    *,
    expected_width: Optional[int] = None,
    expected_height: Optional[int] = None,
    expected_duration: Optional[float] = None,
    require_audio: bool = False,
    owner_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Probe thành phẩm và chỉ trả thành công khi đạt hợp đồng của chế độ render."""
    try:
        if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            raise RuntimeError("FFmpeg không tạo được file đầu ra hợp lệ hoặc file rỗng.")
        probe = probe_video_info(output_path)
        if expected_width and expected_height and (
            int(probe["width"]) != int(expected_width) or int(probe["height"]) != int(expected_height)
        ):
            raise RuntimeError(
                f"Kích thước render sai: nhận {probe['width']}x{probe['height']}, "
                f"cần {expected_width}x{expected_height}."
            )
        if expected_duration is not None:
            tolerance = max(0.15, min(1.5, float(expected_duration) * 0.04))
            if abs(float(probe["seconds"]) - float(expected_duration)) > tolerance:
                raise RuntimeError(
                    f"Thời lượng render sai: nhận {probe['seconds']:.2f}s, "
                    f"dự kiến {float(expected_duration):.2f}s (sai số cho phép {tolerance:.2f}s)."
                )
        if require_audio and not probe.get("has_audio"):
            raise RuntimeError("Output render thiếu audio stream theo cấu hình.")
        try:
            from database import register_managed_artifact
            register_managed_artifact(output_path, "render_output", "queue", owner_id)
        except Exception as exc:
            _log_render_warning(
                "Storage",
                f"Không ghi được manifest cho output render; file được giữ lại ({type(exc).__name__}).",
            )
        return probe
    except BaseException:
        _cleanup_partial_output(output_path)
        raise


def _log_render_warning(module: str, message: str) -> None:
    """Logging là best-effort: lỗi DB/log không được biến output hợp lệ thành render fail."""
    try:
        from database import add_log
        add_log(module, message, "WARNING")
    except Exception:
        pass


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
    safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(stem or "video")).strip(" .")[:160] or "video"
    return f"{prefix}_{time.time_ns()}_{uuid.uuid4().hex[:6]}_{safe_stem}{suffix}"


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
    owner_id: Optional[int] = None,
) -> str:
    """
    Thực thi Render & Lách bản quyền video bằng FFmpeg kế thừa tài nguyên từ C:\\DHB Reup Facebook\\Data\\Render.
    Trả về đường dẫn video thành phẩm trong thư mục output/.
    """
    if not input_path or not os.path.exists(input_path):
        raise FileNotFoundError(f"Không tìm thấy file video đầu vào: {input_path}")

    if stop_event and stop_event.is_set():
        raise InterruptedError("Tiến trình render đã bị dừng trước khi bắt đầu.")

    cfg = dict(render_config or {})
    probe = probe_video_info(input_path)
    has_audio = bool(probe.get("has_audio"))
    total_sec = float(probe["seconds"])
    start_sec = time_str_to_seconds(cut_start or cfg.get("cut_start", "00:00:00"))
    end_cut_sec = time_str_to_seconds(cut_end or cfg.get("cut_end", "00:00:00"))
    if not math.isfinite(start_sec) or not math.isfinite(end_cut_sec) or start_sec < 0 or end_cut_sec < 0:
        raise ValueError("Mốc cắt video phải là thời gian không âm và hợp lệ.")
    effective_dur = total_sec - start_sec - end_cut_sec
    if start_sec >= total_sec or effective_dur <= 0:
        raise ValueError(
            f"Khoảng cắt không còn video: nguồn {total_sec:.2f}s, đầu {start_sec:.2f}s, cuối {end_cut_sec:.2f}s."
        )
    speed = normalize_render_speed(cfg.get("speed", 1.0) if cfg.get("speed") not in (None, "") else 1.0)
    in_file = Path(input_path)
    out_filename = _make_output_filename("yt", in_file.stem, ".mp4")
    output_path = str(APP_OUTPUT_DIR / out_filename)

    if render_mode == "none":
        try:
            shutil.copy2(input_path, output_path)
            _finalize_render_output(
                output_path,
                expected_width=probe["width"], expected_height=probe["height"],
                expected_duration=total_sec, require_audio=has_audio, owner_id=owner_id,
            )
            if progress_callback:
                progress_callback(100, "Giữ nguyên video gốc")
        except BaseException:
            _cleanup_partial_output(output_path)
            raise
        return output_path

    # Video câm (không có track âm thanh): chèn nguồn im lặng anullsrc để FFmpeg không lỗi "Stream specifier ':a' matches no streams"
    silent_audio_input = [] if has_audio else [
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
    ]
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
        if not math.isfinite(max_shorts_dur) or max_shorts_dur < 1.0 or max_shorts_dur > 600.0:
            raise ValueError("Thời lượng giới hạn Shorts phải nằm trong khoảng 1–600 giây.")
        cur_dur = effective_dur / speed
        if cur_dur > max_shorts_dur:
            fit_mode = cfg.get("shorts_fit_mode", "speedup")
            desired_speed = effective_dur / max_shorts_dur
            if fit_mode == "speedup" and desired_speed <= 2.0:
                speed = normalize_render_speed(math.ceil(desired_speed * 100) / 100)
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
        orig_w = max(2, int(probe["width"]) - int(probe["width"]) % 2)
        orig_h = max(2, int(probe["height"]) - int(probe["height"]) % 2)
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
    intro_media, outro_media = (None, None)
    if render_mode not in ("md5_cut", "custom_ffmpeg"):
        intro_media, outro_media = _resolve_intro_outro(cfg)

    def run_main_render(cmd: list, frame_w: int, frame_h: int) -> float:
        main_duration = effective_dur / speed
        cmd[-1:-1] = ["-t", f"{main_duration:.6f}"]
        expected = main_duration
        if intro_media or outro_media:
            cmd, expected = _inline_intro_outro(cmd, intro_media, outro_media, main_duration, frame_w, frame_h, fps)
        _run_ffmpeg_with_progress(cmd, expected, progress_callback, stop_event=stop_event, partial_output=output_path, owner=owner)
        return expected

    # Các chế độ copy stream/custom command không có filter graph để áp crop/xoay/viền/zoom
    pre_edit_chain = ""
    if render_mode not in ("md5_cut", "custom_ffmpeg"):
        pre_edit_chain = _build_pre_edit_chain(
            cfg,
            _clamp_int(probe.get("width"), 0, 100000, 0),
            _clamp_int(probe.get("height"), 0, 100000, 0),
            fps,
        )
    timeline = f"setpts=(PTS-STARTPTS)/{speed:.2f}"
    src_prefix = f"[0:v]{pre_edit_chain},{timeline}," if pre_edit_chain else f"[0:v]{timeline},"

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
        _finalize_render_output(
            output_path, expected_width=probe["width"], expected_height=probe["height"],
            expected_duration=effective_dur, require_audio=has_audio, owner_id=owner_id,
        )
        return output_path

    # Chế độ 7: Custom FFmpeg Command
    if render_mode == "custom_ffmpeg" and cfg.get("custom_command"):
        cmd = _build_custom_ffmpeg_args(str(cfg["custom_command"]), input_path, output_path)
        _run_ffmpeg_with_progress(cmd, effective_dur, progress_callback, stop_event=stop_event, partial_output=output_path, owner=owner)
        _finalize_render_output(output_path, owner_id=owner_id)
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
    sync_audio_filter = f"{a_filter},apad,atrim=duration={effective_dur / speed:.6f}"

    # Chế độ 2: Render Dọc 9:16 Chuẩn YouTube Shorts có nền Blur mờ (Hỗ trợ 1080p đến 4K)
    if render_mode in ("shorts_9_16", "square_1_1"):
        if render_mode == "square_1_1":
            w_shorts, h_shorts = 1080, 1080
        blur_rad = 40 if res_mode == "4k" else (30 if res_mode == "2k" else 20)
        base_fc = (
            f"{src_prefix}scale={w_shorts}:{h_shorts}:force_original_aspect_ratio=increase,crop={w_shorts}:{h_shorts},boxblur={blur_rad}:10[bg];"
            f"{src_prefix}scale={w_shorts}:{h_shorts}:force_original_aspect_ratio=decrease{v_mod_chain}[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[vpre]"
        )

        bg_music_name = str(cfg.get("bg_music_name") or "").strip()
        bg_music_path = _pick_media(
            DHB_RENDER_DIR / "_Backgroud Music", bg_music_name, False,
            (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"),
        ) if bg_music_name else None

        extra_inputs: list = []
        if bg_music_path:
            brand_segments, draw_chain, brand_label, _input_count = _build_branding(cfg, "vpre", 2, extra_inputs, h_shorts)
        else:
            brand_segments, draw_chain, brand_label, _input_count = _build_branding(
                cfg, "vpre", 1 + (0 if has_audio else 1), extra_inputs, h_shorts
            )
        tail_filters = ",".join(draw_chain + ["format=yuv420p"])
        filter_complex = base_fc + (";" + ";".join(brand_segments) if brand_segments else "") + ";" + f"[{brand_label}]{tail_filters}[outv]"

        if bg_music_path:
            bg_pct = cfg.get("bg_music_percent")
            music_vol = (float(bg_pct) / 100.0) if bg_pct is not None else float(cfg.get("bg_music_volume", 0.25) or 0.25)
            if has_audio:
                audio_fc = f";[0:a]{a_filter}[a0];[1:a]volume={music_vol:.2f}[a1];[a0][a1]amix=inputs=2:duration=longest:dropout_transition=2[outa]"
            else:
                # Video câm: chỉ còn nhạc nền làm âm thanh
                audio_fc = f";[1:a]volume={music_vol:.2f}[outa]"
            cmd = (
                [FFMPEG_EXE, "-y"]
                + trim_args
                + ["-i", input_path, "-stream_loop", "-1", "-i", str(bg_music_path)]
                + extra_inputs
                + ["-filter_complex", filter_complex + audio_fc, "-map", "[outv]", "-map", "[outa]"]
                # Nhạc lặp là vô hạn; -shortest bám theo video. amix longest giữ nhạc khi audio gốc ngắn.
                + ["-shortest"]
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
                + ["-af", sync_audio_filter, "-shortest", "-c:v", "libx264", "-r", str(fps), "-crf", crf_val, "-b:v", bitrate, "-preset", preset]
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
        expected_output_duration = run_main_render(cmd, w_shorts, h_shorts)
        out_w, out_h = w_shorts, h_shorts
        rendered = True

    # Chế độ 3: Lách Thu Nhỏ (PiP trên Video Nền từ C:\DHB Reup Facebook\Data\Render\_Backgroud Video)
    if render_mode == "pip_thu_nho":
        bg_name = str(cfg.get("bg_video_name") or "Bubble HD.mp4").strip()
        bg_path = _pick_media(
            DHB_RENDER_DIR / "_Backgroud Video", bg_name, bool(cfg.get("bg_video_random")),
            (".mp4", ".mov", ".webm"),
        )
        if bg_path:
            base_fc = (
                f"[1:v]scale=1280:720,boxblur=4:1[bg];"
                f"{src_prefix}scale=960:540:force_original_aspect_ratio=decrease,pad=iw+8:ih+8:4:4:color=white{v_mod_chain}[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1[vpre]"
            )
            extra_inputs = []
            brand_segments, draw_chain, brand_label, _input_count = _build_branding(
                cfg, "vpre", 2 + (0 if has_audio else 1), extra_inputs, 720
            )
            tail_filters = ",".join(draw_chain + ["format=yuv420p"])
            filter_complex = base_fc + (";" + ";".join(brand_segments) if brand_segments else "") + ";" + f"[{brand_label}]{tail_filters}[outv]"

            if has_audio:
                audio_map_args = ["-map", "[outv]", "-map", "0:a?", "-shortest"]
                audio_fx_args = ["-af", sync_audio_filter]
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
            expected_output_duration = run_main_render(cmd, 1280, 720)
            out_w, out_h = 1280, 720
            rendered = True

    # Mặc định / Chế độ 5 & 6: Lách Full Màn 16:9 (Hỗ trợ Full HD 1080p đến 4K 3840x2160)
    # Guard: chỉ chạy khi chưa có chế độ nào phía trên render (shorts/pip/square) — tránh render đè
    if not rendered:
        target_res = cfg.get("size")
        parsed_target = _parse_pair(target_res, (w_16_9, h_16_9)) if target_res else (w_16_9, h_16_9)
        w_res = max(2, min(7680, parsed_target[0] - parsed_target[0] % 2))
        h_res = max(2, min(4320, parsed_target[1] - parsed_target[1] % 2))

        vf_core = (
            f"{src_prefix}scale={w_res}:{h_res}:force_original_aspect_ratio=decrease,"
            f"pad={w_res}:{h_res}:(ow-iw)/2:(oh-ih)/2:black"
            f"{v_mod_chain}"
        )
        bg_music_name = str(cfg.get("bg_music_name") or "").strip()
        bg_music_path = _pick_media(
            DHB_RENDER_DIR / "_Backgroud Music", bg_music_name, False,
            (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"),
        ) if bg_music_name else None

        extra_inputs = []
        if bg_music_path:
            brand_segments, draw_chain, brand_label, _input_count = _build_branding(cfg, "vpre", 2, extra_inputs, h_res)
        else:
            brand_segments, draw_chain, brand_label, _input_count = _build_branding(
                cfg, "vpre", 1 + (0 if has_audio else 1), extra_inputs, h_res
            )
        tail_filters = ",".join(draw_chain + ["format=yuv420p"])
        video_fc = f"{vf_core}[vpre]" + (";" + ";".join(brand_segments) if brand_segments else "") + ";" + f"[{brand_label}]{tail_filters}[outv]"

        # Nếu có chọn nhạc nền MP3 từ C:\DHB Reup Facebook\Data\Render\_Backgroud Music
        if bg_music_path:
            bg_pct = cfg.get("bg_music_percent")
            music_vol = (float(bg_pct) / 100.0) if bg_pct is not None else float(cfg.get("bg_music_volume", 0.3) or 0.3)
            if has_audio:
                audio_fc = f"[0:a]{a_filter}[a0];[1:a]volume={music_vol:.2f}[a1];[a0][a1]amix=inputs=2:duration=longest:dropout_transition=2[outa]"
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
                # Nhạc lặp là vô hạn; -shortest bám theo video, kể cả audio gốc ngắn hơn.
                + ["-shortest"]
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
                + ["-af", sync_audio_filter, "-shortest", "-c:v", "libx264", "-r", str(fps), "-b:v", bitrate, "-preset", preset]
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

        expected_output_duration = run_main_render(cmd, w_res, h_res)
        out_w, out_h = w_res, h_res
        rendered = True

    _finalize_render_output(
        output_path,
        expected_width=int(out_w), expected_height=int(out_h),
        expected_duration=expected_output_duration,
        require_audio=True,
        owner_id=owner_id,
    )
    return output_path


def _cleanup_partial_output(partial_output: Optional[str]):
    """Xóa file video xuất dở dang khi render thất bại hoặc bị người dùng bấm Dừng."""
    if not partial_output:
        return True
    # Windows can briefly retain a sharing lock after the encoder has exited.
    # Retry only the owned file; never mask the original render/stop exception.
    for attempt in range(5):
        try:
            os.remove(partial_output)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if attempt < 4:
                time.sleep(.05 * (2 ** attempt))
                continue
        except OSError:
            pass
        _log_render_warning('Render', f'Không xóa được file render dở dang; file vẫn được giữ để kiểm tra: {partial_output}')
        return False


def _run_ffmpeg_with_progress(
    cmd: list,
    expected_duration: float,
    progress_callback: Optional[Callable[[int, str], None]],
    stop_event: Optional[threading.Event] = None,
    partial_output: Optional[str] = None,
    owner: str = "",
    silence_timeout: Optional[float] = None,
    total_timeout: Optional[float] = None,
):
    if progress_callback:
        progress_callback(5, "Khởi chạy FFmpeg render...")
    if stop_event and stop_event.is_set():
        raise InterruptedError("Tiến trình render đã bị dừng trước khi bắt đầu.")

    proc = subprocess.Popen(
        cmd,
        # FFmpeg chỉ ghi progress ra stderr; stdout để DEVNULL tránh pipe đầy làm treo render.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="ignore",
        shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        start_new_session=(os.name != "nt"),
    )

    with _ACTIVE_FFMPEG_LOCK:
        _ACTIVE_FFMPEG_PROCS[proc.pid] = (proc, owner)

    # Watchdog chống treo: readline() ở luồng chính là BLOCKING, nên nếu FFmpeg im lặng
    # (treo thật sự, không ghi gì ra stderr) thì stop_event không bao giờ được kiểm tra và
    # worker render kẹt vĩnh viễn. Luồng phụ này kill tiến trình khi (a) người dùng bấm
    # Dừng hoặc (b) không có output trong silence_timeout giây — lúc đó readline nhận EOF
    # và luồng chính thoát ra xử lý lỗi bình thường.
    timeout_sec = FFMPEG_SILENCE_TIMEOUT_SEC if silence_timeout is None else max(1.0, float(silence_timeout))
    total_timeout_sec = FFMPEG_MAX_RUNTIME_SEC if total_timeout is None else max(1.0, float(total_timeout))
    started_at = time.monotonic()
    watchdog_state = {"last_output": started_at, "reason": ""}

    def _watchdog():
        while proc.poll() is None:
            if stop_event is not None and stop_event.is_set():
                _terminate_ffmpeg_proc(proc)
                return
            now = time.monotonic()
            if now - started_at > total_timeout_sec:
                watchdog_state["reason"] = "total_timeout"
                _terminate_ffmpeg_proc(proc)
                return
            if now - watchdog_state["last_output"] > timeout_sec:
                watchdog_state["reason"] = "silence_timeout"
                _terminate_ffmpeg_proc(proc)
                return
            time.sleep(1.0)

    watchdog_t = threading.Thread(target=_watchdog, daemon=True, name="DHB-FFmpeg-Watchdog")

    stderr_lines = deque(maxlen=80)
    try:
        watchdog_t.start()
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

                watchdog_state["last_output"] = time.monotonic()
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
        if watchdog_state["reason"] == "total_timeout":
            raise TimeoutError(
                f"FFmpeg vượt quá thời hạn tổng {int(total_timeout_sec)} giây; đã dừng cây tiến trình render."
            )
        if watchdog_state["reason"] == "silence_timeout":
            raise RuntimeError(
                f"FFmpeg không ghi ra gì trong {int(timeout_sec)}s — tiến trình có thể bị treo, "
                "đã tự hủy để giải phóng slot render. Hãy thử lại; nếu lặp lại, kiểm tra file nguồn/ổ đĩa."
            )
        if proc.returncode != 0:
            err_tail = "".join(list(stderr_lines)[-10:])
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


def generate_thumbnail_from_video(
    video_path: str, output_path: Optional[str] = None, owner_id: Optional[int] = None
) -> Optional[str]:
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
        total_sec = float(probe["seconds"])
        capture_sec = max(0.01, min(total_sec * 0.25, 3.5, max(0.01, total_sec - 0.02)))
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
            timeout=60,
            shell=False,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if res.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
            try:
                from database import register_managed_artifact
                register_managed_artifact(output_path, "render_thumbnail", "queue", owner_id)
            except Exception as exc:
                _log_render_warning(
                    "Storage",
                    f"Không ghi được manifest thumbnail; ảnh được giữ lại ({type(exc).__name__}).",
                )
            return output_path
    except Exception as e:
        _log_render_warning("Render", f"Lỗi tạo thumbnail cho '{os.path.basename(video_path)}': {e}")
    if output_path and os.path.exists(output_path):
        _cleanup_partial_output(output_path)
    return None
