<div align="center">
  <img src="/bk_maya/data/icons/blenderkit_logo.png" alt="Logo" width="100" height="100"/>
  <h3 align="center">BlenderKit for Maya</h3>

  Asset search, download and drag&drop directly inside Autodesk Maya.

  [![Project license](https://img.shields.io/github/license/blenderkit/blenderkit_maya.svg?color=orange)](LICENSE)
</div>

> **Status:** early development. There is **no automated release yet** — installable artefacts must be built locally with `python bk_maya/dev.py build`. A release pipeline will follow soon.

## About
The BlenderKit Maya plugin connects Autodesk Maya to the [BlenderKit service](https://www.blenderkit.com/) — search the library, drag&drop assets straight into the viewport, and re-use the same account / Full plan you already have for the Blender add-on.

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
git clone --recursive https://github.com/BlenderKit/blenderkit_maya.git
cd blenderkit_maya

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
- Share the word about BlenderKit with your friends and colleagues, or on social media.
- [Become a Creator](https://www.blenderkit.com/become-creator/) and upload your assets to the BlenderKit Free or Full Plan database.
- Report a bug or request a feature in the [issue tracker](https://github.com/BlenderKit/blenderkit_maya/issues).
- Contribute code — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License
[GPL-3.0](LICENSE). Same licence as the upstream Blender add-on.
