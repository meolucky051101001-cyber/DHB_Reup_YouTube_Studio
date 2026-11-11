"""Temporary frames from the worker's Studio tab, keyed by the upload attempt."""

import threading
import time


_lock = threading.Lock()
_views = {}
_watch_seconds = 6
_capture_interval = 1.5
_max_frame_bytes = 1024 * 1024


class StudioView:
    def __init__(self, queue_id, attempt_id):
        self.key = (queue_id, attempt_id)
        self.watch_until = 0.0
        self.next_capture = 0.0
        self.sequence = 0
        self.image = None

    def __enter__(self):
        if all(self.key):
            with _lock:
                _views[self.key] = self
        return self

    def __exit__(self, exc_type, exc, traceback):
        with _lock:
            if _views.get(self.key) is self:
                del _views[self.key]
            self.image = None
            self.watch_until = 0.0

    def capture(self, page):
        """Called only by the thread that owns the Playwright page."""
        now = time.monotonic()
        with _lock:
            if now >= self.watch_until:
                self.image = None
                return
            if now < self.next_capture:
                return
            self.next_capture = now + _capture_interval

        try:
            if not page.url.startswith("https://studio.youtube.com/"):
                return
            image = page.screenshot(type="jpeg", quality=50, scale="css", timeout=750)
        except Exception:
            # A screenshot timeout or a closed tab must not fail the upload.
            return
        if not image or len(image) > _max_frame_bytes:
            return
        with _lock:
            if _views.get(self.key) is self and time.monotonic() < self.watch_until:
                self.sequence += 1
                self.image = image


def active_studio_views():
    with _lock:
        return set(_views)


def request_studio_frame(queue_id, attempt_id, after):
    """Return the current frame and renew interest for a short period."""
    with _lock:
        view = _views.get((queue_id, attempt_id))
        if view is None:
            return None
        now = time.monotonic()
        if now >= view.watch_until:
            view.image = None
        view.watch_until = now + _watch_seconds
        if view.image is None or view.sequence <= after:
            return (0, None)
        return (view.sequence, view.image)
