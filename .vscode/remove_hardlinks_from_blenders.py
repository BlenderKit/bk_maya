"""Utility script to remove the dev junctions/symlinks created by build_and_hardlink_addon_to_blenders.py.

It removes the `blenderkit_dev_hl` entry from the Maya modules directory
only when it is a junction/symlink pointing back to this repo.
The intent is to avoid touching real installs or differently linked copies.
"""

import os
import shutil
import sys

THIS_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..")).replace("\\", "/")

if sys.platform == "win32":
    MAYA_MODULES_PATH = os.path.expanduser("~/Documents/maya/modules").replace("\\", "/")
elif sys.platform == "darwin":
    MAYA_MODULES_PATH = os.path.expanduser("~/Library/Preferences/Autodesk/maya/modules")
else:
    MAYA_MODULES_PATH = os.path.expanduser("~/maya/modules")

RESULTING_ADDON_NAME = "blenderkit_dev_hl"


def _remove_existing(path: str) -> None:
    """Remove existing file/dir/link at path safely."""
    if not os.path.lexists(path):
        return
    if os.path.islink(path):
        os.unlink(path)
        return
    if os.path.isdir(path):
        try:
            os.rmdir(path)
        except OSError:
            shutil.rmtree(path, ignore_errors=True)
        return
    os.remove(path)


def _points_to_repo(path: str, repo: str) -> bool:
    try:
        return os.path.samefile(path, repo)
    except FileNotFoundError:
        return False
    except OSError:
        return False


removed = []
skipped = []

addon_path = os.path.join(MAYA_MODULES_PATH, RESULTING_ADDON_NAME).replace("\\", "/")
if not os.path.lexists(addon_path):
    skipped.append((addon_path, "missing"))
elif _points_to_repo(addon_path, THIS_REPO):
    print(f"Removing Maya addon junction at {addon_path}")
    try:
        _remove_existing(addon_path)
        removed.append(addon_path)
    except Exception as exc:
        skipped.append((addon_path, f"failed to remove: {exc}"))
else:
    skipped.append((addon_path, "not linked to this repo; skipping"))

if removed:
    print("\nRemoved junctions:")
    for path in removed:
        print(f"  - {path}")
else:
    print("No junctions matching this repo were removed.")

if skipped:
    print("\nSkipped entries:")
    for path, reason in skipped:
        print(f"  - {path}: {reason}")

sys.exit(0)
