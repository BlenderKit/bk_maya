"""Asset download orchestrator (Maya side).

Coordinates the whole pipeline:

1. Pick the right download URL / resolution from the asset's ``files`` list.
2. Spawn Blender headless via :class:`bk_maya.core.blender_runner.BlenderJob`
   to fetch the .blend and export it to a USD (.usd) file with
   UsdPreviewSurface materials + texture references.
3. Stream progress to the placement-locator gizmo (``downloadProgress``
   attribute) and update its enum ``downloadState`` along the way.
4. On success, import the .usd into the current Maya scene at the requested
   location / rotation and remove the gizmo.

Public entry point: :func:`download_asset`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from typing import Any, Sequence

try:
    import maya.cmds as cmds  # type: ignore[import-not-found]
except ImportError:  # for unit tests outside Maya
    cmds = None  # type: ignore[assignment]

from . import auth
from .blender_runner import (
    BlenderJob,
    MIN_BLENDER_MAJOR,
    find_blender_executable,
    query_blender_version,
    version_meets_min,
)
from .prefs import prefs

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Active jobs registry (keeps QObjects alive)
# ---------------------------------------------------------------------------

_active_jobs: list["_DownloadController"] = []


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class _DownloadController:
    """One-shot controller for a single asset download."""

    def __init__(
        self,
        asset: dict[str, Any],
        location: Sequence[float],
        rotation_y: float,
        locator_name: str = "",
    ) -> None:
        self.asset        = asset
        self.location     = tuple(location)
        self.rotation_y   = float(rotation_y)
        self.locator_name = locator_name
        self.job          = BlenderJob()
        self.work_dir     = ""
        self.args_path    = ""
        self.out_usd      = ""

    # ------------------------------------------------------------------
    def start(self) -> bool:
        exe = find_blender_executable()
        if not exe:
            msg = (
                "Blender executable not found. Open Settings → Files and set "
                "the path to blender.exe (Blender 5.0 or newer is required)."
            )
            log.error("[BK download] %s", msg)
            self._set_locator_state("idle")
            self._notify_ui(msg)
            return False

        version = query_blender_version(exe)
        if version is None:
            msg = (
                f"Could not determine Blender version of {exe!r}. "
                f"Check the path in Settings → Files."
            )
            log.error("[BK download] %s", msg)
            self._set_locator_state("idle")
            self._notify_ui(msg)
            return False
        if not version_meets_min(version):
            v = ".".join(str(x) for x in version)
            msg = (
                f"Blender {v} is too old. BlenderKit for Maya requires Blender "
                f"{MIN_BLENDER_MAJOR}.0 or newer. Update the path in Settings → Files."
            )
            log.error("[BK download] %s", msg)
            self._set_locator_state("idle")
            self._notify_ui(msg)
            return False
        log.info("[BK download] using Blender %s at %s", ".".join(str(x) for x in version), exe)

        # Prepare working files
        self.work_dir = tempfile.mkdtemp(prefix="bk_maya_dl_")
        asset_id = self.asset.get("id") or self.asset.get("assetBaseId") or "asset"
        self.out_usd = os.path.join(self.work_dir, f"{asset_id}.usd")

        args = {
            "asset_data":     self.asset,
            "max_resolution": prefs.max_resolution,
            "out_usd":        self.out_usd,
            "api_key":        auth.get_api_key(),
            "work_dir":       self.work_dir,
        }
        self.args_path = os.path.join(self.work_dir, "args.json")
        with open(self.args_path, "w", encoding="utf-8") as fh:
            json.dump(args, fh)

        # Wire up signals
        self.job.progress.connect(self._on_progress)
        self.job.status.connect(self._on_status)
        self.job.finished.connect(self._on_finished)
        self.job.failed.connect(self._on_failed)
        self.job.log_line.connect(self._on_log_line)

        script_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "scripts", "bg_download.py",
        )
        self._set_locator_state("downloading")
        self._set_locator_progress(0.0)
        # Seed label: asset name + initial step.
        asset_name = str(self.asset.get("name") or self.asset.get("displayName") or "asset")
        self._set_locator_label(name=asset_name, status="Starting…")
        return self.job.start(script_path, [self.args_path], blender_exe=exe)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------
    def _on_progress(self, frac: float, msg: str) -> None:
        log.debug("[BK download] %.0f%% %s", frac * 100, msg)
        self._set_locator_progress(frac)
        # Show "<step> 42%" inside the gizmo
        step = (msg or "Downloading").strip()
        self._set_locator_label(status=f"{step}  {int(round(frac * 100))}%")

    def _on_status(self, s: str) -> None:
        log.info("[BK download] %s", s)
        if s:
            self._set_locator_label(status=s.strip())

    def _on_log_line(self, line: str) -> None:
        log.debug("[BK blender] %s", line)

    def _on_failed(self, msg: str) -> None:
        log.error("[BK download] FAILED: %s", msg)
        self._set_locator_state("idle")
        self._delete_locator()
        self._cleanup()
        self._notify_ui(f"Download failed: {msg}")

    @staticmethod
    def _notify_ui(message: str) -> None:
        try:
            from ..ui.asset_bar import notify_error
            notify_error(message)
        except Exception:  # noqa: BLE001
            pass

    def _on_finished(self, out_path: str) -> None:
        log.info("[BK download] usd ready: %s", out_path)
        try:
            self._import_usd(out_path)
            self._set_locator_state("done")
            self._delete_locator()
        except Exception as exc:
            log.exception("[BK download] import failed: %s", exc)
            self._set_locator_state("idle")
        finally:
            self._cleanup()

    # ------------------------------------------------------------------
    # Maya integration
    # ------------------------------------------------------------------
    def _set_locator_state(self, state: str) -> None:
        if not (cmds and self.locator_name and cmds.objExists(self.locator_name)):
            return
        mapping = {"idle": 0, "downloading": 1, "done": 2}
        try:
            cmds.setAttr(f"{self.locator_name}.downloadState", mapping.get(state, 0))
        except Exception as exc:
            log.debug("setAttr downloadState failed: %s", exc)

    def _set_locator_progress(self, frac: float) -> None:
        if not (cmds and self.locator_name and cmds.objExists(self.locator_name)):
            return
        attr = f"{self.locator_name}.downloadProgress"
        try:
            if cmds.attributeQuery("downloadProgress", node=self.locator_name, exists=True):
                cmds.setAttr(attr, max(0.0, min(1.0, float(frac))))
        except Exception as exc:
            log.debug("setAttr downloadProgress failed: %s", exc)

    def _set_locator_label(self, name: str | None = None,
                            status: str | None = None) -> None:
        if not self.locator_name:
            return
        try:
            from ..plugins import placement_locator as plc
            plc.set_label(self.locator_name, name=name, status=status)
        except Exception as exc:  # noqa: BLE001
            log.debug("set_label failed: %s", exc)

    def _delete_locator(self) -> None:
        # Always clear proxor registry first, even if the node is gone.
        try:
            from ..plugins import placement_locator as plc
            plc.clear_proxor_lines(self.locator_name or "")
            plc.clear_label(self.locator_name or "")
        except Exception:  # noqa: BLE001
            pass
        if not (cmds and self.locator_name and cmds.objExists(self.locator_name)):
            return
        try:
            # The shape's transform is the parent — delete it cleanly.
            parents = cmds.listRelatives(self.locator_name, parent=True, fullPath=True) or []
            target = parents[0] if parents else self.locator_name
            cmds.delete(target)
        except Exception as exc:
            log.debug("delete locator failed: %s", exc)

    def _import_usd(self, usd_path: str) -> None:
        """Import the exported USD into the current Maya scene.

        Maya 2027 ships ``mayaUsdPlugin`` which registers the ``USD Import``
        translator and the ``mayaUSDImport`` command.  We prefer the
        dedicated command because it exposes ``shadingMode`` / material
        options directly; ``cmds.file`` is a fallback for unusual setups.
        """
        if not cmds:
            return
        if not os.path.isfile(usd_path):
            raise FileNotFoundError(usd_path)

        self._ensure_usd_plugin()

        before = set(cmds.ls(assemblies=True) or [])

        imported_via_command = False
        try:
            cmds.mayaUSDImport(
                file=usd_path,
                readAnimData=False,
                shadingMode=[("useRegistry", "UsdPreviewSurface")],
                preferredMaterial="standardSurface",
                importInstances=True,
            )
            imported_via_command = True
        except Exception as exc:  # noqa: BLE001 — fall back to cmds.file
            log.debug("mayaUSDImport unavailable (%s); falling back to cmds.file", exc)

        if not imported_via_command:
            type_candidates = ["USD Import", "usdImport", "USD", None]
            last_exc: Exception | None = None
            for t in type_candidates:
                try:
                    kw = dict(i=True, ignoreVersion=True,
                              mergeNamespacesOnClash=False,
                              options="", preserveReferences=True)
                    if t is not None:
                        kw["type"] = t
                    cmds.file(usd_path, **kw)
                    last_exc = None
                    break
                except RuntimeError as exc:
                    last_exc = exc
                    continue
            if last_exc is not None:
                raise RuntimeError(
                    "Could not import .usd — mayaUsdPlugin is not available. "
                    "Enable it via Windows → Settings/Preferences → "
                    f"Plug-in Manager. Last error: {last_exc}"
                )

        after = set(cmds.ls(assemblies=True) or [])
        new_roots = list(after - before)
        if not new_roots:
            log.warning("usd imported but no new top-level node detected.")
            return

        # Group imports under a single transform we can position.
        grp_name = "BK_" + re.sub(
            r"[^A-Za-z0-9_]", "_", self.asset.get("name", "asset")
        )
        grp = cmds.group(new_roots, name=grp_name)
        x, y, z = self.location
        cmds.xform(grp, worldSpace=True, translation=(x, y, z))
        cmds.xform(grp, relative=True, rotation=(0.0, self.rotation_y, 0.0))

    @staticmethod
    def _ensure_usd_plugin() -> None:
        """Load ``mayaUsdPlugin`` if it isn't already."""
        if cmds is None:
            return
        plug = "mayaUsdPlugin"
        try:
            if cmds.pluginInfo(plug, query=True, loaded=True):
                return
        except Exception:  # noqa: BLE001 — plugin not registered yet
            pass
        try:
            cmds.loadPlugin(plug, quiet=True)
            if cmds.pluginInfo(plug, query=True, loaded=True):
                log.info("[BK download] loaded USD plugin: %s", plug)
                return
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[BK download] could not load mayaUsdPlugin (%s); USD import "
                "may fail. Enable it in the Plug-in Manager.",
                exc,
            )

    # ------------------------------------------------------------------
    def _cleanup(self) -> None:
        if self in _active_jobs:
            _active_jobs.remove(self)
        # Leave temp files for now — useful when debugging.  TODO: reap.


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def download_asset(
    asset: dict[str, Any],
    *,
    location: Sequence[float] = (0.0, 0.0, 0.0),
    rotation_y: float = 0.0,
    locator_name: str = "",
) -> None:
    """Kick off an asynchronous download for *asset*.

    The function returns immediately; progress and completion are reported
    on the placement locator (if *locator_name* is given) and on the log.
    """
    if cmds is None:
        log.warning("download_asset called outside Maya — ignored.")
        return

    ctrl = _DownloadController(asset, location, rotation_y, locator_name)
    _active_jobs.append(ctrl)
    if not ctrl.start():
        # start() already logged + reset locator
        if ctrl in _active_jobs:
            _active_jobs.remove(ctrl)
