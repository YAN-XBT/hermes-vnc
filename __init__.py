"""hermes-vnc — a computer-use backend that drives a machine reachable over VNC (RFB).

Hermes loads exactly one computer-use provider per profile: ``computer_use.backend`` names
the plugin directory to import, so this module is only ever imported when it is selected.
"""

from __future__ import annotations

import logging


from tools.computer_use.backend import ComputerUseBackend, ComputerUseProvider

from . import config as _config

logger = logging.getLogger(__name__)

#: Must match the plugin directory name: that is how ``computer_use.backend`` addresses it.
PROVIDER_NAME = "hermes-vnc"


class VncProvider(ComputerUseProvider):
    """Factory for :class:`backend.VncBackend`."""

    name = PROVIDER_NAME
    display_name = "VNC (remote desktop)"
    description = "Drive a machine over VNC (RFB): capture its screen, move its pointer and keyboard."

    def create_backend(self, *, permission_mode: str) -> ComputerUseBackend:
        from .backend import VncBackend

        return VncBackend(permission_mode=permission_mode)

    def is_available(self) -> bool:
        """A target is configured and the RFB client is importable.

        Deliberately local and cheap: Hermes calls this on every paint of the provider
        picker and from ``hermes setup``, so it must not touch the network. Whether the
        target actually answers is what :meth:`doctor` is for.
        """
        if not _config.load_settings().host:
            return False
        try:
            import vncdotool.client  # noqa: F401
        except Exception:
            logger.debug("%s: vncdotool is not importable", PROVIDER_NAME)
            return False
        return True

    def doctor(self) -> int:
        """Reachability report for ``hermes computer-use doctor``. 0 = usable."""
        settings = _config.load_settings()
        print(f"{PROVIDER_NAME}: target {settings.label}")

        try:
            import vncdotool.client  # noqa: F401

            print("  dependency : vncdotool importable")
        except Exception as exc:
            print(f"  dependency : MISSING ({exc})")
            print("               install it with pip install \"vncdotool>=1.2,<2\"")
            return 1

        if not settings.host:
            print("  target     : not configured — set computer_use.vnc.host (or HERMES_VNC_HOST)")
            return 1

        from .backend import reachable

        if not reachable(settings.host, settings.port):
            print(f"  reachable  : NO — nothing accepted a TCP connection on "
                  f"{settings.host}:{settings.port}")
            print("               is the VNC server running on the target, and is the port open?")
            return 1

        print("  reachable  : yes")
        print(f"  password   : {'from ' + settings.password_env if settings.password else 'NOT SET'}"
              f" ({'found' if settings.password else 'missing — the server will refuse the connection'})")
        return 0


def register(ctx) -> None:
    """Register the provider when this build exposes the computer-use provider API.

    Hermes imports this directory only when ``computer_use.backend`` names it, and the
    computer-use loader hands ``register`` a collector that carries
    ``register_computer_use_provider``. A build (or a generic plugin probe) whose context
    lacks that method is not a computer-use host, so this is a no-op rather than an error —
    raising here would make an unrelated surface report the plugin as broken.
    """
    register_provider = getattr(ctx, "register_computer_use_provider", None)
    if register_provider is None:
        logger.debug("%s: this Hermes build has no computer-use provider API; nothing to register",
                     PROVIDER_NAME)
        return
    register_provider(VncProvider())
