"""Tests for the desktop live view's frame source.

The live view is the one part of the plugin that exists for a *person* rather than for the
agent: it feeds the desktop pane. That makes it worth testing on its own terms — it opens a
second RFB connection, it re-encodes frames, and it caches a session across calls, and every
one of those is a place to hang or lie.

The last group drives the real client against a scripted RFB server on loopback, the same way
``test_live_rfb.py`` does, so the encoding path is exercised end to end and not against a stub.

Run it with the Hermes venv's interpreter, with or without pytest.
"""

from __future__ import annotations

import importlib.util
import io
import os
import socket
import struct
import sys
import threading
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

from hermes_vnc import config as _config  # noqa: E402
from hermes_vnc import live_view as _live  # noqa: E402
from hermes_vnc import session as _session  # noqa: E402

WIDTH, HEIGHT = 800, 600


class RfbServer(threading.Thread):
    """Minimal RFB 3.8 server: enough handshake and one framebuffer update per request."""

    def __init__(self, *, width: int = WIDTH, height: int = HEIGHT) -> None:
        super().__init__(daemon=True)
        self.width, self.height = width, height
        self.connections = 0
        self.updates = 0
        self.error: BaseException | None = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self.port = self._listener.getsockname()[1]
        self._done = threading.Event()

    @staticmethod
    def _recv_exactly(conn: socket.socket, count: int) -> bytes:
        buf = b""
        while len(buf) < count:
            chunk = conn.recv(count - len(buf))
            if not chunk:
                raise ConnectionError("client closed")
            buf += chunk
        return buf

    def _serve(self, conn: socket.socket) -> None:
        conn.sendall(b"RFB 003.008\n")
        self._recv_exactly(conn, 12)
        conn.sendall(bytes([1, 1]))                    # security: none
        self._recv_exactly(conn, 1)
        conn.sendall(struct.pack(">I", 0))
        self._recv_exactly(conn, 1)
        name = b"live-view test server"
        pixel_format = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)
        conn.sendall(struct.pack(">HH", self.width, self.height) + pixel_format
                     + struct.pack(">I", len(name)) + name)

        while not self._done.is_set():
            try:
                message = self._recv_exactly(conn, 1)[0]
            except (ConnectionError, OSError):
                return
            if message == 0:
                self._recv_exactly(conn, 19)
            elif message == 2:
                self._recv_exactly(conn, 1)
                count = struct.unpack(">H", self._recv_exactly(conn, 2))[0]
                self._recv_exactly(conn, 4 * count)
            elif message == 3:
                self._recv_exactly(conn, 9)
                header = struct.pack(">BBH", 0, 0, 1)
                rect = struct.pack(">HHHHi", 0, 0, self.width, self.height, 0)
                pixels = b"".join(struct.pack("<BBBB", x % 256, y % 256, 128, 0)
                                  for y in range(self.height) for x in range(self.width))
                conn.sendall(header + rect + pixels)
                self.updates += 1
            elif message == 4:
                self._recv_exactly(conn, 7)
            elif message == 5:
                self._recv_exactly(conn, 5)
            elif message == 6:
                self._recv_exactly(conn, 3)
                length = struct.unpack(">I", self._recv_exactly(conn, 4))[0]
                self._recv_exactly(conn, length)
            else:
                raise AssertionError(f"unexpected client message {message}")

    def run(self) -> None:
        try:
            while not self._done.is_set():
                try:
                    conn, _ = self._listener.accept()
                except OSError:
                    return
                self.connections += 1
                with conn:
                    self._serve(conn)
        except BaseException as exc:  # noqa: BLE001
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


def settings_for(port: int, *, connect_timeout: float = 5.0) -> _config.Settings:
    return _config.Settings(host="127.0.0.1", port=port, password="", password_env="",
                            connect_timeout=connect_timeout, call_timeout=8.0, action_pause=0.0)


def dead_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def with_server(width: int = WIDTH, height: int = HEIGHT):
    server = RfbServer(width=width, height=height)
    server.start()
    return server


def stop_server(server: RfbServer) -> None:
    server.stop()
    server.join(timeout=10)


# ── the encoder ─────────────────────────────────────────────────────────────


def test_downscale_shrinks_a_wide_frame_and_keeps_the_aspect_ratio():
    from PIL import Image

    source = io.BytesIO()
    Image.new("RGB", (1600, 900), (12, 34, 56)).save(source, format="PNG")

    jpeg, width, height = _live._downscale(source.getvalue(), 800, 60)

    assert (width, height) == (800, 450), (width, height)
    assert jpeg[:2] == b"\xff\xd8", jpeg[:4].hex()
    with Image.open(io.BytesIO(jpeg)) as decoded:
        assert decoded.size == (800, 450), decoded.size
        assert decoded.format == "JPEG", decoded.format


def test_downscale_leaves_a_frame_narrower_than_the_limit_alone():
    from PIL import Image

    source = io.BytesIO()
    Image.new("RGB", (320, 200), (1, 2, 3)).save(source, format="PNG")

    jpeg, width, height = _live._downscale(source.getvalue(), 960, 60)

    assert (width, height) == (320, 200), (width, height)
    assert jpeg[:2] == b"\xff\xd8", jpeg[:4].hex()


def test_downscale_rounds_a_tall_frame_to_at_least_one_pixel():
    from PIL import Image

    source = io.BytesIO()
    Image.new("RGB", (4000, 3), (9, 9, 9)).save(source, format="PNG")

    jpeg, width, height = _live._downscale(source.getvalue(), 800, 60)

    assert width == 800 and height >= 1, (width, height)


# ── configuration and failure paths ─────────────────────────────────────────


def test_grab_jpeg_names_the_config_key_when_no_target_is_set():
    _live.close()
    bare = _config.Settings(host="", port=5900, password="", password_env="",
                            connect_timeout=2.0, call_timeout=2.0, action_pause=0.0)
    try:
        _live.grab_jpeg(settings=bare)
    except _session.VncError as exc:
        assert "computer_use.vnc.host" in str(exc), exc
    else:
        raise AssertionError("a frame without a target must fail")


def test_grab_jpeg_reports_a_dead_target_instead_of_hanging():
    _live.close()
    try:
        _live.grab_jpeg(settings=settings_for(dead_port(), connect_timeout=2.0))
    except _session.VncError as exc:
        assert str(exc), "the error must carry a reason"
    else:
        raise AssertionError("a dead target must fail")
    finally:
        _live.close()


def test_close_is_safe_when_nothing_is_open_and_twice_in_a_row():
    _live.close()
    _live.close()


# ── the real client against a scripted screen ───────────────────────────────


def test_grab_jpeg_returns_a_downscaled_jpeg_of_the_scripted_screen():
    server = with_server()
    try:
        jpeg, width, height = _live.grab_jpeg(settings=settings_for(server.port), max_width=400)
        assert (width, height) == (400, 300), (width, height)
        assert jpeg[:2] == b"\xff\xd8", jpeg[:4].hex()
        assert len(jpeg) > 500, len(jpeg)
        assert server.updates >= 1, server.updates
    finally:
        _live.close()
        stop_server(server)
    assert server.error is None, server.error


def test_grab_jpeg_reuses_one_connection_for_repeated_frames():
    """The pane asks ~3 times a second; reconnecting each time would be a protocol flood."""
    server = with_server()
    try:
        for _ in range(3):
            jpeg, _, _ = _live.grab_jpeg(settings=settings_for(server.port), max_width=200)
            assert jpeg[:2] == b"\xff\xd8"
        assert server.connections == 1, f"expected one connection, saw {server.connections}"
        assert server.updates == 3, server.updates
    finally:
        _live.close()
        stop_server(server)
    assert server.error is None, server.error


def test_close_lets_a_later_grab_open_a_fresh_connection():
    server = with_server()
    try:
        _live.grab_jpeg(settings=settings_for(server.port), max_width=200)
        _live.close()
        _live.grab_jpeg(settings=settings_for(server.port), max_width=200)
        assert server.connections == 2, server.connections
    finally:
        _live.close()
        stop_server(server)
    assert server.error is None, server.error


def test_the_live_view_caps_its_waits_so_a_wedged_link_fails_fast():
    """The pane's request must not outlive the Desktop app's own 30 s backend timeout: a frame
    that takes longer than that shows the user a hang instead of an error."""
    generous = _config.Settings(host="10.0.0.9", port=5900, password="", password_env="",
                                connect_timeout=20.0, call_timeout=60.0, action_pause=0.0)
    capped = _live._cap(generous)
    assert capped.call_timeout == _live.LIVE_VIEW_CALL_TIMEOUT, capped.call_timeout
    assert capped.connect_timeout == _live.LIVE_VIEW_CONNECT_TIMEOUT, capped.connect_timeout
    assert (capped.host, capped.port) == ("10.0.0.9", 5900), "the target must not change"

    tighter = _config.Settings(host="10.0.0.9", port=5900, password="", password_env="",
                               connect_timeout=1.0, call_timeout=2.0, action_pause=0.0)
    assert _live._cap(tighter).call_timeout == 2.0, "a tighter setting must never be raised"


def test_a_failed_capture_drops_the_session_so_the_next_frame_reconnects():
    """A link that died without the reactor noticing still reports itself as connected. Without
    dropping it on failure, every later frame hangs on the same wedged session."""
    server = with_server()
    try:
        _live.grab_jpeg(settings=settings_for(server.port), max_width=200)
        assert server.connections == 1, server.connections

        class Wedged:
            connected = True                     # looks alive, exactly like a silently dropped link

            def capture_screen(self, buffer, fmt="PNG"):
                raise _session.VncError("the server did not answer")

            def close(self):
                pass

        real = _live._session_ref
        assert real is not None, "the first grab must have left a session behind"
        real.close()                       # free the scripted server so it can accept the next one

        with _live._lock:
            _live._session_ref = Wedged()

        try:
            _live.grab_jpeg(settings=settings_for(server.port), max_width=200)
        except _session.VncError:
            pass
        else:
            raise AssertionError("the wedged session must fail, not return a frame")

        assert _live._session_ref is None, "the wedged session must be dropped, not kept"

        jpeg, _, _ = _live.grab_jpeg(settings=settings_for(server.port), max_width=200)
        assert jpeg[:2] == b"\xff\xd8", jpeg[:4].hex()
        assert server.connections == 2, f"expected a fresh connection, saw {server.connections}"
    finally:
        _live.close()
        stop_server(server)
    assert server.error is None, server.error


def test_a_target_that_changes_reopens_instead_of_reusing_the_old_session():
    """Two different settings objects must not share one session."""
    first, second = with_server(320, 200), with_server(640, 480)
    try:
        a, _, _ = _live.grab_jpeg(settings=settings_for(first.port), max_width=160)
        b, _, _ = _live.grab_jpeg(settings=settings_for(second.port), max_width=160)
        assert a[:2] == b"\xff\xd8" == b[:2]
        assert first.connections == 1 and second.connections == 1, (first.connections, second.connections)
    finally:
        _live.close()
        stop_server(first)
        stop_server(second)
    assert first.error is None and second.error is None, (first.error, second.error)


# ── runner ───────────────────────────────────────────────────────────────────


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
