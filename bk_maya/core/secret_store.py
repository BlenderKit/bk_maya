"""Cross-platform secure storage for the Blendkit Maya plugin's secrets.

The plugin needs to persist OAuth tokens (access + refresh) between Maya
sessions. Writing them to a plaintext JSON file in the user's ``Documents``
folder is undesirable: such folders are frequently synced to the cloud, picked
up by backup tools, or readable by other software running as the same user.

This module stores secrets in the operating system's native credential vault
instead, with **no third-party dependencies** (important: Maya ships its own
Python and cannot easily ``pip install`` extra packages):

* **Windows** — Windows Credential Manager via ``advapi32`` (``CredWrite`` /
  ``CredRead`` / ``CredDelete``) using :mod:`ctypes`.
* **macOS** — the login Keychain via the always-present ``security`` CLI.
* **Linux** — the Secret Service / libsecret via ``secret-tool`` (if the
  ``libsecret-tools`` package is installed).

If none of those are available the implementation degrades gracefully to a
JSON file with ``0600`` permissions (owner read/write only) and logs a warning
so the user understands the reduced protection.

Public API::

    get_secret(name)    -> str | None
    set_secret(name, value) -> bool
    delete_secret(name) -> None
    backend_name()      -> str
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

# Service/namespace used for every secret this plugin stores.
_SERVICE = "BlendkitMaya"

# Resolved lazily; one of "wincred", "macos-keychain", "secret-tool", "file".
_backend: str | None = None


# ---------------------------------------------------------------------------
# Fallback file location (only used when no native vault is available)
# ---------------------------------------------------------------------------


def _fallback_path() -> str:
    """Return the path of the 0600 JSON file used when no vault is available."""
    if sys.platform == "win32":
        import ctypes

        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x0005, None, 0, buf)
        docs = buf.value or os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Documents")
    elif sys.platform == "darwin":
        docs = os.path.expanduser("~/Library/Preferences/Autodesk/maya")
    else:
        docs = os.path.expanduser("~/maya")
    return os.path.join(docs, "maya", "blendkit_secrets.json")


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------


def _detect_backend() -> str:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.WinDLL("advapi32")
        except OSError:
            return "file"
        return "wincred"
    if sys.platform == "darwin":
        if shutil.which("security"):
            return "macos-keychain"
        return "file"
    # Linux / other POSIX
    if shutil.which("secret-tool"):
        return "secret-tool"
    return "file"


def backend_name() -> str:
    """Return the active backend identifier (resolved once)."""
    global _backend
    if _backend is None:
        _backend = _detect_backend()
        if _backend == "file":
            log.warning(
                "No OS credential vault available; Blendkit tokens will be stored "
                "in a permission-restricted file (%s). Install 'libsecret-tools' "
                "on Linux for secure storage.",
                _fallback_path(),
            )
        else:
            log.debug("Secret store backend: %s", _backend)
    return _backend


# ---------------------------------------------------------------------------
# Windows Credential Manager (ctypes)
# ---------------------------------------------------------------------------


def _win_target(name: str) -> str:
    return f"{_SERVICE}:{name}"


def _win_get(name: str) -> str | None:
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = (
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        )

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_read = advapi32.CredReadW
    cred_read.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL)))
    cred_read.restype = wintypes.BOOL
    cred_free = advapi32.CredFree
    cred_free.argtypes = (ctypes.c_void_p,)

    pcred = ctypes.POINTER(CREDENTIAL)()
    if not cred_read(_win_target(name), 1, 0, ctypes.byref(pcred)):  # 1 = CRED_TYPE_GENERIC
        return None
    try:
        cred = pcred.contents
        size = int(cred.CredentialBlobSize)
        if size == 0:
            return ""
        blob = ctypes.string_at(cred.CredentialBlob, size)
        return blob.decode("utf-8")
    finally:
        cred_free(pcred)


def _win_set(name: str, value: str) -> bool:
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = (
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        )

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_write = advapi32.CredWriteW
    cred_write.argtypes = (ctypes.POINTER(CREDENTIAL), wintypes.DWORD)
    cred_write.restype = wintypes.BOOL

    blob = value.encode("utf-8")
    blob_buf = ctypes.create_string_buffer(blob, len(blob))

    cred = CREDENTIAL()
    cred.Flags = 0
    cred.Type = 1  # CRED_TYPE_GENERIC
    cred.TargetName = _win_target(name)
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(blob_buf, ctypes.POINTER(ctypes.c_byte))
    cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = _SERVICE
    return bool(cred_write(ctypes.byref(cred), 0))


def _win_delete(name: str) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_delete = advapi32.CredDeleteW
    cred_delete.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD)
    cred_delete.restype = wintypes.BOOL
    cred_delete(_win_target(name), 1, 0)  # 1 = CRED_TYPE_GENERIC


# ---------------------------------------------------------------------------
# macOS Keychain (security CLI)
# ---------------------------------------------------------------------------


def _mac_get(name: str) -> str | None:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", _SERVICE, "-a", name, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        log.debug("security find-generic-password failed: %s", exc)
        return None
    if out.returncode != 0:
        return None
    return out.stdout.rstrip("\n")


def _mac_set(name: str, value: str) -> bool:
    try:
        # -U updates an existing item instead of failing with errSecDuplicateItem.
        out = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", _SERVICE, "-a", name, "-w", value],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        log.debug("security add-generic-password failed: %s", exc)
        return False
    return out.returncode == 0


def _mac_delete(name: str) -> None:
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", _SERVICE, "-a", name],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        log.debug("security delete-generic-password failed: %s", exc)


# ---------------------------------------------------------------------------
# Linux Secret Service (secret-tool CLI)
# ---------------------------------------------------------------------------


def _secret_tool_get(name: str) -> str | None:
    try:
        out = subprocess.run(
            ["secret-tool", "lookup", "service", _SERVICE, "account", name],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        log.debug("secret-tool lookup failed: %s", exc)
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _secret_tool_set(name: str, value: str) -> bool:
    try:
        out = subprocess.run(
            ["secret-tool", "store", "--label", f"{_SERVICE} {name}", "service", _SERVICE, "account", name],
            input=value,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        log.debug("secret-tool store failed: %s", exc)
        return False
    return out.returncode == 0


def _secret_tool_delete(name: str) -> None:
    try:
        subprocess.run(
            ["secret-tool", "clear", "service", _SERVICE, "account", name],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        log.debug("secret-tool clear failed: %s", exc)


# ---------------------------------------------------------------------------
# 0600 file fallback
# ---------------------------------------------------------------------------


def _file_read_all() -> dict[str, str]:
    try:
        with open(_fallback_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _file_write_all(data: dict[str, str]) -> bool:
    path = _fallback_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Create with restrictive permissions from the start (owner-only).
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        finally:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    except OSError as exc:
        log.error("Could not write secret fallback file: %s", exc)
        return False
    return True


def _file_get(name: str) -> str | None:
    return _file_read_all().get(name)


def _file_set(name: str, value: str) -> bool:
    data = _file_read_all()
    data[name] = value
    return _file_write_all(data)


def _file_delete(name: str) -> None:
    data = _file_read_all()
    if name in data:
        del data[name]
        if data:
            _file_write_all(data)
        else:
            try:
                os.remove(_fallback_path())
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_secret(name: str) -> str | None:
    """Return the stored secret value for *name*, or ``None`` if absent."""
    backend = backend_name()
    try:
        if backend == "wincred":
            return _win_get(name)
        if backend == "macos-keychain":
            return _mac_get(name)
        if backend == "secret-tool":
            return _secret_tool_get(name)
    except Exception as exc:
        log.warning("Secret store read failed (%s); falling back to file: %s", backend, exc)
        return _file_get(name)
    return _file_get(name)


def set_secret(name: str, value: str) -> bool:
    """Persist *value* under *name*. Returns ``True`` on success.

    On any native-backend failure the value is written to the 0600 fallback
    file so the user is never locked out of staying logged in.
    """
    backend = backend_name()
    try:
        if backend == "wincred" and _win_set(name, value):
            return True
        if backend == "macos-keychain" and _mac_set(name, value):
            return True
        if backend == "secret-tool" and _secret_tool_set(name, value):
            return True
    except Exception as exc:
        log.warning("Secret store write failed (%s); falling back to file: %s", backend, exc)
    return _file_set(name, value)


def delete_secret(name: str) -> None:
    """Remove *name* from every backend (native vault and fallback file)."""
    backend = backend_name()
    try:
        if backend == "wincred":
            _win_delete(name)
        elif backend == "macos-keychain":
            _mac_delete(name)
        elif backend == "secret-tool":
            _secret_tool_delete(name)
    except Exception as exc:
        log.debug("Native secret delete failed (%s): %s", backend, exc)
    # Always also clear any fallback-file copy (e.g. left by a prior session).
    _file_delete(name)
