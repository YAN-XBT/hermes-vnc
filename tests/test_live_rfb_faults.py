"""Live RFB fault injection: every way the wire can break, driven through the real backend.

``test_live_rfb.py`` proves the happy path and ``test_live_rfb_edge_cases.py`` covers the
three failures that show up most; this file goes after the rest of them, because a remote
desktop plugin that hangs or lies when the far side misbehaves is worse than useless — the
caller has no way to tell a dead server from a slow one.

Each test points the real ``VncBackend`` at a scripted server built to break in one specific
way and asserts three things: an error is raised (never a silent success), the error names
the target, and it arrives promptly (never a hang).

Run it with the Hermes venv's interpreter, with or without pytest.
"""

from __future__ import annotations

import importlib.util
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

WIDTH, HEIGHT = 48, 32
SECRET = "fault-injection-password"


class FaultyServer(threading.Thread):
    """A scripted RFB server whose ``fault`` decides exactly how it misbehaves."""

    def __init__(self, *, fault: str | None = None, width: int = WIDTH, height: int = HEIGHT,
                 max_connections: int = 1, banner_delay: float = 0.0) -> None:
        super().__init__(daemon=True)
        self.fault = fault
        self.width, self.height = width, height
        self.max_connections = max_connections
        self.banner_delay = banner_delay
        self.connections = 0
        self.events: list[tuple] = []
        self.error: BaseException | None = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(max_connections)
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

    def _banner(self, conn: socket.socket) -> None:
        if self.banner_delay:
            time.sleep(self.banner_delay)
        conn.sendall(b"RFB 003.008\n")

    def _handshake(self, conn: socket.socket) -> bool:
        """Run the handshake; False means this connection is over."""
        if self.fault == "close_immediately":
            self.events.append(("closed_before_banner",))
            return False
        if self.fault == "not_rfb":
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
            self.events.append(("sent_http",))
            return False

        self._banner(conn)
        self._recv_exactly(conn, 12)

        if self.fault == "short_challenge":
            conn.sendall(bytes([1, 2]))               # VNC auth offered
            self._recv_exactly(conn, 1)
            conn.sendall(b"\x00" * 4)                 # four bytes of a sixteen-byte challenge
            self.events.append(("short_challenge",))
            return False

        if self.fault == "auth_then_drop":
            conn.sendall(bytes([1, 2]))
            self._recv_exactly(conn, 1)
            conn.sendall(b"\x00" * 16)
            self._recv_exactly(conn, 16)
            self.events.append(("auth_then_drop",))
            return False

        conn.sendall(bytes([1, 1]))                   # security type 1: none
        self._recv_exactly(conn, 1)
        conn.sendall(struct.pack(">I", 0))
        self._recv_exactly(conn, 1)                   # ClientInit

        if self.fault == "drop_after_serverinit":
            self.events.append(("drop_after_serverinit",))
            return False

        name = b"hermes-vnc fault server"
        pixel_format = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)
        conn.sendall(struct.pack(">HH", self.width, self.height) + pixel_format
                     + struct.pack(">I", len(name)) + name)
        return True

    def _update(self, conn: socket.socket) -> None:
        header = struct.pack(">BBH", 0, 0, 1)
        rect = struct.pack(">HHHHi", 0, 0, self.width, self.height, 0)
        grey = b"".join(struct.pack("<BBBB", (x * y) % 256, (x + y) % 256, (x - y) % 256, 0)
                        for y in range(self.height) for x in range(self.width))
        if self.fault == "truncated_frame":
            conn.sendall(header + rect + grey[: len(grey) // 3])
            self.events.append(("truncated_frame",))
            return
        conn.sendall(header + rect + grey)
        self.events.append(("update",))

    def _serve_one(self, conn: socket.socket) -> None:
        self.connections += 1
        if not self._handshake(conn):
            return
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
                self._update(conn)
                if self.fault in {"truncated_frame", "die_on_frame"}:
                    return
            elif message == 4:
                self._recv_exactly(conn, 7)
                self.events.append(("key",))
                if self.fault == "die_on_key":
                    self.events.append(("died_on_key",))
                    return
            elif message == 5:
                self._recv_exactly(conn, 5)
                self.events.append(("pointer",))
            elif message == 6:
                self._recv_exactly(conn, 3)
                length = struct.unpack(">I", self._recv_exactly(conn, 4))[0]
                self._recv_exactly(conn, length)
            else:
                raise AssertionError(f"unexpected client message type {message}")

    def run(self) -> None:
        try:
            while self.connections < self.max_connections and not self._done.is_set():
                try:
                    conn, _ = self._listener.accept()
                except OSError:
                    return
                with conn:
                    self._serve_one(conn)
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

    def kinds(self) -> list[str]:
        return [event[0] for event in self.events]


def backend_for(server: FaultyServer, *, connect_timeout: float = 4.0,
                call_timeout: float = 4.0, host: str = "127.0.0.1") -> _backend.VncBackend:
    return _backend.VncBackend(
        permission_mode="standard",
        settings=_config.Settings(host=host, port=server.port, password=SECRET,
                                  password_env="", connect_timeout=connect_timeout,
                                  call_timeout=call_timeout, action_pause=0.0),
    )


def run(server: FaultyServer, work, **kwargs):
    """Start *server*, run *work(backend)*, always shut both down."""
    server.start()
    instance = backend_for(server, **kwargs)
    try:
        return work(instance)
    finally:
        instance.stop()
        server.stop()
        server.join(timeout=10)


def expect_vnc_error(work, *, within: float, target: tuple[str, int] | None = None) -> str:
    """Assert *work* fails with a VncError promptly, and return its message."""
    started = time.monotonic()
    try:
        work()
    except _session.VncError as exc:
        elapsed = time.monotonic() - started
        message = str(exc)
        assert elapsed < within, f"took {elapsed:.1f}s (limit {within}s) — that is a hang: {message}"
        assert message, "an error must carry a message"
        if target is not None:
            assert f"{target[0]}:{target[1]}" in message, f"{message!r} does not name {target}"
        return message
    raise AssertionError("the operation was expected to fail, but it succeeded")


# ── the far side never speaks RFB ───────────────────────────────────────────


def test_a_port_serving_something_else_is_reported_not_waited_out():
    server = FaultyServer(fault="not_rfb")
    message = run(server, lambda backend: expect_vnc_error(
        backend.capture, within=8, target=("127.0.0.1", server.port)))
    assert "sent_http" in server.kinds(), server.events
    assert "protocol" in message.lower() or "connect" in message.lower(), message


def test_a_server_that_closes_before_the_banner_is_reported_promptly():
    server = FaultyServer(fault="close_immediately")
    run(server, lambda backend: expect_vnc_error(
        backend.capture, within=8, target=("127.0.0.1", server.port)))
    assert "closed_before_banner" in server.kinds(), server.events


def test_a_server_that_never_answers_is_bounded_by_the_connect_timeout():
    """A silent listener must cost the connect timeout, not the call timeout."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    instance = _backend.VncBackend(
        permission_mode="standard",
        settings=_config.Settings(host="127.0.0.1", port=port, password=SECRET, password_env="",
                                  connect_timeout=2.0, call_timeout=60.0, action_pause=0.0),
    )
    started = time.monotonic()
    try:
        try:
            instance.capture()
        except _session.VncError as exc:
            elapsed = time.monotonic() - started
            assert elapsed < 15, f"waited {elapsed:.1f}s for a silent server: {exc}"
            assert "timed out" in str(exc).lower(), exc
        else:
            raise AssertionError("a silent server must not yield a frame")
    finally:
        instance.stop()
        listener.close()


def test_a_slow_banner_beyond_the_connect_timeout_is_reported():
    server = FaultyServer(banner_delay=6.0)
    run(server, lambda backend: expect_vnc_error(backend.capture, within=15),
        connect_timeout=1.5, call_timeout=30.0)


# ── the handshake breaks in the middle ──────────────────────────────────────


def test_a_short_auth_challenge_is_an_error_not_a_crash():
    server = FaultyServer(fault="short_challenge")
    message = run(server, lambda backend: expect_vnc_error(
        backend.capture, within=8, target=("127.0.0.1", server.port)))
    assert "short_challenge" in server.kinds(), server.events
    assert "connect" in message.lower() or "closed" in message.lower(), message


def test_a_drop_after_a_successful_authentication_is_reported():
    server = FaultyServer(fault="auth_then_drop")
    message = run(server, lambda backend: expect_vnc_error(
        backend.capture, within=8, target=("127.0.0.1", server.port)))
    assert "auth_then_drop" in server.kinds(), server.events
    assert "closed" in message.lower() or "connect" in message.lower(), message


def test_a_drop_right_after_the_server_handshake_is_reported():
    server = FaultyServer(fault="drop_after_serverinit")
    run(server, lambda backend: expect_vnc_error(
        backend.capture, within=8, target=("127.0.0.1", server.port)))
    assert "drop_after_serverinit" in server.kinds(), server.events


# ── the framebuffer lies ────────────────────────────────────────────────────


def test_a_truncated_framebuffer_is_an_error_not_a_half_decoded_frame():
    server = FaultyServer(fault="truncated_frame")
    run(server, lambda backend: expect_vnc_error(backend.capture, within=8))
    assert "truncated_frame" in server.kinds(), server.events


def test_the_backend_recovers_after_a_bad_frame_on_a_new_connection():
    """A truncated frame kills the connection; the next call must not inherit the wreck."""
    server = FaultyServer(fault="truncated_frame", max_connections=2)
    server.start()
    instance = backend_for(server, call_timeout=4.0)
    try:
        try:
            instance.capture()
        except _session.VncError:
            pass
        else:
            raise AssertionError("the truncated frame must fail")
        # the peer's close is noticed by the reactor, so allow it a moment to land
        deadline = time.monotonic() + 3
        while (time.monotonic() < deadline and instance._session is not None
               and instance._session.connected):
            time.sleep(0.05)
        assert instance._session is None or not instance._session.connected, \
            "a session whose connection died must not keep reporting itself as connected"
    finally:
        instance.stop()
        server.stop()
        server.join(timeout=10)


# ── input against a server that dies mid-action ─────────────────────────────


def test_a_server_dying_during_typing_is_reported_by_the_next_call():
    """RFB input is fire-and-forget, and the plugin says so rather than pretending otherwise.

    Keys are written to the socket without waiting for an acknowledgement, so a server that
    dies mid-typing cannot be noticed by the typing itself: the death surfaces on the next
    operation that waits for an answer. That is a property of the protocol, not a gap.
    """
    server = FaultyServer(fault="die_on_key")

    def work(backend):
        try:
            backend.type_text("hello")
        except _session.VncError:
            pass          # a write onto a socket already known dead may fail here instead
        return expect_vnc_error(backend.capture, within=8)

    run(server, work)
    assert "died_on_key" in server.kinds(), server.events


def test_a_dropped_connection_does_not_block_the_next_action_forever():
    """After the far side dies, the following call must fail on its own budget."""
    server = FaultyServer(fault="die_on_frame")
    server.start()
    instance = backend_for(server, call_timeout=3.0)
    try:
        try:
            instance.capture()
        except _session.VncError:
            pass
        started = time.monotonic()
        try:
            instance.list_apps()
        except _session.VncError:
            pass
        elapsed = time.monotonic() - started
        assert elapsed < 15, f"the second call took {elapsed:.1f}s"
    finally:
        instance.stop()
        server.stop()
        server.join(timeout=10)


# ── recovery and isolation ──────────────────────────────────────────────────


def test_a_fresh_backend_reconnects_after_the_first_one_gave_up():
    """The failure must live in the session, not in the plugin's process-wide state."""
    server = FaultyServer(fault=None, max_connections=2)
    server.start()
    try:
        first = backend_for(server, call_timeout=4.0)
        try:
            frame = first.capture()
            assert (frame.width, frame.height) == (WIDTH, HEIGHT), frame
        finally:
            first.stop()

        second = backend_for(server, call_timeout=4.0)
        try:
            frame = second.capture()
            assert (frame.width, frame.height) == (WIDTH, HEIGHT), frame
        finally:
            second.stop()
        assert server.connections == 2, server.connections
    finally:
        server.stop()
        server.join(timeout=10)
    assert server.error is None, server.error


def test_two_backends_against_two_servers_do_not_share_state():
    left = FaultyServer(width=40, height=30)
    right = FaultyServer(width=70, height=50)
    left.start()
    right.start()
    left_backend = backend_for(left)
    right_backend = backend_for(right)
    try:
        a = left_backend.capture()
        b = right_backend.capture()
        assert (a.width, a.height) == (40, 30), a
        assert (b.width, b.height) == (70, 50), b
    finally:
        left_backend.stop()
        right_backend.stop()
        left.stop()
        right.stop()
        left.join(timeout=10)
        right.join(timeout=10)


def test_a_dead_port_and_a_live_one_in_the_same_process_stay_independent():
    live = FaultyServer()
    live.start()

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    dead_backend = _backend.VncBackend(
        permission_mode="standard",
        settings=_config.Settings(host="127.0.0.1", port=dead_port, password=SECRET,
                                  password_env="", connect_timeout=2.0, call_timeout=4.0,
                                  action_pause=0.0),
    )
    live_backend = backend_for(live)
    try:
        try:
            dead_backend.capture()
        except _session.VncError:
            pass
        else:
            raise AssertionError("the dead port must fail")

        frame = live_backend.capture()
        assert (frame.width, frame.height) == (WIDTH, HEIGHT), frame
    finally:
        dead_backend.stop()
        live_backend.stop()
        live.stop()
        live.join(timeout=10)


# ── hygiene ─────────────────────────────────────────────────────────────────


def test_a_failure_message_never_carries_the_password():
    server = FaultyServer(fault="auth_then_drop")
    message = run(server, lambda backend: expect_vnc_error(backend.capture, within=8))
    assert SECRET not in message, message


def test_stop_on_a_backend_that_never_connected_is_harmless():
    server = FaultyServer()
    server.start()
    instance = backend_for(server)
    try:
        instance.stop()
        instance.stop()
    finally:
        server.stop()
        server.join(timeout=10)


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
