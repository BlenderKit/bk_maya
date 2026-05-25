"""BlenderKit asset bar — PySide6 side panel for Maya.

Entry point: ``open_asset_bar()``

Architecture
------------
Tile positioning is done **manually** (``widget.move(x, y)`` inside a plain
``QWidget``).  This bypasses all Qt layout-engine quirks that prevented tiles
from filling the available width.

- Placeholder tiles shown immediately on search; populated as data arrives.
- ``thumbnail_notready.jpg`` used as the placeholder image (matches Blender addon).
- Icon-backed FREE / price / cc0 / royalty-free badge overlays.
- Right-click → detail popup (name, author, description, tags, price, links).
- Vertical scrollbar always visible.
- Smooth exponential-easing wheel scroll.
- Columns reflow automatically when the panel resizes.
- Infinite scroll: placeholder tiles for the next page are added at 80 %.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import webbrowser
from typing import Any

import maya.cmds as cmds

from qtpy.QtCore import Qt, Signal, QObject, QTimer, QEvent
from qtpy.QtGui  import QPixmap, QColor, QCursor
from qtpy.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QScrollArea,
    QLabel, QFrame, QButtonGroup, QStackedWidget,
    QCheckBox, QSpinBox, QSizePolicy, QDialog,
    QTextEdit, QAction, QMenu, QFormLayout,
)

from ..core import auth, search as bk_search
from ..core import icons as bk_icons
from ..core.prefs import prefs
from ..api import client as api

log = logging.getLogger(__name__)

CONTROL_NAME  = "BlenderKitAssetBar"
GRID_SPACING  = 6
PAGE_SIZE     = 24

ASSET_TYPES = [
    ("model",     "Models"),
    ("material",  "Materials"),
    ("scene",     "Scenes"),
    ("hdr",       "HDRIs"),
    ("printable", "Printables"),
]

# Assigned in _populate_workspace_control()
_current_bar: "AssetBarWidget | None" = None


# ---------------------------------------------------------------------------
# Smooth-scrolling QScrollArea  (also emits viewport_resized)
# ---------------------------------------------------------------------------

class _SmoothScrollArea(QScrollArea):
    """QScrollArea with exponential-easing wheel animation."""

    viewport_resized = Signal()

    _EASE    = 0.15
    _TICK_MS = 14
    _STOP_PX = 1.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._target: float = 0.0
        self._anim = QTimer()
        self._anim.setSingleShot(False)
        self._anim.setInterval(self._TICK_MS)
        self._anim.timeout.connect(self._tick)
        self.viewport().installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.viewport() and event.type() == QEvent.Resize:
            self.viewport_resized.emit()
        return super().eventFilter(obj, event)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        delta = event.angleDelta().y()
        bar   = self.verticalScrollBar()
        if not self._anim.isActive():
            self._target = float(bar.value())
        self._target = max(0.0, min(float(bar.maximum()), self._target - delta * 1.5))
        self._anim.start()
        event.accept()

    def _tick(self) -> None:
        bar = self.verticalScrollBar()
        self._target = max(0.0, min(float(bar.maximum()), self._target))
        cur  = float(bar.value())
        diff = self._target - cur
        if abs(diff) <= self._STOP_PX:
            bar.setValue(int(round(self._target)))
            self._anim.stop()
            return
        bar.setValue(int(round(cur + diff * self._EASE)))


# ---------------------------------------------------------------------------
# Thumbnail loader  (background thread → Qt signal)
# ---------------------------------------------------------------------------

class _ThumbnailLoader(QObject):
    loaded = Signal(str, str)   # (asset_id, local_path)

    def load(self, asset_id: str, url: str) -> None:
        def _run() -> None:
            try:
                dest = os.path.join(tempfile.gettempdir(), f"bk_thumb_{asset_id}.jpg")
                if not os.path.exists(dest):
                    api.download_thumbnail(url, dest)
                self.loaded.emit(asset_id, dest)
            except Exception as exc:
                log.debug("Thumb load failed %s: %s", asset_id, exc)
        threading.Thread(target=_run, daemon=True).start()


_thumb_loader = _ThumbnailLoader()


# ---------------------------------------------------------------------------
# Asset detail dialog  (right-click)
# ---------------------------------------------------------------------------

class AssetDetailDialog(QDialog):
    """Non-modal detail popup — shows asset metadata and thumbnail."""

    def __init__(self, asset: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._asset = asset
        self.setWindowTitle(asset.get("name", "Asset detail"))
        self.setMinimumWidth(420)
        self.setStyleSheet(
            "QDialog { background: #1e1e1e; color: #dedede; }"
            "QLabel  { color: #dedede; }"
            "QTextEdit { background: #252525; color: #dedede; border: 1px solid #444; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # ── Header row: thumbnail + quick info ────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(12)

        thumb_lbl = QLabel()
        thumb_lbl.setFixedSize(128, 128)
        thumb_lbl.setAlignment(Qt.AlignCenter)
        thumb_lbl.setStyleSheet("background: #2a2a2a; border-radius: 4px;")

        asset_id = asset.get("assetBaseId") or asset.get("id", "")
        cached = os.path.join(tempfile.gettempdir(), f"bk_thumb_{asset_id}.jpg")
        if os.path.exists(cached):
            pix = QPixmap(cached)
            if not pix.isNull():
                thumb_lbl.setPixmap(
                    pix.scaled(128, 128, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                )
            else:
                thumb_lbl.setPixmap(bk_icons.notready_pixmap(128))
        else:
            thumb_lbl.setPixmap(bk_icons.notready_pixmap(128))

        header.addWidget(thumb_lbl)

        info_layout = QFormLayout()
        info_layout.setHorizontalSpacing(8)
        info_layout.setVerticalSpacing(4)
        info_layout.setLabelAlignment(Qt.AlignRight)

        def _row(label: str, value: str) -> None:
            lbl = QLabel(f"<b>{label}</b>")
            val = QLabel(value or "—")
            val.setWordWrap(True)
            info_layout.addRow(lbl, val)

        _row("Name",      asset.get("name", ""))
        _row("Author",    (asset.get("author", {}) or {}).get("fullName", "")
                          or asset.get("authorUsername", ""))
        _row("Type",      (asset.get("assetType") or "").capitalize())
        is_free = asset.get("isFree", False)
        price   = asset.get("priceExVatFormatted") or asset.get("price")
        _row("Price",     "FREE" if is_free else (str(price) if price else "—"))
        _row("License",   _license_label(asset))
        _row("Downloads", str(asset.get("downloadCount") or ""))

        # Licence icon next to label
        lic_icon = _license_icon_pix(asset, 16)
        if lic_icon and not lic_icon.isNull():
            lic_row = QHBoxLayout()
            lic_ico_lbl = QLabel()
            lic_ico_lbl.setPixmap(lic_icon)
            lic_row.addWidget(lic_ico_lbl)
            lic_row.addWidget(QLabel(_license_label(asset)))
            lic_row.addStretch()

        header.addLayout(info_layout)
        header.addStretch()
        root.addLayout(header)

        # ── Description ────────────────────────────────────────────────────
        desc = asset.get("description", "")
        if desc:
            root.addWidget(QLabel("<b>Description</b>"))
            desc_edit = QTextEdit()
            desc_edit.setPlainText(desc)
            desc_edit.setReadOnly(True)
            desc_edit.setMaximumHeight(100)
            root.addWidget(desc_edit)

        # ── Tags ────────────────────────────────────────────────────────────
        tags: list[str] = asset.get("tags") or []
        if tags:
            tag_lbl = QLabel("<b>Tags:</b>  " + ", ".join(str(t) for t in tags[:20]))
            tag_lbl.setWordWrap(True)
            tag_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
            root.addWidget(tag_lbl)

        # ── Action buttons ─────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        slug = asset.get("slug", "") or asset_id
        if slug:
            view_btn = QPushButton("View on BlenderKit.com")
            view_btn.clicked.connect(lambda: webbrowser.open(
                f"https://www.blenderkit.com/asset-gallery-detail/{slug}/"
            ))
            btn_row.addWidget(view_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    def show_near_cursor(self) -> None:
        """Show dialog near the current cursor, clamped to screen."""
        from qtpy.QtWidgets import QApplication
        pos = QCursor.pos()
        screen = QApplication.screenAt(pos)
        self.adjustSize()
        x = pos.x() + 12
        y = pos.y() + 12
        if screen:
            geo = screen.availableGeometry()
            x = min(x, geo.right()  - self.width()  - 4)
            y = min(y, geo.bottom() - self.height() - 4)
        self.move(x, y)
        self.show()


# ---------------------------------------------------------------------------
# Licence / badge helpers
# ---------------------------------------------------------------------------

_BADGE_SIZE     = 20
_DRAG_THRESHOLD = 8   # px manhattan distance before drag-to-place starts


def _license_label(asset: dict[str, Any]) -> str:
    lic = (asset.get("license") or "").lower()
    mapping = {
        "royalty_free": "Royalty Free",
        "cc_zero":      "CC0",
        "cc-zero":      "CC0",
        "cc0":          "CC0",
        "editorial":    "Editorial",
        "commercial":   "Commercial",
    }
    return mapping.get(lic, lic.replace("_", " ").title()) if lic else "—"


def _license_icon_pix(asset: dict[str, Any], size: int = _BADGE_SIZE) -> "QPixmap | None":
    lic = (asset.get("license") or "").lower()
    if lic in ("cc_zero", "cc-zero", "cc0"):
        return bk_icons.icon("cc0", size=size)
    if lic == "royalty_free":
        return bk_icons.icon("royalty_free", size=size)
    return None


def _main_badge_pix(asset: dict[str, Any]) -> "QPixmap | None":
    """Return the primary badge pixmap for a tile (FREE / sale / cc0 / rf)."""
    is_free = bool(asset.get("isFree", False))
    if is_free:
        return bk_icons.icon("free_plan", size=_BADGE_SIZE)

    lic = (asset.get("license") or "").lower()
    if lic in ("cc_zero", "cc-zero", "cc0"):
        return bk_icons.icon("cc0", size=_BADGE_SIZE)
    if lic == "royalty_free":
        return bk_icons.icon("royalty_free", size=_BADGE_SIZE)

    price = asset.get("priceExVatFormatted") or asset.get("price")
    if price:
        return bk_icons.icon("sale_purple", size=_BADGE_SIZE)

    return None


def _verification_pix(asset: dict[str, Any]) -> "QPixmap | None":
    """Return verification-status badge (vs_*.png), or None."""
    status = (asset.get("verificationStatus") or "").lower().replace(" ", "_")
    icon_map = {
        "validated":  "vs_validated",
        "ready":      "vs_ready",
        "on_hold":    "vs_on_hold",
        "uploaded":   "vs_uploaded",
        "uploading":  "vs_uploading",
        "rejected":   "vs_rejected",
        "deleted":    "vs_deleted",
    }
    key = icon_map.get(status)
    return bk_icons.icon(key, size=_BADGE_SIZE) if key else None


# ---------------------------------------------------------------------------
# Asset tile  — placeholder first, populated later
# ---------------------------------------------------------------------------

class AssetTile(QFrame):
    """Single asset card.

    - Placeholder: shows ``thumbnail_notready.jpg`` (same as Blender addon).
    - ``populate(asset)`` fills with real data and starts thumb download.
    - Icon-backed badges: FREE (free_plan.png), CC0 (cc0.png), sale (sale_purple.png),
      verification status (vs_validated.png etc.) at bottom-right.
    - Right-click → context menu → ``AssetDetailDialog``.
    """

    def __init__(self, cell_w: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._asset:          dict[str, Any] = {}
        self._asset_id:       str  = ""
        self._thumb_path:     str  = ""
        self._is_placeholder: bool = True
        self._thumb_sz:       int  = max(32, cell_w - 8)
        self._press_pos:      QPoint | None = None

        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            "AssetTile { background: #252525; border-radius: 4px; }"
            "AssetTile:hover { background: #2f2f2f; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(3)

        # Thumbnail (parent for badge overlays)
        self._thumb = QLabel()
        self._thumb.setFixedSize(self._thumb_sz, self._thumb_sz)
        self._thumb.setAlignment(Qt.AlignCenter)
        self._thumb.setStyleSheet("background: #1a1a1a; border-radius: 3px;")
        self._show_notready()
        root.addWidget(self._thumb)

        # Top-left: price / FREE text badge
        self._badge_text = QLabel(self._thumb)
        self._badge_text.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._badge_text.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self._badge_text.hide()

        # Bottom-right: lock icon (shown when asset cannot be downloaded)
        self._lock_badge = QLabel(self._thumb)
        self._lock_badge.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._lock_badge.setFixedSize(_BADGE_SIZE, _BADGE_SIZE)
        self._lock_badge.hide()

        # Asset name label
        self._name = QLabel("…")
        self._name.setAlignment(Qt.AlignCenter)
        self._name.setWordWrap(False)
        self._name.setStyleSheet("color: #555; font-size: 11px;")
        root.addWidget(self._name)

        self.setFixedSize(cell_w, self._thumb_sz + 36)

    # ── Public API ────────────────────────────────────────────────────────

    def populate(self, asset: dict[str, Any]) -> None:
        """Fill placeholder with real asset data.  Called exactly once."""
        if not self._is_placeholder:
            return
        self._is_placeholder = False
        self._asset    = asset
        self._asset_id = asset.get("assetBaseId", "") or asset.get("id", "")

        name = asset.get("name", "Unnamed")
        self._name.setText(name[:30])
        self._name.setStyleSheet("color: #dedede; font-size: 12px;")
        self.setToolTip(name)

        # ── Price badge (top-left) — only shown for paid assets ──────────
        if not asset.get("isFree", False):
            price = asset.get("priceExVatFormatted") or asset.get("price")
            if price:
                self._set_badge_text(str(price), "#ffffff", "#9620d1")

        # ── Lock badge (bottom-right) — shown when asset can't be downloaded
        if not asset.get("canDownload", True):
            lock_pix = bk_icons.icon("locked", size=_BADGE_SIZE)
            if lock_pix and not lock_pix.isNull():
                self._lock_badge.setPixmap(lock_pix)
                self._lock_badge.move(self._thumb_sz - _BADGE_SIZE - 4,
                                      self._thumb_sz - _BADGE_SIZE - 4)
                self._lock_badge.show()
                self._lock_badge.raise_()

        # ── Thumbnail download ─────────────────────────────────────────────
        url = asset.get("thumbnailSmallUrl") or asset.get("thumbnailMiddleUrl", "")
        if url:
            _thumb_loader.loaded.connect(self._on_loaded)
            _thumb_loader.load(self._asset_id, url)

    def resize_to(self, cell_w: int) -> None:
        """Resize for reflow without losing loaded state."""
        thumb_sz = max(32, cell_w - 8)
        self._thumb_sz = thumb_sz
        self._thumb.setFixedSize(thumb_sz, thumb_sz)
        self.setFixedSize(cell_w, thumb_sz + 36)

        if self._thumb_path:
            self._apply_pix(self._thumb_path)
        else:
            self._show_notready()

        if self._lock_badge.isVisible():
            self._lock_badge.move(thumb_sz - _BADGE_SIZE - 4,
                                  thumb_sz - _BADGE_SIZE - 4)

    # ── Context menu ─────────────────────────────────────────────────────

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        if not self._asset:
            return
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #2a2a2a; color: #dedede; border: 1px solid #444; }"
            "QMenu::item:selected { background: #0078d4; }"
        )

        detail_act = QAction("Asset detail…", menu)
        detail_act.triggered.connect(self._open_detail)
        menu.addAction(detail_act)

        slug = self._asset.get("slug") or self._asset.get("assetBaseId", "")
        if slug:
            web_act = QAction("View on BlenderKit.com", menu)
            web_act.triggered.connect(
                lambda: webbrowser.open(
                    f"https://www.blenderkit.com/asset-gallery-detail/{slug}/"
                )
            )
            menu.addAction(web_act)

        menu.exec(event.globalPos())

    # ── Mouse drag initiation ─────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton and not self._is_placeholder:
            self._press_pos = event.globalPos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if (
            self._press_pos is not None
            and not self._is_placeholder
            and self._asset
            and (event.globalPos() - self._press_pos).manhattanLength()
            >= _DRAG_THRESHOLD
        ):
            self._press_pos = None   # prevent re-triggering
            from bk_maya.ui.placement import start_drag
            start_drag(self._asset, self._thumb_path)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def _open_detail(self) -> None:
        dlg = AssetDetailDialog(self._asset, parent=self.window())
        dlg.setWindowModality(Qt.NonModal)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.show_near_cursor()

    # ── Private ───────────────────────────────────────────────────────────

    def _show_notready(self) -> None:
        pix = bk_icons.notready_pixmap(self._thumb_sz)
        if pix.isNull():
            pix = QPixmap(self._thumb_sz, self._thumb_sz)
            pix.fill(QColor("#1e1e1e"))
        self._thumb.setPixmap(pix)

    def _set_badge_text(self, text: str, color: str, bg: str) -> None:
        self._badge_text.setText(text)
        self._badge_text.setStyleSheet(
            f"background: {bg}; border-radius: 3px; "
            f"font-size: 9px; font-weight: bold; color: {color}; padding: 1px 5px;"
        )
        self._badge_text.adjustSize()
        self._badge_text.move(4, 4)
        self._badge_text.show()
        self._badge_text.raise_()

    def _apply_pix(self, path: str) -> None:
        pix = QPixmap(path)
        if not pix.isNull():
            self._thumb.setPixmap(
                pix.scaled(
                    self._thumb_sz, self._thumb_sz,
                    Qt.KeepAspectRatioByExpanding,
                    Qt.SmoothTransformation,
                )
            )

    def _on_loaded(self, asset_id: str, path: str) -> None:
        if asset_id != self._asset_id:
            return
        self._thumb_path = path
        self._apply_pix(path)


# ---------------------------------------------------------------------------
# Tile container  — no Qt layout manager, manual geometry
# ---------------------------------------------------------------------------

class _TileContainer(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(8)

    def reflow(self, tiles: list[AssetTile], cols: int, cell_w: int) -> None:
        if not tiles or cols < 1 or cell_w < 1:
            self.setFixedHeight(8)
            return

        cell_h = max(32, cell_w - 8) + 36
        margin = 4

        for idx, tile in enumerate(tiles):
            tile.resize_to(cell_w)
            row, col_i = divmod(idx, cols)
            x = margin + col_i * (cell_w + GRID_SPACING)
            y = margin + row   * (cell_h + GRID_SPACING)
            tile.move(x, y)
            tile.show()

        n_rows  = (len(tiles) + cols - 1) // cols
        total_h = margin + n_rows * cell_h + max(0, n_rows - 1) * GRID_SPACING + margin
        self.setFixedHeight(max(total_h, 8))


# ---------------------------------------------------------------------------
# Asset grid  — search state + tile lifecycle
# ---------------------------------------------------------------------------

class _ResultsBridge(QObject):
    results_ready  = Signal(list, int, str)   # (results, total, next_url)
    error_occurred = Signal(str)


class AssetGrid(QWidget):

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._bridge = _ResultsBridge()
        self._bridge.results_ready.connect(self._on_results)
        self._bridge.error_occurred.connect(self._on_error)

        self._query:           str  = ""
        self._asset_type:      str  = "model"
        self._results:         list[dict[str, Any]] = []
        self._total:           int  = 0
        self._next_url:        str  = ""    # cursor URL for next page
        self._loading:         bool = False
        self._free_only:       bool = False
        self._texture_res_min: int  = 0
        self._texture_res_max: int  = 0

        self._tiles:     list[AssetTile] = []
        self._next_fill: int = 0

        self._cols:    int = 4
        self._last_vw: int = 0

        self._reflow_timer = QTimer()
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.setInterval(120)
        self._reflow_timer.timeout.connect(self._do_reflow)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._stack = QStackedWidget()
        outer.addWidget(self._stack)

        self._scroll = _SmoothScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self._scroll.viewport_resized.connect(self._on_viewport_resized)

        self._container = _TileContainer()
        self._scroll.setWidget(self._container)
        self._stack.addWidget(self._scroll)

        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)

        self._status_lbl = QLabel("Search for assets above.")
        self._status_lbl.setAlignment(Qt.AlignCenter)
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet("color: #888; font-size: 13px; padding: 20px;")
        self._stack.addWidget(self._status_lbl)

        self._stack.setCurrentIndex(1)

    # ── Column / cell geometry ────────────────────────────────────────────

    def _tile_w(self) -> int:
        return max(64, prefs.thumbnail_size)

    def _cols_for_width(self, px: int) -> int:
        return max(1, (px - 8) // (self._tile_w() + GRID_SPACING))

    # ── Resize handling ───────────────────────────────────────────────────

    def _on_viewport_resized(self) -> None:
        vw = self._scroll.viewport().width()
        if vw < 32 or vw == self._last_vw:
            return
        self._last_vw = vw
        self._reflow_timer.start()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._reflow_timer.start()

    def _do_reflow(self) -> None:
        if not self._tiles:
            return
        vw = self._scroll.viewport().width() or self.width()
        if vw >= 32:
            self._cols    = self._cols_for_width(vw)
            self._last_vw = vw
        self._container.reflow(self._tiles, self._cols, self._tile_w())

    # ── Infinite scroll ───────────────────────────────────────────────────

    def _on_scroll(self, value: int) -> None:
        """Trigger next-page load when user scrolls to 80 % of content.
        Auto-fill (content shorter than viewport) is handled by
        _check_scroll_threshold; this handler only fires on real scrolling.
        """
        bar = self._scroll.verticalScrollBar()
        mx  = bar.maximum()
        if mx == 0:
            return  # no scrollbar yet — auto-fill is not this handler's job
        more = (self._total == 0) or (len(self._results) < self._total)
        if more and value >= mx * 0.80:
            self._load_next_page()

    def _load_next_page(self) -> None:
        if self._loading:
            return
        if self._total > 0 and len(self._results) >= self._total:
            return
        if not self._results:
            return
        self._loading = True
        log.debug("Loading next page, offset=%d next_url=%r",
                  len(self._results), self._next_url or "(none)")
        self._add_placeholders(PAGE_SIZE)
        bk_search.search(
            query           = self._query,
            asset_type      = self._asset_type,
            free_only       = self._free_only,
            texture_res_min = self._texture_res_min,
            texture_res_max = self._texture_res_max,
            page_size       = PAGE_SIZE,
            next_url        = self._next_url,
            on_results      = self._bridge.results_ready.emit,
            on_error        = self._bridge.error_occurred.emit,
        )

    # ── Placeholder management ────────────────────────────────────────────

    def _add_placeholders(self, count: int) -> None:
        tw = self._tile_w()
        for _ in range(count):
            tile = AssetTile(tw, self._container)
            self._tiles.append(tile)
        self._container.reflow(self._tiles, self._cols, tw)

    def _trim_placeholders(self) -> None:
        while len(self._tiles) > self._next_fill:
            tile = self._tiles.pop()
            tile.hide()
            tile.deleteLater()
        if self._tiles:
            self._container.reflow(self._tiles, self._cols, self._tile_w())

    # ── Public API ────────────────────────────────────────────────────────

    def start_search(
        self,
        query:           str,
        asset_type:      str,
        free_only:       bool = False,
        texture_res_min: int  = 0,
        texture_res_max: int  = 0,
    ) -> None:
        self._query           = query
        self._asset_type      = asset_type
        self._free_only       = free_only
        self._texture_res_min = texture_res_min
        self._texture_res_max = texture_res_max
        self._results   = []
        self._total     = 0
        self._next_url  = ""
        self._loading   = True
        self._next_fill = 0
        self._clear_tiles()

        log.debug("start_search query=%r type=%s free=%s", query, asset_type, free_only)

        vw = self._scroll.viewport().width() or self.width()
        if vw >= 32:
            self._cols    = self._cols_for_width(vw)
            self._last_vw = vw

        self._stack.setCurrentIndex(0)
        self._add_placeholders(PAGE_SIZE)

        bk_search.search(
            query           = query,
            asset_type      = asset_type,
            free_only       = free_only,
            texture_res_min = texture_res_min,
            texture_res_max = texture_res_max,
            page_size       = PAGE_SIZE,
            on_results      = self._bridge.results_ready.emit,
            on_error        = self._bridge.error_occurred.emit,
        )

    def set_tile_size(self, size: int) -> None:
        self._last_vw = 0
        self._do_reflow()

    # ── Internals ─────────────────────────────────────────────────────────

    def _clear_tiles(self) -> None:
        for tile in self._tiles:
            tile.hide()
            tile.deleteLater()
        self._tiles.clear()
        self._next_fill   = 0
        self._container.setFixedHeight(8)

    def _set_status(self, text: str) -> None:
        self._status_lbl.setText(text)
        self._stack.setCurrentIndex(1)

    def _on_results(self, results: list[dict[str, Any]], total: int, next_url: str) -> None:
        self._loading   = False
        self._total     = total
        self._next_url  = next_url or ""
        log.debug("_on_results count=%d total=%d next=%r",
                  len(results), total, self._next_url or "(none)")

        if not results:
            if not self._results:
                self._clear_tiles()
                self._set_status("No results found.")
            else:
                self._trim_placeholders()
            return

        self._stack.setCurrentIndex(0)

        if not self._results:
            vw = self._scroll.viewport().width()
            if vw >= 32 and vw != self._last_vw:
                self._cols    = self._cols_for_width(vw)
                self._last_vw = vw
                self._container.reflow(self._tiles, self._cols, self._tile_w())

        tw = self._tile_w()
        for asset in results:
            if self._next_fill < len(self._tiles):
                self._tiles[self._next_fill].populate(asset)
                self._next_fill += 1
            else:
                tile = AssetTile(tw, self._container)
                tile.populate(asset)
                self._tiles.append(tile)
                self._next_fill = len(self._tiles)

        self._results.extend(results)
        self._trim_placeholders()

        # Scrollbar maximum just changed — re-check 80 % threshold
        QTimer.singleShot(0, self._check_scroll_threshold)

    def _on_error(self, message: str) -> None:
        self._loading = False
        log.error("Search error: %s", message)
        self._clear_tiles()
        self._set_status(f"Search error:\n{message}")

    def _check_scroll_threshold(self) -> None:
        """Load the next page when:
        - The user has scrolled to ≥ 80 % of the current content, OR
        - The content does not yet fill the viewport (scrollbar max == 0)
          and there are still more results to fetch.

        This second condition is what makes the grid auto-fill: after each
        page the Qt event loop settles, max is recomputed, and if the
        viewport is still not full we immediately kick off another page.
        """
        bar  = self._scroll.verticalScrollBar()
        mx   = bar.maximum()
        more = (self._total == 0) or (len(self._results) < self._total)

        if more and (mx == 0 or bar.value() >= mx * 0.80):
            self._load_next_page()


# ---------------------------------------------------------------------------
# Search bar  (query input + asset-type pills)
# ---------------------------------------------------------------------------

class SearchBar(QWidget):
    search_requested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("SearchBar { background: #334066; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 4)
        layout.setSpacing(4)

        input_row = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText("Search BlenderKit assets…")
        self._input.returnPressed.connect(self._emit)
        input_row.addWidget(self._input)
        btn = QPushButton("Search")
        btn.setFixedWidth(62)
        btn.clicked.connect(self._emit)
        input_row.addWidget(btn)
        layout.addLayout(input_row)

        pills_row = QHBoxLayout()
        pills_row.setSpacing(4)
        self._pill_group = QButtonGroup(self)
        self._pill_group.setExclusive(True)
        for type_id, label in ASSET_TYPES:
            pill = QPushButton(label)
            pill.setCheckable(True)
            pill.setProperty("asset_type", type_id)
            pill.setFixedHeight(22)
            pill.setStyleSheet(
                "QPushButton { border: 1px solid #555; border-radius: 11px; "
                "             padding: 0 8px; color: #aaa; font-size: 11px;"
                "             background: transparent; }"
                "QPushButton:checked { background: #0078d4; border-color: #0078d4;"
                "                      color: white; }"
            )
            self._pill_group.addButton(pill)
            pills_row.addWidget(pill)
            if type_id == "model":
                pill.setChecked(True)
        pills_row.addStretch()
        layout.addLayout(pills_row)
        # Switching tabs immediately fires a new search
        self._pill_group.buttonToggled.connect(
            lambda btn, checked: self._emit() if checked else None
        )

    def _emit(self) -> None:
        checked    = self._pill_group.checkedButton()
        asset_type = checked.property("asset_type") if checked else "model"
        self.search_requested.emit(self._input.text().strip(), asset_type)

    @property
    def current_query(self) -> str:
        return self._input.text().strip()

    @property
    def current_asset_type(self) -> str:
        checked = self._pill_group.checkedButton()
        return checked.property("asset_type") if checked else "model"


# ---------------------------------------------------------------------------
# Filters panel  (collapsible)
# ---------------------------------------------------------------------------

class _FiltersPanel(QWidget):
    filters_changed = Signal()
    _DEBOUNCE_MS = 450

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._debounce = QTimer()
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self._DEBOUNCE_MS)
        self._debounce.timeout.connect(self.filters_changed)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._toggle = QPushButton("▶  Filters")
        self._toggle.setCheckable(True)
        self._toggle.setFlat(True)
        self._toggle.setStyleSheet(
            "QPushButton { text-align: left; padding: 3px 8px; "
            "              color: #c8d0e0; font-size: 11px; border: none;"
            "              background: #334066; }"
            "QPushButton:hover { color: #ffffff; }"
        )
        self._toggle.toggled.connect(self._on_toggled)
        root.addWidget(self._toggle)

        self._body = QWidget()
        self._body.setStyleSheet("background: #252525;")
        body_l = QVBoxLayout(self._body)
        body_l.setContentsMargins(12, 6, 12, 8)
        body_l.setSpacing(5)

        self._free_only = QCheckBox("Free assets only")
        self._free_only.setChecked(prefs.search_free_only)
        self._free_only.toggled.connect(self._schedule)
        body_l.addWidget(self._free_only)

        self._tex_filter = QCheckBox("Limit texture resolution")
        self._tex_filter.setChecked(prefs.search_texture_resolution)
        self._tex_filter.toggled.connect(self._schedule)
        body_l.addWidget(self._tex_filter)

        tex_row = QHBoxLayout()
        tex_row.setContentsMargins(20, 0, 0, 0)
        tex_row.setSpacing(4)
        tex_row.addWidget(QLabel("Min"))
        self._tex_min = QSpinBox()
        self._tex_min.setRange(64, 16384)
        self._tex_min.setSingleStep(256)
        self._tex_min.setSuffix(" px")
        self._tex_min.setFixedWidth(78)
        self._tex_min.setValue(prefs.search_texture_resolution_min)
        self._tex_min.valueChanged.connect(self._schedule)
        tex_row.addWidget(self._tex_min)
        tex_row.addWidget(QLabel("Max"))
        self._tex_max = QSpinBox()
        self._tex_max.setRange(64, 16384)
        self._tex_max.setSingleStep(256)
        self._tex_max.setSuffix(" px")
        self._tex_max.setFixedWidth(78)
        self._tex_max.setValue(prefs.search_texture_resolution_max)
        self._tex_max.valueChanged.connect(self._schedule)
        tex_row.addWidget(self._tex_max)
        tex_row.addStretch()
        body_l.addLayout(tex_row)

        self._tex_min.setEnabled(prefs.search_texture_resolution)
        self._tex_max.setEnabled(prefs.search_texture_resolution)
        self._tex_filter.toggled.connect(self._tex_min.setEnabled)
        self._tex_filter.toggled.connect(self._tex_max.setEnabled)

        root.addWidget(self._body)
        self._body.setVisible(False)

    def _on_toggled(self, checked: bool) -> None:
        self._toggle.setText(("▼" if checked else "▶") + "  Filters")
        self._body.setVisible(checked)

    def _schedule(self) -> None:
        prefs.search_free_only              = self._free_only.isChecked()
        prefs.search_texture_resolution     = self._tex_filter.isChecked()
        prefs.search_texture_resolution_min = self._tex_min.value()
        prefs.search_texture_resolution_max = self._tex_max.value()
        self._debounce.start()

    @property
    def free_only(self) -> bool:
        return self._free_only.isChecked()

    @property
    def tex_res_enabled(self) -> bool:
        return self._tex_filter.isChecked()

    @property
    def tex_res_min(self) -> int:
        return self._tex_min.value() if self.tex_res_enabled else 0

    @property
    def tex_res_max(self) -> int:
        return self._tex_max.value() if self.tex_res_enabled else 0


# ---------------------------------------------------------------------------
# Login banner  (logo + message + button)
# ---------------------------------------------------------------------------

class _LoginBanner(QWidget):
    login_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        logo_lbl = QLabel()
        logo_pix = bk_icons.logo_pixmap(40)
        if not logo_pix.isNull():
            logo_lbl.setPixmap(logo_pix)
        layout.addWidget(logo_lbl)

        lbl = QLabel("Not logged in — some results may be limited.")
        lbl.setStyleSheet("color: #c8d0e0; font-size: 11px;")
        layout.addWidget(lbl)
        layout.addStretch()
        btn = QPushButton("Log in")
        btn.setFixedWidth(56)
        btn.clicked.connect(self.login_clicked)
        layout.addWidget(btn)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background: #334066;")


# ---------------------------------------------------------------------------
# Main panel widget
# ---------------------------------------------------------------------------

class AssetBarWidget(QWidget):

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("BlenderKit")
        self.setMinimumWidth(260)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._login_banner = _LoginBanner()
        self._login_banner.login_clicked.connect(self._do_login)
        layout.addWidget(self._login_banner)

        self._search_bar = SearchBar()
        self._search_bar.search_requested.connect(self._on_search)
        layout.addWidget(self._search_bar)

        self._filters = _FiltersPanel()
        self._filters.filters_changed.connect(self._on_filters_changed)
        layout.addWidget(self._filters)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #333;")
        layout.addWidget(sep)

        self._grid = AssetGrid()
        layout.addWidget(self._grid, stretch=1)

        self._refresh_login_state()
        QTimer.singleShot(500, self._default_search)

    def _refresh_login_state(self) -> None:
        self._login_banner.setVisible(not auth.is_logged_in())

    def _do_login(self) -> None:
        def _run() -> None:
            ok = auth.login()
            if ok:
                self._refresh_login_state()
        threading.Thread(target=_run, daemon=True).start()

    def _on_search(self, query: str, asset_type: str) -> None:
        self._grid.start_search(
            query, asset_type,
            free_only       = self._filters.free_only,
            texture_res_min = self._filters.tex_res_min,
            texture_res_max = self._filters.tex_res_max,
        )

    def _on_filters_changed(self) -> None:
        self._grid.start_search(
            self._search_bar.current_query,
            self._search_bar.current_asset_type,
            free_only       = self._filters.free_only,
            texture_res_min = self._filters.tex_res_min,
            texture_res_max = self._filters.tex_res_max,
        )

    def _default_search(self) -> None:
        self._grid.start_search(
            "",
            self._search_bar.current_asset_type,
            free_only       = self._filters.free_only,
            texture_res_min = self._filters.tex_res_min,
            texture_res_max = self._filters.tex_res_max,
        )


# ---------------------------------------------------------------------------
# Maya integration
# ---------------------------------------------------------------------------

def open_asset_bar() -> None:
    """Create or restore the BlenderKit side panel (docked by default)."""
    if cmds.workspaceControl(CONTROL_NAME, query=True, exists=True):
        cmds.workspaceControl(CONTROL_NAME, edit=True, restore=True)
        return

    kw: dict = dict(
        label        = "BlenderKit",
        uiScript     = (
            "import bk_maya.ui.asset_bar as _ab; "
            "_ab._populate_workspace_control()"
        ),
        initialWidth = 340,
        retain       = False,
    )

    try:
        if cmds.workspaceControl("AttributeEditor", query=True, exists=True):
            kw["tabToControl"] = ("AttributeEditor", -1)
            kw["floating"]     = False
    except Exception:
        kw["floating"] = True

    cmds.workspaceControl(CONTROL_NAME, **kw)
    log.info("BlenderKit asset bar opened.")


def set_tile_size(size: int) -> None:
    """Update thumbnail density and reflow the live panel if open."""
    prefs.thumbnail_size = size
    prefs.save()
    if _current_bar is not None:
        _current_bar._grid.set_tile_size(size)
    log.info("Thumbnail size → %d px", size)


def _populate_workspace_control() -> None:
    """Called by Maya's uiScript when the workspaceControl is created."""
    global _current_bar
    try:
        from maya.OpenMayaUI import MQtUtil   # type: ignore
        import shiboken6                       # type: ignore
    except ImportError:
        log.error("shiboken6 not available — cannot embed Qt widget.")
        return

    ptr = MQtUtil.findControl(CONTROL_NAME)
    if not ptr:
        log.error("workspaceControl '%s' not found.", CONTROL_NAME)
        return

    parent_widget: QWidget = shiboken6.wrapInstance(int(ptr), QWidget)
    parent_layout = parent_widget.layout()

    bar = AssetBarWidget(parent_widget)
    _current_bar = bar

    if parent_layout is not None:
        parent_layout.addWidget(bar)
    log.debug("AssetBarWidget embedded into workspaceControl.")
