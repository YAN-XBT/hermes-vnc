"""On-demand frame source for the desktop live view.

The `computer_use` contract gives the *agent* still frames: look, decide, act. A person
watching the same remote desktop wants the opposite — a continuous picture. This module
serves that second audience, and it is deliberately separate from the tool path so nothing
here can slow an agent turn down.

How it gets frames: it opens **its own** RFB connection to the same target, through the same
session code the backend uses, and caches it. That means the VNC server must allow more than
one viewer at a time — x11vnc (`-shared`), TightVNC, UltraVNC and macOS Screen Sharing all do
by default, but a server configured for a single viewer will drop the other one. Frames are
downscaled and JPEG-encoded before they leave, because this path exists to be watched, not
read pixel-exactly.

Nothing here runs on its own: a frame is captured only when the desktop pane asks for one.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import threading
from typing import Optional, Tuple

from . import config as _config
from . import session as _session

logger = logging.getLogger(__name__)

DEFAULT_MAX_WIDTH = 960
DEFAULT_QUALITY = 62
# The live view is a viewer, not a tool call. A frame that takes longer than this is worse than
# no frame: the pane's request would outlive the Desktop app's own 30 s backend timeout, and the
# user sees a hang instead of an error. The tool path keeps the configured call_timeout.
LIVE_VIEW_CALL_TIMEOUT = 8.0
LIVE_VIEW_CONNECT_TIMEOUT = 6.0

_lock = threading.Lock()
_session_ref: Optional[_session.RfbSession] = None
_session_settings: Optional[_config.Settings] = None


def _drop_locked() -> None:
    """Close and forget the cached session. The caller must already hold ``_lock``."""
    global _session_ref, _session_settings
    client, _session_ref, _session_settings = _session_ref, None, None
    if client is not None:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            logger.debug("hermes-vnc: closing the live-view session failed", exc_info=True)


def _cap(settings: _config.Settings) -> _config.Settings:
    """The same target, with waits short enough that a wedged link fails instead of hanging."""
    return dataclasses.replace(
        settings,
        call_timeout=min(settings.call_timeout, LIVE_VIEW_CALL_TIMEOUT),
        connect_timeout=min(settings.connect_timeout, LIVE_VIEW_CONNECT_TIMEOUT),
    )


def _open(settings: _config.Settings) -> _session.RfbSession:
    global _session_ref, _session_settings
    if _session_ref is not None and _session_ref.connected and _session_settings == settings:
        return _session_ref
    if _session_ref is not None:
        try:
            _session_ref.close()
        except Exception:  # noqa: BLE001 - a dead session is not worth reporting
            logger.debug("hermes-vnc: closing the previous live-view session failed", exc_info=True)
    _session_ref = _session.open_session(settings)
    _session_settings = settings
    return _session_ref


def _downscale(raw: bytes, max_width: int, quality: int) -> Tuple[bytes, int, int]:
    """Re-encode a PNG frame as a smaller JPEG. Returns (jpeg, width, height)."""
    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        raise _session.VncError(f"the live view needs Pillow to re-encode frames: {exc}") from exc

    with Image.open(io.BytesIO(raw)) as image:
        image = image.convert("RGB")
        width, height = image.size
        if max_width and width > max_width:
            height = max(1, round(height * max_width / width))
            width = max_width
            image = image.resize((width, height), Image.BILINEAR)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=quality, optimize=False)
    return out.getvalue(), width, height


def grab_jpeg(settings: Optional[_config.Settings] = None,
              max_width: int = DEFAULT_MAX_WIDTH,
              quality: int = DEFAULT_QUALITY) -> Tuple[bytes, int, int]:
    """Capture one downscaled JPEG frame. Raises VncError with a readable reason."""
    resolved = _cap(settings if settings is not None else _config.load_settings())
    if not resolved.host:
        raise _session.VncError(
            "no VNC target configured: set computer_use.vnc.host (or HERMES_VNC_HOST)"
        )
    with _lock:                       # one capture at a time: the session is single-flight
        client = _open(resolved)
        raw = io.BytesIO()
        try:
            client.capture_screen(raw, fmt="PNG")
        except BaseException:
            # A capture that failed leaves the session suspect — a link that died without the
            # reactor noticing looks "connected" forever, and every later frame would hang on it.
            # Drop it here so the next frame opens a fresh connection instead of repeating this.
            _drop_locked()
            raise
        return _downscale(raw.getvalue(), max_width, quality)


def close() -> None:
    """Drop the cached session. Safe to call when nothing is open."""
    with _lock:
        _drop_locked()
