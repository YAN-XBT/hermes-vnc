"""Live RFB failure-mode tests: the connection dies, the screen resizes, the password is wrong.

``test_live_rfb.py`` proves the happy path against a scripted server. This file points the
same real backend at servers built to misbehave in the three ways that actually happen in
the field, and asserts the plugin reports them instead of hanging or lying.

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
from hermes_vnc import session as _session  # noqa: E402


class RfbServer(threading.Thread):
    """A minimal RFB 3.8 server with switches for the three failure modes."""

    def __init__(self, *, width: int = 64, height: int = 48, security: str = "none",
                 close_after_updates: int | None = None) -> None:
        super().__init__(daemon=True)
        self.width, self.height = width, height
        self.security = security                  # "none" | "reject"
        self.close_after_updates = close_after_updates
        self.updates_sent = 0
        self.events: list[tuple] = []
        self.error: BaseException | None = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._done = threading.Event()

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _recv_exactly(conn: socket.socket, count: int) -> bytes:
        buf = b""
        while len(buf) < count:
            chunk = conn.recv(count - len(buf))
            if not chunk:
                raise ConnectionError("client closed the connection")
            buf += chunk
        return buf

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = width, height

    def _send_update(self, conn: socket.socket) -> None:
        header = struct.pack(">BBH", 0, 0, 1)
        rect = struct.pack(">HHHHi", 0, 0, self.width, self.height, 0)
        grey = b"".join(struct.pack("<BBBB", (x + y) % 256, (x + y) % 256, (x + y) % 256, 0)
                        for y in range(self.height) for x in range(self.width))
        conn.sendall(header + rect + grey)
        self.updates_sent += 1

    def _handle(self, conn: socket.socket) -> None:
        conn.sendall(b"RFB 003.008\n")
        self._recv_exactly(conn, 12)

        if self.security == "reject":
            conn.sendall(bytes([1, 2]))               # one type offered: VNC auth
            self.events.append(("security", self._recv_exactly(conn, 1)[0]))
            conn.sendall(b"\x00" * 16)                # the challenge the client answers
            self._recv_exactly(conn, 16)              # the challenge response
            conn.sendall(struct.pack(">I", 1))        # SecurityResult: failed
            self.events.append(("auth_rejected",))
            return

        conn.sendall(bytes([1, 1]))
        self.events.append(("security", self._recv_exactly(conn, 1)[0]))
        conn.sendall(struct.pack(">I", 0))
        self._recv_exactly(conn, 1)                   # ClientInit

        name = b"hermes-vnc failure-mode server"
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
                self._send_update(conn)
                if self.close_after_updates and self.updates_sent >= self.close_after_updates:
                    self.events.append(("closing_after_update", self.updates_sent))
                    return
            elif message == 4:
                self._recv_exactly(conn, 7)
            elif message == 5:
                self._recv_exactly(conn, 5)
            elif message == 6:
                self._recv_exactly(conn, 3)
                length = struct.unpack(">I", self._recv_exactly(conn, 4))[0]
                self._recv_exactly(conn, length)
            else:
                raise AssertionError(f"unexpected client message type {message}")

    def run(self) -> None:
        try:
            conn, _ = self._listener.accept()
            with conn:
                self._handle(conn)
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


def backend_for(server: RfbServer, *, call_timeout: float = 5.0, password: str = "") -> _backend.VncBackend:
    return _backend.VncBackend(
        permission_mode="standard",
        settings=_config.Settings(host="127.0.0.1", port=server.port, password=password,
                                  password_env="", connect_timeout=5.0, call_timeout=call_timeout,
                                  action_pause=0.0),
    )


# ── the three failure modes ─────────────────────────────────────────────────


def test_a_server_that_dies_mid_session_fails_cleanly_instead_of_hanging():
    server = RfbServer(width=32, height=24, close_after_updates=1)
    server.start()
    instance = backend_for(server, call_timeout=4.0)
    try:
        first = instance.capture()
        assert (first.width, first.height) == (32, 24), first

        # the server has closed its side; the next operation must report, not hang
        started = time.monotonic()
        try:
            instance.capture()
        except _session.VncError as exc:
            assert "did not answer" in str(exc) or "connect" in str(exc).lower(), exc
        else:
            raise AssertionError("a capture on a dead connection must fail, not succeed")
        elapsed = time.monotonic() - started
        assert elapsed < 30, f"the failure took {elapsed:.1f}s — that is a hang, not an error"
        assert ("closing_after_update", 1) in server.events, server.events
    finally:
        instance.stop()
        server.stop()
        server.join(timeout=10)
    assert server.error is None, server.error


def test_a_growing_framebuffer_is_reported_at_its_new_size():
    server = RfbServer(width=64, height=48)
    server.start()
    instance = backend_for(server)
    try:
        first = instance.capture()
        assert (first.width, first.height) == (64, 48), first

        server.resize(128, 96)
        second = instance.capture()
        assert (second.width, second.height) == (128, 96), second
        assert second.png_bytes_len != first.png_bytes_len, "a bigger frame cannot be the same size"
    finally:
        instance.stop()
        server.stop()
        server.join(timeout=10)
    assert server.error is None, server.error


def test_a_rejected_password_is_reported_as_a_connection_error_naming_the_target():
    server = RfbServer(security="reject")
    server.start()
    instance = backend_for(server, password="wrong-on-purpose")
    try:
        try:
            instance.capture()
        except _session.VncError as exc:
            message = str(exc)
            assert "could not connect" in message, message
            assert f"127.0.0.1:{server.port}" in message, message
        else:
            raise AssertionError("a rejected password must surface as a connection error")
        assert ("auth_rejected",) in server.events, server.events
        assert ("security", 2) in server.events, server.events
    finally:
        instance.stop()
        server.stop()
        server.join(timeout=10)
    assert server.error is None, server.error


def test_a_rejected_password_leaves_the_backend_usable_for_a_second_attempt():
    """Failing to authenticate must not poison the session object for the next call."""
    server = RfbServer(security="reject")
    server.start()
    instance = backend_for(server, password="wrong-on-purpose")
    try:
        for _ in range(2):
            try:
                instance.capture()
            except _session.VncError:
                pass
            else:
                raise AssertionError("both attempts must fail while the password is wrong")
        assert instance._session is None or not instance._session.connected
    finally:
        instance.stop()
        server.stop()
        server.join(timeout=10)


def test_a_server_that_hangs_up_during_the_handshake_reports_promptly():
    """A dropped handshake must not be waited out as if it were a slow server."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            conn.sendall(b"RFB 003.008\n")   # start the handshake, then vanish
            time.sleep(0.2)
        listener.close()

    threading.Thread(target=serve, daemon=True).start()

    instance = _backend.VncBackend(
        permission_mode="standard",
        settings=_config.Settings(host="127.0.0.1", port=port, password="", password_env="",
                                  connect_timeout=10.0, call_timeout=5.0, action_pause=0.0),
    )
    started = time.monotonic()
    try:
        try:
            instance.capture()
        except _session.VncError as exc:
            elapsed = time.monotonic() - started
            assert elapsed < 8, f"waited {elapsed:.1f}s — a hangup must be reported, not waited out"
            assert "connect" in str(exc).lower() or "closed" in str(exc), exc
        else:
            raise AssertionError("a hangup during the handshake must fail")
    finally:
        instance.stop()


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
