"""Offline tests for hermes-vnc.

They run with or without pytest and need no VNC server, no network and no Hermes session:
the package is loaded straight off disk and the RFB session is replaced by a recorder that
mirrors the interface :class:`hermes_vnc.session.RfbSession` exposes. Every assertion is
made against what the backend asked the session to do.

``tests/test_live_rfb.py`` covers the other half — the same backend against a scripted RFB
server, through the real client.

Run it with the Hermes venv's interpreter so the package's own imports resolve exactly as
they do at runtime::

    <HERMES_HOME>/hermes-agent/venv/Scripts/python.exe tests/test_hermes_vnc.py
"""

from __future__ import annotations

import base64
import importlib.util
import os
import struct
import sys
import zlib
from pathlib import Path


def _ensure_hermes_importable() -> None:
    """Make the Hermes source tree importable when the tests run outside a Hermes process."""
    try:
        import tools.computer_use.backend  # noqa: F401

        return
    except Exception:
        pass
    candidates = []
    if home := os.environ.get("HERMES_HOME"):
        candidates.append(Path(home) / "hermes-agent")
    candidates += [
        Path.home() / ".hermes" / "hermes-agent",
        Path(os.environ.get("LOCALAPPDATA", ".")) / "hermes" / "hermes-agent",
    ]
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


# ── fixtures ──────────────────────────────────────────────────────────────────


def png_bytes(width: int, height: int) -> bytes:
    """A real, minimal RGB PNG of the given size (no Pillow needed)."""
    raw = b"".join(b"\x00" + b"\x00" * (width * 3) for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


class FakeSession:
    """Records every session-level call the backend makes."""

    def __init__(self, png: bytes = b"", fail: BaseException | None = None) -> None:
        self.calls: list[tuple] = []
        self.png = png
        self.fail = fail
        self.closed = False

    def _maybe_fail(self):
        if self.fail is not None:
            raise self.fail

    @property
    def connected(self) -> bool:
        return not self.closed

    def capture_screen(self, buffer, fmt="PNG"):
        self.calls.append(("capture_screen", fmt))
        self._maybe_fail()
        buffer.write(self.png)

    def move(self, x, y):
        self.calls.append(("move", x, y))
        self._maybe_fail()

    def press(self, button):
        self.calls.append(("press", button))
        self._maybe_fail()

    def button_down(self, button):
        self.calls.append(("button_down", button))
        self._maybe_fail()

    def button_up(self, button):
        self.calls.append(("button_up", button))
        self._maybe_fail()

    def key_down(self, name):
        self.calls.append(("key_down", name))
        self._maybe_fail()

    def key_up(self, name):
        self.calls.append(("key_up", name))
        self._maybe_fail()

    def key_press(self, combo):
        self.calls.append(("key_press", combo))
        self._maybe_fail()

    def type_keysym(self, keysym):
        self.calls.append(("type_keysym", keysym))
        self._maybe_fail()

    def cut_text(self, text):
        self.calls.append(("cut_text", text))
        self._maybe_fail()

    def close(self):
        self.closed = True
        self.calls.append(("close",))

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


def settings(**over) -> _config.Settings:
    base = dict(host="desk.example.test", port=5900, password="", password_env="HERMES_VNC_PASSWORD",
                connect_timeout=5.0, call_timeout=5.0, action_pause=0.0, display_name="",
                max_capture_bytes=1_000_000)
    base.update(over)
    return _config.Settings(**base)


def backend_with(fake: FakeSession, **over) -> _backend.VncBackend:
    """A backend already holding *fake* as its session — no socket, no reactor."""
    instance = _backend.VncBackend(permission_mode="standard", settings=settings(**over))
    instance._session = fake
    return instance


# ── protocol helpers ─────────────────────────────────────────────────────────


def test_png_size_reads_the_ihdr():
    assert _backend.png_size(png_bytes(640, 400)) == (640, 400)
    assert _backend.png_size(png_bytes(1, 1)) == (1, 1)


def test_png_size_rejects_a_non_png_framebuffer():
    try:
        _backend.png_size(b"not an image at all........")
    except _backend.VncError as exc:
        assert "PNG" in str(exc)
    else:
        raise AssertionError("a non-PNG framebuffer must be rejected")


def test_normalize_combo_maps_hermes_spellings_to_vncdotool():
    assert _backend.normalize_combo("cmd+s") == "super-s"
    assert _backend.normalize_combo("ctrl+alt+t") == "ctrl-alt-t"
    assert _backend.normalize_combo("Ctrl - Shift - S") == "ctrl-shift-s"
    assert _backend.normalize_combo("escape") == "esc"
    assert _backend.normalize_combo("pagedown") == "pgdn"
    assert _backend.normalize_combo("return") == "enter"
    assert _backend.normalize_combo("f5") == "f5"
    assert _backend.normalize_combo("") == ""


def test_modifier_names_dedupes_and_aliases():
    assert _backend.modifier_names(["cmd", "command", "shift"]) == ["super", "shift"]
    assert _backend.modifier_names(None) == []


# ── negative first: nothing configured means nothing happens ─────────────────


def test_unconfigured_target_never_connects_and_reports_cleanly():
    empty = settings(host="")
    assert _backend.VncBackend(permission_mode="standard", settings=empty).is_available() is False

    attempted = []

    def explode(_settings):
        attempted.append(_settings)
        raise AssertionError("open_session() must not be reached without a host")

    real = _session.open_session
    _session.open_session = explode
    try:
        instance = _backend.VncBackend(permission_mode="standard", settings=empty)
        try:
            instance.start()
        except _backend.VncError as exc:
            assert "no VNC target configured" in str(exc)
        else:
            raise AssertionError("start() without a host must raise VncError")

        result = instance.click(x=10, y=20)
        assert result.ok is False and result.code == "transport_error", result
        try:
            instance.capture()
        except _backend.VncError as exc:
            assert "no VNC target configured" in str(exc)
        else:
            raise AssertionError("capture() without a host must raise VncError")
    finally:
        _session.open_session = real

    assert attempted == [], "no connection attempt may be made without a host"


def test_transport_failure_is_a_failed_action_not_an_exception():
    fake = FakeSession(fail=ConnectionResetError("server went away"))
    instance = backend_with(fake)
    for result in (instance.click(x=1, y=2), instance.drag(from_xy=(0, 0), to_xy=(5, 5)),
                   instance.scroll(direction="down", amount=2), instance.type_text("hi"),
                   instance.key("enter")):
        assert result.ok is False, result
        assert result.code == "transport_error", result
        assert "ConnectionResetError" in result.message, result


# ── capture: the vision lane, at native size ─────────────────────────────────


def test_capture_returns_a_vision_frame_at_native_pixel_size():
    fake = FakeSession(png=png_bytes(1024, 768))
    cap = backend_with(fake).capture()
    assert cap.mode == "vision", cap
    assert (cap.width, cap.height) == (1024, 768), cap
    assert cap.elements == [], "VNC has no accessibility tree; elements must stay empty"
    assert base64.b64decode(cap.png_b64) == png_bytes(1024, 768)
    assert cap.image_mime_type == "image/png"
    assert cap.png_bytes_len == len(png_bytes(1024, 768))
    assert "x/y" in cap.note and "no accessibility elements" in cap.note
    assert fake.calls == [("capture_screen", "PNG")], fake.calls


def test_capture_is_always_vision_even_when_som_is_requested():
    cap = backend_with(FakeSession(png=png_bytes(8, 8))).capture(mode="som")
    assert cap.mode == "vision" and cap.elements == []


def test_capture_enforces_the_byte_cap():
    cap_size = len(png_bytes(64, 64))
    instance = backend_with(FakeSession(png=png_bytes(64, 64)), max_capture_bytes=cap_size - 1)
    try:
        instance.capture()
    except _backend.VncError as exc:
        assert "over the" in str(exc) and "max_capture_bytes" in str(exc)
    else:
        raise AssertionError("a screenshot over the cap must be refused")


def test_capture_rejects_an_empty_framebuffer():
    try:
        backend_with(FakeSession(png=b"")).capture()
    except _backend.VncError as exc:
        assert "empty framebuffer" in str(exc)
    else:
        raise AssertionError("an empty framebuffer must be refused")


# ── pointer ──────────────────────────────────────────────────────────────────


def test_click_moves_then_presses_and_reports_the_pixel_path():
    fake = FakeSession(png=png_bytes(4, 4))
    result = backend_with(fake).click(x=120, y=340)
    assert result.ok is True and result.action == "click"
    assert result.path == "vnc_pixel" and result.delivery_mode == "foreground"
    assert fake.calls == [("move", 120, 340), ("press", 1)], fake.calls
    assert "120,340" in result.message


def test_click_honours_button_click_count_and_modifiers():
    fake = FakeSession(png=png_bytes(4, 4))
    backend_with(fake).click(x=5, y=6, button="right", click_count=2, modifiers=["cmd"])
    assert fake.calls == [
        ("move", 5, 6), ("key_down", "super"),
        ("press", 3), ("press", 3), ("key_up", "super"),
    ], fake.calls


def test_click_by_element_is_unsupported_rather_than_guessed():
    fake = FakeSession(png=png_bytes(4, 4))
    result = backend_with(fake).click(element=3)
    assert result.ok is False and result.code == "unsupported_action", result
    assert "x and y" in result.message
    assert fake.calls == [], "an unsupported click must not touch the remote pointer"


def test_click_without_coordinates_is_an_argument_error():
    result = backend_with(FakeSession(png=png_bytes(4, 4))).click()
    assert result.ok is False and result.code == "invalid_arguments", result


def test_drag_presses_moves_and_releases():
    fake = FakeSession(png=png_bytes(4, 4))
    result = backend_with(fake).drag(from_xy=(0, 0), to_xy=(120, 60))
    assert result.ok is True and result.action == "drag"
    assert fake.calls[0] == ("move", 0, 0)
    assert fake.calls[1] == ("button_down", 1)
    assert fake.calls[-1] == ("button_up", 1)
    assert fake.names().count("move") >= 3, fake.calls
    assert fake.calls[-2] == ("move", 120, 60), fake.calls


def test_drag_by_element_is_unsupported():
    result = backend_with(FakeSession(png=png_bytes(4, 4))).drag(from_element=1, to_element=2)
    assert result.ok is False and result.code == "unsupported_action", result


def test_scroll_uses_the_rfb_wheel_buttons():
    for direction, button in (("up", 4), ("down", 5), ("left", 6), ("right", 7)):
        fake = FakeSession(png=png_bytes(4, 4))
        result = backend_with(fake).scroll(direction=direction, amount=3)
        assert result.ok is True, result
        assert fake.calls == [("press", button)] * 3, (direction, fake.calls)


def test_scroll_at_coordinates_moves_first_and_rejects_a_bad_direction():
    fake = FakeSession(png=png_bytes(4, 4))
    backend_with(fake).scroll(direction="down", amount=1, x=9, y=11)
    assert fake.calls == [("move", 9, 11), ("press", 5)], fake.calls

    bad = backend_with(FakeSession(png=png_bytes(4, 4))).scroll(direction="sideways")
    assert bad.ok is False and bad.code == "invalid_arguments", bad


# ── keyboard ─────────────────────────────────────────────────────────────────


def test_type_text_sends_ascii_as_keysyms():
    fake = FakeSession(png=png_bytes(4, 4))
    result = backend_with(fake).type_text("Hi 5\n")
    assert result.ok is True and result.action == "type"
    assert fake.calls == [
        ("type_keysym", ord("H")), ("type_keysym", ord("i")), ("type_keysym", ord(" ")),
        ("type_keysym", ord("5")), ("type_keysym", 0xFF0D),
    ], fake.calls
    assert "as keystrokes" in result.message


def test_type_text_uses_tab_keysym():
    fake = FakeSession(png=png_bytes(4, 4))
    backend_with(fake).type_text("\t")
    assert fake.calls == [("type_keysym", 0xFF09)], fake.calls


def test_type_text_falls_back_to_clipboard_for_latin1_and_says_so():
    fake = FakeSession(png=png_bytes(4, 4))
    result = backend_with(fake).type_text("café")
    assert result.ok is True, result
    assert fake.calls == [("cut_text", "café")], fake.calls
    assert "clipboard" in result.message


def test_type_text_refuses_text_outside_latin1_instead_of_failing_on_the_wire():
    fake = FakeSession(png=png_bytes(4, 4))
    result = backend_with(fake).type_text("привіт")
    assert result.ok is False and result.code == "unsupported_text", result
    assert "Latin-1" in result.message and "Cyrillic" in result.message
    assert fake.calls == [], "refused text must not reach the wire"


def test_type_text_refuses_an_empty_string():
    result = backend_with(FakeSession(png=png_bytes(4, 4))).type_text("")
    assert result.ok is False and result.code == "invalid_arguments", result


def test_key_sends_the_normalized_combo():
    fake = FakeSession(png=png_bytes(4, 4))
    result = backend_with(fake).key("cmd+s")
    assert result.ok is True and fake.calls == [("key_press", "super-s")], fake.calls
    assert "super-s" in result.message

    empty = backend_with(FakeSession(png=png_bytes(4, 4))).key("   ")
    assert empty.ok is False and empty.code == "invalid_arguments", empty


# ── element lanes answer honestly ────────────────────────────────────────────


def test_element_lanes_report_unsupported_and_the_app_list_is_empty():
    instance = backend_with(FakeSession(png=png_bytes(4, 4)))
    assert instance.list_apps() == []
    assert instance.list_windows() == []
    for result in (instance.focus_app("Finder"), instance.set_value("on", element=2)):
        assert result.ok is False and result.code == "unsupported_action", result


# ── lifecycle ────────────────────────────────────────────────────────────────


def test_stop_closes_the_session_and_clears_it():
    fake = FakeSession(png=png_bytes(4, 4))
    instance = backend_with(fake)
    instance.stop()
    assert fake.closed is True and ("close",) in fake.calls, fake.calls
    assert instance._session is None


def test_reachability_probe_is_swappable_and_never_raises():
    real = _backend.reachable
    try:
        _backend.reachable = lambda host, port, timeout=2.0: False
        assert _backend.VncBackend(permission_mode="standard", settings=settings()).is_available() is False
        _backend.reachable = lambda host, port, timeout=2.0: True
        assert _backend.VncBackend(permission_mode="standard", settings=settings()).is_available() is True
        assert _backend.VncBackend(permission_mode="standard",
                                   settings=settings(host="")).is_available() is False
    finally:
        _backend.reachable = real


# ── provider wiring ──────────────────────────────────────────────────────────


class FakeCtx:
    """Mirrors the collector the computer-use loader uses."""

    def __init__(self) -> None:
        self.provider = None

    def register_computer_use_provider(self, provider) -> None:
        self.provider = provider


def test_register_exposes_a_provider_named_after_the_plugin_directory():
    ctx = FakeCtx()
    pkg.register(ctx)
    assert ctx.provider is not None
    assert ctx.provider.name == _PKG_DIR.name, (ctx.provider.name, _PKG_DIR.name)
    assert ctx.provider.name == pkg.PROVIDER_NAME
    instance = ctx.provider.create_backend(permission_mode="standard")
    assert isinstance(instance, _backend.VncBackend)
    assert instance.permission_mode == "standard"


def test_register_is_a_no_op_on_a_context_without_the_computer_use_api():
    class BareCtx:
        pass

    pkg.register(BareCtx())  # must not raise: a generic plugin probe gets this context


def test_provider_is_unavailable_without_a_target_and_never_raises():
    provider = pkg.VncProvider()
    real = _config.load_settings
    try:
        _config.load_settings = lambda: settings(host="")
        assert provider.is_available() is False
        _config.load_settings = lambda: settings(host="127.0.0.1", port=1)
        assert provider.is_available() is False  # nothing listens on port 1
    finally:
        _config.load_settings = real


# ── config resolution ────────────────────────────────────────────────────────


def test_settings_target_and_label():
    assert settings().target == "desk.example.test::5900"
    assert settings().label == "desk.example.test:5900"
    assert settings(host="", display_name="lab").target == ""
    assert settings(host="", display_name="lab").label == "lab"
    assert settings(host="").label == "no target"


def test_config_block_is_read_and_env_overrides_win():
    real_block, real_load = _config._raw_block, _config.read_secret
    try:
        _config._raw_block = lambda: {"host": "from-config", "port": 5901, "display_name": "lab",
                                      "action_pause": 0, "password_env": "LAB_VNC_PASSWORD",
                                      "call_timeout": 12.5}
        _config.read_secret = lambda name: f"secret-for-{name}"
        resolved = _config.load_settings()
        assert resolved.host == "from-config" and resolved.port == 5901
        assert resolved.display_name == "lab" and resolved.password == "secret-for-LAB_VNC_PASSWORD"
        assert resolved.call_timeout == 12.5

        os.environ[_config.ENV_HOST] = "from-env"
        os.environ[_config.ENV_PORT] = "5999"
        try:
            overridden = _config.load_settings()
            assert overridden.host == "from-env" and overridden.port == 5999
        finally:
            del os.environ[_config.ENV_HOST]
            del os.environ[_config.ENV_PORT]
    finally:
        _config._raw_block, _config.read_secret = real_block, real_load


def test_settings_tolerates_a_missing_or_broken_block():
    real_block = _config._raw_block
    try:
        _config._raw_block = lambda: {}
        assert _config.load_settings().host == ""
        _config._raw_block = lambda: {"host": "h", "port": "not-a-number", "connect_timeout": -3,
                                      "call_timeout": -1}
        fallback = _config.load_settings()
        assert fallback.port == _config.DEFAULT_PORT
        assert fallback.connect_timeout == _config.DEFAULT_CONNECT_TIMEOUT
        assert fallback.call_timeout == _config.DEFAULT_CALL_TIMEOUT
    finally:
        _config._raw_block = real_block


def test_read_secret_prefers_the_scope_then_the_environment_and_never_raises():
    import agent.secret_scope as scope

    real_get, real_read = scope.get_secret, _config.read_secret
    try:
        scope.get_secret = lambda name, default=None: "from-scope"
        assert real_read("ANY_NAME") == "from-scope"

        scope.get_secret = lambda name, default=None: None
        os.environ["HERMES_VNC_TEST_SECRET"] = "from-env"
        try:
            assert real_read("HERMES_VNC_TEST_SECRET") == "from-env"
        finally:
            del os.environ["HERMES_VNC_TEST_SECRET"]

        def _boom(name, default=None):
            raise RuntimeError("multiplexed scope refused an unscoped read")

        scope.get_secret = _boom
        assert real_read("ANY_NAME") == ""
        assert real_read("") == ""
    finally:
        scope.get_secret = real_get
        _config.read_secret = real_read


# ── runner (works with or without pytest) ────────────────────────────────────


def _main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - the runner reports everything
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
