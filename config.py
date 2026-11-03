import os
import json
from pathlib import Path

# Đường dẫn gốc tới bộ công cụ DHB Reup Facebook (có thể ghi đè cho cài mới).
DHB_ROOT = Path(os.environ.get("DHB_ROOT", r"C:\DHB Reup Facebook")).expanduser()
DHB_DATA_DIR = DHB_ROOT / "Data"
DHB_CONFIG_DIR = DHB_DATA_DIR / "Config"
DHB_RENDER_DIR = DHB_DATA_DIR / "Render"
DHB_PROJECT_DIR = DHB_DATA_DIR / "Project"
DHB_PROFILE_CHROME_DIR = (DHB_DATA_DIR / "Profile Chrome") if (DHB_DATA_DIR / "Profile Chrome").exists() else (DHB_DATA_DIR / "Profile_Chrome")
DHB_LIB_DIR = DHB_ROOT / "lib"

# Các executable kế thừa từ DHB có thể được chỉ định riêng khi cài mới.
def _tool_path(env_name: str, bundled_path: Path, fallback: str) -> str:
    configured = os.environ.get(env_name, "").strip()
    if configured:
        return str(Path(configured).expanduser())
    return str(bundled_path) if bundled_path.exists() else fallback


FFMPEG_EXE = _tool_path("DHB_FFMPEG", DHB_ROOT / "ffmpeg.exe", "ffmpeg")
FFPROBE_EXE = _tool_path("DHB_FFPROBE", DHB_ROOT / "ffprobe.exe", "ffprobe")
FFPLAY_EXE = _tool_path("DHB_FFPLAY", DHB_ROOT / "ffplay.exe", "ffplay")
YTDL_EXE = _tool_path("DHB_YTDL", DHB_ROOT / "youtube-dl.exe", "yt-dlp")
BROWSER_HOST_EXE = _tool_path("DHB_BROWSER_HOST", DHB_ROOT / "DHBBrowserHost.exe", "DHBBrowserHost.exe")
MAGICK_EXE = _tool_path("DHB_MAGICK", DHB_LIB_DIR / "magick.exe", "magick")
STHP_EXE = _tool_path("DHB_STHP", DHB_LIB_DIR / "sthp.exe", "sthp.exe")

# Các cơ sở dữ liệu sẵn có của DHB Reup
DHB_MAIN_SQLITE = DHB_DATA_DIR / "data.sqlite"
DHB_OPTION_SQLITE = DHB_DATA_DIR / "DataOptionUpload.sqlite"
DHB_SETTING_JSON = DHB_CONFIG_DIR / "setting.json"
DHB_FILTER_JSON = DHB_CONFIG_DIR / "filter.json"

# Thư mục dự án hiện tại (DHB Reup YouTube Studio)
APP_ROOT = Path(__file__).parent.resolve()
APP_DATA_DIR = APP_ROOT / "data"
APP_TEMP_DIR = APP_ROOT / "temp"
APP_OUTPUT_DIR = APP_ROOT / "output"
APP_PROFILES_DIR = APP_DATA_DIR / "profiles_youtube"
APP_DB_PATH = APP_DATA_DIR / "dhb_youtube.sqlite"

for _d in [APP_DATA_DIR, APP_TEMP_DIR, APP_OUTPUT_DIR, APP_PROFILES_DIR]:
    _d.mkdir(parents=True, exist_ok=True)


def load_dhb_settings() -> dict:
    """Đọc cấu hình gốc từ C:\\DHB Reup Facebook\\Data\\Config\\setting.json."""
    if DHB_SETTING_JSON.exists():
        try:
            with open(DHB_SETTING_JSON, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def list_dhb_render_resources() -> dict:
    """Liệt kê toàn bộ kho tài nguyên Render sẵn có tại C:\\DHB Reup Facebook\\Data\\Render."""
    resources = {
        "background_music": [],
        "background_video": [],
        "filters": [],
        "fonts": [],
        "images": [],
        "intros": [],
        "outros": [],
        "thumbnails": [],
        "logo_videos": [],
        "ffmpeg_presets": [],
    }
    mapping = {
        "background_music": DHB_RENDER_DIR / "_Backgroud Music",
        "background_video": DHB_RENDER_DIR / "_Backgroud Video",
        "filters": DHB_RENDER_DIR / "_Filter",
        "fonts": DHB_RENDER_DIR / "_Font",
        "images": DHB_RENDER_DIR / "_Image",
        "intros": DHB_RENDER_DIR / "_Intro",
        "outros": DHB_RENDER_DIR / "_Outro",
        "thumbnails": DHB_RENDER_DIR / "_Thumbnail",
        "logo_videos": DHB_RENDER_DIR / "_Video",
    }
    for key, folder in mapping.items():
        if folder.exists():
            resources[key] = [
                {"name": p.name, "path": str(p)}
                for p in sorted(folder.iterdir())
                if p.is_file()
            ]

    ffmpeg_code_dir = DHB_CONFIG_DIR / "Code FFmpeg"
    if ffmpeg_code_dir.exists():
        for p in sorted(ffmpeg_code_dir.glob("*.txt")):
            try:
                cmd_text = p.read_text(encoding="utf-8", errors="ignore").strip()
                resources["ffmpeg_presets"].append({
                    "name": p.stem,
                    "path": str(p),
                    "command": cmd_text,
                })
            except Exception:
                pass

    return resources


def load_dhb_render_configs() -> dict:
    """Đọc các preset cấu hình Render mặc định từ C:\\DHB Reup Facebook\\Data\\Config."""
    preset_folders = [
        "Lach Thu Nho",
        "Lach Full Man",
        "Render Vuong",
        "RenderDoc",
        "Convert 169",
        "Video Mp3",
        "Lach Am Thanh",
        "Cut and Change MD5",
    ]
    result = {}
    for folder_name in preset_folders:
        folder = DHB_CONFIG_DIR / folder_name
        if folder.exists():
            result[folder_name] = {}
            for jf in folder.glob("*.json"):
                try:
                    result[folder_name][jf.stem] = json.loads(
                        jf.read_text(encoding="utf-8", errors="ignore")
                    )
                except Exception:
                    pass
    return result
