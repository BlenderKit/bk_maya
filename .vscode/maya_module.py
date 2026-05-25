"""Dev utility: register / unregister this repo as a Maya module.

Usage:
    python .vscode/maya_module.py hardlink   # write .mod files for all installed Maya versions
    python .vscode/maya_module.py remove     # remove those .mod files

How detection works:
    Installed Maya versions are discovered by scanning the Autodesk directory
    in Program Files (Windows), /Applications/Autodesk (macOS), or
    /usr/autodesk (Linux) for folders matching 'Maya<year>'.
    A .mod file is then written to both the version-independent user modules
    dir and each version-specific dir, guaranteeing Maya finds it regardless
    of which entry it prefers.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THIS_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BK_MAYA_DIR = os.path.join(THIS_REPO, "bk_maya")

ADDON_NAME = "blenderkit_dev_hl"

if sys.platform == "win32":
    _AUTODESK_INSTALL_DIR = os.path.join(
        os.environ.get("ProgramFiles", r"C:\Program Files"), "Autodesk"
    )
elif sys.platform == "darwin":
    _AUTODESK_INSTALL_DIR = "/Applications/Autodesk"
else:
    _AUTODESK_INSTALL_DIR = "/usr/autodesk"


def _get_maya_app_dirs() -> list[str]:
    """Return all candidate Maya user app dirs for this OS.

    On Windows, returns BOTH the OneDrive-redirected Documents path AND the
    physical %USERPROFILE%\\Documents path so whichever one Maya reads from
    is covered.  Deduplicates if they happen to be the same.
    """
    if sys.platform == "win32":
        dirs: set[str] = set()
        # Shell-redirected Documents (may be OneDrive\Documents)
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x0005, None, 0, buf)
        if buf.value:
            dirs.add(os.path.join(buf.value, "maya"))
        # Physical %USERPROFILE%\Documents (differs from above when OneDrive is active)
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            dirs.add(os.path.join(userprofile, "Documents", "maya"))
        return sorted(dirs)
    elif sys.platform == "darwin":
        return [os.path.expanduser("~/Library/Preferences/Autodesk/maya")]
    else:
        return [os.path.expanduser("~/maya")]

# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _installed_maya_versions() -> list[str]:
    """Scan the Autodesk install directory for Maya<year> folders.

    Returns a sorted list of year strings, e.g. ['2026', '2027'].
    """
    versions: list[str] = []
    if not os.path.isdir(_AUTODESK_INSTALL_DIR):
        return versions
    for entry in sorted(os.listdir(_AUTODESK_INSTALL_DIR)):
        m = re.match(r"^[Mm]aya(\d{4})", entry)
        if m and os.path.isdir(os.path.join(_AUTODESK_INSTALL_DIR, entry)):
            versions.append(m.group(1))
    return versions


def _all_module_dirs() -> list[str]:
    """Return every Maya user modules directory to write/remove .mod files.

    Covers all candidate app dirs (OneDrive + physical) × (version-independent
    + one dir per installed Maya version).
    """
    dirs: list[str] = []
    for maya_app in _get_maya_app_dirs():
        dirs.append(os.path.join(maya_app, "modules"))
        for ver in _installed_maya_versions():
            dirs.append(os.path.join(maya_app, ver, "modules"))
    return dirs


# ---------------------------------------------------------------------------
# File-system helpers
# ---------------------------------------------------------------------------


def _remove_path(path: str) -> None:
    """Remove a file, symlink, directory, or junction at *path*."""
    if not os.path.lexists(path):
        return
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.isdir(path):
        try:
            os.rmdir(path)          # works for empty dirs and junctions
        except OSError:
            shutil.rmtree(path, ignore_errors=True)
    else:
        os.remove(path)


def _mod_points_here(mod_path: str) -> bool:
    """Return True if the .mod file's base path resolves to THIS_REPO."""
    try:
        with open(mod_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("+"):
                    parts = line.split()
                    if len(parts) >= 4:
                        recorded = os.path.normcase(os.path.normpath(parts[3]))
                        expected = os.path.normcase(os.path.normpath(THIS_REPO))
                        return recorded == expected
    except OSError:
        pass
    return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_hardlink() -> None:
    """Write .mod registration files for all installed Maya versions.

    Also runs `bk_maya/dev.py vendor` to populate bk_maya/lib/ with qtpy.
    """
    # Vendor pure-Python dependencies first.
    dev_script = os.path.join(BK_MAYA_DIR, "dev.py")
    print("Vendoring dependencies into bk_maya/lib/ ...")
    subprocess.run([sys.executable, dev_script, "vendor"], cwd=BK_MAYA_DIR, check=True)

    # Build .mod content.
    # Base = repo root so relative paths can address bk_maya/ subdirs.
    # MAYA_PLUG_IN_PATH points to bk_maya/plugins/ — only maya_plugin.py lives
    # there, so it is the only file shown in Maya's Plug-in Manager.
    # PYTHONPATH+:= . adds the repo root so `import bk_maya` works.
    # PYTHONPATH+:= bk_maya\lib adds the vendored pure-Python packages.
    base = os.path.normpath(THIS_REPO)
    plug_in_path = os.path.join("bk_maya", "plugins")
    lib_path = os.path.join("bk_maya", "lib")
    content = (
        f"+ {ADDON_NAME} 1.0 {base}\n"
        f"MAYA_PLUG_IN_PATH+:= {plug_in_path}\n"
        "PYTHONPATH+:= .\n"
        f"PYTHONPATH+:= {lib_path}\n"
    )

    installed = _installed_maya_versions()
    print(f"\nDetected Maya installs : {installed if installed else '(none found)'}")
    print(f"Module base path       : {base}\n")

    written: list[str] = []
    for modules_dir in _all_module_dirs():
        mod_file = os.path.join(modules_dir, ADDON_NAME + ".mod")

        # Clean up any stale directory junction from an older approach.
        old_junc = os.path.join(modules_dir, ADDON_NAME)
        if os.path.lexists(old_junc) and not os.path.isfile(old_junc):
            print(f"  Removing stale junction : {old_junc}")
            _remove_path(old_junc)

        os.makedirs(modules_dir, exist_ok=True)
        _remove_path(mod_file)

        with open(mod_file, "w", encoding="utf-8") as fh:
            fh.write(content)

        print(f"  Written : {mod_file}")
        written.append(mod_file)

    print(f"\nDone — wrote {len(written)} .mod file(s).")
    print("Restart Maya and enable the plugin in: Windows > Settings/Preferences > Plug-in Manager.")


def cmd_remove() -> None:
    """Remove .mod registration files written by hardlink."""
    installed = _installed_maya_versions()
    print(f"Detected Maya installs : {installed if installed else '(none found)'}\n")

    removed: list[str] = []
    skipped: list[tuple[str, str]] = []

    for modules_dir in _all_module_dirs():
        mod_file = os.path.join(modules_dir, ADDON_NAME + ".mod")

        old_junc = os.path.join(modules_dir, ADDON_NAME)
        if os.path.lexists(old_junc) and not os.path.isfile(old_junc):
            print(f"  Removing stale junction : {old_junc}")
            _remove_path(old_junc)

        if not os.path.lexists(mod_file):
            skipped.append((mod_file, "not found"))
        elif _mod_points_here(mod_file):
            _remove_path(mod_file)
            print(f"  Removed : {mod_file}")
            removed.append(mod_file)
        else:
            skipped.append((mod_file, "base path does not match this repo"))

    print(f"\nRemoved {len(removed)} file(s).")
    if skipped:
        print("Skipped:")
        for path, reason in skipped:
            print(f"  {path}  ({reason})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Maya module dev helper")
    parser.add_argument("command", choices=["hardlink", "remove"])
    args = parser.parse_args()

    if args.command == "hardlink":
        cmd_hardlink()
    else:
        cmd_remove()
