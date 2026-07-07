# Contributing

Blendkit add-on is an open-source project and we welcome contributions from the community.

## Add-on Architecture
Blendkit add-on is made of two main parts:
- Blender add-on written in Python, which is responsible for the user interface and interaction with Blender. It draws the search panel, does the snaping, asset imports, and communicates with the Client locally.
- Client written in Go, which serves as background HTTP server - a bridge between Blendkit add-on and Blendkit server. It's purpose is to offload the work from Blender and to provide a performant way to communicate with Blendkit server.

Client is compiled and it's binaries are bundled into the add-on .zip file, so the user does not need to install anything else than the add-on itself.

### How it is packaged
Blendkit add-on is packaged as a zip file (standard way for Blender add-ons), which contains all the necessary files for the add-on to work.
This includes not only the Python files, icons and other files, but also the Client binaries for 3 platforms on 2 architectures (windows x86_64, windows arm64, macos x86_64, macos arm64, linux x86_64, linux arm64).
When add-on is registered, it chooses the correct Client binary for the platform and architecture and copies it to the user's Blendkit data directory, from this location the Client is later started.

### How it works
Communication between Add-on and Client happens in one way direction: add-on schedules Tasks via request and periodically gets updates about the progress and results of the tasks in reponses to the requests:
`Add-on -> Client -> Server`

1. add-on checks whether the Client is running. If it is not, it starts the Client binary located at `<global-directory>/client/bin/vX.Y.Z/blenderkit-client-<platform>-<architecture>`,
2. add-on periodically asks for results with GET request and Client responds to the request,

3. if needed add-on sends requests (identifying itself with app_id which is PID of running Blender instance) for search, download asset, get notifications, download thumbnails etc. to the Client
4. Client receives the request for work, saves it into `var Tasks map[int]map[string]*Task` and ASAP responds by OK to not block the add-on,
5. Client starts the work in goroutine, or makes request to Blendkit server, or combination of both,
6. When work is done, or response comes from Blendkit server, Client updates the results into `var Tasks map[int]map[string]*Task`.
7. next time when add-on periodically asks for results of the Tasks, Client sends the results as response.

Communication between Client and Server currently happens in one way also Client -> Server (Client makes requests to Server).

## Development

### Logging

Do not use `print()` statements in the code, use logging instead.
In the beginning of the file, there is a logger setup, if it is not already there, add it:
```python
import logging
bk_logger = logging.getLogger(__name__)
```

Then instead of `print()` use the `bk_logger`:
```python
bk_logger.debug("Some minor stuff happened")
bk_logger.info("Something expected has happened")
bk_logger.warning("Something unexpected has happened")
bk_logger.error("Something went very wrong")
```

If you have an exception which you can log, use `bk_logger.exception()`, e.g.:
```python
except Exception as e:
    bk_logger.exception("Something went wrong and you will see full traceback below")
```

### Codestyle

We use `ruff` for lint and formatting of Python code, `pydoclint` for docstring
consistency and `bandit` for security checks. `go fmt` formats Go code in
`./client`. The exact versions used by CI are pinned in `pyproject.toml` under
`[dependency-groups].dev`.

Install the dev tools into your active environment:
```
pip install -e .
pip install "ruff>=0.15.6" "pydoclint>=0.8.3" "bandit>=1.9.4" pre-commit
pre-commit install
```

Before committing, run the same checks CI runs:
```
ruff check .
ruff format .
pydoclint .
bandit -c _bandit.yaml -ll -r .
gofmt -l ./client
```

Pull requests will fail in CI if any of these report errors.

### Building the add-on

Use `bk_maya/dev.py` from the repo root to build the add-on. The script copies
the relevant files into `out/blenderkit` (skipping anything not needed in the
shipped add-on) and produces `out/blenderkit.zip`.

To build run:
```
python bk_maya/dev.py build
```

#### Development build: build for quick testing

`bk_maya/dev.py` accepts `--install-at` to copy the built `out/blenderkit`
directly into a Maya modules / scripts location, so the add-on is ready to load
on the next Maya start. The flag can be passed multiple times to install into
several targets at once.

```
python bk_maya/dev.py build --install-at /path/to/maya/modules
```

`--clean-dir` can be used to wipe a stale client binaries directory (required
when you change the Go client, otherwise the cached binary is not overwritten):

```
python bk_maya/dev.py build --install-at /path/to/maya/modules --clean-dir ~/blenderkit_data/client/bin
```

## Releasing

Before release update the add-on version in `__init__.py` and
`blender_manifest.toml`. The Go client version is managed in its own repository
(the `bk_client` submodule, `bk_client/client/VERSION`) — releases pick up the
newest client binaries available there automatically. Make sure the bump is
merged into `main`.

Releases run through `python bk_maya/dev.py release`, which grabs prebuilt
client binaries from the `bk_client` submodule when available and otherwise
builds them from source.

## Testing

The Maya port ships pure-Python unit tests under `tests/`. They cover the
PRX coordinate converters, locator state registries, PRX round-tripping and
global var handling, and have no dependency on Maya or Qt.

The `bk_proxor` submodule is required by some tests, so first ensure it is
initialised:
```
git submodule update --init --recursive
```

Then run the suite from the repo root:
```
python -m unittest discover tests
```

Or run the same subset CI runs, with coverage:
```
python -m coverage run --source=bk_maya/bk_proxor/src/bk_proxor,bk_maya/core \
    -m unittest \
        tests.test_proxor_maya_draw \
        tests.test_locator_state \
        tests.test_prx_format_roundtrip \
        tests.test_global_vars
```

Go tests for the client live in `./client` and can be run with:
```
cd client && go test ./...
```

### Pull Requests

To contribute to the project, please create a Pull Request.
PR should contain a description of the changes and the reason for the changes.
Ideally PR should be linked to an issue in the issue tracker.

PR will be reviewed by the team and if it passes the automated tests and checks, it will be merged.

#### Automated tests

We run automated checks on Pull Requests and on pushes to `main`/`master`.
The checks which must pass for a PR to be accepted are:
- `ruff check .` — lint,
- `ruff format --check .` — formatting,
- `pydoclint .` — docstring consistency,
- `bandit -c _bandit.yaml -ll -r .` — security (medium+ severity),
- `gofmt` check for Go code in `./client`,
- Go unit tests for the client,
- Maya-port unit tests on Python 3.11 and 3.12,
- automated build of the add-on via `python bk_maya/dev.py build`.

Those CI jobs are defined in a single workflow: `.github/workflows/CI.yml`.
