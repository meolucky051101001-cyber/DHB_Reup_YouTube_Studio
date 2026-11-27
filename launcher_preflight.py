"""Report the local Windows launch requirements without opening a browser."""

import json
import subprocess
import sys
from pathlib import Path

from config import APP_ROOT, FFMPEG_EXE, FFPROBE_EXE
from runtime_identity import capture_runtime


def check_tools():
    warnings = []
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        warnings.append("Playwright chua duoc cai dat.")
    else:
        try:
            with sync_playwright() as browser:
                if not Path(browser.chromium.executable_path).is_file():
                    warnings.append("Chromium chua duoc cai dat cho Playwright.")
                else:
                    browser.chromium.launch(headless=True, timeout=10000).close()
        except (Error, RuntimeError, OSError):
            warnings.append("Khong kiem tra duoc Playwright Chromium.")
    for label, executable in (("FFmpeg", FFMPEG_EXE), ("FFprobe", FFPROBE_EXE)):
        try:
            subprocess.run([executable, "-version"], timeout=5, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            warnings.append(f"Khong chay duoc {label}.")
    return warnings


if __name__ == "__main__":
    if sys.version_info < (3, 10):
        sys.exit("Can Python 3.10 tro len.")
    print(json.dumps({**capture_runtime(APP_ROOT), "warnings": check_tools()}))
