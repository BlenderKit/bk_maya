"""BlenderKit Settings dialog for Maya.

A tabbed QDialog mirroring the options available in Blender's N-panel /
addon preferences.  Open it via the BlenderKit top menu → Settings…

Tabs
----
General     thumbnail size, show on start, tips
Files       global download directory, max import resolution
Search      texture resolution filter, free-only toggle
Networking  proxy mode + address, SSL verification
Account     login status, API key, log-in / log-out button
"""

from __future__ import annotations

import logging
import os
import sys
import threading

from qtpy.QtCore import QObject, Qt, Signal
from qtpy.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import auth
from ..core.prefs import prefs

log = logging.getLogger(__name__)

_MAX_RESOLUTION_OPTIONS = ["512", "1024", "2048", "4096", "8192", "ORIGINAL"]
_PROXY_OPTIONS = [
    ("SYSTEM", "System — use OS networking settings"),
    ("ENVIRONMENT", "Environment — use HTTPS_PROXY variable"),
    ("NONE", "None — bypass all proxies"),
    ("CUSTOM", "Custom — specify address below"),
]

# region: Helpers


def _section(title: str) -> QLabel:
    lbl = QLabel(title)
    font = lbl.font()
    font.setBold(True)
    lbl.setFont(font)
    lbl.setStyleSheet("color: #b0b0b0; padding-top: 8px;")
    return lbl


def _hr() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet("color: #3a3a3a;")
    return f


def _note(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet("color: #888; font-size: 11px; padding: 0 0 4px 0;")
    return lbl


# endregion: Helpers

# region: General


class _GeneralTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        # ── Startup ────────────────────────────────────────────────────────
        layout.addWidget(_section("Startup"))
        layout.addWidget(_hr())

        self._show_on_start = QCheckBox("Open asset bar when Maya starts")
        self._show_on_start.setChecked(prefs.show_on_start)
        layout.addWidget(self._show_on_start)

        self._tips_on_start = QCheckBox("Show tips at startup")
        self._tips_on_start.setChecked(prefs.tips_on_start)
        layout.addWidget(self._tips_on_start)

        # ── Thumbnail ─────────────────────────────────────────────────────
        layout.addWidget(_section("Asset Bar"))
        layout.addWidget(_hr())
        layout.addWidget(_note("Thumbnail tile size in the asset browser grid."))

        thumb_row = QHBoxLayout()
        thumb_row.setSpacing(8)
        self._thumb_slider = QSlider(Qt.Horizontal)
        self._thumb_slider.setRange(48, 256)
        self._thumb_slider.setValue(prefs.thumbnail_size)
        self._thumb_slider.setTickPosition(QSlider.TicksBelow)
        self._thumb_slider.setTickInterval(32)
        thumb_row.addWidget(self._thumb_slider)
        self._thumb_spin = QSpinBox()
        self._thumb_spin.setRange(48, 256)
        self._thumb_spin.setValue(prefs.thumbnail_size)
        self._thumb_spin.setSuffix(" px")
        self._thumb_spin.setFixedWidth(72)
        thumb_row.addWidget(self._thumb_spin)
        layout.addLayout(thumb_row)

        # keep slider and spinbox in sync
        self._thumb_slider.valueChanged.connect(self._thumb_spin.setValue)
        self._thumb_spin.valueChanged.connect(self._thumb_slider.setValue)

        layout.addStretch()

    def apply(self) -> None:
        prefs.show_on_start = self._show_on_start.isChecked()
        prefs.tips_on_start = self._tips_on_start.isChecked()
        prefs.thumbnail_size = self._thumb_spin.value()


# endregion: General

# region: Files


class _FilesTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        # ── Download directory ─────────────────────────────────────────────
        layout.addWidget(_section("Download Directory"))
        layout.addWidget(_hr())
        layout.addWidget(_note("Root folder for all downloaded assets. Leave blank to use the default location."))

        dir_row = QHBoxLayout()
        self._dir_edit = QLineEdit(prefs.global_dir)
        self._dir_edit.setPlaceholderText(prefs.global_dir_resolved())
        dir_row.addWidget(self._dir_edit)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(70)
        browse_btn.clicked.connect(self._browse)
        dir_row.addWidget(browse_btn)
        layout.addLayout(dir_row)

        # ── Import resolution ──────────────────────────────────────────────
        layout.addWidget(_section("Import Resolution"))
        layout.addWidget(_hr())
        layout.addWidget(
            _note("Cap texture dimensions when importing assets. ORIGINAL keeps whatever resolution is in the file."),
        )

        self._res_combo = QComboBox()
        for opt in _MAX_RESOLUTION_OPTIONS:
            label = "ORIGINAL FILE" if opt == "ORIGINAL" else f"{opt}x{opt}"
            self._res_combo.addItem(label, userData=opt)
        current_idx = (
            _MAX_RESOLUTION_OPTIONS.index(prefs.max_resolution)
            if prefs.max_resolution in _MAX_RESOLUTION_OPTIONS
            else 2
        )
        self._res_combo.setCurrentIndex(current_idx)
        layout.addWidget(self._res_combo)

        # ── Blender executable ────────────────────────────────────────────
        layout.addWidget(_section("Blender Executable"))
        layout.addWidget(_hr())
        layout.addWidget(
            _note(
                "Path to blender.exe. Used to fetch and convert assets in the "
                "background. Blender 5.0 or newer is required. Leave blank to "
                "auto-detect.",
            ),
        )

        be_row = QHBoxLayout()
        self._blender_edit = QLineEdit(prefs.blender_exe)
        self._blender_edit.setPlaceholderText("auto-detect")
        be_row.addWidget(self._blender_edit)
        be_browse = QPushButton("Browse…")
        be_browse.setFixedWidth(70)
        be_browse.clicked.connect(self._browse_blender)
        be_row.addWidget(be_browse)
        be_detect = QPushButton("Auto-detect")
        be_detect.setFixedWidth(90)
        be_detect.clicked.connect(self._autodetect_blender)
        be_row.addWidget(be_detect)
        layout.addLayout(be_row)

        self._blender_status = QLabel("")
        self._blender_status.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self._blender_status)
        self._refresh_blender_status()

        layout.addStretch()

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Select Download Directory",
            self._dir_edit.text() or os.path.expanduser("~"),
        )
        if path:
            self._dir_edit.setText(path)

    def _browse_blender(self) -> None:
        if os.name == "nt":
            filt = "Blender executable (blender.exe);;All files (*)"
        elif sys.platform == "darwin":
            filt = "Blender (Blender);;All files (*)"
        else:
            filt = "Blender executable (blender);;All files (*)"
        start = os.path.dirname(self._blender_edit.text()) or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, "Select Blender Executable", start, filt)
        if path:
            self._blender_edit.setText(path)
            self._refresh_blender_status()

    def _autodetect_blender(self) -> None:
        from ..core.blender_runner import find_blender_executable

        # Temporarily clear the pref so auto-detection ignores any stale value
        saved = prefs.blender_exe
        prefs.blender_exe = ""
        try:
            found = find_blender_executable()
        finally:
            prefs.blender_exe = saved
        if found:
            self._blender_edit.setText(found)
        self._refresh_blender_status()

    def _refresh_blender_status(self) -> None:
        from ..core.blender_runner import (
            MIN_BLENDER_MAJOR,
            query_blender_version,
            version_meets_min,
        )

        path = self._blender_edit.text().strip()
        if not path:
            self._blender_status.setText("No path set — will auto-detect at runtime.")
            self._blender_status.setStyleSheet("color: #888; font-size: 11px;")
            return
        if not os.path.isfile(path):
            self._blender_status.setText("⚠ File not found.")
            self._blender_status.setStyleSheet("color: #cc6666; font-size: 11px;")
            return
        version = query_blender_version(path)
        if not version:
            self._blender_status.setText("⚠ Could not read Blender version.")
            self._blender_status.setStyleSheet("color: #cc6666; font-size: 11px;")
            return
        v_str = f"{version[0]}.{version[1]}.{version[2]}"
        if version_meets_min(version):
            self._blender_status.setText(f"✓ Blender {v_str} detected.")
            self._blender_status.setStyleSheet("color: #88cc88; font-size: 11px;")
        else:
            self._blender_status.setText(f"⚠ Blender {v_str} is too old. Requires {MIN_BLENDER_MAJOR}.0 or newer.")
            self._blender_status.setStyleSheet("color: #cc6666; font-size: 11px;")

    def apply(self) -> None:
        prefs.global_dir = self._dir_edit.text().strip()
        prefs.max_resolution = self._res_combo.currentData()
        prefs.blender_exe = self._blender_edit.text().strip()


# endregion: Files

# region: Search


class _SearchTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        # ── Free assets ───────────────────────────────────────────────────
        layout.addWidget(_section("Asset Filter"))
        layout.addWidget(_hr())

        self._free_only = QCheckBox("Show free assets only")
        self._free_only.setChecked(prefs.search_free_only)
        layout.addWidget(self._free_only)

        # ── Texture resolution filter ─────────────────────────────────────
        layout.addWidget(_section("Texture Resolution Filter"))
        layout.addWidget(_hr())
        layout.addWidget(_note("Only show assets whose textures fall within the selected resolution range."))

        self._tex_filter = QCheckBox("Enable texture resolution filter")
        self._tex_filter.setChecked(prefs.search_texture_resolution)
        layout.addWidget(self._tex_filter)

        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 0)
        form.setSpacing(6)

        self._tex_min = QSpinBox()
        self._tex_min.setRange(64, 16384)
        self._tex_min.setSingleStep(256)
        self._tex_min.setSuffix(" px")
        self._tex_min.setValue(prefs.search_texture_resolution_min)
        form.addRow("Min resolution:", self._tex_min)

        self._tex_max = QSpinBox()
        self._tex_max.setRange(64, 16384)
        self._tex_max.setSingleStep(256)
        self._tex_max.setSuffix(" px")
        self._tex_max.setValue(prefs.search_texture_resolution_max)
        form.addRow("Max resolution:", self._tex_max)

        layout.addLayout(form)

        # enable/disable form based on checkbox
        self._tex_filter.toggled.connect(self._tex_min.setEnabled)
        self._tex_filter.toggled.connect(self._tex_max.setEnabled)
        self._tex_min.setEnabled(prefs.search_texture_resolution)
        self._tex_max.setEnabled(prefs.search_texture_resolution)

        layout.addStretch()

    def apply(self) -> None:
        prefs.search_free_only = self._free_only.isChecked()
        prefs.search_texture_resolution = self._tex_filter.isChecked()
        prefs.search_texture_resolution_min = self._tex_min.value()
        prefs.search_texture_resolution_max = self._tex_max.value()


# endregion: Search

# region: Networking


class _NetworkingTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        # ── Proxy ─────────────────────────────────────────────────────────
        layout.addWidget(_section("Proxy"))
        layout.addWidget(_hr())

        self._proxy_combo = QComboBox()
        for val, label in _PROXY_OPTIONS:
            self._proxy_combo.addItem(label, userData=val)
        current_proxy = next((i for i, (v, _) in enumerate(_PROXY_OPTIONS) if v == prefs.proxy_which), 0)
        self._proxy_combo.setCurrentIndex(current_proxy)
        layout.addWidget(self._proxy_combo)

        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 0)
        self._proxy_addr = QLineEdit(prefs.proxy_address)
        self._proxy_addr.setPlaceholderText("http://proxy.example.com:8080")
        form.addRow("Custom proxy address:", self._proxy_addr)
        layout.addLayout(form)

        # show/hide custom address row
        self._proxy_combo.currentIndexChanged.connect(self._on_proxy_changed)
        self._on_proxy_changed(current_proxy)

        # ── SSL ───────────────────────────────────────────────────────────
        layout.addWidget(_section("Security"))
        layout.addWidget(_hr())
        layout.addWidget(_note("Disable only in isolated test environments."))

        self._ssl = QCheckBox("Verify SSL certificates")
        self._ssl.setChecked(prefs.ssl_verification)
        layout.addWidget(self._ssl)

        layout.addStretch()

    def _on_proxy_changed(self, idx: int) -> None:
        val = self._proxy_combo.itemData(idx)
        self._proxy_addr.setVisible(val == "CUSTOM")

    def apply(self) -> None:
        prefs.proxy_which = self._proxy_combo.currentData()
        prefs.proxy_address = self._proxy_addr.text().strip()
        prefs.ssl_verification = self._ssl.isChecked()


# endregion: Networking

# region: Account


class _AccountBridge(QObject):
    login_done = Signal(bool)
    logout_done = Signal()


class _AccountTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bridge = _AccountBridge()
        self._bridge.login_done.connect(self._on_login_done)
        self._bridge.logout_done.connect(self._refresh)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        layout.addWidget(_section("Account"))
        layout.addWidget(_hr())

        # Status row
        status_row = QHBoxLayout()
        self._status_lbl = QLabel()
        self._status_lbl.setStyleSheet("font-size: 13px;")
        status_row.addWidget(self._status_lbl)
        status_row.addStretch()
        layout.addLayout(status_row)

        # API key (read-only, masked)
        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 0)
        self._key_lbl = QLabel()
        self._key_lbl.setStyleSheet("color: #888; font-family: monospace;")
        form.addRow("API key:", self._key_lbl)
        layout.addLayout(form)

        layout.addWidget(
            _note(
                "The API key is filled automatically on login. "
                "You can also paste a key from your profile on blenderkit.com.",
            ),
        )

        # Manual API key entry
        self._manual_key = QLineEdit()
        self._manual_key.setPlaceholderText("Paste API key here…")
        self._manual_key.setEchoMode(QLineEdit.Password)
        layout.addWidget(self._manual_key)

        # Buttons
        btn_row = QHBoxLayout()
        self._login_btn = QPushButton("Log In via Browser…")
        self._login_btn.clicked.connect(self._do_login)
        btn_row.addWidget(self._login_btn)

        self._logout_btn = QPushButton("Log Out")
        self._logout_btn.clicked.connect(self._do_logout)
        btn_row.addWidget(self._logout_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        layout.addStretch()

        self._refresh()

    def _refresh(self) -> None:
        logged_in = auth.is_logged_in()
        if logged_in:
            self._status_lbl.setText("● Logged in")
            self._status_lbl.setStyleSheet("color: #4caf50; font-size: 13px;")
            key = auth.get_api_key()
            masked = key[:6] + "…" + key[-4:] if len(key) > 10 else "●●●●●●"
            self._key_lbl.setText(masked)
        else:
            self._status_lbl.setText("○ Not logged in")
            self._status_lbl.setStyleSheet("color: #888; font-size: 13px;")
            self._key_lbl.setText("—")
        self._login_btn.setEnabled(not logged_in)
        self._logout_btn.setEnabled(logged_in)

    def _do_login(self) -> None:
        self._login_btn.setEnabled(False)
        self._login_btn.setText("Waiting for browser…")

        def _run():
            ok = auth.login()
            self._bridge.login_done.emit(ok)

        threading.Thread(target=_run, daemon=True).start()

    def _on_login_done(self, success: bool) -> None:
        if not success:
            self._login_btn.setText("Log In via Browser…")
        self._refresh()

    def _do_logout(self) -> None:
        auth.logout()
        self._bridge.logout_done.emit()

    def apply(self) -> None:
        """Apply manually-entered API key if provided."""
        manual = self._manual_key.text().strip()
        if manual:
            import time

            tokens = {"access_token": manual, "expires_at": time.time() + 86400 * 30}
            from ..core.auth import _save_tokens

            _save_tokens(tokens)
            self._manual_key.clear()
            self._refresh()


# endregion: Account

# region: Main dialog

_dialog_instance: SettingsDialog | None = None


class SettingsDialog(QDialog):
    """Main settings dialog for the BlenderKit Maya client."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("BlenderKit — Settings")
        self.setMinimumSize(520, 480)
        self.setModal(False)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 12)
        root.setSpacing(0)

        self._tabs = QTabWidget()
        self._tab_general = _GeneralTab()
        self._tab_files = _FilesTab()
        self._tab_search = _SearchTab()
        self._tab_network = _NetworkingTab()
        self._tab_account = _AccountTab()

        self._tabs.addTab(self._tab_general, "General")
        self._tabs.addTab(self._tab_files, "Files")
        self._tabs.addTab(self._tab_search, "Search")
        self._tabs.addTab(self._tab_network, "Networking")
        self._tabs.addTab(self._tab_account, "Account")
        root.addWidget(self._tabs)

        # Standard OK / Apply / Cancel buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self._save)
        root.addWidget(buttons)

    def _save(self) -> None:
        for tab in (self._tab_general, self._tab_files, self._tab_search, self._tab_network, self._tab_account):
            tab.apply()
        prefs.save()
        log.info("Settings saved.")

    def _accept(self) -> None:
        self._save()
        self.accept()


def open_settings(parent: QWidget | None = None, tab: str | None = None) -> None:
    """Show the settings dialog (singleton — raises existing one if open).

    If *tab* is given (e.g. ``"Account"``), the dialog opens on that tab.
    """
    global _dialog_instance
    if _dialog_instance is None or not _dialog_instance.isVisible():
        _dialog_instance = SettingsDialog(parent)
    if tab:
        for i in range(_dialog_instance._tabs.count()):
            if _dialog_instance._tabs.tabText(i).lower() == tab.lower():
                _dialog_instance._tabs.setCurrentIndex(i)
                break
    _dialog_instance.show()
    _dialog_instance.raise_()
    _dialog_instance.activateWindow()


# endregion: Main dialog
