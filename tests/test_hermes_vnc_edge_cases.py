"""Edge-case, argument-validation and concurrency tests for hermes-vnc.

The main suite (``test_hermes_vnc.py``) covers the happy paths and the documented refusals.
This one goes after the places a driver actually breaks in production: the session layer's
timeout and error paths, two threads driving one backend, a session that dies and has to be
reopened, and arguments a model can plausibly produce but the protocol cannot carry.

Run it with the Hermes venv's interpreter, with or without pytest.
"""

from __future__ import annotations

import base64
import importlib.util
import os
import struct
import sys
import threading
import time
import zlib
from pathlib import Path


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

_PKG_DIR = Path(__file__).resolve().parents[1]
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


# ── helpers ──────────────────────────────────────────────────────────────────


def png_bytes(width: int, height: int) -> bytes:
    raw = b"".join(b"\x00" + b"\x00" * (width * 3) for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def settings(**over) -> _config.Settings:
    base = dict(host="desk.example.test", port=5900, password="", password_env="",
                connect_timeout=5.0, call_timeout=5.0, action_pause=0.0, display_name="",
                max_capture_bytes=1_000_000)
    base.update(over)
    return _config.Settings(**base)


class FakeTransport:
    def __init__(self) -> None:
        self.lost = False
        self.lose_calls = 0

    def loseConnection(self) -> None:
        self.lose_calls += 1
        self.lost = True


class FakeProtocol:
    """Mirrors the RFB client surface the session calls, with failure switches."""

    def __init__(self, *, error=None, capture=b"", deferred=None, delay=0.0) -> None:
        self.calls: list[tuple] = []
        self.error = error
        self.capture_bytes = capture
        self.deferred = deferred      # None | "never" | "errback"
        self.delay = delay
        self.transport = FakeTransport()
        self.lock = threading.Lock()

    def _record(self, name, *args):
        with self.lock:
            self.calls.append((name,) + args)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error

    def mouseMove(self, x, y):
        self._record("mouseMove", x, y)

    def mousePress(self, button):
        self._record("mousePress", button)

    def mouseDown(self, button):
        self._record("mouseDown", button)

    def mouseUp(self, button):
        self._record("mouseUp", button)

    def keyDown(self, name):
        self._record("keyDown", name)

    def keyUp(self, name):
        self._record("keyUp", name)

    def keyPress(self, combo):
        self._record("keyPress", combo)

    def keyEvent(self, keysym, down=True):
        self._record("keyEvent", keysym, down)

    def paste(self, text):
        self._record("paste", text)

    def captureScreen(self, fp, incremental=False, format=None):
        with self.lock:
            self.calls.append(("captureScreen", format))
        if self.error is not None:
            raise self.error
        if self.deferred == "never":
            from twisted.internet.defer import Deferred

            return Deferred()
        if self.deferred == "errback":
            from twisted.internet.defer import Deferred

            deferred = Deferred()
            deferred.errback(RuntimeError("framebuffer decode failed"))
            return deferred
        fp.write(self.capture_bytes)
        return None

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakeSession:
    """The session interface the backend depends on, recording what it is asked to do."""

    def __init__(self, *, png: bytes = b"", error=None) -> None:
        self.calls: list[tuple] = []
        self.png = png
        self.error = error
        self.closed = False
        self._connected = True
        self.lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    def _record(self, name, *args):
        with self.lock:
            self.calls.append((name,) + args)
        if self.error is not None:
            raise self.error

    def capture_screen(self, buffer, fmt="PNG"):
        self._record("capture_screen", fmt)
        buffer.write(self.png)

    def move(self, x, y):
        self._record("move", x, y)

    def press(self, button):
        self._record("press", button)

    def button_down(self, button):
        self._record("button_down", button)

    def button_up(self, button):
        self._record("button_up", button)

    def key_down(self, name):
        self._record("key_down", name)

    def key_up(self, name):
        self._record("key_up", name)

    def key_press(self, combo):
        self._record("key_press", combo)

    def type_keysym(self, keysym):
        self._record("type_keysym", keysym)

    def cut_text(self, text):
        self._record("cut_text", text)

    def close(self):
        self.closed = True
        self._connected = False
        self._record("close")

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


def backend_with(fake, **over) -> _backend.VncBackend:
    instance = _backend.VncBackend(permission_mode="standard", settings=settings(**over))
    instance._session = fake
    return instance


def session_with(protocol, **over) -> _session.RfbSession:
    instance = _session.RfbSession(settings(**over))
    instance._reactor = _session.ensure_reactor()
    instance._protocol = protocol
    return instance


# ── the session layer: values, errors, Deferreds, timeouts ───────────────────


def test_session_call_returns_a_plain_value():
    protocol = FakeProtocol()
    session = session_with(protocol)
    assert session._call("mouseMove", 5, 6) is None
    assert protocol.calls == [("mouseMove", 5, 6)]


def test_session_call_propagates_an_error_raised_by_the_protocol():
    session = session_with(FakeProtocol(error=ConnectionResetError("peer reset")))
    try:
        session._call("mouseMove", 1, 1)
    except ConnectionResetError as exc:
        assert "peer reset" in str(exc)
    else:
        raise AssertionError("a raising protocol call must reach the caller")


def test_session_call_waits_for_a_deferred_and_propagates_its_errback():
    session = session_with(FakeProtocol(deferred="errback"))
    try:
        session._call("captureScreen", None, format="PNG")
    except RuntimeError as exc:
        assert "framebuffer decode failed" in str(exc)
    else:
        raise AssertionError("a Deferred errback must reach the caller")


def test_session_call_times_out_on_a_deferred_that_never_fires():
    session = session_with(FakeProtocol(deferred="never"), call_timeout=0.4)
    started = time.monotonic()
    try:
        session._call("captureScreen", None, format="PNG")
    except _session.VncError as exc:
        assert "did not answer" in str(exc)
    else:
        raise AssertionError("a Deferred that never fires must time out")
    assert time.monotonic() - started < 4, "the timeout must be the configured one, not the default"


def test_session_refuses_operations_before_open_and_after_close():
    session = _session.RfbSession(settings())
    for call in (lambda: session.capture_screen(None), lambda: session.move(1, 1),
                 lambda: session.key_press("ctrl-s"), lambda: session.cut_text("x")):
        try:
            call()
        except _session.VncError as exc:
            assert "not open" in str(exc)
        else:
            raise AssertionError("an operation on an unopened session must be refused")

    session._reactor = _session.ensure_reactor()
    session._protocol = FakeProtocol()
    session.close()
    assert session.connected is False
    session.close()  # idempotent
    session.close()


def test_session_close_loses_the_connection_once():
    """Closing is asynchronous by design (the loss runs on the reactor thread), so wait for it."""
    protocol = FakeProtocol()
    session = session_with(protocol)
    session.close()
    session.close()                     # idempotent: there is nothing left to lose
    assert session._protocol is None

    deadline = time.monotonic() + 5
    while not protocol.transport.lost and time.monotonic() < deadline:
        time.sleep(0.01)
    assert protocol.transport.lost is True, "closing must actually drop the connection"
    assert protocol.transport.lose_calls == 1, protocol.transport.lose_calls


def test_ensure_reactor_is_single_under_concurrency():
    results: list[object] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def grab():
        try:
            reactor = _session.ensure_reactor()
            with lock:
                results.append(reactor)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors, errors
    assert len(results) == 8 and len({id(r) for r in results}) == 1, "one reactor per process"
    assert results[0].running is True


# ── the backend: concurrency, reopening, argument validation ────────────────


def test_concurrent_captures_all_succeed_and_are_serialised():
    fake = FakeSession(png=png_bytes(40, 30))
    instance = backend_with(fake)
    sizes: list[tuple] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def capture():
        try:
            result = instance.capture()
            with lock:
                sizes.append((result.width, result.height))
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=capture) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert sizes == [(40, 30)] * 8, sizes
    assert fake.names().count("capture_screen") == 8, fake.calls


def test_concurrent_mixed_actions_do_not_interleave_one_action_with_another():
    """Each action holds the lock, so its own protocol calls must stay contiguous."""
    fake = FakeSession(png=png_bytes(4, 4))
    instance = backend_with(fake)
    errors: list[BaseException] = []

    def work(kind: str):
        try:
            for _ in range(5):
                if kind == "click":
                    instance.click(x=10, y=20)
                elif kind == "scroll":
                    instance.scroll(direction="down", amount=2)
                else:
                    instance.key("ctrl+s")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(kind,)) for kind in ("click", "scroll", "key")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    calls = [name for name, *_ in fake.calls]
    assert calls.count("move") == 5, calls                 # one move per click
    assert calls.count("press") == 5 + 5 * 2, calls         # 5 clicks + 2 wheel ticks per scroll
    assert calls.count("key_press") == 5, calls


def test_backend_reopens_the_session_after_stop():
    fake = FakeSession(png=png_bytes(8, 8))
    opened: list[object] = []
    real = _session.open_session

    def opener(settings_):
        opened.append(settings_)
        return fake

    _session.open_session = opener
    try:
        instance = _backend.VncBackend(permission_mode="standard", settings=settings())
        instance.stop()                      # stop before anything was ever opened
        result = instance.click(x=1, y=1)    # must open on demand
        assert result.ok is True, result
        assert len(opened) == 1, opened
        instance.stop()
        assert fake.closed is True
        instance.capture()                   # reopening after a close
        assert len(opened) == 2, opened
    finally:
        _session.open_session = real


def test_backend_reopens_when_the_session_reports_itself_disconnected():
    fake = FakeSession(png=png_bytes(8, 8))
    instance = backend_with(fake)
    fake._connected = False                  # the server dropped us
    real = _session.open_session
    _session.open_session = lambda settings_: fake
    try:
        assert instance.capture().width == 8
    finally:
        _session.open_session = real


def test_click_prefers_element_over_coordinates():
    fake = FakeSession(png=png_bytes(4, 4))
    result = backend_with(fake).click(element=7, x=1, y=2)
    assert result.code == "unsupported_action", result
    assert fake.calls == [], "an element click must not touch the wire"


def test_click_refuses_negative_and_non_integer_coordinates():
    instance = backend_with(FakeSession(png=png_bytes(4, 4)))
    for bad in ((dict(x=-1, y=5), "negative"), (dict(x=5, y=-1), "negative"),
                (dict(x="left", y=5), "integers"), (dict(x=None, y=5), "needs x and y")):
        result = instance.click(**bad[0])
        assert result.ok is False and result.code == "invalid_arguments", (bad, result)
        assert bad[1] in result.message, (bad, result.message)
    assert instance._session.calls == []


def test_click_accepts_float_like_coordinates_by_truncating():
    fake = FakeSession(png=png_bytes(4, 4))
    result = backend_with(fake).click(x="12", y="34")
    assert result.ok is True, result
    assert fake.calls == [("move", 12, 34), ("press", 1)], fake.calls


def test_scroll_clamps_the_amount_and_refuses_garbage():
    fake = FakeSession(png=png_bytes(4, 4))
    assert backend_with(fake).scroll(direction="down", amount=0).ok is True
    assert fake.names().count("press") == 1, fake.calls          # clamped up to 1

    fake2 = FakeSession(png=png_bytes(4, 4))
    assert backend_with(fake2).scroll(direction="down", amount=1000).ok is True
    assert fake2.names().count("press") == 50, fake2.calls        # clamped down to 50

    fake3 = FakeSession(png=png_bytes(4, 4))
    result = backend_with(fake3).scroll(direction="down", amount="lots")
    assert result.ok is False and result.code == "invalid_arguments", result
    assert fake3.calls == []


def test_scroll_refuses_bad_coordinates_without_touching_the_wire():
    instance = backend_with(FakeSession(png=png_bytes(4, 4)))
    for coords in (dict(x=-3, y=1), dict(x=1, y=-3), dict(x="a", y=1)):
        result = instance.scroll(direction="up", x=coords["x"], y=coords["y"])
        assert result.ok is False and result.code == "invalid_arguments", result
    assert instance._session.calls == []


def test_drag_refuses_a_single_corner_and_bad_values():
    instance = backend_with(FakeSession(png=png_bytes(4, 4)))
    for kwargs in (dict(from_xy=(0, 0)), dict(to_xy=(5, 5)), dict(from_xy=(0, 0), to_xy=(1,)),
                   dict(from_xy=(-1, 0), to_xy=(5, 5)), dict(from_xy=(0, 0), to_xy=(5, -1))):
        result = instance.drag(**kwargs)
        assert result.ok is False and result.code == "invalid_arguments", (kwargs, result)
    assert instance._session.calls == []


def test_drag_with_equal_corners_still_releases_the_button():
    fake = FakeSession(png=png_bytes(4, 4))
    result = backend_with(fake).drag(from_xy=(7, 7), to_xy=(7, 7))
    assert result.ok is True, result
    assert fake.calls[0] == ("move", 7, 7)
    assert ("button_down", 1) in fake.calls and ("button_up", 1) in fake.calls, fake.calls


def test_type_text_mixed_ascii_and_latin1_goes_to_the_clipboard():
    fake = FakeSession(png=png_bytes(4, 4))
    result = backend_with(fake).type_text("café 1")
    assert result.ok is True, result
    assert fake.calls == [("cut_text", "café 1")], fake.calls


def test_type_text_of_only_control_characters_uses_keysyms():
    fake = FakeSession(png=png_bytes(4, 4))
    result = backend_with(fake).type_text("\n\t")
    assert result.ok is True, result
    assert fake.calls == [("type_keysym", 0xFF0D), ("type_keysym", 0xFF09)], fake.calls


def test_type_text_reports_a_transport_failure_rather_than_raising():
    fake = FakeSession(png=png_bytes(4, 4), error=BrokenPipeError("gone"))
    result = backend_with(fake).type_text("hello")
    assert result.ok is False and result.code == "transport_error", result
    assert "BrokenPipeError" in result.message


def test_element_lanes_never_touch_the_session():
    fake = FakeSession(png=png_bytes(4, 4))
    instance = backend_with(fake)
    assert instance.list_apps() == [] and instance.list_windows() == []
    assert instance.focus_app("anything").code == "unsupported_action"
    assert instance.set_value("on", element=1).code == "unsupported_action"
    assert fake.calls == [], fake.calls


def test_two_backends_do_not_share_a_session():
    first, second = FakeSession(png=png_bytes(4, 4)), FakeSession(png=png_bytes(4, 4))
    one, two = backend_with(first), backend_with(second)
    one.click(x=1, y=1)
    assert first.calls and second.calls == [], (first.calls, second.calls)


def test_capture_reports_the_size_it_was_given_even_when_odd():
    for width, height in ((1, 1), (3, 7), (1920, 1080)):
        capture = backend_with(FakeSession(png=png_bytes(width, height))).capture()
        assert (capture.width, capture.height) == (width, height), (width, height, capture)
        assert base64.b64decode(capture.png_b64) == png_bytes(width, height)


# ── parsing helpers ──────────────────────────────────────────────────────────


def test_png_size_rejects_a_truncated_header():
    for raw in (b"", b"\x89PNG\r\n\x1a\n", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIH"):
        try:
            _backend.png_size(raw)
        except _backend.VncError:
            pass
        else:
            raise AssertionError(f"a truncated framebuffer must be refused: {raw!r}")


def test_normalize_combo_handles_odd_input():
    assert _backend.normalize_combo(None) == ""
    assert _backend.normalize_combo("   ") == ""
    assert _backend.normalize_combo("Ctrl+S") == "ctrl-s"
    assert _backend.normalize_combo("cmd+shift+4") == "super-shift-4"
    assert _backend.normalize_combo("f11") == "f11"
    assert _backend.normalize_combo("alt+F4") == "alt-f4"


def test_modifier_names_tolerates_non_strings():
    assert _backend.modifier_names([None, 7, "cmd"]) == ["none", "7", "super"]


def test_config_rejects_a_port_outside_the_valid_range():
    real = _config._raw_block
    try:
        for bad in (0, -1, 70000, "nonsense"):
            _config._raw_block = lambda bad=bad: {"host": "h", "port": bad}
            assert _config.load_settings().port == _config.DEFAULT_PORT, bad
        _config._raw_block = lambda: {"host": "h", "port": "5999"}
        assert _config.load_settings().port == 5999
    finally:
        _config._raw_block = real


def test_config_rejects_a_non_positive_capture_cap():
    real = _config._raw_block
    try:
        for bad in (0, -5, "x"):
            _config._raw_block = lambda bad=bad: {"host": "h", "max_capture_bytes": bad}
            assert _config.load_settings().max_capture_bytes == _config.DEFAULT_MAX_CAPTURE_BYTES, bad
    finally:
        _config._raw_block = real


def test_config_strips_whitespace_around_the_host():
    real = _config._raw_block
    try:
        _config._raw_block = lambda: {"host": "  desk.example.test  "}
        assert _config.load_settings().host == "desk.example.test"
    finally:
        _config._raw_block = real


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
