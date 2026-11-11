"""VNC (RFB) computer-use backend: pixels out, pointer and keyboard in.

Everything protocol-specific lives in :mod:`session`; this module maps the computer-use
contract onto it.

What it can and cannot do is fixed by RFB, not by this code:

* **Screenshots** come back as a PNG of the remote framebuffer at the framebuffer's own
  pixel size, so a coordinate the model reads off the image is the coordinate it clicks.
  ``CaptureResult.mode`` is always ``"vision"``.
* **There is no accessibility tree.** RFB carries pixels, not roles or labels, so
  ``capture`` returns no ``elements`` and the element-addressed operations
  (``click(element=…)``, ``focus_app``, ``set_value``, ``list_apps``) answer
  ``unsupported_action`` rather than inventing an index.
* **Pointer and keyboard input** is delivered to whatever the remote machine has focused.
  There is no background delivery mode over VNC — the remote pointer really moves — so the
  requested ``delivery_mode`` is reported back rather than honoured.
* **Typing** is keysym-based, which covers printable ASCII, Tab and Return exactly. Text that
  is outside printable ASCII but inside Latin-1 goes to the server as clipboard text, and the
  result message says so, because a VNC server is free to treat a clipboard update as a
  clipboard update and not as keystrokes. Anything outside Latin-1 (Cyrillic, Greek, CJK) is
  refused with ``unsupported_text``: RFB's cut-text message is Latin-1, so there is no honest
  way to type it.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import socket
import threading
import time
from typing import Any, Optional

from tools.computer_use.backend import (
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
)

from . import config as _config
from . import session as _session
from .session import VncError

logger = logging.getLogger(__name__)

#: RFB pointer button numbers: 1/2/3 are left/middle/right, 4-7 are wheel directions.
_BUTTONS = {"left": 1, "middle": 2, "right": 3}
_SCROLL_BUTTONS = {"up": 4, "down": 5, "left": 6, "right": 7}

#: Keysym names Hermes' ``keys`` argument may use, mapped to vncdotool's names.
_KEY_ALIASES = {
    "control": "ctrl",
    "option": "alt",
    "cmd": "super",
    "command": "super",
    "meta": "super",
    "win": "super",
    "windows": "super",
    "escape": "esc",
    "pageup": "pgup",
    "pagedown": "pgdn",
    "backspace": "bsp",
    "delete": "del",
    "spacebar": "space",
    "return": "enter",
}

#: X11 keysyms for the two control characters typing has to send by name.
_RETURN_KEYSYM = 0xFF0D
_TAB_KEYSYM = 0xFF09
_TYPABLE = {chr(code) for code in range(0x20, 0x7F)} | {"\n", "\t"}

_DRAG_STEPS = 12


def png_size(raw: bytes) -> tuple[int, int]:
    """``(width, height)`` from a PNG's IHDR, without decoding the image."""
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        raise VncError("the VNC server did not return a PNG framebuffer")
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")


def normalize_combo(keys: str) -> str:
    """``ctrl+shift+s`` / ``cmd - s`` -> vncdotool's ``ctrl-shift-s`` form."""
    parts = [part for part in re.split(r"[+\-\s]+", (keys or "").strip().lower()) if part]
    return "-".join(_KEY_ALIASES.get(part, part) for part in parts)


def modifier_names(modifiers: Optional[list]) -> list[str]:
    """Held-key names for a click/drag/scroll, in vncdotool spelling."""
    out: list[str] = []
    for raw in modifiers or []:
        name = _KEY_ALIASES.get(str(raw).strip().lower(), str(raw).strip().lower())
        if name and name not in out:
            out.append(name)
    return out


def reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """True when *host*:*port* accepts a TCP connection. Named so a test can swap it."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _unsupported(action: str, message: str) -> ActionResult:
    return ActionResult(ok=False, action=action, code="unsupported_action", message=message)


def _failed(action: str, exc: BaseException) -> ActionResult:
    return ActionResult(ok=False, action=action, code="transport_error",
                        message=f"{type(exc).__name__}: {exc}")


class VncBackend(ComputerUseBackend):
    """One RFB session to one VNC target, driven through the computer_use tool."""

    def __init__(self, *, permission_mode: str, settings: Optional[_config.Settings] = None) -> None:
        self.permission_mode = permission_mode
        self.settings = settings if settings is not None else _config.load_settings()
        self._session: Optional[_session.RfbSession] = None
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        with self._lock:
            if self._session is not None and self._session.connected:
                return
            if not self.settings.host:
                raise VncError(
                    "no VNC target configured: set computer_use.vnc.host (or HERMES_VNC_HOST) "
                    "to the machine running the VNC server"
                )
            self._session = _session.open_session(self.settings)

    def stop(self) -> None:
        """Close this session's connection.

        The Twisted reactor is process-wide and cannot be restarted, so it is deliberately
        left running (see :mod:`session`): a Hermes process that opens and closes many
        sessions keeps using the one reactor instead of failing with
        ``ReactorNotRestartable``.
        """
        with self._lock:
            current, self._session = self._session, None
        if current is not None:
            try:
                current.close()
            except Exception:
                logger.debug("hermes-vnc: closing the session failed", exc_info=True)

    def is_available(self) -> bool:
        """Cheap gate for the tool's check_fn: a target is configured and its port answers."""
        if not self.settings.host:
            return False
        return reachable(self.settings.host, self.settings.port)

    # ---------------------------------------------------------------- capture

    def capture(self, mode: str = "som", app: Optional[str] = None, pid: Optional[int] = None,
                window_id: Optional[int] = None) -> CaptureResult:
        """Grab the whole remote framebuffer. Always the vision lane: RFB has no element tree."""
        client = self._require()
        buffer = io.BytesIO()
        with self._lock:
            client.capture_screen(buffer, fmt="PNG")
        raw = buffer.getvalue()
        if not raw:
            raise VncError("the VNC server returned an empty framebuffer")
        if len(raw) > self.settings.max_capture_bytes:
            raise VncError(
                f"the screenshot is {len(raw)} bytes, over the {self.settings.max_capture_bytes}-byte cap "
                "(raise computer_use.vnc.max_capture_bytes or lower the target's resolution)"
            )
        width, height = png_size(raw)
        return CaptureResult(
            mode="vision",
            width=width,
            height=height,
            png_b64=base64.b64encode(raw).decode("ascii"),
            app=app or "screen",
            window_title=f"VNC {self.settings.label}",
            png_bytes_len=len(raw),
            image_mime_type="image/png",
            note=("VNC desktop: this backend returns pixels only (no accessibility elements), so act by "
                  "x/y coordinates read off this screenshot. list_apps/focus_app/set_value are unavailable."),
        )

    # ---------------------------------------------------------------- pointer

    def click(self, *, element: Optional[int] = None, x: Optional[int] = None, y: Optional[int] = None,
              button: str = "left", click_count: int = 1, modifiers: Optional[list] = None,
              delivery_mode: Optional[str] = None, bring_to_front: bool = False) -> ActionResult:
        if element is not None:
            return _unsupported("click", "VNC has no accessibility elements; pass x and y from a capture.")
        if x is None or y is None:
            return ActionResult(ok=False, action="click", code="invalid_arguments",
                                message="click needs x and y; an element index cannot be resolved over VNC.")
        try:
            client = self._require()
            held = modifier_names(modifiers)
            number = _BUTTONS.get(button, 1)
            with self._lock:
                client.move(x, y)
                for name in held:
                    client.key_down(name)
                for _ in range(max(1, int(click_count))):
                    client.press(number)
                for name in reversed(held):
                    client.key_up(name)
        except BaseException as exc:
            return _failed("click", exc)
        self._settle()
        return ActionResult(ok=True, action="click", path="vnc_pixel", delivery_mode="foreground",
                            message=f"clicked {button} at {int(x)},{int(y)} on {self.settings.label}"
                                    f" (VNC delivers input to the remote machine's focus)")

    def drag(self, *, from_element: Optional[int] = None, to_element: Optional[int] = None,
             from_xy: Optional[tuple] = None, to_xy: Optional[tuple] = None, button: str = "left",
             modifiers: Optional[list] = None, delivery_mode: Optional[str] = None,
             bring_to_front: bool = False) -> ActionResult:
        if from_element is not None or to_element is not None:
            return _unsupported("drag", "VNC has no accessibility elements; pass from_xy and to_xy.")
        if not from_xy or not to_xy:
            return ActionResult(ok=False, action="drag", code="invalid_arguments",
                                message="drag needs from_xy and to_xy.")
        x0, y0 = int(from_xy[0]), int(from_xy[1])
        x1, y1 = int(to_xy[0]), int(to_xy[1])
        try:
            client = self._require()
            held = modifier_names(modifiers)
            number = _BUTTONS.get(button, 1)
            with self._lock:
                client.move(x0, y0)
                for name in held:
                    client.key_down(name)
                client.button_down(number)
                for step in range(1, _DRAG_STEPS + 1):
                    client.move(x0 + (x1 - x0) * step // _DRAG_STEPS,
                                y0 + (y1 - y0) * step // _DRAG_STEPS)
                client.button_up(number)
                for name in reversed(held):
                    client.key_up(name)
        except BaseException as exc:
            return _failed("drag", exc)
        self._settle()
        return ActionResult(ok=True, action="drag", path="vnc_pixel", delivery_mode="foreground",
                            message=f"dragged {button} from {x0},{y0} to {x1},{y1} on {self.settings.label}")

    def scroll(self, *, direction: str, amount: int = 3, element: Optional[int] = None,
               x: Optional[int] = None, y: Optional[int] = None, modifiers: Optional[list] = None,
               delivery_mode: Optional[str] = None, bring_to_front: bool = False) -> ActionResult:
        number = _SCROLL_BUTTONS.get(str(direction or "").strip().lower())
        if number is None:
            return ActionResult(ok=False, action="scroll", code="invalid_arguments",
                                message="scroll direction must be up, down, left or right.")
        ticks = max(1, min(int(amount or 1), 50))
        try:
            client = self._require()
            held = modifier_names(modifiers)
            with self._lock:
                if x is not None and y is not None:
                    client.move(x, y)
                for name in held:
                    client.key_down(name)
                for _ in range(ticks):
                    client.press(number)
                for name in reversed(held):
                    client.key_up(name)
        except BaseException as exc:
            return _failed("scroll", exc)
        self._settle()
        return ActionResult(ok=True, action="scroll", path="vnc_pixel", delivery_mode="foreground",
                            message=f"scrolled {direction} {ticks} tick(s) on {self.settings.label}")

    # ---------------------------------------------------------------- keyboard

    def type_text(self, text: str, *, delivery_mode: Optional[str] = None,
                  bring_to_front: bool = False) -> ActionResult:
        text = "" if text is None else str(text)
        if not text:
            return ActionResult(ok=False, action="type", code="invalid_arguments", message="nothing to type.")
        try:
            text.encode("latin-1")
            latin1 = True
        except UnicodeEncodeError:
            latin1 = False
        if not latin1 and not all(char in _TYPABLE for char in text):
            return ActionResult(
                ok=False, action="type", code="unsupported_text",
                message=("this text is outside the character set RFB can carry: keystrokes are keysyms "
                         "(printable ASCII) and the cut-text message is Latin-1, so scripts such as Cyrillic, "
                         "Greek or CJK cannot be typed through VNC. Put the text on the remote machine another "
                         "way, or have a person type it."),
            )
        try:
            client = self._require()
            if all(char in _TYPABLE for char in text):
                with self._lock:
                    for char in text:
                        keysym = _RETURN_KEYSYM if char == "\n" else _TAB_KEYSYM if char == "\t" else ord(char)
                        client.type_keysym(keysym)
                detail = f"typed {len(text)} character(s) as keystrokes"
            else:
                with self._lock:
                    client.cut_text(text)
                detail = ("text is outside printable ASCII, so it was sent as clipboard text; the remote "
                          "machine must paste it into the focused field")
        except BaseException as exc:
            return _failed("type", exc)
        self._settle()
        return ActionResult(ok=True, action="type", path="vnc_keys", delivery_mode="foreground",
                            message=f"{detail} on {self.settings.label}")

    def key(self, keys: str, *, delivery_mode: Optional[str] = None,
            bring_to_front: bool = False) -> ActionResult:
        combo = normalize_combo(keys)
        if not combo:
            return ActionResult(ok=False, action="key", code="invalid_arguments", message="no key given.")
        try:
            client = self._require()
            with self._lock:
                client.key_press(combo)
        except BaseException as exc:
            return _failed("key", exc)
        self._settle()
        return ActionResult(ok=True, action="key", path="vnc_keys", delivery_mode="foreground",
                            message=f"sent {combo} to {self.settings.label}")

    # ---------------------------------------------------------------- element lanes

    def list_apps(self) -> list[dict[str, Any]]:
        """RFB exposes no application list; the empty list is the honest answer."""
        return []

    def list_windows(self) -> list[dict[str, Any]]:
        return []

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        return _unsupported(
            "focus_app",
            "VNC has no window or application inventory: bring the window to the front on the remote "
            "machine itself (a click on it, or a keyboard shortcut) and act by coordinates.",
        )

    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        return _unsupported(
            "set_value",
            "VNC has no accessibility elements: click the field by coordinate and use type_text instead.",
        )

    # ---------------------------------------------------------------- helpers

    def _require(self) -> _session.RfbSession:
        with self._lock:
            if self._session is None or not self._session.connected:
                self.start()
            client = self._session
        if client is None:
            raise VncError("the VNC session is not open")
        return client

    def _settle(self) -> None:
        """Give the remote compositor a moment to react before the next capture."""
        pause = self.settings.action_pause
        if pause:
            time.sleep(pause)
