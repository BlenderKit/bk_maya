"""Blendkit asset bar — PySide6 side panel for Maya.

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
import threading
import webbrowser
from typing import Any

import maya.cmds as cmds
from qtpy.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from qtpy.QtGui import QColor, QCursor, QPixmap
from qtpy.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import auth, client_lib
from ..core import bookmarks as bk_bookmarks
from ..core import icons as bk_icons
from ..core import search as bk_search
from ..core.prefs import prefs

log = logging.getLogger(__name__)

CONTROL_NAME = "BlendkitAssetBar"
GRID_SPACING = 6
PAGE_SIZE = 24

ASSET_TYPES = [
    ("model", "Models"),
    ("material", "Materials"),
    ("scene", "Scenes"),
    ("hdr", "HDRIs"),
    ("printable", "Printables"),
]

# Assigned in _populate_workspace_control()
_current_bar: AssetBarWidget | None = None


def _on_login_state_changed() -> None:
    """Login listener (registered once at import): refresh the live panel.

    Called by ``auth`` on the poller thread after a fresh login; marshals onto
    the Qt GUI thread to hide the login banner and re-run the active search.
    """
    bar = _current_bar
    if bar is None:
        return
    QTimer.singleShot(0, bar.on_logged_in)


auth.add_login_listener(_on_login_state_changed)


# ---------------------------------------------------------------------------
# Smooth-scrolling QScrollArea  (also emits viewport_resized)
# ---------------------------------------------------------------------------


class _SmoothScrollArea(QScrollArea):
    """QScrollArea with exponential-easing wheel animation."""

    viewport_resized = Signal()

    _EASE = 0.15
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
        bar = self.verticalScrollBar()
        if not self._anim.isActive():
            self._target = float(bar.value())
        self._target = max(0.0, min(float(bar.maximum()), self._target - delta * 1.5))
        self._anim.start()
        event.accept()

    def _tick(self) -> None:
        bar = self.verticalScrollBar()
        self._target = max(0.0, min(float(bar.maximum()), self._target))
        cur = float(bar.value())
        diff = self._target - cur
        if abs(diff) <= self._STOP_PX:
            bar.setValue(int(round(self._target)))  # noqa: RUF046
            self._anim.stop()
            return
        bar.setValue(int(round(cur + diff * self._EASE)))  # noqa: RUF046


# ---------------------------------------------------------------------------
# Report poller  — pulls task updates from the local blendkit-client
# ---------------------------------------------------------------------------
#
# Search results and thumbnail file paths are delivered as ``search`` and
# ``thumbnail_download`` tasks on ``/report``.  A single QTimer per Maya
# session drains the queue and dispatches them via ``client_lib`` to the
# callbacks registered by ``core.search`` and ``AssetTile``.


class _ReportPoller(QObject):
    INTERVAL_MS = 200

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        # Skip the blocking /report call while no client is running or the
        # binary is known missing — otherwise this 200 ms GUI-thread timer
        # stalls trying to reach a dead port and Maya becomes unresponsive.
        if not client_lib.should_poll_reports():
            return
        try:
            tasks = client_lib.get_reports(api_key=auth.get_api_key())
        except Exception as exc:
            log.debug("Report poll error: %s", exc)
            return
        if tasks:
            client_lib.dispatch_tasks(tasks)


_poller: _ReportPoller | None = None


def _ensure_poller() -> _ReportPoller:
    global _poller
    if _poller is None:
        _poller = _ReportPoller()
        _poller.start()
    return _poller


# ---------------------------------------------------------------------------
# Asset detail dialog  (right-click)
# ---------------------------------------------------------------------------


class AssetDetailDialog(QDialog):
    """Non-modal detail popup — shows asset metadata and thumbnail."""

    def __init__(self, asset: dict[str, Any], parent: QWidget | None = None, thumb_path: str = "") -> None:
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
        # Auto-fit the dialog to its content (eliminates empty vertical gaps).
        root.setSizeConstraint(QLayout.SetMinimumSize)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

        # ── Header row: thumbnail + quick info ────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(12)

        thumb_lbl = QLabel()
        thumb_lbl.setFixedSize(128, 128)
        thumb_lbl.setAlignment(Qt.AlignCenter)
        thumb_lbl.setStyleSheet("background: #2a2a2a; border-radius: 4px;")

        asset_id = asset.get("assetBaseId") or asset.get("id", "")
        tempdir = bk_search.get_tempdir(asset.get("assetType") or "model")
        # Prefer the thumbnail the tile already resolved; otherwise probe the
        # client's cache (basenames may be URL-encoded, e.g. "," → "%2C").
        pix = QPixmap(thumb_path) if thumb_path and os.path.exists(thumb_path) else QPixmap()
        if pix.isNull():
            cached = AssetTile._find_cached_thumb(tempdir, asset)
            if cached:
                pix = QPixmap(cached)
        if not pix.isNull():
            thumb_lbl.setPixmap(pix.scaled(128, 128, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
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

        _row("Name", asset.get("name", ""))
        author = asset.get("author", {}) or {}
        author_name = author.get("fullName", "") or asset.get("authorUsername", "")
        _row("Author", author_name)
        _row("Type", (asset.get("assetType") or "").capitalize())
        is_free = asset.get("isFree", False)
        price = asset.get("priceExVatFormatted") or asset.get("price")
        _row("Price", "FREE" if is_free else (str(price) if price else "—"))
        _row("License", _license_label(asset))
        _row("Downloads", str(asset.get("downloadCount") or ""))

        # Average rating (Blendkit returns ``ratingsAverage`` as dict
        # ``{"quality": x, "working_hours": y}`` and ``ratingsCount`` similarly).
        rat_avg = asset.get("ratingsAverage") or {}
        rat_cnt = asset.get("ratingsCount") or {}
        if isinstance(rat_avg, dict):  # noqa: SIM108
            avg_q = rat_avg.get("quality")
        else:
            avg_q = rat_avg
        cnt_q = rat_cnt.get("quality") if isinstance(rat_cnt, dict) else rat_cnt
        if avg_q:
            try:
                avg_f = float(avg_q)
                # Blendkit-maya uses a 1..10 scale (same as the Blendkit-blender addon).
                filled = max(0, min(10, int(round(avg_f))))  # noqa: RUF046
                stars = "★" * filled + "☆" * (10 - filled)
                _row("Rating", f"<span style='color:#f5c33b'>{stars}</span>  {avg_f:.1f}  ({cnt_q or 0})")
            except (TypeError, ValueError):
                pass

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
            desc_lbl = QLabel(desc)
            desc_lbl.setWordWrap(True)
            desc_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
            desc_lbl.setStyleSheet(
                "QLabel { background: #252525; color: #dedede;          border: 1px solid #444; padding: 6px; }"
            )
            desc_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
            desc_lbl.setMinimumWidth(380)
            root.addWidget(desc_lbl)

        # ── Tags ────────────────────────────────────────────────────────────
        tags: list[str] = asset.get("tags") or []
        if tags:
            tag_lbl = QLabel("<b>Tags:</b>  " + ", ".join(str(t) for t in tags[:20]))
            tag_lbl.setWordWrap(True)
            tag_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
            root.addWidget(tag_lbl)

        # ── Your rating (click stars to rate) ───────────────────────────────
        if asset_id and auth.is_logged_in():
            self._rate_status = QLabel("")
            self._rate_status.setStyleSheet("color: #888; font-size: 11px;")
            rate_row = QHBoxLayout()
            rate_row.setSpacing(2)
            rate_row.addWidget(QLabel("<b>Your rating:</b>"))
            self._star_labels: list[QLabel] = []
            for i in range(1, 11):
                star = QLabel("☆")
                star.setStyleSheet("QLabel { color: #f5c33b; font-size: 18px; padding: 0 1px; }")
                star.setCursor(Qt.PointingHandCursor)
                star.mousePressEvent = lambda ev, n=i: self._submit_rating(n)  # type: ignore[assignment]
                self._star_labels.append(star)
                rate_row.addWidget(star)
            rate_row.addWidget(self._rate_status, 1)
            root.addLayout(rate_row)

        # ── Action buttons ─────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        author_id = author.get("id")
        if author_id:
            author_btn = QPushButton(f"More by {author_name or 'this author'}")
            author_btn.clicked.connect(lambda: self._trigger_author_search(int(author_id), author_name))
            btn_row.addWidget(author_btn)

        slug = asset.get("slug", "") or asset_id
        if slug:
            view_btn = QPushButton("View on Blendkit.com")
            view_btn.clicked.connect(lambda: webbrowser.open(f"https://www.blendkit.com/asset-gallery-detail/{slug}/"))
            btn_row.addWidget(view_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    # ── Rating + author-search helpers ──────────────────────────────────────

    def _submit_rating(self, score: int) -> None:
        # The rating endpoint expects the asset version ``id``
        # (``/api/v1/assets/<id>/rating/...``), not ``assetBaseId``.
        asset_id = self._asset.get("id") or self._asset.get("assetBaseId", "")
        if not asset_id:
            return
        # Optimistic UI: fill stars now.
        for idx, lbl in enumerate(getattr(self, "_star_labels", []), start=1):
            lbl.setText("★" if idx <= score else "☆")
        self._rate_status.setText("Submitting…")

        api_key = auth.get_api_key() or ""

        def _worker() -> None:
            try:
                from ..api import client as api_client

                api_client.rate_asset(asset_id, "quality", float(score), api_key)
                msg, ok = (f"Rated {score}/10", True)
            except Exception as exc:
                log.warning("Rating submission failed: %s", exc)
                msg, ok = (f"Failed: {exc}", False)

            def _apply() -> None:
                if not hasattr(self, "_rate_status"):
                    return
                self._rate_status.setText(msg)
                if not ok:
                    # Revert stars on failure.
                    for lbl in getattr(self, "_star_labels", []):
                        lbl.setText("☆")

            QTimer.singleShot(0, _apply)

        import threading

        threading.Thread(target=_worker, daemon=True).start()

    def _trigger_author_search(self, author_id: int, author_name: str) -> None:
        if _current_bar is not None:
            _current_bar.search_by_author(author_id, author_name)
        self.accept()

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
            x = min(x, geo.right() - self.width() - 4)
            y = min(y, geo.bottom() - self.height() - 4)
        self.move(x, y)
        self.show()


# ---------------------------------------------------------------------------
# Licence / badge helpers
# ---------------------------------------------------------------------------

_BADGE_SIZE = 20
_DRAG_THRESHOLD = 8  # px manhattan distance before drag-to-place starts


def _license_label(asset: dict[str, Any]) -> str:
    lic = (asset.get("license") or "").lower()
    mapping = {
        "royalty_free": "Royalty Free",
        "cc_zero": "CC0",
        "cc-zero": "CC0",
        "cc0": "CC0",
        "editorial": "Editorial",
        "commercial": "Commercial",
    }
    return mapping.get(lic, lic.replace("_", " ").title()) if lic else "—"


def _license_icon_pix(asset: dict[str, Any], size: int = _BADGE_SIZE) -> QPixmap | None:
    lic = (asset.get("license") or "").lower()
    if lic in ("cc_zero", "cc-zero", "cc0"):
        return bk_icons.icon("cc0", size=size)
    if lic == "royalty_free":
        return bk_icons.icon("royalty_free", size=size)
    return None


def _main_badge_pix(asset: dict[str, Any]) -> QPixmap | None:
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


def _verification_pix(asset: dict[str, Any]) -> QPixmap | None:
    """Return verification-status badge (vs_*.png), or None.

    Mirrors the Blender addon (``ui.verification_icons``): "validated" maps to
    no badge so normal browsing — where every public asset is validated — stays
    visually clean.  Only non-validated states get a badge.
    """
    status = (asset.get("verificationStatus") or "").lower().replace(" ", "_")
    icon_map = {
        "ready": "vs_ready",
        "on_hold": "vs_on_hold",
        "uploaded": "vs_uploaded",
        "uploading": "vs_uploading",
        "rejected": "vs_rejected",
        "deleted": "vs_deleted",
    }
    key = icon_map.get(status)
    return bk_icons.icon(key, size=_BADGE_SIZE) if key else None


class _ClickableBadge(QLabel):
    """A small icon label that emits :attr:`clicked` on left-press.

    Used for the bookmark badge overlaid on a tile — it consumes the press so
    it never starts the tile's drag-to-place gesture.
    """

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


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

        self._asset: dict[str, Any] = {}
        self._asset_id: str = ""
        self._bookmark_id: str = ""
        self._thumb_path: str = ""
        self._is_placeholder: bool = True
        self._thumb_sz: int = max(32, cell_w - 8)
        self._press_pos: QPoint | None = None

        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            "AssetTile { background: #252525; border-radius: 4px; }AssetTile:hover { background: #2f2f2f; }"
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

        # Top-right: verification-status icon (vs_uploaded / vs_rejected / …).
        # Hidden for validated assets so normal browsing stays clean — mirrors
        # the Blender addon (ui.verification_icons maps "validated" → None).
        self._vs_badge = QLabel(self._thumb)
        self._vs_badge.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._vs_badge.setFixedSize(_BADGE_SIZE, _BADGE_SIZE)
        self._vs_badge.hide()

        # Bottom-left: bookmark toggle. Shown filled when the asset is
        # bookmarked; an empty outline appears on hover so it can be added.
        # Clickable — consumes the press so it doesn't start a drag.
        self._bookmark_badge = _ClickableBadge(self._thumb)
        self._bookmark_badge.setFixedSize(_BADGE_SIZE, _BADGE_SIZE)
        self._bookmark_badge.setCursor(Qt.PointingHandCursor)
        self._bookmark_badge.clicked.connect(self._toggle_bookmark)
        self._bookmark_badge.hide()
        self._hovering = False

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
        self._asset = asset
        self._asset_id = asset.get("assetBaseId", "") or asset.get("id", "")
        # Ratings/bookmarks are keyed by the asset's specific ``id`` (the
        # ``/assets/{id}/rating/`` endpoint 404s on an assetBaseId).
        self._bookmark_id = asset.get("id", "") or asset.get("assetBaseId", "")

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
                self._lock_badge.move(self._thumb_sz - _BADGE_SIZE - 4, self._thumb_sz - _BADGE_SIZE - 4)
                self._lock_badge.show()
                self._lock_badge.raise_()

        # ── Verification-status badge (top-right) — non-validated assets only.
        vs_pix = _verification_pix(asset)
        if vs_pix and not vs_pix.isNull():
            self._vs_badge.setPixmap(vs_pix)
            self._vs_badge.move(self._thumb_sz - _BADGE_SIZE - 4, 4)
            self._vs_badge.setToolTip((asset.get("verificationStatus") or "").replace("_", " ").title())
            self._vs_badge.show()
            self._vs_badge.raise_()

        # ── Bookmark badge (bottom-left) ─────────────────────────────────
        self._bookmark_badge.move(4, self._thumb_sz - _BADGE_SIZE - 4)
        self.refresh_bookmark_badge()

        # ── Thumbnail handling ─────────────────────────────────────────────
        # The local client downloads all thumbnails into the search tempdir
        # and notifies us via ``thumbnail_download`` tasks.  Register here
        # so the report poller can deliver the file path.  If the file is
        # already on disk (cached from a prior search), pick it up now.
        if self._asset_id:
            tempdir = bk_search.get_tempdir(asset.get("assetType") or "model")
            cached = self._find_cached_thumb(tempdir, asset)
            if cached:
                self._on_thumb_ready(cached)
            else:
                client_lib.thumb_registry.register(self._asset_id, self._on_thumb_ready)
                # Watchdog: if the report poller missed the delivery
                # (e.g. tile created after the task was popped), re-probe
                # the cache that the client wrote.
                QTimer.singleShot(2500, self._recheck_cached_thumb)
                QTimer.singleShot(6000, self._recheck_cached_thumb)

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
            self._lock_badge.move(thumb_sz - _BADGE_SIZE - 4, thumb_sz - _BADGE_SIZE - 4)
        if self._vs_badge.isVisible():
            self._vs_badge.move(thumb_sz - _BADGE_SIZE - 4, 4)
        if not self._is_placeholder:
            self._bookmark_badge.move(4, thumb_sz - _BADGE_SIZE - 4)

    # ── Bookmarks ─────────────────────────────────────────────────────────

    def refresh_bookmark_badge(self) -> None:
        """Sync the bookmark badge icon/visibility with the current state.

        Shows a filled bookmark when the asset is bookmarked; otherwise an
        empty outline is shown only while the tile is hovered so the user can
        add one without cluttering the grid.
        """
        if self._is_placeholder or not self._bookmark_id:
            self._bookmark_badge.hide()
            return
        marked = bk_bookmarks.is_bookmarked(self._bookmark_id)
        if marked:
            self._bookmark_badge.setPixmap(bk_icons.icon("bookmark_full", size=_BADGE_SIZE))
            self._bookmark_badge.setToolTip("Bookmarked — click to remove")
            self._bookmark_badge.show()
            self._bookmark_badge.raise_()
        elif self._hovering:
            self._bookmark_badge.setPixmap(bk_icons.icon("bookmark_empty", size=_BADGE_SIZE))
            self._bookmark_badge.setToolTip("Click to bookmark")
            self._bookmark_badge.show()
            self._bookmark_badge.raise_()
        else:
            self._bookmark_badge.hide()

    def _toggle_bookmark(self) -> None:
        if self._is_placeholder or not self._bookmark_id:
            return
        if not auth.is_logged_in():
            bar = _current_bar
            if bar is not None:
                bar.show_error("Please log in to bookmark assets.")
            return
        bk_bookmarks.toggle(self._bookmark_id)
        # Optimistic: reflect immediately (a listener also refreshes all tiles).
        self.refresh_bookmark_badge()

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._hovering = True
        self.refresh_bookmark_badge()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        # Moving the cursor onto the bookmark badge (a child that accepts
        # clicks) fires this leave; ignore it while the pointer is still
        # inside the tile so the badge doesn't flicker.
        if self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            return super().leaveEvent(event)
        self._hovering = False
        self.refresh_bookmark_badge()
        return super().leaveEvent(event)

    # ── Context menu ─────────────────────────────────────────────────────

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        # Right-click opens the asset detail window directly (author search,
        # "View on Blendkit.com" and rating all live inside it).
        if not self._asset:
            return
        self._open_detail()
        event.accept()

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
            and (event.globalPos() - self._press_pos).manhattanLength() >= _DRAG_THRESHOLD
        ):
            self._press_pos = None  # prevent re-triggering
            from bk_maya.ui.placement import start_drag

            start_drag(self._asset, self._thumb_path)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if (
            event.button() == Qt.LeftButton
            and not self._is_placeholder
            and self._asset
            and self._press_pos is not None
            and (event.globalPos() - self._press_pos).manhattanLength() < 8
        ):
            # Click without drag: place at origin
            from bk_maya.ui.placement import place_at_origin

            place_at_origin(self._asset, self._thumb_path)
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def _open_detail(self) -> None:
        dlg = AssetDetailDialog(self._asset, parent=self.window(), thumb_path=self._thumb_path)
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
                    self._thumb_sz,
                    self._thumb_sz,
                    Qt.KeepAspectRatioByExpanding,
                    Qt.SmoothTransformation,
                )
            )

    def _on_thumb_ready(self, path: str) -> None:
        """Called by the report poller when the client has the file ready."""
        if not path or not os.path.exists(path):
            return
        self._thumb_path = path
        self._apply_pix(path)
        client_lib.thumb_registry.unregister(self._asset_id)

    def _recheck_cached_thumb(self) -> None:
        """Watchdog: re-probe the on-disk cache the client wrote.

        Handles the race where the client's ``thumbnail_download`` task
        was dispatched before this tile registered (so the callback
        never fired), but the file exists on disk.
        """
        if self._thumb_path or self._is_placeholder or not self._asset:
            return
        tempdir = bk_search.get_tempdir(self._asset.get("assetType") or "model")
        cached = self._find_cached_thumb(tempdir, self._asset)
        if cached:
            self._on_thumb_ready(cached)

    @staticmethod
    def _find_cached_thumb(tempdir: str, asset: dict[str, Any]) -> str:
        """Return a previously-downloaded thumbnail path, or ''.

        The Go client writes thumbnails with URL-encoded basenames
        (``,`` → ``%2C``); also try common variants and the larger
        ``Middle`` thumb as a fallback before giving up.
        """
        import urllib.parse as _up

        candidates: list[str] = []
        for key in (
            "thumbnailSmallUrlWebp",
            "thumbnailSmallUrl",
            "thumbnailMiddleUrlWebp",
            "thumbnailMiddleUrl",
        ):
            url = asset.get(key) or ""
            if not url:
                continue
            raw = url.rsplit("/", 1)[-1].split("?", 1)[0]
            if not raw:
                continue
            candidates.append(raw)
            quoted = _up.quote(raw, safe="")
            if quoted != raw:
                candidates.append(quoted)
        for name in candidates:
            p = os.path.join(tempdir, name)
            if os.path.exists(p):
                return p
        return ""


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
            y = margin + row * (cell_h + GRID_SPACING)
            tile.move(x, y)
            tile.show()

        n_rows = (len(tiles) + cols - 1) // cols
        total_h = margin + n_rows * cell_h + max(0, n_rows - 1) * GRID_SPACING + margin
        self.setFixedHeight(max(total_h, 8))


# ---------------------------------------------------------------------------
# Asset grid  — search state + tile lifecycle
# ---------------------------------------------------------------------------


class _ResultsBridge(QObject):
    results_ready = Signal(list, int, str)  # (results, total, next_url)
    error_occurred = Signal(str)


class AssetGrid(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._bridge = _ResultsBridge()
        self._bridge.results_ready.connect(self._on_results)
        self._bridge.error_occurred.connect(self._on_error)

        self._query: str = ""
        self._asset_type: str = "model"
        self._results: list[dict[str, Any]] = []
        self._seen_ids: set[str] = set()
        self._total: int = 0
        self._next_url: str = ""  # cursor URL for next page
        self._loading: bool = False
        self._free_only: bool = False
        self._my_assets_only: bool = False
        self._bookmarked_only: bool = False
        self._quality_limit: int = 0
        self._license_filter: str = "ANY"
        self._animated_only: bool = False
        self._texture_res_min: int = 0
        self._texture_res_max: int = 0
        self._file_size_min: int = 0
        self._file_size_max: int = 0
        self._poly_count_min: int = 0
        self._poly_count_max: int = 0
        self._style: str = "ANY"
        self._condition: str = "UNSPECIFIED"
        self._design_year_min: int = 0
        self._design_year_max: int = 0
        self._geometry_nodes: bool = False
        self._extra_filters: dict[str, Any] = {}

        self._tiles: list[AssetTile] = []
        self._next_fill: int = 0

        self._cols: int = 4
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
            self._cols = self._cols_for_width(vw)
            self._last_vw = vw
        self._container.reflow(self._tiles, self._cols, self._tile_w())

    # ── Infinite scroll ───────────────────────────────────────────────────

    def _on_scroll(self, value: int) -> None:
        """Trigger next-page load when user scrolls to 80 % of content.
        Auto-fill (content shorter than viewport) is handled by
        _check_scroll_threshold; this handler only fires on real scrolling.
        """
        bar = self._scroll.verticalScrollBar()
        mx = bar.maximum()
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
        log.debug("Loading next page, offset=%d next_url=%r", len(self._results), self._next_url or "(none)")
        self._add_placeholders(PAGE_SIZE)
        bk_search.search(
            query=self._query,
            asset_type=self._asset_type,
            free_only=self._free_only,
            my_assets_only=self._my_assets_only,
            bookmarked_only=self._bookmarked_only,
            quality_limit=self._quality_limit,
            license_filter=self._license_filter,
            animated_only=self._animated_only,
            texture_res_min=self._texture_res_min,
            texture_res_max=self._texture_res_max,
            file_size_min=self._file_size_min,
            file_size_max=self._file_size_max,
            poly_count_min=self._poly_count_min,
            poly_count_max=self._poly_count_max,
            style=self._style,
            condition=self._condition,
            design_year_min=self._design_year_min,
            design_year_max=self._design_year_max,
            geometry_nodes=self._geometry_nodes,
            page_size=PAGE_SIZE,
            next_url=self._next_url,
            extra_filters=self._extra_filters or None,
            on_results=self._bridge.results_ready.emit,
            on_error=self._bridge.error_occurred.emit,
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
        query: str,
        asset_type: str,
        free_only: bool = False,
        my_assets_only: bool = False,
        bookmarked_only: bool = False,
        quality_limit: int = 0,
        license_filter: str = "ANY",
        animated_only: bool = False,
        texture_res_min: int = 0,
        texture_res_max: int = 0,
        file_size_min: int = 0,
        file_size_max: int = 0,
        poly_count_min: int = 0,
        poly_count_max: int = 0,
        style: str = "ANY",
        condition: str = "UNSPECIFIED",
        design_year_min: int = 0,
        design_year_max: int = 0,
        geometry_nodes: bool = False,
    ) -> None:
        self._query = query
        self._asset_type = asset_type
        self._free_only = free_only
        self._my_assets_only = my_assets_only
        self._bookmarked_only = bookmarked_only
        self._quality_limit = quality_limit
        self._license_filter = license_filter
        self._animated_only = animated_only
        self._texture_res_min = texture_res_min
        self._texture_res_max = texture_res_max
        self._file_size_min = file_size_min
        self._file_size_max = file_size_max
        self._poly_count_min = poly_count_min
        self._poly_count_max = poly_count_max
        self._style = style
        self._condition = condition
        self._design_year_min = design_year_min
        self._design_year_max = design_year_max
        self._geometry_nodes = geometry_nodes
        self._results = []
        self._seen_ids = set()
        self._total = 0
        self._next_url = ""
        self._loading = True
        self._next_fill = 0
        self._clear_tiles()

        log.debug(
            "start_search query=%r type=%s free=%s quality=%d license=%s animated=%s "
            "tex_res=%d-%d file_size=%d-%d poly=%d-%d",
            query,
            asset_type,
            free_only,
            quality_limit,
            license_filter,
            animated_only,
            texture_res_min,
            texture_res_max,
            file_size_min,
            file_size_max,
            poly_count_min,
            poly_count_max,
        )

        vw = self._scroll.viewport().width() or self.width()
        if vw >= 32:
            self._cols = self._cols_for_width(vw)
            self._last_vw = vw

        self._stack.setCurrentIndex(0)
        self._add_placeholders(PAGE_SIZE)

        bk_search.search(
            query=query,
            asset_type=asset_type,
            free_only=free_only,
            my_assets_only=my_assets_only,
            bookmarked_only=bookmarked_only,
            quality_limit=quality_limit,
            license_filter=license_filter,
            animated_only=animated_only,
            texture_res_min=texture_res_min,
            texture_res_max=texture_res_max,
            file_size_min=file_size_min,
            file_size_max=file_size_max,
            poly_count_min=poly_count_min,
            poly_count_max=poly_count_max,
            style=style,
            condition=condition,
            design_year_min=design_year_min,
            design_year_max=design_year_max,
            geometry_nodes=geometry_nodes,
            page_size=PAGE_SIZE,
            extra_filters=self._extra_filters or None,
            on_results=self._bridge.results_ready.emit,
            on_error=self._bridge.error_occurred.emit,
        )

    def set_tile_size(self, size: int) -> None:
        self._last_vw = 0
        self._do_reflow()

    def set_extra_filters(self, extra: dict[str, Any] | None) -> None:
        """Replace the active per-search filter dict (e.g. ``author_id``)."""
        self._extra_filters = dict(extra) if extra else {}

    def refresh_bookmark_badges(self) -> None:
        """Refresh every live tile's bookmark badge (e.g. after a sync)."""
        for tile in self._tiles:
            try:
                tile.refresh_bookmark_badge()
            except RuntimeError:
                # Tile's underlying C++ object was already deleted — skip.
                continue

    # ── Internals ─────────────────────────────────────────────────────────

    def _clear_tiles(self) -> None:
        for tile in self._tiles:
            if tile._asset_id:
                client_lib.thumb_registry.unregister(tile._asset_id)
            tile.hide()
            tile.deleteLater()
        self._tiles.clear()
        self._next_fill = 0
        self._container.setFixedHeight(8)

    def _set_status(self, text: str) -> None:
        self._status_lbl.setText(text)
        self._stack.setCurrentIndex(1)

    def _on_results(self, results: list[dict[str, Any]], total: int, next_url: str) -> None:
        self._loading = False
        self._total = total
        self._next_url = next_url or ""
        log.debug("_on_results count=%d total=%d next=%r", len(results), total, self._next_url or "(none)")

        # Deleted assets still exist on the backend but must not be shown to the
        # user — they only cause confusion. Drop them before populating tiles.
        # Also drop assets we've already shown: pages can overlap and return the
        # same asset twice, which would spawn duplicate thumbnail tiles.
        deduped: list[dict[str, Any]] = []
        for a in results:
            if (a.get("verificationStatus") or "").lower() == "deleted":
                continue
            asset_id = a.get("assetBaseId") or a.get("id") or ""
            if asset_id and asset_id in self._seen_ids:
                continue
            if asset_id:
                self._seen_ids.add(asset_id)
            deduped.append(a)
        results = deduped

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
                self._cols = self._cols_for_width(vw)
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
        bar = self._scroll.verticalScrollBar()
        mx = bar.maximum()
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
        self._input.setPlaceholderText("Search Blendkit assets…")
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
        # Per-tab search text: each asset type keeps its own query so switching
        # tabs doesn't carry (and re-run) another tab's search. Defaults blank.
        self._active_type = "model"
        self._queries: dict[str, str] = {}
        # Switching tabs restores that tab's remembered query and searches it.
        self._pill_group.buttonToggled.connect(self._on_pill_toggled)

    def _on_pill_toggled(self, btn, checked: bool) -> None:
        if not checked:
            return
        new_type = btn.property("asset_type")
        if new_type == self._active_type:
            return
        # Stash the text typed for the tab we're leaving.
        self._queries[self._active_type] = self._input.text().strip()
        self._active_type = new_type
        # Restore this tab's own query (blank if it was never searched).
        self._input.blockSignals(True)
        self._input.setText(self._queries.get(new_type, ""))
        self._input.blockSignals(False)
        self._emit()

    def _emit(self) -> None:
        checked = self._pill_group.checkedButton()
        asset_type = checked.property("asset_type") if checked else "model"
        query = self._input.text().strip()
        # Remember this tab's query so it persists across tab switches.
        self._queries[asset_type] = query
        self.search_requested.emit(query, asset_type)

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

        # Free assets only
        self._free_only = QCheckBox("Free assets only")
        self._free_only.setChecked(prefs.search_free_only)
        self._free_only.toggled.connect(self._schedule)
        body_l.addWidget(self._free_only)

        # My assets only (requires login; constrains results to author_id)
        self._my_assets = QCheckBox("My assets only")
        self._my_assets.setChecked(prefs.search_my_assets_only)
        self._my_assets.toggled.connect(self._schedule)
        body_l.addWidget(self._my_assets)

        # Bookmarked only (requires login; filters to the account's bookmarks)
        self._bookmarked = QCheckBox("Bookmarked only")
        self._bookmarked.setChecked(prefs.search_bookmarked_only)
        self._bookmarked.toggled.connect(self._schedule)
        body_l.addWidget(self._bookmarked)

        # Quality limit
        quality_row = QHBoxLayout()
        quality_row.setContentsMargins(0, 0, 0, 0)
        self._quality_check = QCheckBox("Min quality")
        self._quality_check.setChecked(prefs.search_quality_limit > 0)
        self._quality_check.toggled.connect(self._schedule)
        quality_row.addWidget(self._quality_check)
        self._quality_limit = QSpinBox()
        self._quality_limit.setRange(0, 5)
        self._quality_limit.setValue(prefs.search_quality_limit)
        self._quality_limit.setFixedWidth(50)
        self._quality_limit.valueChanged.connect(self._schedule)
        quality_row.addWidget(self._quality_limit)
        quality_row.addStretch()
        body_l.addLayout(quality_row)

        # License filter
        license_row = QHBoxLayout()
        license_row.setContentsMargins(0, 0, 0, 0)
        license_row.addWidget(QLabel("License:"))
        self._license = QComboBox()
        self._license.addItems(["Any", "Free", "Royalty Free", "Full", "Usage Rights"])
        self._license.setCurrentText(prefs.search_license or "Any")
        self._license.setFixedWidth(100)
        self._license.currentTextChanged.connect(self._schedule)
        license_row.addWidget(self._license)
        license_row.addStretch()
        body_l.addLayout(license_row)

        # Animated only
        self._animated_only = QCheckBox("Animated assets only")
        self._animated_only.setChecked(prefs.search_animated_only)
        self._animated_only.toggled.connect(self._schedule)
        body_l.addWidget(self._animated_only)

        # Texture resolution
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

        # Polycount filter (model-specific)
        self._poly_filter = QCheckBox("Limit polygon count")
        self._poly_filter.setChecked(prefs.search_poly_count)
        self._poly_filter.toggled.connect(self._schedule)
        body_l.addWidget(self._poly_filter)

        poly_row = QHBoxLayout()
        poly_row.setContentsMargins(20, 0, 0, 0)
        poly_row.setSpacing(4)
        poly_row.addWidget(QLabel("Min"))
        self._poly_min = QSpinBox()
        self._poly_min.setRange(0, 100000000)
        self._poly_min.setSingleStep(10000)
        self._poly_min.setSuffix(" K")
        self._poly_min.setFixedWidth(80)
        self._poly_min.setValue(prefs.search_poly_count_min // 1000)
        self._poly_min.valueChanged.connect(self._schedule)
        poly_row.addWidget(self._poly_min)
        poly_row.addWidget(QLabel("Max"))
        self._poly_max = QSpinBox()
        self._poly_max.setRange(0, 100000000)
        self._poly_max.setSingleStep(10000)
        self._poly_max.setSuffix(" K")
        self._poly_max.setFixedWidth(80)
        self._poly_max.setValue(prefs.search_poly_count_max // 1000)
        self._poly_max.valueChanged.connect(self._schedule)
        poly_row.addWidget(self._poly_max)
        poly_row.addStretch()
        body_l.addLayout(poly_row)

        self._poly_min.setEnabled(prefs.search_poly_count)
        self._poly_max.setEnabled(prefs.search_poly_count)
        self._poly_filter.toggled.connect(self._poly_min.setEnabled)
        self._poly_filter.toggled.connect(self._poly_max.setEnabled)

        # File size filter (MB)
        self._fsize_filter = QCheckBox("Limit file size")
        self._fsize_filter.setChecked(prefs.search_file_size)
        self._fsize_filter.toggled.connect(self._schedule)
        body_l.addWidget(self._fsize_filter)

        fsize_row = QHBoxLayout()
        fsize_row.setContentsMargins(20, 0, 0, 0)
        fsize_row.setSpacing(4)
        fsize_row.addWidget(QLabel("Min"))
        self._fsize_min = QSpinBox()
        self._fsize_min.setRange(0, 100000)
        self._fsize_min.setSingleStep(10)
        self._fsize_min.setSuffix(" MB")
        self._fsize_min.setFixedWidth(80)
        self._fsize_min.setValue(prefs.search_file_size_min)
        self._fsize_min.valueChanged.connect(self._schedule)
        fsize_row.addWidget(self._fsize_min)
        fsize_row.addWidget(QLabel("Max"))
        self._fsize_max = QSpinBox()
        self._fsize_max.setRange(0, 100000)
        self._fsize_max.setSingleStep(10)
        self._fsize_max.setSuffix(" MB")
        self._fsize_max.setFixedWidth(80)
        self._fsize_max.setValue(prefs.search_file_size_max)
        self._fsize_max.valueChanged.connect(self._schedule)
        fsize_row.addWidget(self._fsize_max)
        fsize_row.addStretch()
        body_l.addLayout(fsize_row)
        self._fsize_min.setEnabled(prefs.search_file_size)
        self._fsize_max.setEnabled(prefs.search_file_size)
        self._fsize_filter.toggled.connect(self._fsize_min.setEnabled)
        self._fsize_filter.toggled.connect(self._fsize_max.setEnabled)

        # --- Model-specific filters --------------------------------------
        # Style
        style_row = QHBoxLayout()
        style_row.setContentsMargins(0, 0, 0, 0)
        style_row.addWidget(QLabel("Style:"))
        self._style = QComboBox()
        self._STYLE_LABELS = [
            ("ANY", "Any"),
            ("REALISTIC", "Realistic"),
            ("PAINTERLY", "Painterly"),
            ("LOWPOLY", "Lowpoly"),
            ("ANIME", "Anime"),
            ("2D_VECTOR", "2D Vector"),
            ("3D_GRAPHICS", "3D Graphics"),
            ("OTHER", "Other"),
        ]
        for _api, _lbl in self._STYLE_LABELS:
            self._style.addItem(_lbl, _api)
        # Restore from prefs by API value
        for i, (api_v, _) in enumerate(self._STYLE_LABELS):
            if api_v == (prefs.search_style or "ANY"):
                self._style.setCurrentIndex(i)
                break
        self._style.setFixedWidth(110)
        self._style.currentIndexChanged.connect(self._schedule)
        style_row.addWidget(self._style)
        style_row.addStretch()
        body_l.addLayout(style_row)

        # Condition
        cond_row = QHBoxLayout()
        cond_row.setContentsMargins(0, 0, 0, 0)
        cond_row.addWidget(QLabel("Condition:"))
        self._condition = QComboBox()
        self._COND_LABELS = [
            ("UNSPECIFIED", "Any"),
            ("NEW", "New"),
            ("USED", "Used"),
            ("OLD", "Old"),
            ("DESOLATE", "Desolate"),
        ]
        for _api, _lbl in self._COND_LABELS:
            self._condition.addItem(_lbl, _api)
        for i, (api_v, _) in enumerate(self._COND_LABELS):
            if api_v == (prefs.search_condition or "UNSPECIFIED"):
                self._condition.setCurrentIndex(i)
                break
        self._condition.setFixedWidth(110)
        self._condition.currentIndexChanged.connect(self._schedule)
        cond_row.addWidget(self._condition)
        cond_row.addStretch()
        body_l.addLayout(cond_row)

        # Design year filter
        self._dyear_filter = QCheckBox("Limit design year")
        self._dyear_filter.setChecked(prefs.search_design_year)
        self._dyear_filter.toggled.connect(self._schedule)
        body_l.addWidget(self._dyear_filter)

        dyear_row = QHBoxLayout()
        dyear_row.setContentsMargins(20, 0, 0, 0)
        dyear_row.setSpacing(4)
        dyear_row.addWidget(QLabel("Min"))
        self._dyear_min = QSpinBox()
        self._dyear_min.setRange(1900, 2100)
        self._dyear_min.setFixedWidth(75)
        self._dyear_min.setValue(prefs.search_design_year_min)
        self._dyear_min.valueChanged.connect(self._schedule)
        dyear_row.addWidget(self._dyear_min)
        dyear_row.addWidget(QLabel("Max"))
        self._dyear_max = QSpinBox()
        self._dyear_max.setRange(1900, 2100)
        self._dyear_max.setFixedWidth(75)
        self._dyear_max.setValue(prefs.search_design_year_max)
        self._dyear_max.valueChanged.connect(self._schedule)
        dyear_row.addWidget(self._dyear_max)
        dyear_row.addStretch()
        body_l.addLayout(dyear_row)
        self._dyear_min.setEnabled(prefs.search_design_year)
        self._dyear_max.setEnabled(prefs.search_design_year)
        self._dyear_filter.toggled.connect(self._dyear_min.setEnabled)
        self._dyear_filter.toggled.connect(self._dyear_max.setEnabled)

        # Geometry nodes
        self._geo_nodes = QCheckBox("Uses Geometry Nodes")
        self._geo_nodes.setChecked(prefs.search_geometry_nodes)
        self._geo_nodes.toggled.connect(self._schedule)
        body_l.addWidget(self._geo_nodes)

        root.addWidget(self._body)
        self._body.setVisible(False)
        self._update_toggle_text(False)

    def _active_filter_tokens(self) -> list[str]:
        """Return short human-readable labels for every active filter."""
        tokens: list[str] = []
        if self._free_only.isChecked():
            tokens.append("Free")
        if self._my_assets.isChecked():
            tokens.append("Mine")
        if self._bookmarked.isChecked():
            tokens.append("Bookmarked")
        if self._quality_check.isChecked() and self._quality_limit.value() > 0:
            tokens.append(f"Quality≥{self._quality_limit.value()}")
        lic = self._license.currentText()
        if lic and lic != "Any":
            tokens.append(lic)
        if self._animated_only.isChecked():
            tokens.append("Animated")
        if self._tex_filter.isChecked():
            tokens.append(f"Tex {self._tex_min.value()}–{self._tex_max.value()}px")
        if self._poly_filter.isChecked():
            tokens.append(f"Poly {self._poly_min.value()}–{self._poly_max.value()}K")
        if self._fsize_filter.isChecked():
            tokens.append(f"Size {self._fsize_min.value()}–{self._fsize_max.value()}MB")
        if self._style.currentData() and self._style.currentData() != "ANY":
            tokens.append(self._style.currentText())
        if self._condition.currentData() and self._condition.currentData() != "UNSPECIFIED":
            tokens.append(self._condition.currentText())
        if self._dyear_filter.isChecked():
            tokens.append(f"Year {self._dyear_min.value()}–{self._dyear_max.value()}")
        if self._geo_nodes.isChecked():
            tokens.append("Geo Nodes")
        return tokens

    def _update_toggle_text(self, checked: bool) -> None:
        """Refresh the header label, summarising active filters when collapsed."""
        arrow = "▼" if checked else "▶"
        tokens = self._active_filter_tokens()
        if not tokens:
            self._toggle.setText(f"{arrow}  Filters")
            self._toggle.setToolTip("")
            return
        if checked:
            # Expanded: keep it short, just show the count.
            self._toggle.setText(f"{arrow}  Filters  ({len(tokens)} active)")
        else:
            # Collapsed: show the active filters inline (truncated if long).
            summary = " · ".join(tokens)
            if len(summary) > 60:
                summary = summary[:57].rstrip(" ·") + "…"
            self._toggle.setText(f"{arrow}  Filters:  {summary}")
        self._toggle.setToolTip("Active filters:\n• " + "\n• ".join(tokens))

    def _on_toggled(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self._update_toggle_text(checked)

    def _schedule(self) -> None:
        prefs.search_free_only = self._free_only.isChecked()
        prefs.search_my_assets_only = self._my_assets.isChecked()
        prefs.search_bookmarked_only = self._bookmarked.isChecked()
        prefs.search_quality_limit = self._quality_limit.value() if self._quality_check.isChecked() else 0
        prefs.search_license = self._license_to_api(self._license.currentText())
        prefs.search_animated_only = self._animated_only.isChecked()
        prefs.search_texture_resolution = self._tex_filter.isChecked()
        prefs.search_texture_resolution_min = self._tex_min.value()
        prefs.search_texture_resolution_max = self._tex_max.value()
        prefs.search_poly_count = self._poly_filter.isChecked()
        prefs.search_poly_count_min = self._poly_min.value() * 1000
        prefs.search_poly_count_max = self._poly_max.value() * 1000
        prefs.search_file_size = self._fsize_filter.isChecked()
        prefs.search_file_size_min = self._fsize_min.value()
        prefs.search_file_size_max = self._fsize_max.value()
        prefs.search_style = self._style.currentData() or "ANY"
        prefs.search_condition = self._condition.currentData() or "UNSPECIFIED"
        prefs.search_design_year = self._dyear_filter.isChecked()
        prefs.search_design_year_min = self._dyear_min.value()
        prefs.search_design_year_max = self._dyear_max.value()
        prefs.search_geometry_nodes = self._geo_nodes.isChecked()
        self._update_toggle_text(self._toggle.isChecked())
        self._debounce.start()

    @staticmethod
    def _license_to_api(label: str) -> str:
        """Convert UI license label to API value."""
        mapping = {
            "Any": "ANY",
            "Free": "FREE",
            "Royalty Free": "ROYALTY_FREE",
            "Full": "FULL",
            "Usage Rights": "USAGE_RIGHTS",
        }
        return mapping.get(label, "ANY")

    @staticmethod
    def _api_to_license(api_val: str) -> str:
        """Convert API license value to UI label."""
        mapping = {
            "ANY": "Any",
            "FREE": "Free",
            "ROYALTY_FREE": "Royalty Free",
            "FULL": "Full",
            "USAGE_RIGHTS": "Usage Rights",
        }
        return mapping.get(api_val, "Any")

    @property
    def free_only(self) -> bool:
        return self._free_only.isChecked()

    @property
    def my_assets_only(self) -> bool:
        return self._my_assets.isChecked()

    @property
    def bookmarked_only(self) -> bool:
        return self._bookmarked.isChecked()

    @property
    def quality_limit(self) -> int:
        return self._quality_limit.value() if self._quality_check.isChecked() else 0

    @property
    def license_filter(self) -> str:
        return self._license_to_api(self._license.currentText())

    @property
    def animated_only(self) -> bool:
        return self._animated_only.isChecked()

    @property
    def tex_res_enabled(self) -> bool:
        return self._tex_filter.isChecked()

    @property
    def tex_res_min(self) -> int:
        return self._tex_min.value() if self.tex_res_enabled else 0

    @property
    def tex_res_max(self) -> int:
        return self._tex_max.value() if self.tex_res_enabled else 0

    @property
    def poly_count_min(self) -> int:
        return self._poly_min.value() * 1000 if self._poly_filter.isChecked() else 0

    @property
    def poly_count_max(self) -> int:
        return self._poly_max.value() * 1000 if self._poly_filter.isChecked() else 0

    @property
    def file_size_min(self) -> int:
        return self._fsize_min.value() if self._fsize_filter.isChecked() else 0

    @property
    def file_size_max(self) -> int:
        return self._fsize_max.value() if self._fsize_filter.isChecked() else 0

    @property
    def model_style(self) -> str:
        return self._style.currentData() or "ANY"

    @property
    def condition(self) -> str:
        return self._condition.currentData() or "UNSPECIFIED"

    @property
    def design_year_min(self) -> int:
        return self._dyear_min.value() if self._dyear_filter.isChecked() else 0

    @property
    def design_year_max(self) -> int:
        return self._dyear_max.value() if self._dyear_filter.isChecked() else 0

    @property
    def geometry_nodes(self) -> bool:
        return self._geo_nodes.isChecked()


# ---------------------------------------------------------------------------
# Login banner  (logo + message + button)
# ---------------------------------------------------------------------------


class _LoginBanner(QWidget):
    """Status banner shown at the top of the asset bar.

    Two modes:
      * "info"  — gentle reminder that the user isn't logged in.
      * "error" — login or download failed.  Exposes Retry / Settings /
        Paste API key / Show logs actions so the user can recover without
        leaving the asset bar.
    """

    login_clicked = Signal()
    open_settings_clicked = Signal()
    show_logs_clicked = Signal()
    dismiss_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        logo_lbl = QLabel()
        logo_pix = bk_icons.logo_pixmap(40)
        if not logo_pix.isNull():
            logo_lbl.setPixmap(logo_pix)
        layout.addWidget(logo_lbl)

        self._lbl = QLabel("Not logged in — some results may be limited.")
        self._lbl.setStyleSheet("color: #c8d0e0; font-size: 11px;")
        self._lbl.setWordWrap(True)
        layout.addWidget(self._lbl, stretch=1)

        # When True the primary button acts as a "Dismiss" (emits
        # ``dismiss_clicked``) instead of starting a login. Set per-message by
        # :meth:`show_error` so a non-login error banner doesn't open the
        # browser when the user just wants to close it.
        self._primary_is_dismiss = False

        self._login_btn = QPushButton("Log in")
        self._login_btn.setFixedWidth(70)
        self._login_btn.clicked.connect(self._on_primary_clicked)
        layout.addWidget(self._login_btn)

        self._settings_btn = QPushButton("Settings…")
        self._settings_btn.setFixedWidth(80)
        self._settings_btn.clicked.connect(self.open_settings_clicked)
        self._settings_btn.setVisible(False)
        layout.addWidget(self._settings_btn)

        self._logs_btn = QPushButton("Logs")
        self._logs_btn.setFixedWidth(54)
        self._logs_btn.clicked.connect(self.show_logs_clicked)
        self._logs_btn.setVisible(False)
        layout.addWidget(self._logs_btn)

        self._dismiss_btn = QPushButton("✕")
        self._dismiss_btn.setFixedWidth(24)
        self._dismiss_btn.clicked.connect(self.dismiss_clicked)
        self._dismiss_btn.setVisible(False)
        layout.addWidget(self._dismiss_btn)

        self.setAttribute(Qt.WA_StyledBackground, True)
        self._apply_info_style()

    def _on_primary_clicked(self) -> None:
        """Route the primary button to login or dismiss based on context."""
        if self._primary_is_dismiss:
            self.dismiss_clicked.emit()
        else:
            self.login_clicked.emit()

    def _apply_info_style(self) -> None:
        self.setStyleSheet("background: #334066;")
        self._lbl.setStyleSheet("color: #c8d0e0; font-size: 11px;")

    def _apply_error_style(self) -> None:
        self.setStyleSheet("background: #6e2b2b;")
        self._lbl.setStyleSheet("color: #ffe0e0; font-size: 11px;")

    def show_info(self, message: str = "Not logged in — some results may be limited.") -> None:
        self._lbl.setText(message)
        self._apply_info_style()
        self._primary_is_dismiss = False
        self._login_btn.setVisible(True)
        self._login_btn.setText("Log in")
        self._login_btn.setEnabled(True)
        self._settings_btn.setVisible(False)
        self._logs_btn.setVisible(False)
        self._dismiss_btn.setVisible(False)

    def show_error(self, message: str, *, retry_label: str = "Retry", primary_dismiss: bool = False) -> None:
        self._lbl.setText(message)
        self._apply_error_style()
        self._primary_is_dismiss = primary_dismiss
        self._login_btn.setVisible(True)
        self._login_btn.setText(retry_label)
        self._login_btn.setEnabled(True)
        self._settings_btn.setVisible(True)
        self._logs_btn.setVisible(True)
        self._dismiss_btn.setVisible(True)

    def set_busy(self, busy: bool, busy_text: str = "Working…") -> None:
        self._login_btn.setEnabled(not busy)
        if busy:
            self._login_btn.setText(busy_text)


# ---------------------------------------------------------------------------
# Main panel widget
# ---------------------------------------------------------------------------


class _BrandHeader(QWidget):
    """Blue title strip with the Blendkit logo, shown at the very top of
    the asset bar.  Always visible — it survives Maya redocking and floating
    the workspaceControl, so the panel keeps its identity regardless of how
    Maya chrome renders the tab.
    """

    settings_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("BKBrandHeader")
        self.setFixedHeight(36)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            "QWidget#BKBrandHeader {"
            "  background-color: #2a6bd6;"  # Blendkit blue
            "  border-bottom: 1px solid #1d4f9e;"
            "}"
            "QLabel#BKBrandText {"
            "  color: white;"
            "  font-size: 13px;"
            "  font-weight: 600;"
            "  letter-spacing: 0.3px;"
            "}"
            "QPushButton#BKSettingsBtn {"
            "  color: white;"
            "  font-size: 16px;"
            "  border: none;"
            "  background: transparent;"
            "  padding: 0px;"
            "}"
            "QPushButton#BKSettingsBtn:hover {"
            "  color: #d6e4ff;"
            "}"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 4, 10, 4)
        row.setSpacing(8)

        logo = QLabel(self)
        try:
            pix = bk_icons.logo_pixmap(24)
            if pix and not pix.isNull():
                logo.setPixmap(pix)
        except Exception:
            pass
        row.addWidget(logo)

        text = QLabel("Blendkit", self)
        text.setObjectName("BKBrandText")
        row.addWidget(text)
        row.addStretch()

        self._settings_btn = QPushButton("\u2699", self)
        self._settings_btn.setObjectName("BKSettingsBtn")
        self._settings_btn.setToolTip("Open Blendkit settings")
        self._settings_btn.setCursor(Qt.PointingHandCursor)
        self._settings_btn.setFixedSize(24, 24)
        self._settings_btn.clicked.connect(self.settings_clicked)
        row.addWidget(self._settings_btn)


class AssetBarWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Blendkit")
        self.setMinimumWidth(260)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._brand = _BrandHeader(self)
        self._brand.settings_clicked.connect(self._open_settings_general)
        layout.addWidget(self._brand)

        self._login_banner = _LoginBanner()
        self._login_banner.login_clicked.connect(self._do_login)
        self._login_banner.open_settings_clicked.connect(self._open_settings)
        self._login_banner.show_logs_clicked.connect(self._show_logs)
        self._login_banner.dismiss_clicked.connect(self._dismiss_banner)
        layout.addWidget(self._login_banner)
        # Tab the banner's "Settings…" button should open (None → Account).
        self._settings_tab: str | None = None

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

        # Refresh a pending "My assets only" search once the profile id loads.
        auth.add_profile_listener(self._on_profile_loaded)
        # Refresh bookmark badges whenever the account's bookmarks change.
        bk_bookmarks.add_listener(self._grid.refresh_bookmark_badges)

        _ensure_poller()
        self._refresh_login_state()
        # Pre-fetch the profile so "My assets only" works on first toggle.
        if auth.is_logged_in():
            auth.fetch_profile()
            bk_bookmarks.refresh()
        QTimer.singleShot(500, self._default_search)

    def _on_profile_loaded(self) -> None:
        """Re-run the active search when the user profile id arrives, but only
        if the 'My assets only' filter is enabled (it needs the author_id).
        """
        if self._filters.my_assets_only:
            self._on_filters_changed()

    def _refresh_login_state(self) -> None:
        if auth.is_logged_in():
            self._login_banner.setVisible(False)
        else:
            self._login_banner.show_info()
            self._login_banner.setVisible(True)

    def on_logged_in(self) -> None:
        """React to a fresh login (banner away + refresh content).

        Invoked on the GUI thread via the module-level login listener. Hides
        the login banner and re-runs the active search so the grid reflects the
        now logged-in state (e.g. enables personalised/"My assets" results).
        """
        self._refresh_login_state()
        bk_bookmarks.refresh()
        self._on_filters_changed()

    def _do_login(self) -> None:
        self._login_banner.set_busy(True, "Waiting for browser…")

        def _run() -> None:
            try:
                ok = auth.login()
            except Exception as exc:
                log.exception("Login raised: %s", exc)
                ok = False
                err = str(exc)
            else:
                err = "" if ok else "Login failed or was cancelled. See logs for details."
            # Marshal back onto the GUI thread
            QTimer.singleShot(0, lambda: self._on_login_result(ok, err))

        threading.Thread(target=_run, daemon=True).start()

    def _on_login_result(self, ok: bool, err: str) -> None:
        log.info("Login result: %s, err=%s", ok, err)
        if ok:
            self._refresh_login_state()
            return
        self._settings_tab = None
        self._login_banner.show_error(
            err or "Login failed. Try again, open Settings to paste an API key, or check logs.",
            retry_label="Try again",
        )
        self._login_banner.setVisible(True)

    def _open_settings(self) -> None:
        try:
            from .settings_dialog import open_settings

            open_settings(tab=self._settings_tab or "Account")
        except Exception as exc:
            log.error("Cannot open settings dialog: %s", exc)

    def _open_settings_general(self) -> None:
        """Open the settings dialog from the panel header (General tab)."""
        try:
            from .settings_dialog import open_settings

            open_settings(tab="General")
        except Exception as exc:
            log.error("Cannot open settings dialog: %s", exc)

    def _show_logs(self) -> None:
        # Open Maya's Script Editor — the simplest "show logs" surface
        try:
            import maya.cmds as cmds  # type: ignore[import-not-found]

            cmds.ScriptEditor()
        except Exception as exc:
            log.warning("Cannot open Maya Script Editor: %s", exc)

    def _dismiss_banner(self) -> None:
        if auth.is_logged_in():
            self._login_banner.setVisible(False)
        else:
            self._login_banner.show_info()

    def show_error(self, message: str, settings_tab: str | None = None) -> None:
        """Public entry: surface a non-login error (e.g. download failure).

        *settings_tab* (e.g. "Files") routes the banner's "Settings…" button
        to the relevant tab; defaults to the Account tab.
        """
        self._settings_tab = settings_tab
        self._login_banner.show_error(message, retry_label="Dismiss", primary_dismiss=True)
        self._login_banner.setVisible(True)

    def _on_search(self, query: str, asset_type: str) -> None:
        # User-initiated search via the search bar clears any sticky filters
        # (such as the author filter set by "Search by author").
        self._grid.set_extra_filters(None)
        self._grid.start_search(
            query,
            asset_type,
            free_only=self._filters.free_only,
            my_assets_only=self._filters.my_assets_only,
            bookmarked_only=self._filters.bookmarked_only,
            quality_limit=self._filters.quality_limit,
            license_filter=self._filters.license_filter,
            animated_only=self._filters.animated_only,
            texture_res_min=self._filters.tex_res_min,
            texture_res_max=self._filters.tex_res_max,
            file_size_min=self._filters.file_size_min,
            file_size_max=self._filters.file_size_max,
            poly_count_min=self._filters.poly_count_min,
            poly_count_max=self._filters.poly_count_max,
            style=self._filters.model_style,
            condition=self._filters.condition,
            design_year_min=self._filters.design_year_min,
            design_year_max=self._filters.design_year_max,
            geometry_nodes=self._filters.geometry_nodes,
        )

    def _on_filters_changed(self) -> None:
        self._grid.start_search(
            self._search_bar.current_query,
            self._search_bar.current_asset_type,
            free_only=self._filters.free_only,
            my_assets_only=self._filters.my_assets_only,
            bookmarked_only=self._filters.bookmarked_only,
            quality_limit=self._filters.quality_limit,
            license_filter=self._filters.license_filter,
            animated_only=self._filters.animated_only,
            texture_res_min=self._filters.tex_res_min,
            texture_res_max=self._filters.tex_res_max,
            file_size_min=self._filters.file_size_min,
            file_size_max=self._filters.file_size_max,
            poly_count_min=self._filters.poly_count_min,
            poly_count_max=self._filters.poly_count_max,
            style=self._filters.model_style,
            condition=self._filters.condition,
            design_year_min=self._filters.design_year_min,
            design_year_max=self._filters.design_year_max,
            geometry_nodes=self._filters.geometry_nodes,
        )

    def _default_search(self) -> None:
        self._grid.start_search(
            "",
            self._search_bar.current_asset_type,
            free_only=self._filters.free_only,
            my_assets_only=self._filters.my_assets_only,
            bookmarked_only=self._filters.bookmarked_only,
            quality_limit=self._filters.quality_limit,
            license_filter=self._filters.license_filter,
            animated_only=self._filters.animated_only,
            texture_res_min=self._filters.tex_res_min,
            texture_res_max=self._filters.tex_res_max,
            file_size_min=self._filters.file_size_min,
            file_size_max=self._filters.file_size_max,
            poly_count_min=self._filters.poly_count_min,
            poly_count_max=self._filters.poly_count_max,
            style=self._filters.model_style,
            condition=self._filters.condition,
            design_year_min=self._filters.design_year_min,
            design_year_max=self._filters.design_year_max,
            geometry_nodes=self._filters.geometry_nodes,
        )

    def search_by_author(self, author_id: int, author_name: str = "") -> None:
        """Reset the search bar and show only assets by the given author."""
        try:
            self._search_bar._input.clear()
        except Exception:
            pass
        self._grid.set_extra_filters({"author_id": author_id})
        self._grid.start_search(
            "",
            self._search_bar.current_asset_type,
            free_only=self._filters.free_only,
            my_assets_only=self._filters.my_assets_only,
            bookmarked_only=self._filters.bookmarked_only,
            quality_limit=self._filters.quality_limit,
            license_filter=self._filters.license_filter,
            animated_only=self._filters.animated_only,
            texture_res_min=self._filters.tex_res_min,
            texture_res_max=self._filters.tex_res_max,
            file_size_min=self._filters.file_size_min,
            file_size_max=self._filters.file_size_max,
            poly_count_min=self._filters.poly_count_min,
            poly_count_max=self._filters.poly_count_max,
            style=self._filters.model_style,
            condition=self._filters.condition,
            design_year_min=self._filters.design_year_min,
            design_year_max=self._filters.design_year_max,
            geometry_nodes=self._filters.geometry_nodes,
        )
        log.info("Search-by-author: id=%s name=%r", author_id, author_name)


# ---------------------------------------------------------------------------
# Maya integration
# ---------------------------------------------------------------------------


def open_asset_bar() -> None:
    """Create or restore the Blendkit side panel (docked by default)."""
    if cmds.workspaceControl(CONTROL_NAME, query=True, exists=True):
        cmds.workspaceControl(
            CONTROL_NAME,
            edit=True,
            restore=True,
            visible=True,
        )
        return

    # Best-effort: clean up the *legacy* control name from older revisions
    # of this addon so we don't leave a dead tab in the Maya UI.
    for legacy in ("BlenderKitAssetBar",):
        try:
            if cmds.workspaceControl(legacy, query=True, exists=True):
                cmds.deleteUI(legacy, control=True)
        except Exception:
            pass

    # Allow the panel to be closed via the X button or panel close.
    kw = {
        "label": "Blendkit",
        "uiScript": ("import bk_maya.ui.asset_bar as _ab; _ab._populate_workspace_control()"),
        "initialWidth": 340,
        "retain": True,
        # No closeCommand: allow normal close
    }

    try:
        if cmds.workspaceControl("AttributeEditor", query=True, exists=True):
            kw["tabToControl"] = ("AttributeEditor", -1)
            kw["floating"] = False
    except Exception:
        kw["floating"] = True

    cmds.workspaceControl(CONTROL_NAME, **kw)
    # Raise/focus the freshly created control so it becomes the active tab
    # (without this, a new control docked next to the AttributeEditor stays
    # behind whatever tab was previously on top).
    try:
        cmds.workspaceControl(CONTROL_NAME, edit=True, restore=True, visible=True)
    except Exception as exc:
        log.debug("Could not raise new asset bar control: %s", exc)
    log.info("Blendkit asset bar opened.")


def set_tile_size(size: int) -> None:
    """Update thumbnail density and reflow the live panel if open."""
    prefs.thumbnail_size = size
    prefs.save()
    if _current_bar is not None:
        _current_bar._grid.set_tile_size(size)
    log.info("Thumbnail size → %d px", size)


def notify_error(message: str, settings_tab: str | None = None) -> None:
    """Surface an error on the asset bar's status banner.

    Safe to call from non-GUI threads — marshals to the main thread.
    *settings_tab* routes the banner's "Settings…" button to a specific tab
    (e.g. "Files" for a missing Blender executable).
    """
    bar = _current_bar
    if bar is None:
        log.warning("notify_error called but asset bar is not open: %s", message)
        return
    QTimer.singleShot(0, lambda: bar.show_error(message, settings_tab=settings_tab))


def _reopen_on_close() -> None:
    # No longer used: panel can now be closed normally.
    pass


def _populate_workspace_control() -> None:
    """Called by Maya's uiScript when the workspaceControl is created."""
    global _current_bar
    try:
        import shiboken6  # type: ignore
        from maya.OpenMayaUI import MQtUtil  # type: ignore
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
