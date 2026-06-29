<div align="center">
  <img src="/bk_maya/data/icons/blendkit_logo.png" alt="Logo" width="100" height="100"/>
  <h3 align="center">Blendkit for Maya</h3>

  Asset search, download and drag&drop directly inside Autodesk Maya.

  ![GitHub Downloads (all assets, all releases)](https://img.shields.io/github/downloads/blenderkit/bk_maya/total?color=blue)
  ![GitHub Downloads (all assets, latest release)](https://img.shields.io/github/downloads/blenderkit/bk_maya/latest/total?color=blue)
  [![GitHub Release](https://img.shields.io/github/v/release/blenderkit/bk_maya?color=green)](https://github.com/BlenderKit/bk_maya/releases/latest)
  [![Project license](https://img.shields.io/github/license/blenderkit/bk_maya.svg?color=orange)](LICENSE)
  </br>
  ![GitHub commit activity](https://img.shields.io/github/commit-activity/y/blenderkit/bk_maya?color=blue)
  ![GitHub branch check runs](https://img.shields.io/github/check-runs/blenderkit/bk_maya/main?color=green)

</div>


> **Status:** early development / **alpha**. Automated releases are now
> published to [GitHub Releases](https://github.com/BlenderKit/bk_maya/releases):
> every merge to `main` produces a rolling **Alpha** prerelease, and `master`
> (or the manual *Run workflow* button) produces a regular release. The zip is
> self-contained — unzip into a Maya `modules` directory and restart Maya, no
> extra packages or setup required. You can still build locally with
> `python bk_maya/dev.py build`.

## About
The Blendkit Maya plugin connects Autodesk Maya to the [Blendkit service](https://www.blendkit.com/) — search the library, drag&drop assets straight into the viewport, and re-use the same account / Full plan you already have for the Blender add-on.

It is a port of the official Blender add-on built on:

- Maya 2027 (Python 3.11, PySide6, OpenMaya 2.0)
- The shared Go `blenderkit-client` for downloads, auth and search
- A vendored `qtpy` / `requests` / `packaging` (see [bk_maya/lib](bk_maya/lib))

## Repository layout
- [bk_maya/](bk_maya) — the Maya plugin (core, UI, plugins, vendored libs)
- [bk_maya/bk_proxor/](bk_maya/bk_proxor) — proxor mesh-preview submodule (`.prx` / `.prxc`)
- [client/](client) — Go client binary shared with the Blender add-on
- [tests/](tests) — pure-Python unit tests runnable without Maya

## Getting started (developers)

```powershell
# 1. clone with submodules
git clone --recursive https://github.com/BlenderKit/bk_maya.git
cd bk_maya

# 2. create a venv and install dev tooling
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"  # or: pdm install / uv sync --group dev

# 3. enable pre-commit hooks (ruff + pydoclint)
pre-commit install

# 4. run the synthetic test suite
python -m unittest discover tests
```

To build a distributable bundle:

```powershell
python bk_maya/dev.py build
```

To load the plugin inside Maya, point Maya's plug-in path at `bk_maya/plugins/`
and load `maya_plugin.py` from `Windows ▸ Settings/Preferences ▸ Plug-in Manager`.

## Releases & versioning

- **Version scheme:** `major.minor.YYMMDDHHmm`, with a `-alpha` suffix on
  automated `main` builds (e.g. `0.1.2506071430-alpha`). The `major.minor`
  part is the single human-editable knob in [bk_maya/_version.py](bk_maya/_version.py)
  (`BASE_VERSION`); the timestamped patch + channel are generated at build time.
- **Where the version lives at runtime:** the build writes a generated
  `bk_maya/_build_version.py` into the package. The plugin reads it via
  [bk_maya/_version.py](bk_maya/_version.py) and surfaces it in the Plug-in
  Manager, the **Blendkit ▸ About** menu, and the Maya `.mod` module version —
  so users and admins can see exactly which build is installed.
- **Automated releases** (see [.github/workflows/release.yml](.github/workflows/release.yml)):
  - merge to **`main`** → rolling **Alpha** prerelease,
  - push to **`master`** or the manual *Run workflow* button → **stable** release.
- **Zip contents** (`blendkit-maya-<version>.zip`): the version is in the
  filename, and the archive holds the `blendkit.mod` file next to the
  `blendkit/` module folder. Unzip **both** into a Maya `modules` directory
  and restart Maya — see the bundled `INSTALL.txt`.
- **Build channels locally:**

  ```powershell
  python bk_maya/dev.py build                 # dev build  -> 0.1.<stamp>.dev style
  python bk_maya/dev.py build --channel alpha # alpha      -> 0.1.<stamp>-alpha
  python bk_maya/dev.py build --channel stable
  python bk_maya/dev.py build --version 0.1.2506071430   # explicit override
  ```

> **Client binaries (future change):** today the Go client is compiled from
> `client/` on every build. When it moves to its own repo and ships *signed*
> binaries, point the build at the downloaded folder with
> `--client-build <folder>` (or the `BLENDKIT_CLIENT_BINARIES` env-var) — see
> the comments in [bk_maya/dev.py](bk_maya/dev.py). No other packaging changes
> are needed.

## Quality

| Check        | Local                                   | CI                                            |
|--------------|-----------------------------------------|-----------------------------------------------|
| Lint         | `ruff check .`                          | `.github/workflows/lint.yml` → **Ruff**       |
| Format       | `ruff format --check .`                 | `.github/workflows/lint.yml` → **Ruff**       |
| Docstrings   | `pydoclint .`                           | `.github/workflows/lint.yml` → **Pydoclint**  |
| Security     | `bandit -c _bandit.yaml -r .`           | `.github/workflows/lint.yml` → **Bandit**     |
| Unit tests   | `python -m unittest discover tests`     | `.github/workflows/PR.yml` → **Maya-Port-Unit-Tests** |
| Go client    | `go test ./client/...`                  | `.github/workflows/PR.yml` → **Client-Unit-Tests** |

All checks are also wired up as a [pre-commit](https://pre-commit.com) hook — see [.pre-commit-config.yaml](.pre-commit-config.yaml).

## How to contribute
- Share the word about Blendkit with your friends and colleagues, or on social media.
- [Become a Creator](https://www.blendkit.com/become-creator/) and upload your assets to the Blendkit Free or Full Plan database.
- Report a bug or request a feature in the [issue tracker](https://github.com/BlenderKit/bk_maya/issues).
- Contribute code — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License
[GPL-3.0](LICENSE). Same license as the upstream Blender add-on.
