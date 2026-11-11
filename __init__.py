"""hermes-vnc — a computer-use backend that drives a machine reachable over VNC (RFB).

Hermes loads exactly one computer-use provider per profile: ``computer_use.backend`` names
the plugin directory to import, so this module is only ever imported when it is selected.
"""

from __future__ import annotations

import logging
import socket


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
        """A target is configured and its VNC port accepts a connection right now."""
        settings = _config.load_settings()
        if not settings.host:
            logger.debug("%s: no target configured (computer_use.vnc.host)", PROVIDER_NAME)
            return False
        try:
            with socket.create_connection((settings.host, int(settings.port)), timeout=2.0):
                return True
        except OSError as exc:
            logger.debug("%s: %s:%s did not answer (%s)", PROVIDER_NAME, settings.host, settings.port, exc)
            return False


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
