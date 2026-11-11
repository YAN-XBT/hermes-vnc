"""Live RFB test for hermes-vnc: the real client, against a scripted RFB server.

``test_hermes_vnc.py`` proves the backend asks the protocol for the right calls. This file
proves the other half — that ``vncdotool`` actually completes an RFB handshake with the
settings the backend builds, returns a decodable framebuffer, and puts real pointer and key
events on the wire.

It runs a minimal RFB 3.8 server (no authentication, Raw encoding only) on a loopback port,
points a real backend at it, and asserts both directions: what came back as a screenshot,
and what arrived at the server.

Needs ``vncdotool`` (and therefore Pillow) importable, which is the state of a machine with
the plugin installed::

    <HERMES_HOME>/hermes-agent/venv/Scripts/python.exe tests/test_live_rfb.py
"""

from __future__ import annotations

import base64
import importlib.util
import io
import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parents[1]


def _ensure_hermes_importable() -> None:
    try:
        import tools.computer_use.backend  # noqa: F401

        return
    except Exception:
        pass
    candidates = []
    if home := os.environ.get("HERMES_HOME"):
        candidates.append(Path(home) / "hermes-agent")
    candidates += [Path.home() / ".hermes" / "hermes-agent",
                   Path(os.environ.get("LOCALAPPDATA", ".")) / "hermes" / "hermes-agent"]
    for root in candidates:
        if (root / "tools" / "computer_use" / "backend.py").is_file():
            sys.path.insert(0, str(root))
            return


_ensure_hermes_importable()
sys.path.insert(0, str(_PKG_DIR.parent))
_spec = importlib.util.spec_from_file_location(
    "hermes_vnc", _PKG_DIR / "__init__.py", submodule_search_locations=[str(_PKG_DIR)]
)
pkg = importlib.util.module_from_spec(_spec)
sys.modules["hermes_vnc"] = pkg
_spec.loader.exec_module(pkg)

from hermes_vnc import backend as _backend  # noqa: E402
from hermes_vnc import config as _config  # noqa: E402

WIDTH, HEIGHT = 64, 48
_GREY = [(x + y) % 256 for y in range(HEIGHT) for x in range(WIDTH)]


class RfbServer(threading.Thread):
    """A minimal RFB 3.8 server: no auth, one Raw rectangle, events recorded."""

    def __init__(self, width: int = WIDTH, height: int = HEIGHT) -> None:
        super().__init__(daemon=True)
        self.width, self.height = width, height
        self.events: list[tuple] = []
        self.error: BaseException | None = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._done = threading.Event()

    # -- wire helpers ---------------------------------------------------------

    @staticmethod
    def _recv_exactly(conn: socket.socket, count: int) -> bytes:
        buf = b""
        while len(buf) < count:
            chunk = conn.recv(count - len(buf))
            if not chunk:
                raise ConnectionError("client closed the connection")
            buf += chunk
        return buf

    def _send_framebuffer_update(self, conn: socket.socket) -> None:
        header = struct.pack(">BBH", 0, 0, 1)                      # update, pad, 1 rectangle
        rect = struct.pack(">HHHHi", 0, 0, self.width, self.height, 0)  # x, y, w, h, Raw
        pixels = b"".join(struct.pack("<BBBB", value, value, value, 0) for value in _GREY)
        conn.sendall(header + rect + pixels)

    def _handle_client(self, conn: socket.socket) -> None:
        conn.sendall(b"RFB 003.008\n")
        version = self._recv_exactly(conn, 12)
        self.events.append(("version", version.decode("ascii", "replace").strip()))

        if version.startswith(b"RFB 003.003"):
            conn.sendall(struct.pack(">I", 1))                     # security type None
        else:
            conn.sendall(bytes([1, 1]))                            # one type: None
            chosen = self._recv_exactly(conn, 1)
            self.events.append(("security", chosen[0]))
            conn.sendall(struct.pack(">I", 0))                     # SecurityResult: OK

        self._recv_exactly(conn, 1)                                # ClientInit (shared flag)

        name = b"hermes-vnc test server"
        pixel_format = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)
        conn.sendall(struct.pack(">HH", self.width, self.height) + pixel_format
                     + struct.pack(">I", len(name)) + name)

        while not self._done.is_set():
            try:
                message = self._recv_exactly(conn, 1)[0]
            except (ConnectionError, OSError):
                return
            if message == 0:                                       # SetPixelFormat
                self._recv_exactly(conn, 3 + 16)
                self.events.append(("set_pixel_format",))
            elif message == 2:                                     # SetEncodings
                self._recv_exactly(conn, 1)                        # padding
                count = struct.unpack(">H", self._recv_exactly(conn, 2))[0]
                encodings = [struct.unpack(">i", self._recv_exactly(conn, 4))[0] for _ in range(count)]
                self.events.append(("set_encodings", encodings))
            elif message == 3:                                     # FramebufferUpdateRequest
                self._recv_exactly(conn, 9)
                self.events.append(("fb_update_request",))
                self._send_framebuffer_update(conn)
            elif message == 4:                                     # KeyEvent
                down, _, keysym = struct.unpack(">BHI", self._recv_exactly(conn, 7))
                self.events.append(("key", bool(down), keysym))
            elif message == 5:                                     # PointerEvent
                mask, x, y = struct.unpack(">BHH", self._recv_exactly(conn, 5))
                self.events.append(("pointer", mask, x, y))
            elif message == 6:                                     # ClientCutText
                self._recv_exactly(conn, 3)                        # padding
                length = struct.unpack(">I", self._recv_exactly(conn, 4))[0]
                text = self._recv_exactly(conn, length)
                self.events.append(("cut_text", text.decode("latin-1")))
            else:
                raise AssertionError(f"unexpected client message type {message}")

    def run(self) -> None:
        try:
            conn, _ = self._listener.accept()
            with conn:
                self._handle_client(conn)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            self.error = exc
        finally:
            self._listener.close()
            self._done.set()

    def stop(self) -> None:
        self._done.set()
        try:
            self._listener.close()
        except OSError:
            pass


def _backend_for(server: RfbServer) -> _backend.VncBackend:
    settings = _config.Settings(host="127.0.0.1", port=server.port, password="", password_env="",
                                connect_timeout=10.0, action_pause=0.0)
    return _backend.VncBackend(permission_mode="standard", settings=settings)


def _pointer_events(server: RfbServer) -> list[tuple]:
    return [event for event in server.events if event[0] == "pointer"]


def _key_events(server: RfbServer) -> list[tuple]:
    """``(down, keysym)`` pairs, without the event tag."""
    return [(event[1], event[2]) for event in server.events if event[0] == "key"]


def test_real_rfb_handshake_capture_and_input():
    server = RfbServer()
    server.start()
    instance = _backend_for(server)
    try:
        capture = instance.capture()
        assert capture.mode == "vision", capture
        assert (capture.width, capture.height) == (WIDTH, HEIGHT), capture

        from PIL import Image

        image = Image.open(io.BytesIO(base64.b64decode(capture.png_b64))).convert("RGB")
        assert image.size == (WIDTH, HEIGHT), image.size
        for point in ((0, 0), (10, 5), (63, 47)):
            assert image.getpixel(point)[0] == _GREY[point[1] * WIDTH + point[0]], (point, image.getpixel(point))

        results = {
            "click": instance.click(x=12, y=7),
            "scroll": instance.scroll(direction="down", amount=2),
            "type_text": instance.type_text("hi"),
            "key": instance.key("ctrl+s"),
            "drag": instance.drag(from_xy=(1, 1), to_xy=(30, 20)),
            "type_latin1": instance.type_text("café"),
        }
        for name, result in results.items():
            assert result.ok is True, (name, result)
        refused = instance.type_text("привіт")
        assert refused.ok is False and refused.code == "unsupported_text", refused
        time.sleep(0.4)  # let the scripted server read the last writes before teardown
    except BaseException as exc:
        raise AssertionError(f"{type(exc).__name__}: {exc}\nserver error: {server.error!r}\n"
                             f"server events: {server.events}") from exc
    finally:
        instance.stop()
        server.stop()
        server.join(timeout=10)

    assert server.error is None, server.error

    pointers = _pointer_events(server)
    assert pointers, server.events
    assert ("pointer", 0, 12, 7) in pointers, pointers                  # the click's move
    assert any(mask == 1 for _, mask, _, _ in pointers), pointers       # left button down
    assert any(mask & 16 for _, mask, _, _ in pointers), pointers       # wheel down (button 5)
    assert ("pointer", 0, 30, 20) in pointers, pointers                 # the drag's end point
    assert any(mask & 1 for _, mask, _, _ in pointers), pointers        # drag held the button

    keys = _key_events(server)
    assert (True, ord("h")) in keys and (False, ord("h")) in keys, keys
    assert (True, ord("i")) in keys, keys
    assert (True, 0xFFE3) in keys, keys                                 # ctrl held
    assert (True, ord("s")) in keys, keys
    assert ("cut_text", "café") in server.events, (server.events, {k: (v.ok, v.path, v.message) for k, v in results.items()})


def test_real_backend_reports_a_refused_connection_cleanly():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens here now

    instance = _backend.VncBackend(
        permission_mode="standard",
        settings=_config.Settings(host="127.0.0.1", port=port, connect_timeout=3.0, action_pause=0.0),
    )
    assert instance.is_available() is False
    result = instance.click(x=1, y=1)
    assert result.ok is False and result.code == "transport_error", result
    instance.stop()


def _main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
