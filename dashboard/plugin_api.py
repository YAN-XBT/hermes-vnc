"""Backend routes for the desktop live view — mounted at ``/api/plugins/hermes-vnc/``.

The desktop half (`desktop/plugin.js`) polls ``/frame`` a few times a second and paints the
result. Going through the gateway's own plugin namespace rather than opening a port keeps this
to zero extra listeners: nothing new is bound, and the pane talks to the app the same way every
other panel does.
"""

from __future__ import annotations

import base64
import importlib
import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

router = APIRouter()

PLUGIN_DIR = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "hermes_vnc_live_view"


def _plugin_package():
    """The plugin package the loader already imported, else load it from its own path.

    Reusing the loaded module matters: it is the same config and the same session code the
    agent's tool path uses, so the pane and the tool can never disagree about the target.
    """
    target = (PLUGIN_DIR / "__init__.py").resolve()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not path:
            continue
        try:
            if Path(path).resolve() == target:
                return module
        except OSError:
            continue

    spec = importlib.util.spec_from_file_location(
        _PACKAGE_NAME, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _live_view():
    package = _plugin_package()
    return importlib.import_module(package.__name__ + ".live_view")


@router.get("/status")
async def status():
    """Cheap probe the pane can call before it starts pulling frames."""
    try:
        settings = _live_view_settings()
        return {
            "ok": True,
            "target": f"{settings.host}:{settings.port}" if settings.host else "",
            "configured": bool(settings.host),
        }
    except Exception as exc:  # plugin contract: never raise out of the handler
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _live_view_settings():
    from importlib import import_module

    package = _plugin_package()
    config = import_module(package.__name__ + ".config")
    return config.load_settings()


@router.get("/frame")
async def frame(max_width: int = 960, quality: int = 62):
    """One downscaled JPEG frame of the remote screen, base64 for the pane to paint."""
    try:
        live_view = _live_view()
        jpeg, width, height = await run_in_threadpool(
            lambda: live_view.grab_jpeg(max_width=max_width, quality=quality)
        )
        return {
            "ok": True,
            "width": width,
            "height": height,
            "jpeg": base64.b64encode(jpeg).decode("ascii"),
        }
    except Exception as exc:  # plugin contract: never raise out of the handler
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
