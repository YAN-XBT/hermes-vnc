"""One RFB session, driven synchronously from the backend's thread.

This is the only module that knows about ``vncdotool`` and Twisted. Everything above it
talks to the small method set on :class:`RfbSession`.

Why not ``vncdotool.api.connect``: that helper is documented as EXPERIMENTAL, and on
vncdotool 1.4.2 its threaded proxy resolves the live protocol for the first call only —
every later call reaches a ``None`` protocol and dies with
``'NoneType' object has no attribute 'transport'``. Measured with a scripted RFB server:
``keyEvent(down=True)`` succeeded and ``keyEvent(down=False)`` on the same connection
failed. So this module starts the reactor itself, keeps the protocol instance the factory
hands back, and marshals each call onto the reactor thread with its own completion event.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any, Callable, Optional

from . import config as _config

logger = logging.getLogger(__name__)

_reactor_lock = threading.Lock()


class VncError(RuntimeError):
    """A transport-level failure, reported to the caller as a failed action."""


def _imports():
    """Import the protocol pieces, turning a missing dependency into a clear error."""
    try:
        from twisted.internet import reactor  # type: ignore
        from vncdotool.client import VNCDoToolFactory, factory_connect  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on the host environment
        raise VncError(
            "vncdotool is not installed in the Hermes environment; install it with "
            "`pip install \"vncdotool>=1.2,<2\"` or reinstall this plugin with --yes-deps"
        ) from exc
    return reactor, VNCDoToolFactory, factory_connect


def ensure_reactor() -> Any:
    """Start Twisted's reactor on a daemon thread once per process, and return it.

    The reactor is process-wide and cannot be restarted, so this is idempotent and nothing
    ever stops it: a Hermes process that opens and closes many sessions keeps using the one
    reactor.
    """
    reactor, _, _ = _imports()
    if reactor.running:
        return reactor
    with _reactor_lock:
        if not reactor.running:
            thread = threading.Thread(target=reactor.run, name="hermes-vnc reactor",
                                      kwargs={"installSignalHandlers": False}, daemon=True)
            thread.start()
    return reactor


class RfbSession:
    """A live RFB connection with synchronous, thread-safe operations."""

    def __init__(self, settings: _config.Settings) -> None:
        self.settings = settings
        self._reactor = None
        self._factory = None
        self._protocol = None
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- lifecycle

    def open(self) -> None:
        reactor, factory_class, factory_connect = _imports()
        reactor = ensure_reactor()
        with self._lock:
            if self._protocol is not None:
                return
            factory = factory_class()
            if self.settings.password:
                factory.password = self.settings.password

            connected = threading.Event()
            outcome: dict[str, Any] = {}

            def _on_connect(protocol):
                outcome["protocol"] = protocol
                connected.set()
                return protocol

            def _on_failure(reason):
                outcome["failure"] = reason
                connected.set()
                return None

            factory.deferred.addCallbacks(_on_connect, _on_failure)
            reactor.callFromThread(factory_connect, factory, self.settings.host,
                                   int(self.settings.port), socket.AF_INET)
            if not connected.wait(self.settings.connect_timeout):
                raise VncError(
                    f"timed out after {self.settings.connect_timeout:.0f}s connecting to "
                    f"{self.settings.label}"
                )
            protocol = outcome.get("protocol")
            if protocol is None:
                raise VncError(f"could not connect to {self.settings.label}: {outcome.get('failure')}")
            self._reactor, self._factory, self._protocol = reactor, factory, protocol

    def close(self) -> None:
        with self._lock:
            protocol, self._protocol = self._protocol, None
            self._factory = None
        if protocol is None or self._reactor is None:
            return
        reactor = self._reactor

        def _lose():
            try:
                protocol.transport.loseConnection()
            except Exception:
                logger.debug("hermes-vnc: losing the connection failed", exc_info=True)

        try:
            reactor.callFromThread(_lose)
        except Exception:
            logger.debug("hermes-vnc: the reactor was gone before close", exc_info=True)

    @property
    def connected(self) -> bool:
        return self._protocol is not None

    # ---------------------------------------------------------------- plumbing

    def _call(self, method: str, *args, **kwargs) -> Any:
        """Run ``protocol.<method>(*args)`` on the reactor thread and wait for its result.

        A method that returns a Twisted ``Deferred`` (``captureScreen`` does) is awaited too,
        so the caller sees the completed write rather than the request.
        """
        if self._protocol is None:
            raise VncError("the VNC session is not open")
        from twisted.internet.defer import Deferred  # type: ignore

        protocol = self._protocol
        done = threading.Event()
        box: dict[str, Any] = {}

        def _finish(value: Any = None) -> Any:
            box["value"] = value
            done.set()
            return value

        def _fail(failure: Any) -> None:
            box["error"] = getattr(failure, "value", failure)
            done.set()

        def _run() -> None:
            try:
                result = getattr(protocol, method)(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
                box["error"] = exc
                done.set()
                return
            if isinstance(result, Deferred):
                result.addCallbacks(_finish, _fail)
            else:
                _finish(result)

        try:
            self._reactor.callFromThread(_run)
        except Exception as exc:
            raise VncError(f"the reactor is not running: {exc}") from exc

        if not done.wait(self.settings.call_timeout):
            raise VncError(f"{method} did not answer within {self.settings.call_timeout:.0f}s")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    # ---------------------------------------------------------------- operations

    def capture_screen(self, buffer, fmt: str = "PNG") -> None:
        self._call("captureScreen", buffer, format=fmt)

    def move(self, x: int, y: int) -> None:
        self._call("mouseMove", int(x), int(y))

    def press(self, button: int) -> None:
        self._call("mousePress", int(button))

    def button_down(self, button: int) -> None:
        self._call("mouseDown", int(button))

    def button_up(self, button: int) -> None:
        self._call("mouseUp", int(button))

    def key_down(self, name: str) -> None:
        self._call("keyDown", name)

    def key_up(self, name: str) -> None:
        self._call("keyUp", name)

    def key_press(self, combo: str) -> None:
        self._call("keyPress", combo)

    def type_keysym(self, keysym: int) -> None:
        self._call("keyEvent", int(keysym), True)
        self._call("keyEvent", int(keysym), False)

    def cut_text(self, text: str) -> None:
        self._call("paste", text)


#: The seam the backend opens sessions through, so tests never touch a socket.
SessionFactory = Callable[[_config.Settings], RfbSession]


def open_session(settings: _config.Settings) -> RfbSession:
    """Open and return a live session for *settings*."""
    session = RfbSession(settings)
    session.open()
    return session
