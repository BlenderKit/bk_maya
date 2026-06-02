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
import shutil
import subprocess
import sys
import tempfile
import zipfile

# Pure-Python packages to vendor into lib/.
# Both qtpy and packaging ship as py3-none-any wheels, so a single download
# covers all platforms (Windows, macOS, Linux) and all architectures.
VENDOR_PACKAGES = [
    "qtpy",
    "packaging",  # required by qtpy
    "requests",  # HTTP client used by core/ and api/
]


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


def blenderkit_client_build(abs_build_dir: str):
    """Build blenderkit-client for all platforms in parallel."""
    with open("client/VERSION") as f:
        client_version = f.read().strip()
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
            cwd="./client",
        )
        build["process"] = process

    print(f"BlenderKit-Client v{client_version} build started for {len(builds)} platforms.")
    builds_ok = True
    for build in builds:
        build["process"].wait()
        if build["process"].returncode != 0:
            print(f"Client build ({build['env']}) failed")
            builds_ok = False

    if not builds_ok:
        exit(1)
    print(f"BlenderKit-Client v{client_version} builds completed.")


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
            expected = "origin=Developer ID Application: BlenderKit s.r.o. (A839AY9877)"
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


def copy_client_binaries(binaries_path: str, addon_build_dir: str):
    if not os.path.exists(binaries_path):
        print(f"Client binaries path {binaries_path} does not exist, exiting.")
        exit(1)
    if not os.path.isdir(binaries_path):
        print(f"Client binaries path {binaries_path} is not a directory, exiting.")
        exit(1)

    with open("client/VERSION") as f:
        expected_client_version = f"v{f.read().strip()}"

    client_version = os.path.basename(os.path.normpath(binaries_path))
    if client_version != expected_client_version:
        print(
            f"Client binaries version {client_version} does not match expected version {expected_client_version}, exiting."
        )
        exit(1)

    target_dir = os.path.join(addon_build_dir, "client", expected_client_version)
    os.makedirs(target_dir)

    files = os.listdir(binaries_path)
    client_files = [f for f in files if f.startswith("blenderkit-client")]
    for file_name in client_files:
        source_file = os.path.join(binaries_path, file_name)
        target_file = os.path.join(target_dir, file_name)
        shutil.copy2(source_file, target_file)
        print(f"Copied {source_file} to {target_file}")

    print(f"BlenderKit-Client binaries copied from {binaries_path} to {target_dir}")


def do_build(install_at=None, include_tests=False, clean_dir=None, client_binaries_path=None):
    """Build the Maya add-on into ``./out/blenderkit`` and ``./out/blenderkit.zip``.

    Layout produced (mirrors what the runtime expects, see
    ``bk_maya/core/client_lib.py:_addon_root``)::

        out/blenderkit/
            bk_maya/            # python sources (incl. vendored lib/ and bk_proxor/)
            client/vX.Y.Z/      # platform client binaries
            blenderkit.mod      # Maya module registration

    - install_at: list of Maya ``modules`` directories where the addon should
      be copied. The .mod file ships inside the build, so a recursive copy of
      ``out/blenderkit`` into each location is all that is required.
    - include_tests: also copy the repo-level ``tests/`` directory into the build.
    - clean_dir: directory to wipe after building (e.g. cached client binaries
      under the user's BlenderKit data dir).
    - client_binaries_path: use pre-signed binaries from this directory instead
      of rebuilding (``release`` command).
    """
    out_dir = os.path.abspath("out")
    addon_build_dir = os.path.join(out_dir, "blenderkit")
    shutil.rmtree(out_dir, True)
    os.makedirs(addon_build_dir)

    # Refresh vendored pure-Python dependencies inside the source tree so the
    # in-place dev install and the packaged build see the same files.
    vendor_packages(os.path.abspath(os.path.join("bk_maya", "lib")))

    if client_binaries_path is None:
        blenderkit_client_build(addon_build_dir)
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

    if include_tests:
        shutil.copytree(
            "tests",
            os.path.join(addon_build_dir, "tests"),
            ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"),
        )

    # Write the Maya module file so dropping the folder into a Maya modules
    # directory is enough — no manual PYTHONPATH/MAYA_PLUG_IN_PATH editing.
    mod_path = os.path.join(addon_build_dir, "blenderkit.mod")
    mod_content = (
        "+ blenderkit 1.0 blenderkit\n"
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

    # CREATE ZIP
    print("Creating ZIP archive.")
    shutil.make_archive("out/blenderkit", "zip", "out", "blenderkit")

    if install_at is not None:
        for location in install_at:
            target = os.path.join(location, "blenderkit")
            print(f"Copying to {target}")
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(addon_build_dir, target)

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
  BUILD   = vendor lib/, build client binaries, assemble out/blenderkit and zip it.
  RELEASE = like BUILD but uses already signed client binaries from --client-build.
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
    help="Directory to wipe after building (e.g. cached client binaries under the user's BlenderKit data dir).",
)
parser.add_argument(
    "--client-build",
    type=str,
    default=None,
    help="Path to client_builds/vX.Y.Z. Binaries in this directory will be used instead of building new ones.",
)
args = parser.parse_args()

if args.command == "build":
    do_build(
        args.install_at,
        clean_dir=args.clean_dir,
        client_binaries_path=args.client_build,
    )
elif args.command == "release":
    if args.client_build is None:
        print("Error: Client binaries path (containing signed binaries) is required for release")
        exit(1)
    verify_client_binaries(args.client_build)
    do_build(
        args.install_at,
        clean_dir=args.clean_dir,
        client_binaries_path=args.client_build,
    )
elif args.command == "vendor":
    vendor_packages(os.path.abspath(os.path.join("bk_maya", "lib")))
else:
    parser.print_help()
