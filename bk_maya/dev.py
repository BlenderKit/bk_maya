# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####
# type: ignore

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

# ── Release channels ──────────────────────────────────────────────────────────
# These mirror bk_maya/_version.py. Keep the strings in sync.
CHANNEL_STABLE = "stable"
CHANNEL_ALPHA = "alpha"
CHANNEL_DEV = "dev"

# ── Client source (see blendkit_client_build / copy_client_binaries) ────────
# The Go client now lives in its own repository, embedded here as the
# ``bk_client`` submodule (see .gitmodules). Its Go sources are at
# ``bk_client/client`` and its version is ``bk_client/client/VERSION``.
#
# Build policy:
#   • ``build``   (local / CI testing) compiles the client from the submodule
#     sources for every platform, so client changes are exercised end-to-end.
#   • ``release`` grabs prebuilt binaries when available — either an explicit
#     ``--client-build <folder>`` of *signed* binaries, or, failing that, the
#     newest ``vX.Y.Z`` folder committed inside the submodule. If neither is
#     present it falls back to building from source, so a release never blocks
#     on missing binaries. In the future a trigger will have the client repo
#     publish signed binaries that ``--client-build`` (or ``$BLENDKIT_CLIENT_BINARIES``)
#     points at.
# The env-var lets CI inject a path without changing the command line.
CLIENT_BINARIES_ENV = "BLENDKIT_CLIENT_BINARIES"

# Location of the bk_client submodule and its Go client sources.
CLIENT_SUBMODULE_DIR = "bk_client"
CLIENT_SRC_DIR = os.path.join(CLIENT_SUBMODULE_DIR, "client")

# Pure-Python packages to vendor into lib/.
# Both qtpy and packaging ship as py3-none-any wheels, so a single download
# covers all platforms (Windows, macOS, Linux) and all architectures.
VENDOR_PACKAGES = [
    "qtpy",
    "packaging",  # required by qtpy
    "requests",  # HTTP client used by core/ and api/
]

# Absolute path to the vendored lib/ directory. Anchored to this file's location
# (this script lives in the ``bk_maya/`` package dir) rather than the current
# working directory, so vendoring always targets ``bk_maya/lib`` no matter where
# the command is invoked from. A cwd-relative path here previously produced a
# stray ``bk_maya/bk_maya/lib`` when run from inside the package folder.
_LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")


def vendor_packages(lib_dir: str, packages: list[str] = VENDOR_PACKAGES) -> None:
    """Download pure-Python wheels and extract them into *lib_dir*.

    Uses ``pip download --no-deps --only-binary=:all:`` so only pre-built
    wheels are accepted. Because qtpy and packaging are pure Python the wheels
    are tagged ``py3-none-any`` and are identical on every platform/arch, so
    one vendoring pass covers Windows, macOS and Linux for both x86_64 and
    arm64.
    """
    print(f"Vendoring {packages} into {lib_dir} ...")
    os.makedirs(lib_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--no-deps",
                "--only-binary=:all:",
                "--dest",
                tmp,
                *packages,
            ],
            check=True,
        )

        for whl_name in sorted(os.listdir(tmp)):
            if not whl_name.endswith(".whl"):
                continue
            whl_path = os.path.join(tmp, whl_name)
            with zipfile.ZipFile(whl_path) as zf:
                for member in zf.namelist():
                    # Skip wheel metadata — we only want importable Python files.
                    if ".dist-info/" in member or member.endswith(".dist-info"):
                        continue
                    zf.extract(member, lib_dir)
            print(f"  Extracted {whl_name}")

    print(f"Vendoring complete: {lib_dir}")


def read_client_version() -> str:
    """Read the client version (e.g. ``1.10.0``) from the submodule VERSION file."""
    version_file = os.path.join(CLIENT_SRC_DIR, "VERSION")
    if not os.path.isfile(version_file):
        print(
            f"error: {version_file} not found. Is the bk_client submodule checked out?\n"
            "        Run: git submodule update --init --recursive"
        )
        exit(1)
    with open(version_file) as f:
        return f.read().strip()


def blendkit_client_build(abs_build_dir: str):
    """Build blendkit-client for all platforms in parallel."""
    client_version = read_client_version()
    build_dir = os.path.join(abs_build_dir, "client")
    builds = [
        {
            "env": {"GOOS": "windows", "GOARCH": "amd64", "CGO_ENABLED": "0"},
            "output": os.path.join(f"v{client_version}", "blenderkit-client-windows-x86_64.exe"),
        },
        {
            "env": {"GOOS": "windows", "GOARCH": "arm64", "CGO_ENABLED": "0"},
            "output": os.path.join(f"v{client_version}", "blenderkit-client-windows-arm64.exe"),
        },
        {
            "env": {"GOOS": "darwin", "GOARCH": "amd64", "CGO_ENABLED": "0"},
            "output": os.path.join(f"v{client_version}", "blenderkit-client-macos-x86_64"),
        },
        {
            "env": {"GOOS": "darwin", "GOARCH": "arm64", "CGO_ENABLED": "0"},
            "output": os.path.join(f"v{client_version}", "blenderkit-client-macos-arm64"),
        },
        {
            "env": {"GOOS": "linux", "GOARCH": "amd64", "CGO_ENABLED": "0"},
            "output": os.path.join(f"v{client_version}", "blenderkit-client-linux-x86_64"),
        },
        {
            "env": {"GOOS": "linux", "GOARCH": "arm64", "CGO_ENABLED": "0"},
            "output": os.path.join(f"v{client_version}", "blenderkit-client-linux-arm64"),
        },
    ]
    ldflags = f"-X main.ClientVersion={client_version}"
    for build in builds:
        build_path = os.path.join(build_dir, build["output"])
        env = {**build["env"], **os.environ}
        process = subprocess.Popen(
            ["go", "build", "-o", build_path, "-ldflags", ldflags, "."],
            env=env,
            cwd=CLIENT_SRC_DIR,
        )
        build["process"] = process

    print(f"Blendkit-Client v{client_version} build started for {len(builds)} platforms.")
    builds_ok = True
    for build in builds:
        build["process"].wait()
        if build["process"].returncode != 0:
            print(f"Client build ({build['env']}) failed")
            builds_ok = False

    if not builds_ok:
        exit(1)
    print(f"Blendkit-Client v{client_version} builds completed.")


def verify_client_binaries(binaries_path: str):
    """Verify client binaries that they were signed correctly.
    - osslsigncode needs to be on PATH (https://github.com/mtrojnar/osslsigncode)
    -
    """
    print("===== VERIFYING CLIENT BINARIES =====")
    signatures_ok = True
    files = os.listdir(binaries_path)
    client_files = [f for f in files if f.startswith("blenderkit-client")]
    for file_name in client_files:
        print(f"\n\n==={file_name}")
        file_path = os.path.join(binaries_path, file_name)
        expected = ""

        # WINDOWS
        if file_path.endswith(".exe"):
            process = subprocess.Popen(
                ["osslsigncode", "verify", "-in", file_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            output, error = process.communicate()
            # print(f"out:{output}, err:{error}")
            stdout = str(output)
            if (
                "CN=Blender Kit s.r.o." in stdout
                and "O=Blender Kit s.r.o." in stdout
                and "L=Prague" in stdout
                and "ST=Prague" in stdout
                and "C=CZ" in stdout
            ):
                print(">>> OK!")
            elif expected in str(error):
                print(">>> WARNING")
            else:
                print(">>> ERROR")
                signatures_ok = False
            continue

        # MACOS
        if "macos" in file_path:
            # validate codesigning
            process = subprocess.Popen(
                ["codesign", "--verify", "-vvvv", file_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            output, error = process.communicate()
            print(f"out:{output}, err:{error}")
            expected = "satisfies its Designated Requirement"
            if expected in str(output) or expected in str(error):
                print(">>> OK on codesigning")
            else:
                print(">>> ERROR on codesigning")
                signatures_ok = False

            # validate notarization
            process = subprocess.Popen(
                ["spctl", "--assess", "-vvv", "--ignore-cache", file_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            output, error = process.communicate()
            print(f"out:{output}, err:{error}")
            expected = "origin=Developer ID Application: Blender Kit s.r.o. (A839AY9877)"
            if expected in str(output):
                print(">>> OK notarization!")
            elif expected in str(error):
                print(">>> WARNING notarization")
            else:
                print(">>> ERROR notarization")
                signatures_ok = False

            continue

    if not signatures_ok:
        print("\n>>>>> Verification failed for one or more files, exiting.")
        exit(1)

    print("\n>>>>> Verification OK for all files!\n\n")


def _iter_version_dirs(root: str):
    """Yield ``(version_tuple, name, abspath)`` for ``vX.Y.Z`` folders in *root*."""
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        if not name.startswith("v"):
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            version = tuple(int(p) for p in name[1:].split("."))
        except ValueError:
            continue
        yield version, name, path


def find_prebuilt_client_binaries() -> str | None:
    """Return the newest folder of prebuilt client binaries, or ``None``.

    Looks inside the bk_client submodule — both ``bk_client/client/vX.Y.Z`` (if
    the client repo commits binaries) and ``bk_client/out/vX.Y.Z`` (a local
    ``dev.py build`` in the submodule). Picks the highest version that actually
    contains ``blenderkit-client`` binaries, so releases always grab the latest
    available without pinning a version. Returns ``None`` when nothing is found,
    which lets the caller fall back to building from source.
    """
    search_roots = [
        CLIENT_SRC_DIR,
        os.path.join(CLIENT_SUBMODULE_DIR, "out"),
    ]
    best: tuple[tuple[int, ...], str] | None = None
    for root in search_roots:
        for version, _name, path in _iter_version_dirs(root):
            has_binaries = any(f.startswith("blenderkit-client") for f in os.listdir(path))
            if not has_binaries:
                continue
            if best is None or version > best[0]:
                best = (version, path)
    return best[1] if best else None


def copy_client_binaries(binaries_path: str, addon_build_dir: str):
    if not os.path.exists(binaries_path):
        print(f"Client binaries path {binaries_path} does not exist, exiting.")
        exit(1)
    if not os.path.isdir(binaries_path):
        print(f"Client binaries path {binaries_path} is not a directory, exiting.")
        exit(1)

    # The binaries folder name (``vX.Y.Z``) is authoritative for the packaged
    # version — we intentionally do not pin to a single VERSION here so a newer
    # prebuilt client is picked up automatically. Warn if it disagrees with the
    # submodule's VERSION file, but do not fail the build.
    client_version = os.path.basename(os.path.normpath(binaries_path))
    version_file = os.path.join(CLIENT_SRC_DIR, "VERSION")
    if os.path.isfile(version_file):
        with open(version_file) as f:
            expected_client_version = f"v{f.read().strip()}"
        if client_version != expected_client_version:
            print(
                f"warning: client binaries version {client_version} differs from "
                f"submodule VERSION {expected_client_version}; using {client_version}."
            )

    target_dir = os.path.join(addon_build_dir, "client", client_version)
    os.makedirs(target_dir)

    files = os.listdir(binaries_path)
    client_files = [f for f in files if f.startswith("blenderkit-client")]
    for file_name in client_files:
        source_file = os.path.join(binaries_path, file_name)
        target_file = os.path.join(target_dir, file_name)
        shutil.copy2(source_file, target_file)
        print(f"Copied {source_file} to {target_file}")

    print(f"Blendkit-Client binaries copied from {binaries_path} to {target_dir}")


# ── Versioning ────────────────────────────────────────────────────────────────


def read_base_version() -> str:
    """Read ``BASE_VERSION`` (major.minor) from ``bk_maya/_version.py``.

    Parsed textually so this script has no import-time dependency on the
    package itself (and works regardless of the current working directory's
    sys.path).
    """
    version_py = os.path.join("bk_maya", "_version.py")
    with open(version_py, encoding="utf-8") as fh:
        text = fh.read()
    match = re.search(r'^BASE_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError(f"BASE_VERSION not found in {version_py}")
    return match.group(1)


def compute_version(channel: str, explicit: str | None = None) -> str:
    """Return the full version string for this build.

    Scheme: ``major.minor.YYMMDDHHmm`` with a ``-alpha`` suffix on the alpha
    channel. Pass *explicit* to override entirely (e.g. a hand-cut tag).
    """
    if explicit:
        return explicit
    base = read_base_version()
    stamp = datetime.now(timezone.utc).strftime("%y%m%d%H%M")
    version = f"{base}.{stamp}"
    if channel == CHANNEL_ALPHA:
        version += "-alpha"
    return version


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def write_build_version(addon_build_dir: str, version: str, channel: str) -> None:
    """Write the generated ``_build_version.py`` into the *built* package.

    Only the build output is touched — the source tree stays clean. At runtime
    ``bk_maya/_version.py`` imports this file to report the exact version.
    """
    target = os.path.join(addon_build_dir, "bk_maya", "_build_version.py")
    build_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = (
        "# Generated by bk_maya/dev.py at build time — DO NOT EDIT.\n"
        f'VERSION = "{version}"\n'
        f'CHANNEL = "{channel}"\n'
        f'BUILD_TIME = "{build_time}"\n'
        f'GIT_COMMIT = "{_git_commit()}"\n'
    )
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"Stamped version {version} (channel={channel}) -> {target}")


# ── Install instructions shipped inside the zip ───────────────────────────────
# Hardcoded here and written verbatim into INSTALL.txt at build time, so the
# release zip is self-documenting. Edit the wording here; it is the single
# source of truth. ``{version}`` / ``{channel}`` are filled in per build.
INSTALL_TEXT = """\
Blendkit for Maya — version {version} ({channel})
================================================================

This package is self-contained: it bundles the Python code, all required
third-party libraries, and the Blendkit client binaries for every platform.
You do NOT need to pip-install anything or run any setup.

After unzipping you have two items:

    blendkit.mod      <- the Maya module file
    blendkit/         <- the module folder (Python + client binaries)

KEEP THESE TWO TOGETHER. Copy BOTH into one of Maya's "modules" folders:

  Windows : C:\\Users\\<you>\\Documents\\maya\\modules
  macOS   : ~/Library/Preferences/Autodesk/maya/modules
  Linux   : ~/maya/modules

  (Create the "modules" folder if it does not exist. A version-specific
   folder such as .../maya/2026/modules also works.)

So the result looks like:

  .../maya/modules/blendkit.mod
  .../maya/modules/blendkit/

Then:

  1. Start (or restart) Maya.
  2. Open Windows > Settings/Preferences > Plug-in Manager.
  3. Find "maya_plugin.py", tick "Loaded" (and "Auto load" to keep it on).
  4. A "Blendkit" menu appears in the main menu bar.

To update: replace both "blendkit.mod" and the "blendkit" folder with the
newer ones and restart Maya. The installed version is shown in the Plug-in
Manager and under Blendkit > About.

To uninstall: untick the plug-in, then delete "blendkit.mod" and the
"blendkit" folder from the modules directory.

Questions / bugs: https://github.com/BlenderKit/bk_maya/issues
"""


def write_install_text(stage_dir: str, version: str, channel: str) -> None:
    """Write ``INSTALL.txt`` (and a copy inside the module folder) at build time."""
    text = INSTALL_TEXT.format(version=version, channel=channel)
    # Top-level next to blendkit.mod — the first thing a user sees in the zip.
    with open(os.path.join(stage_dir, "INSTALL.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    # Also inside the module folder so it travels with an installed copy.
    with open(os.path.join(stage_dir, "blendkit", "INSTALL.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    print("Wrote INSTALL.txt")


def do_build(
    install_at=None,
    include_tests=False,
    clean_dir=None,
    client_binaries_path=None,
    channel=CHANNEL_DEV,
    version=None,
):
    """Build the Maya add-on into ``./out`` and a versioned zip.

    Layout produced (mirrors what the runtime expects, see
    ``bk_maya/core/client_lib.py:_addon_root``)::

        out/stage/
            blendkit.mod          # Maya module file (SIBLING of the folder)
            INSTALL.txt             # hardcoded install instructions
            blendkit/             # the module root
                bk_maya/            # python sources (incl. vendored lib/ + bk_proxor/)
                bk_maya/_build_version.py  # generated version stamp
                client/vX.Y.Z/      # platform client binaries
                README.md, LICENSE, INSTALL.txt
        out/blendkit-maya-<version>.zip   # ships blendkit.mod + blendkit/

    The ``.mod`` lives *next to* (not inside) the ``blendkit/`` folder because
    Maya resolves the module path on its ``+ blendkit <ver> blendkit`` line
    relative to the ``.mod`` file's own directory. Users drop BOTH items into a
    Maya ``modules`` directory.

    - install_at: list of Maya ``modules`` directories. Both ``blendkit.mod``
      and the ``blendkit/`` folder are copied into each location.
    - include_tests: also copy the repo-level ``tests/`` directory into the build.
    - clean_dir: directory to wipe after building (e.g. cached client binaries
      under the user's Blendkit data dir).
    - client_binaries_path: use pre-signed binaries from this directory instead
      of rebuilding (``release`` command, and the future external signed-client
      repo — see CLIENT_BINARIES_ENV at the top of this file).
    - channel: release channel (``stable`` / ``alpha`` / ``dev``) — controls the
      ``-alpha`` suffix and is recorded in the built package.
    - version: explicit full version override; otherwise computed from
      ``BASE_VERSION`` + a UTC ``YYMMDDHHmm`` stamp.
    """
    full_version = compute_version(channel, version)
    print(f"=== Building Blendkit for Maya {full_version} (channel={channel}) ===")

    out_dir = os.path.abspath("out")
    stage_dir = os.path.join(out_dir, "stage")
    addon_build_dir = os.path.join(stage_dir, "blendkit")
    shutil.rmtree(out_dir, True)
    os.makedirs(addon_build_dir)

    # Refresh vendored pure-Python dependencies inside the source tree so the
    # in-place dev install and the packaged build see the same files.
    vendor_packages(_LIB_DIR)

    if client_binaries_path is None:
        blendkit_client_build(addon_build_dir)
    else:
        copy_client_binaries(client_binaries_path, addon_build_dir)

    # Copy bk_maya/ Python sources (including vendored lib/ and the
    # bk_proxor submodule contents). Drop dev/test artefacts from inside it.
    bk_ignore = shutil.ignore_patterns(
        "__pycache__",
        "*.pyc",
        ".DS_Store",
        ".git",
        ".gitignore",
        ".vscode",
        ".ruff_cache",
        "dev.py",
    )
    shutil.copytree(
        "bk_maya",
        os.path.join(addon_build_dir, "bk_maya"),
        ignore=bk_ignore,
    )

    # Stamp the exact version into the *built* package (source tree stays clean).
    write_build_version(addon_build_dir, full_version, channel)

    if include_tests:
        shutil.copytree(
            "tests",
            os.path.join(addon_build_dir, "tests"),
            ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"),
        )

    # Write the Maya module file as a SIBLING of the blendkit/ folder so the
    # module path resolves correctly once both are dropped into a Maya modules
    # directory. The module version mirrors the plugin version (minus any
    # -alpha suffix, which Maya's .mod parser does not accept) so admins can see
    # which build is registered via Maya's module manager.
    mod_path = os.path.join(stage_dir, "blendkit.mod")
    mod_version = full_version.split("-")[0]
    mod_content = (
        f"+ blendkit {mod_version} blendkit\n"
        "MAYA_PLUG_IN_PATH+:= bk_maya/plugins\n"
        "PYTHONPATH+:= .\n"
        "PYTHONPATH+:= bk_maya/lib\n"
    )
    with open(mod_path, "w", encoding="utf-8") as fh:
        fh.write(mod_content)

    # Top-level README / LICENSE are useful in the zip but not required.
    for top_level in ("README.md", "LICENSE"):
        if os.path.isfile(top_level):
            shutil.copy(top_level, os.path.join(addon_build_dir, top_level))

    # Hardcoded install instructions (top-level + inside the module folder).
    write_install_text(stage_dir, full_version, channel)

    # CREATE ZIP — name carries the version; contents are blendkit.mod +
    # blendkit/ at the archive root (so unzip-into-modules just works).
    zip_base = os.path.join(out_dir, f"blendkit-maya-{full_version}")
    print("Creating ZIP archive.")
    zip_path = shutil.make_archive(zip_base, "zip", stage_dir)
    print(f"Wrote {zip_path}")

    if install_at is not None:
        for location in install_at:
            print(f"Installing into modules dir {location}")
            os.makedirs(location, exist_ok=True)
            # Replace the module folder.
            target_folder = os.path.join(location, "blendkit")
            shutil.rmtree(target_folder, ignore_errors=True)
            shutil.copytree(addon_build_dir, target_folder)
            # Replace the .mod file.
            shutil.copy2(mod_path, os.path.join(location, "blendkit.mod"))

    if clean_dir is not None:
        print(f"Cleaning directory {clean_dir}")
        shutil.rmtree(clean_dir, ignore_errors=True)

    print("Build done!")


### COMMAND LINE INTERFACE

parser = argparse.ArgumentParser()
parser.add_argument(
    "command",
    default="build",
    choices=["build", "release", "vendor"],
    help="""
  BUILD   = vendor lib/, build client binaries from the bk_client submodule
            source, assemble out/blendkit and zip it (used for testing).
  RELEASE = like BUILD but grabs prebuilt client binaries when available
            (--client-build signed folder, else the newest binaries committed
            in the bk_client submodule); falls back to building from source.
  VENDOR  = (re)download pure-Python vendor packages into bk_maya/lib/.
  """,
)
parser.add_argument(
    "--install-at",
    type=str,
    action="append",  # This allows multiple --install-at arguments
    default=None,
    help="Maya modules directory to copy the built addon into. Can be used multiple times.",
)
parser.add_argument(
    "--clean-dir",
    type=str,
    default=None,
    help="Directory to wipe after building (e.g. cached client binaries under the user's Blendkit data dir).",
)
parser.add_argument(
    "--client-build",
    type=str,
    default=os.environ.get(CLIENT_BINARIES_ENV),
    help=(
        "Path to a folder of prebuilt (signed) client binaries, named vX.Y.Z. "
        "Binaries in this directory are used instead of building from source. "
        f"Defaults to ${CLIENT_BINARIES_ENV} if set. Used by 'release'."
    ),
)
parser.add_argument(
    "--channel",
    type=str,
    choices=[CHANNEL_STABLE, CHANNEL_ALPHA, CHANNEL_DEV],
    default=CHANNEL_DEV,
    help=(
        "Release channel. 'alpha' appends a -alpha suffix to the version "
        "(automated builds from 'main'); 'stable' is the regular release; "
        "'dev' is the default for local builds."
    ),
)
parser.add_argument(
    "--version",
    type=str,
    default=None,
    help=(
        "Explicit full version override (e.g. '0.1.2506071430'). When omitted "
        "the version is computed from BASE_VERSION + a UTC YYMMDDHHmm stamp."
    ),
)
args = parser.parse_args()

if args.command == "build":
    do_build(
        args.install_at,
        clean_dir=args.clean_dir,
        client_binaries_path=args.client_build,
        channel=args.channel,
        version=args.version,
    )
elif args.command == "release":
    # Prefer explicit signed binaries; verify their code-signing before use.
    binaries_path = args.client_build
    if binaries_path is not None:
        verify_client_binaries(binaries_path)
    else:
        # Grab the newest prebuilt binaries committed in the bk_client submodule.
        binaries_path = find_prebuilt_client_binaries()
        if binaries_path is not None:
            print(f"Using prebuilt client binaries from submodule: {binaries_path}")
        else:
            print("No prebuilt client binaries found in submodule; building from source.")
    do_build(
        args.install_at,
        clean_dir=args.clean_dir,
        client_binaries_path=binaries_path,
        channel=args.channel,
        version=args.version,
    )
elif args.command == "vendor":
    vendor_packages(_LIB_DIR)
else:
    parser.print_help()
