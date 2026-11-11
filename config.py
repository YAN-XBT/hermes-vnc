"""Settings for the ``hermes-vnc`` computer-use backend.

Read from the active profile's ``config.yaml`` under ``computer_use.vnc`` — the same file
and the same read route the core provider registry uses for ``computer_use.backend``.
Environment variables override the file, so an operator can point the backend at another
host without editing config:

* ``HERMES_VNC_HOST`` — target host or IP
* ``HERMES_VNC_PORT`` — target port (default 5900)
* ``HERMES_VNC_PASSWORD_ENV`` — name of the variable holding the VNC password

The password itself is never read from ``config.yaml``. It is resolved through Hermes'
secret scope (``agent.secret_scope.get_secret``) under the active profile, so a
multiplexed gateway never picks up another profile's value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

CONFIG_SECTION = "computer_use"
CONFIG_BLOCK = "vnc"

ENV_HOST = "HERMES_VNC_HOST"
ENV_PORT = "HERMES_VNC_PORT"
ENV_PASSWORD_ENV = "HERMES_VNC_PASSWORD_ENV"

DEFAULT_PORT = 5900
DEFAULT_PASSWORD_ENV = "HERMES_VNC_PASSWORD"
DEFAULT_CONNECT_TIMEOUT = 20.0
DEFAULT_CALL_TIMEOUT = 60.0
DEFAULT_ACTION_PAUSE = 0.12
DEFAULT_MAX_CAPTURE_BYTES = 12_000_000

#: Keys read out of ``computer_use.vnc``. Anything else in the block is ignored.
SETTING_KEYS = (
    "host",
    "port",
    "password_env",
    "connect_timeout",
    "call_timeout",
    "action_pause",
    "display_name",
    "max_capture_bytes",
)


@dataclass(frozen=True)
class Settings:
    """Resolved connection settings for one VNC target."""

    host: str = ""
    port: int = DEFAULT_PORT
    password: str = ""
    password_env: str = DEFAULT_PASSWORD_ENV
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    call_timeout: float = DEFAULT_CALL_TIMEOUT
    action_pause: float = DEFAULT_ACTION_PAUSE
    display_name: str = ""
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES

    @property
    def target(self) -> str:
        """``host::port``, the form vncdotool parses."""
        return f"{self.host}::{int(self.port)}" if self.host else ""

    @property
    def label(self) -> str:
        """Short human label used in tool messages."""
        if self.display_name:
            return self.display_name
        return f"{self.host}:{int(self.port)}" if self.host else "no target"


def _raw_block() -> dict:
    """The ``computer_use.vnc`` mapping, or ``{}`` when absent/unreadable."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
    except Exception:
        return {}
    section = cfg.get(CONFIG_SECTION) if isinstance(cfg, dict) else None
    block = section.get(CONFIG_BLOCK) if isinstance(section, dict) else None
    return block if isinstance(block, dict) else {}


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_port(value, default: int) -> int:
    """A TCP port must be 1-65535; anything else falls back to the default."""
    port = _as_int(value, default)
    return port if 1 <= port <= 65535 else default


def _as_positive_int(value, default: int) -> int:
    size = _as_int(value, default)
    return size if size > 0 else default


def _as_float(value, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out >= 0 else default


def read_secret(name: str) -> str:
    """Resolve a credential by variable name through Hermes' secret scope.

    Falls back to the process environment only when the scope has no value for it, and
    degrades to an empty string rather than raising: a missing password is a connection
    error the backend reports, not an import-time crash.
    """
    name = (name or "").strip()
    if not name:
        return ""
    value = None
    try:
        from agent.secret_scope import get_secret

        value = get_secret(name, None)
    except Exception:
        value = None
    if value is None:
        value = os.environ.get(name)
    return (value or "").strip()


def load_settings() -> Settings:
    """Merge ``computer_use.vnc`` with the environment overrides."""
    block = _raw_block()

    host = str(block.get("host") or "").strip()
    port = _as_port(block.get("port"), DEFAULT_PORT)
    password_env = str(block.get("password_env") or "").strip() or DEFAULT_PASSWORD_ENV
    connect_timeout = _as_float(block.get("connect_timeout"), DEFAULT_CONNECT_TIMEOUT)
    call_timeout = _as_float(block.get("call_timeout"), DEFAULT_CALL_TIMEOUT)
    action_pause = _as_float(block.get("action_pause"), DEFAULT_ACTION_PAUSE)
    display_name = str(block.get("display_name") or "").strip()
    max_capture_bytes = _as_positive_int(block.get("max_capture_bytes"), DEFAULT_MAX_CAPTURE_BYTES)

    env_host = os.environ.get(ENV_HOST)
    if env_host:
        host = env_host.strip()
    env_port = os.environ.get(ENV_PORT)
    if env_port:
        port = _as_int(env_port, port)
    env_password_env = os.environ.get(ENV_PASSWORD_ENV)
    if env_password_env:
        password_env = env_password_env.strip() or password_env

    return Settings(
        host=host,
        port=port,
        password=read_secret(password_env),
        password_env=password_env,
        connect_timeout=connect_timeout,
        call_timeout=call_timeout,
        action_pause=action_pause,
        display_name=display_name,
        max_capture_bytes=max_capture_bytes,
    )
