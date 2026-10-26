"""Popup viewer window.

A frameless, always-on-top, non-focus-stealing popup that renders clipboard
content. Visual highlights:

* rounded 16px panel with a soft painted shadow (cached pixmap, no per-frame
  QGraphicsEffect cost while scrolling),
* 180ms fade in / fade out,
* smooth (animated) mouse-wheel scrolling and Ctrl+Wheel zoom,
* an in-window incremental search bar with match highlighting.

Windows-specific behaviour (``WS_EX_NOACTIVATE`` so showing the popup does
not steal focus from the active application, edge hit-testing for resizing,
activation on client-area click) is implemented with ``ctypes`` and guarded
so the code still runs on other platforms.
"""

from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import (
    QBuffer,
    QEasingCurve,
    QEvent,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSizeF,
    QPropertyAnimation,
    QRegularExpression,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QShortcut,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from clipboard import ClipboardService
from markdown_renderer import FG, LINK, Renderer
from settings import (
    MAX_FONT_SCALE,
    MIN_FONT_SCALE,
    MIN_HEIGHT,
    MIN_WIDTH,
    AppSettings,
)

# --------------------------------------------------------------------------- #
# windows constants (ctypes interop)
# --------------------------------------------------------------------------- #
WM_MOUSEACTIVATE = 0x0021
WM_NCHITTEST = 0x0084
MA_ACTIVATE = 1
HTCAPTION = 2
HTLEFT, HTRIGHT = 10, 11
HTTOP, HTTOPLEFT, HTTOPRIGHT = 12, 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17
WS_EX_NOACTIVATE = 0x08000000
GWL_EXSTYLE = -20

#: Window margin reserved for the painted drop shadow.
SHADOW_MARGIN = 22
#: Extra horizontal breathing room between the panel edge and the content,
#: so text never hugs the popup borders (user-requested ~0.5cm).
CONTENT_SIDE_PADDING = 10
#: Padding between the dark content card and the text itself (~0.5cm).
CARD_PADDING = 20
#: Width of the invisible resize band along the window edges.
RESIZE_BAND = SHADOW_MARGIN + 6
#: Fade duration in milliseconds.
FADE_MS = 180
#: Guard rail for the search highlighter.
MAX_SEARCH_MATCHES = 2000

WINDOW_QSS = """
QFrame#panel {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 16px;
}
QFrame#browserCard {
    background: #0d1117;
    border-radius: 10px;
}
QTextBrowser {
    background: transparent;
    border: none;
    selection-background-color: #1f6feb;
    selection-color: #ffffff;
}
QFrame#searchBar {
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 8px;
}
QLineEdit#searchEdit {
    background: transparent;
    border: none;
    color: #e6edf3;
    selection-background-color: #1f6feb;
    selection-color: #ffffff;
    padding: 4px 8px;
}
QLabel#searchInfo { color: #8b949e; font-size: 12px; }
QToolButton {
    background: transparent;
    border: none;
    border-radius: 4px;
    color: #8b949e;
    font-size: 13px;
    padding: 2px 6px;
}
QToolButton:hover { background: #21262d; color: #e6edf3; }
QToolButton#pinBtn, QToolButton#gearBtn {
    background: rgba(13, 17, 23, 200);
    border: 1px solid #30363d;
    border-radius: 12px;
    padding: 0;
    font-size: 12px;
}
QToolButton#pinBtn:hover, QToolButton#gearBtn:hover {
    background: #21262d; color: #e6edf3;
}
QToolButton#pinBtn:checked { background: #1f6feb; border-color: #1f6feb; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px 0; }
QScrollBar::handle:vertical {
    background: #30363d; border-radius: 5px; min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: #484f58; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0 2px; }
QScrollBar::handle:horizontal {
    background: #30363d; border-radius: 5px; min-width: 30px;
}
QScrollBar::handle:horizontal:hover { background: #484f58; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent;
}
"""


# --------------------------------------------------------------------------- #
# shell / shadow
# --------------------------------------------------------------------------- #
class Shell(QWidget):
    """Transparent root widget that paints a cached soft drop shadow."""

    #: radius of the panel the shadow hugs (matches the QSS border-radius).
    PANEL_RADIUS = 16

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._shadow: Optional[QPixmap] = None

    def resizeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._shadow = self._render_shadow(self.size())

    def paintEvent(self, event: QEvent) -> None:  # noqa: N802
        if self._shadow is not None:
            painter = QPainter(self)
            painter.drawPixmap(0, 0, self._shadow)

    def _render_shadow(self, size) -> QPixmap:
        """Produce a blurred rounded-rect glow via down/up-scaling."""

        dpr = self.devicePixelRatioF() or 1.0
        ratio = 6  # down-scale factor == cheap blur radius
        small_w = max(4, int(size.width() * dpr / ratio))
        small_h = max(4, int(size.height() * dpr / ratio))
        small = QPixmap(small_w, small_h)
        small.fill(Qt.transparent)
        painter = QPainter(small)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 190))
        margin = SHADOW_MARGIN / ratio
        painter.drawRoundedRect(
            QRectF(margin, margin, small_w - 2 * margin, small_h - 2 * margin),
            self.PANEL_RADIUS / ratio,
            self.PANEL_RADIUS / ratio,
        )
        painter.end()
        big = small.scaled(
            int(size.width() * dpr),
            int(size.height() * dpr),
            Qt.IgnoreAspectRatio,
            Qt.SmoothTransformation,
        )
        big.setDevicePixelRatio(dpr)
        return big


# --------------------------------------------------------------------------- #
# browser
# --------------------------------------------------------------------------- #
class ViewerBrowser(QTextBrowser):
    """Read-only rich-text view with smooth scroll, zoom and image support."""

    #: Emitted with +1/-1 when the user Ctrl+Wheel-zooms.
    zoom_requested = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setOpenExternalLinks(True)
        self.setReadOnly(True)
        self.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse
        )
        self.setFrameShape(QFrame.NoFrame)
        # Padding via viewport margins: content is inset inside the browser
        # while the scrollbars stay at the far edges of the card, away from
        # the text. Document margins would scroll away (lines would clip at
        # the edges) and widget margins would pull the scrollbar next to the
        # text -- viewport margins avoid both problems.
        self.document().setDocumentMargin(0)
        self._apply_padding()

        self._scroll_anim = QPropertyAnimation(
            self.verticalScrollBar(), b"value", self
        )
        self._scroll_anim.setDuration(140)
        self._scroll_anim.setEasingCurve(QEasingCurve.OutCubic)

        # Created lazily: importing QtNetwork at startup costs several MB of
        # RAM and is only needed when a Markdown image is actually remote.
        self._nam = None
        self._images: dict = {}  # url string -> QPixmap | None (pending/failed)

        # browser-style middle-click auto-scroll state
        self._autoscroll_timer: Optional[QTimer] = None
        self._autoscroll_origin: Optional[QPoint] = None
        self._compass: Optional[QWidget] = None

    # ------------------------------ auto-scroll ----------------------- #
    def is_autoscrolling(self) -> bool:
        """True while middle-click auto-scroll is active."""

        return self._autoscroll_origin is not None

    def _start_autoscroll(self, global_pos: QPoint, local_pos: QPointF) -> None:
        self._autoscroll_origin = global_pos

        global _COMPASS_CACHE
        if _COMPASS_CACHE is None:
            _COMPASS_CACHE = _compass_pixmap()
        compass = QLabel(self.viewport())
        compass.setPixmap(_COMPASS_CACHE)
        compass.setAttribute(Qt.WA_TransparentForMouseEvents)
        compass.resize(_COMPASS_CACHE.size())
        compass.move(int(local_pos.x()) - _COMPASS_CACHE.width() // 2,
                     int(local_pos.y()) - _COMPASS_CACHE.height() // 2)
        compass.show()
        compass.raise_()
        if self._compass is not None:
            self._compass.deleteLater()
        self._compass = compass

        self._autoscroll_timer = QTimer(self)
        self._autoscroll_timer.setInterval(AUTOSCROLL_TICK_MS)
        self._autoscroll_timer.timeout.connect(self._autoscroll_tick)
        self._autoscroll_timer.start()

    def stop_autoscroll(self) -> None:
        """Cancel auto-scroll and remove the direction indicator."""

        self._autoscroll_origin = None
        if self._autoscroll_timer is not None:
            self._autoscroll_timer.stop()
            self._autoscroll_timer.deleteLater()
            self._autoscroll_timer = None
        if self._compass is not None:
            self._compass.deleteLater()
            self._compass = None

    @staticmethod
    def _cursor_global() -> QPoint:
        return QCursor.pos()

    def _autoscroll_tick(self) -> None:
        if self._autoscroll_origin is None:
            return
        delta = self._cursor_global() - self._autoscroll_origin
        dt = AUTOSCROLL_TICK_MS / 1000.0

        def speed(distance: int) -> float:
            excess = abs(distance) - AUTOSCROLL_DEADZONE
            if excess <= 0:
                return 0.0
            return min(AUTOSCROLL_MAX_SPEED, excess * AUTOSCROLL_GAIN)

        v_speed, h_speed = speed(delta.y()), speed(delta.x())
        if v_speed:
            bar = self.verticalScrollBar()
            step = 1 if delta.y() > 0 else -1
            bar.setValue(int(bar.value() + step * v_speed * dt))
        if h_speed:
            bar = self.horizontalScrollBar()
            step = 1 if delta.x() > 0 else -1
            bar.setValue(int(bar.value() + step * h_speed * dt))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MiddleButton:
            if self.is_autoscrolling():
                self.stop_autoscroll()
            else:
                self._start_autoscroll(
                    event.globalPosition().toPoint(), event.position()
                )
            event.accept()
            return
        if self.is_autoscrolling():
            # any other click just cancels auto-scroll, like browsers
            self.stop_autoscroll()
            event.accept()
            return
        super().mousePressEvent(event)

    def _apply_padding(self) -> None:
        """Inset the content inside the viewport (~0.5cm on every side)."""

        self.setViewportMargins(
            CARD_PADDING, CARD_PADDING, CARD_PADDING, CARD_PADDING
        )

    # ------------------------------ zoom ------------------------------ #
    def wheelEvent(self, event) -> None:  # noqa: N802
        if self.is_autoscrolling():
            self.stop_autoscroll()
        if event.modifiers() & Qt.ControlModifier:
            delta_y = event.angleDelta().y()
            if delta_y:
                self.zoom_requested.emit(1 if delta_y > 0 else -1)
            event.accept()
            return

        delta_y = event.angleDelta().y()
        if delta_y:
            bar = self.verticalScrollBar()
            if self._scroll_anim.state() == QPropertyAnimation.Running:
                base = int(self._scroll_anim.currentValue())
            else:
                base = bar.value()
            target = max(bar.minimum(), min(bar.maximum(), int(base - delta_y * 0.9)))
            self._scroll_anim.stop()
            self._scroll_anim.setStartValue(base)
            self._scroll_anim.setEndValue(target)
            self._scroll_anim.start()
            event.accept()
            return
        super().wheelEvent(event)

    # ------------------------------ search ---------------------------- #
    def find_matches(self, pattern: str) -> List[QTextCursor]:
        """Return cursors selecting every case-insensitive match of *pattern*."""

        if not pattern:
            return []
        rx = QRegularExpression(
            QRegularExpression.escape(pattern),
            QRegularExpression.CaseInsensitiveOption,
        )
        matches: List[QTextCursor] = []
        cursor = QTextCursor(self.document())
        while True:
            cursor = self.document().find(rx, cursor)
            if cursor.isNull():
                break
            matches.append(QTextCursor(cursor))
            if len(matches) >= MAX_SEARCH_MATCHES:
                break
        return matches

    def highlight_matches(self, matches: List[QTextCursor], current: int) -> None:
        """Paint all *matches*; the one at *current* gets a stronger colour."""

        selections = []
        for index, match in enumerate(matches):
            selection = QTextEdit.ExtraSelection()
            if index == current:
                selection.format.setBackground(QColor("#9e6a03"))
                selection.format.setForeground(QColor("#ffffff"))
            else:
                selection.format.setBackground(QColor(187, 128, 9, 80))
            selection.cursor = match
            selections.append(selection)
        self.setExtraSelections(selections)

    def goto_match(self, cursor: QTextCursor) -> None:
        self.setTextCursor(cursor)

    def clear_search(self) -> None:
        self.setExtraSelections([])

    # ------------------------------ images ---------------------------- #
    def loadResource(self, resource_type, name):  # noqa: N802
        if resource_type != QTextDocument.ImageResource:
            return super().loadResource(resource_type, name)
        url = name if isinstance(name, QUrl) else QUrl(str(name))
        if not url.isValid():
            return None
        key = url.toString()
        if key in self._images:
            cached = self._images[key]
            return cached if cached is not None else None

        if url.scheme() == "data":
            pixmap = self._decode_data_url(url)
            self._images[key] = pixmap
            return pixmap
        if url.isLocalFile():
            pixmap = QPixmap(url.toLocalFile())
            self._images[key] = pixmap if not pixmap.isNull() else None
            return self._images[key]
        if url.scheme() in ("http", "https"):
            self._fetch_remote(url)
        return None

    @staticmethod
    def _decode_data_url(url: QUrl) -> Optional[QPixmap]:
        try:
            payload = url.toString().split(",", 1)[1]
            data = base64.b64decode(payload, validate=False)
        except (IndexError, ValueError):
            return None
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        return pixmap if not pixmap.isNull() else None

    def _fetch_remote(self, url: QUrl) -> None:
        from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest

        if self._nam is None:
            self._nam = QNetworkAccessManager(self)
        key = url.toString()
        self._images[key] = None  # mark as in-flight / failed
        reply = self._nam.get(QNetworkRequest(url))

        def on_finished() -> None:
            try:
                if reply.error() == reply.NoError:
                    pixmap = QPixmap()
                    if pixmap.loadFromData(reply.readAll()) and not pixmap.isNull():
                        self._images[key] = pixmap
                        document = self.document()
                        document.addResource(
                            QTextDocument.ImageResource, url, pixmap
                        )
                        document.markContentsDirty(
                            0, max(1, int(document.size().height()))
                        )
                        self.viewport().update()
            finally:
                reply.deleteLater()

        reply.finished.connect(on_finished)


# --------------------------------------------------------------------------- #
# search bar
# --------------------------------------------------------------------------- #
class SearchBar(QFrame):
    """Incremental search strip shown at the top of the popup."""

    text_changed = Signal(str)
    next_requested = Signal()
    prev_requested = Signal()
    closed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("searchBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        self.edit = QLineEdit(self)
        self.edit.setObjectName("searchEdit")
        self.edit.setPlaceholderText("جستجو…")
        self.edit.setClearButtonEnabled(True)
        self.info = QLabel("", self)
        self.info.setObjectName("searchInfo")

        prev = QToolButton(self)
        prev.setText("▲")
        prev.setToolTip("نتیجه قبلی (Shift+Enter)")
        next_ = QToolButton(self)
        next_.setText("▼")
        next_.setToolTip("نتیجه بعدی (Enter)")
        close = QToolButton(self)
        close.setText("✕")
        close.setToolTip("بستن جستجو (Esc)")

        layout.addWidget(self.edit, 1)
        layout.addWidget(self.info)
        layout.addWidget(prev)
        layout.addWidget(next_)
        layout.addWidget(close)

        self.edit.textChanged.connect(self.text_changed)
        self.edit.returnPressed.connect(self._on_return)
        prev.clicked.connect(self.prev_requested.emit)
        next_.clicked.connect(self.next_requested.emit)
        close.clicked.connect(self.closed.emit)

    def _on_return(self) -> None:
        if QApplication.keyboardModifiers() & Qt.ShiftModifier:
            self.prev_requested.emit()
        else:
            self.next_requested.emit()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.closed.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def focus_edit(self) -> None:
        self.edit.setFocus()
        self.edit.selectAll()

    def set_result(self, count: int, current: int) -> None:
        if count == 0:
            self.info.setText("بدون نتیجه")
        else:
            self.info.setText(f"{current + 1} / {count}")


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #
#: Virtual-key codes used by the outside-click watcher.
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
#: How often (ms) the outside-click watcher polls the mouse buttons.
#: 16ms (~60Hz) so even very short (40ms) presses are always sampled twice.
CLICK_WATCH_INTERVAL_MS = 16

#: Middle-click auto-scroll tuning (browser-style).
AUTOSCROLL_TICK_MS = 16
AUTOSCROLL_DEADZONE = 24      # px of mouse movement that scrolls nothing
AUTOSCROLL_GAIN = 4.0         # px/s of scrolling per px of displacement
AUTOSCROLL_MAX_SPEED = 3000.0  # px/s cap


def _compass_pixmap() -> QPixmap:
    """Build the browser-style direction indicator shown while auto-scrolling."""

    size = 34
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(QPen(QColor(0, 0, 0, 170), 3))
    painter.setBrush(QColor(0x8b, 0x94, 0x9e, 235))
    painter.drawEllipse(3, 3, size - 6, size - 6)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#0d1117"))
    half = size // 2
    # up / down triangles
    painter.drawPolygon(QPolygonF([QPointF(half - 5, half - 3),
                                   QPointF(half + 5, half - 3),
                                   QPointF(half, half - 9)]))
    painter.drawPolygon(QPolygonF([QPointF(half - 5, half + 3),
                                   QPointF(half + 5, half + 3),
                                   QPointF(half, half + 9)]))
    # left / right dots
    painter.drawEllipse(QPointF(half - 8, half), 1.6, 1.6)
    painter.drawEllipse(QPointF(half + 8, half), 1.6, 1.6)
    painter.end()
    return pixmap


_COMPASS_CACHE: Optional[QPixmap] = None


class ViewerWindow(QMainWindow):
    """The popup itself: owns rendering, animations, search and shortcuts."""

    #: Emitted when the global Escape hook should be (de)registered.
    escape_hook_wanted = Signal(bool)
    #: Emitted when the user clicks the ⚙ corner button of the popup.
    settings_requested = Signal()

    def __init__(
        self,
        settings: AppSettings,
        clipboard: ClipboardService,
        renderer_factory: Callable[[], Renderer],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._clipboard = clipboard
        self._renderer_factory = renderer_factory
        self._renderer: Optional[Renderer] = None
        self._raw_text = ""
        self._is_hiding = False
        self._exstyle_applied = False
        self._normal_geometry = QRect()
        self._matches: List[QTextCursor] = []
        self._match_index = -1
        self._click_timer: Optional[QTimer] = None
        self._mouse_button_down = False
        #: True while the branded launch page (not clipboard) is shown.
        self._welcome_mode = False
        self._welcome_top = 0
        self._welcome_guide = False
        self._welcome_spacer_h: Optional[float] = None
        self._logo_cache: Optional[Tuple[str, int, int]] = None
        #: Per-popup "lock": while set, clicks outside the window do not
        #: close it (toggled from the right-click menu).
        self._view_locked = False

        self.setWindowTitle("RTL Viewer")
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self.setStyleSheet(WINDOW_QSS)

        self._build_ui()
        self._build_shortcuts()

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)
        self._fade.finished.connect(self._on_fade_finished)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        shell = Shell()
        self.setCentralWidget(shell)
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(
            SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN
        )

        panel = QFrame(shell)
        panel.setObjectName("panel")
        self.panel = panel
        # Mirror only the popup chrome (search bar ordering, text input
        # direction); the browser itself must stay LTR at the widget level so
        # its document default direction does not flip English/code blocks.
        panel.setLayoutDirection(Qt.RightToLeft)
        shell_layout.addWidget(panel)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(
            10 + CONTENT_SIDE_PADDING,
            10 + CONTENT_SIDE_PADDING,
            10 + CONTENT_SIDE_PADDING,
            10 + CONTENT_SIDE_PADDING,
        )
        panel_layout.setSpacing(6)

        self.search_bar = SearchBar(panel)
        self.search_bar.hide()
        self.search_bar.text_changed.connect(self._on_search_changed)
        self.search_bar.next_requested.connect(lambda: self._search_step(+1))
        self.search_bar.prev_requested.connect(lambda: self._search_step(-1))
        self.search_bar.closed.connect(self.close_search)

        self.browser = ViewerBrowser(panel)
        # Keep the browser LTR at the widget level (inherited RTL would make
        # the document default direction RTL and right-align English/code).
        self.browser.setLayoutDirection(Qt.LeftToRight)
        self.browser.zoom_requested.connect(self._on_zoom)

        card = QFrame(panel)
        card.setObjectName("browserCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.addWidget(self.browser)

        panel_layout.addWidget(self.search_bar)
        panel_layout.addWidget(card, 1)

        # One shared right-click menu for the whole popup (panel + browser),
        # including the lock that disables close-on-outside-click.
        for area in (panel, self.browser):
            area.setContextMenuPolicy(Qt.CustomContextMenu)
            area.customContextMenuRequested.connect(self._show_context_menu)

        # Floating pin button in the top-right corner: same lock state as
        # the right-click lock, just always visible.
        self._pin_btn = QToolButton(panel)
        self._pin_btn.setObjectName("pinBtn")
        self._pin_btn.setText("📌")
        self._pin_btn.setCheckable(True)
        self._pin_btn.setFixedSize(24, 24)
        self._pin_btn.setCursor(Qt.PointingHandCursor)
        self._pin_btn.setToolTip("پین کردن پنجره (بسته‌نشدن با کلیک بیرون)")
        self._pin_btn.toggled.connect(self._on_pin_toggled)
        self._pin_btn.show()

        # Settings button in the opposite (top-left) corner.
        self._settings_btn = QToolButton(panel)
        self._settings_btn.setObjectName("gearBtn")
        self._settings_btn.setText("⚙")
        self._settings_btn.setFixedSize(24, 24)
        self._settings_btn.setCursor(Qt.PointingHandCursor)
        self._settings_btn.setToolTip("تنظیمات")
        self._settings_btn.clicked.connect(self.settings_requested.emit)
        self._settings_btn.show()

        panel.installEventFilter(self)
        self._position_corner_buttons()

    def _build_shortcuts(self) -> None:
        shortcuts = {
            "Ctrl+F": self.toggle_search,
            "Ctrl+Shift+C": self._copy_raw_clipboard,
            "F11": self._toggle_fullscreen,
        }
        for sequence, handler in shortcuts.items():
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(handler)
        # Disabled while the search bar is open so Esc closes only the search.
        self._esc_shortcut = QShortcut(QKeySequence("Escape"), self)
        self._esc_shortcut.activated.connect(self.close_view)

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def toggle(self) -> None:
        """Global hotkey entry point: show or hide the popup."""

        if self.isVisible() and not self._is_hiding:
            self.close_view()
        else:
            self.open_view()

    def open_view(self) -> None:
        """Read the clipboard and present it; silently ignore empty clipboards."""

        text = self._clipboard.read_text()
        if not text.strip():
            return
        self._raw_text = text
        self._welcome_mode = False
        self._present(self._render_current())

    def show_welcome(self) -> None:
        """Manual-launch feedback: a big branded page, not clipboard content.

        On the very first launch the page also carries a short guide
        (how the hotkey works, where settings live); the flag is written
        immediately with a full save so later runtime saves cannot drop it.
        """

        first_run = not self._settings.extra.get("guide_shown", False)
        if first_run:
            self._settings.extra["guide_shown"] = True
            self._settings.save()
        self._welcome_guide = first_run
        self._raw_text = "SAGA RTL"
        self._welcome_mode = True
        self._present(self._welcome_html())
        self._center_welcome()

    def _welcome_logo(self) -> Optional[Tuple[str, int, int]]:
        """(base64 png, width, height) of the bundled logo, cached.

        The PNG is pre-rendered from the user's SVG with the black
        background stripped; its alpha channel is scaled to 30% here so
        the logo sits on the dark canvas at 70% transparency.
        """

        if self._logo_cache is None:
            try:
                if hasattr(sys, "_MEIPASS"):  # PyInstaller bundle
                    path = Path(sys._MEIPASS) / "resources" / "logo.png"
                else:
                    path = Path(__file__).resolve().parent / "resources" / "logo.png"
                image = QImage.fromData(path.read_bytes(), "PNG")
                if image.isNull():
                    return None
                image = image.convertToFormat(QImage.Format_ARGB32)
                for y in range(image.height()):
                    for x in range(image.width()):
                        color = image.pixelColor(x, y)
                        color.setAlpha(round(color.alpha() * 0.3))
                        image.setPixelColor(x, y, color)
                buffer = QBuffer()
                buffer.open(QBuffer.WriteOnly)
                image.save(buffer, "PNG")
                self._logo_cache = (
                    base64.b64encode(bytes(buffer.data())).decode("ascii"),
                    image.width(),
                    image.height(),
                )
            except OSError:
                return None
        return self._logo_cache

    def _welcome_html(self, top: Optional[int] = None) -> str:
        if top is None:
            top = self._welcome_top
        scale = self._settings.font_scale
        big = max(24, round(84 * scale))
        # Qt ignores the FIRST block's top margin, so a 2px spacer
        # paragraph must lead the document for the centering margin to
        # actually render.
        spacer = '<p style="font-size:2px;margin:0">&nbsp;</p>'
        # Branded logo above the text (transparent background PNG).
        logo_html = ""
        logo = self._welcome_logo()
        if logo is not None:
            b64, w, h = logo
            lh = max(24, round(120 * scale))
            lw = round(lh * w / h)
            logo_html = (
                f'<p dir="ltr" align="center" style="margin-top:{max(0, int(top))}px;'
                f'margin-bottom:16px">'
                f'<img src="data:image/png;base64,{b64}" width="{lw}" height="{lh}"></p>'
            )
        text_margin = max(0, int(top)) if not logo_html else 0
        # First-launch guide: short, muted, centered under the brand text.
        guide_html = ""
        if self._welcome_guide:
            gsize = max(11, round(14 * scale))
            guide_html = (
                f'<p dir="rtl" align="center" style="color:#8b949e;'
                f'font-size:{gsize}px;margin:26px 0px 0px 0px">'
                f'هر متنی را کپی کنید و کلید <b>→</b> را بزنید تا همین‌جا رندر شود</p>'
                f'<p dir="rtl" align="center" style="color:#8b949e;'
                f'font-size:{gsize}px;margin:8px 0px 0px 0px">'
                f'بستن: <b>Esc</b> یا کلیک بیرون — نگه‌داشتن روی صفحه: دکمهٔ 📌</p>'
                f'<p dir="rtl" align="center" style="color:#8b949e;'
                f'font-size:{gsize}px;margin:8px 0px 0px 0px">'
                f'میانبر را از <b>تنظیمات</b> عوض کنید: دکمهٔ ⚙️ بالا-چپ یا راست‌کلیک</p>'
            )
        # Welcome text at 70% transparency; "RTL" uses the logo amber.
        return (
            spacer + logo_html +
            f'<p dir="ltr" align="center" style="margin-top:{text_margin}px;'
            f'font-size:{big}px;font-weight:bold">'
            f'<span style="color:rgba(230,237,243,77)">SAGA </span>'
            f'<span style="color:rgba(251,191,27,77)">RTL</span></p>'
            + guide_html
        )

    def _welcome_spacer_height(self) -> float:
        """Laid-out height of the leading spacer paragraph (measured once)."""

        if self._welcome_spacer_h is None:
            doc = self.browser.document()
            doc.setHtml('<p style="font-size:2px;margin:0">&nbsp;</p>')
            doc.setTextWidth(-1)
            self._welcome_spacer_h = doc.size().height()
        return self._welcome_spacer_h

    def _center_welcome(self) -> None:
        """Vertically center the welcome text in the current viewport.

        Measures the paragraph height at zero margin, then re-renders with
        the matching top margin. Re-run on window resize and zoom (both
        change the text/viewport ratio). Horizontal centering is enforced
        programmatically on the block afterwards — the HTML ``align``
        attribute alone has proven fragile on real (RTL-locale) machines.
        """

        if not self._welcome_mode or not self.isVisible():
            return
        doc = self.browser.document()
        doc.setHtml(self._welcome_html(0))
        # measure at the DISPLAY width (viewport) so wrapping matches what
        # the user actually sees — a wrap-free measure would disagree with
        # the rendered height on narrow windows / high zoom
        doc.setTextWidth(-1)
        doc.setPageSize(QSizeF(self.browser.viewport().size()))
        total = doc.size().height()  # spacer + (possibly wrapped) text
        text_height = total - self._welcome_spacer_height()
        view_height = self.browser.viewport().height()
        # 2px slack keeps the document just under the viewport height so a
        # rounding difference can never summon a vertical scrollbar.
        self._welcome_top = max(
            0, int((view_height - text_height) / 2) - 2
        )
        self._set_html(self._welcome_html())

        doc.setPageSize(QSizeF(self.browser.viewport().size()))
        cursor = QTextCursor(doc)
        block = doc.begin().next()  # skip the 2px spacer block
        while block.isValid():
            cursor.setPosition(block.position())
            fmt = block.blockFormat()
            fmt.setAlignment(Qt.AlignHCenter)  # logo block has no text, so
            cursor.setBlockFormat(fmt)         # center every block, not just text ones
            block = block.next()

    def _present(self, html: str) -> None:
        """Common show path: geometry, fade-in, watchers, escape hook."""

        self.close_search()
        self._set_html(html)
        self._apply_geometry()

        self._is_hiding = False
        self._apply_view_lock(False)  # every fresh popup starts unlocked
        self._fade.stop()
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        if self._settings.close_on_outside_click:
            self._start_click_watch()
        self._update_escape_hook()

    def close_view(self) -> None:
        """Start the fade-out; safe to call repeatedly.

        If middle-click auto-scroll is running, the first Escape/click only
        cancels the scrolling (browser behaviour) instead of hiding the popup.
        """

        if self.browser is not None and self.browser.is_autoscrolling():
            self.browser.stop_autoscroll()
            return
        if not self.isVisible() or self._is_hiding:
            return
        self._is_hiding = True
        self._stop_click_watch()
        if not self.isFullScreen():
            self._settings.window_width = self.width()
            self._settings.window_height = self.height()
            self._settings.window_x = self.x()
            self._settings.window_y = self.y()
        # Geometry only — a full save here would revert preference fields
        # (e.g. a freshly recorded hotkey) to whatever this instance loaded
        # at startup.
        self._settings.save_runtime()
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()
        self._update_escape_hook()

    # ------------------------------------------------------------------ #
    # rendering helpers
    # ------------------------------------------------------------------ #
    def _ensure_renderer(self) -> Renderer:
        if self._renderer is None:
            self._renderer = self._renderer_factory()
        return self._renderer

    def _render_current(self) -> str:
        if self._welcome_mode:
            return self._welcome_html()
        return self._ensure_renderer().render(
            self._raw_text, self._settings.font_scale
        )

    def _set_html(self, html: str) -> None:
        self.browser.setHtml(html)

    def _rerender_preserving_scroll(self) -> None:
        if not self._raw_text:
            return
        bar = self.browser.verticalScrollBar()
        ratio = bar.value() / bar.maximum() if bar.maximum() else 0.0
        self._set_html(self._render_current())
        if self._welcome_mode:
            self._center_welcome()
        bar.setValue(int(ratio * bar.maximum()))

    # ------------------------------------------------------------------ #
    # geometry
    # ------------------------------------------------------------------ #
    def _apply_geometry(self) -> None:
        width = max(
            MIN_WIDTH,
            min(self._settings.window_width,
                QApplication.primaryScreen().availableGeometry().width() - 40),
        )
        height = max(MIN_HEIGHT, self._settings.window_height)

        # restore the last dragged position when it is still on a screen
        if self._settings.window_x is not None and self._settings.window_y is not None:
            candidate = QRect(
                self._settings.window_x, self._settings.window_y, width, height
            )
            for screen in QApplication.screens():
                available = screen.availableGeometry()
                if available.intersects(candidate):
                    candidate.setHeight(min(height, available.height() - 40))
                    self.setGeometry(candidate)
                    return

        screen = (
            QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        )
        available = screen.availableGeometry()
        height = min(height, available.height() - 40)
        self.resize(width, height)
        geometry = QRect(0, 0, self.width(), self.height())
        geometry.moveCenter(available.center())
        self.move(geometry.topLeft())

    # ------------------------------------------------------------------ #
    # zoom
    # ------------------------------------------------------------------ #
    def _on_zoom(self, steps: int) -> None:
        new_scale = round(self._settings.font_scale + 0.1 * steps, 2)
        new_scale = max(MIN_FONT_SCALE, min(MAX_FONT_SCALE, new_scale))
        if new_scale == self._settings.font_scale:
            return
        self._settings.font_scale = new_scale
        self._rerender_preserving_scroll()

    # ------------------------------------------------------------------ #
    # search
    # ------------------------------------------------------------------ #
    def toggle_search(self) -> None:
        if self.search_bar.isVisible():
            self.close_search()
        else:
            self.search_bar.show()
            self._esc_shortcut.setEnabled(False)
            self.search_bar.focus_edit()
            self._on_search_changed(self.search_bar.edit.text())
            self._update_escape_hook()

    def close_search(self) -> None:
        was_visible = self.search_bar.isVisible()
        self.search_bar.hide()
        self._esc_shortcut.setEnabled(True)
        self._matches = []
        self._match_index = -1
        self.browser.clear_search()
        self.browser.setFocus()
        if was_visible:
            self._update_escape_hook()

    def _on_search_changed(self, pattern: str) -> None:
        self._matches = self.browser.find_matches(pattern)
        if self._matches:
            self._match_index = 0
            self.browser.highlight_matches(self._matches, self._match_index)
            self.browser.goto_match(self._matches[0])
        else:
            self._match_index = -1
            self.browser.clear_search()
        self.search_bar.set_result(len(self._matches), self._match_index)

    def _search_step(self, direction: int) -> None:
        if not self._matches:
            return
        count = len(self._matches)
        self._match_index = (self._match_index + direction) % count
        self.browser.highlight_matches(self._matches, self._match_index)
        self.browser.goto_match(self._matches[self._match_index])
        self.search_bar.set_result(count, self._match_index)

    # ------------------------------------------------------------------ #
    # misc actions
    # ------------------------------------------------------------------ #
    def _copy_raw_clipboard(self) -> None:
        if self._raw_text:
            self._clipboard.write_text(self._raw_text)

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            if self._normal_geometry.isValid():
                self.setGeometry(self._normal_geometry)
        else:
            self._normal_geometry = self.geometry()
            self.showFullScreen()

    # ------------------------------------------------------------------ #
    # fade / visibility
    # ------------------------------------------------------------------ #
    def _on_fade_finished(self) -> None:
        if self._is_hiding:
            self._is_hiding = False
            self.hide()
            self.setWindowOpacity(1.0)
            self._update_escape_hook()

    def _update_escape_hook(self) -> None:
        wanted = (
            self.isVisible()
            and not self._is_hiding
            and not self.search_bar.isVisible()
        )
        self.escape_hook_wanted.emit(wanted)

    # ------------------------------------------------------------------ #
    # Qt event overrides
    # ------------------------------------------------------------------ #
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._exstyle_applied:
            self._exstyle_applied = True
            self._apply_no_activate()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.isVisible():
            event.ignore()
            self.close_view()
            return
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close_view()
            event.accept()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    # outside-click watcher
    # ------------------------------------------------------------------ #
    def _show_context_menu(self, pos: QPoint) -> None:
        """Right-click menu: popup lock, copy, search and close."""

        widget = self.sender() or self.panel
        menu = self._context_menu()

        # Clicks on the open menu land outside frameGeometry — pause the
        # watcher while it is up so picking an item cannot close the popup.
        self._stop_click_watch()
        menu.exec(widget.mapToGlobal(pos))
        self._maybe_resume_click_watch()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.panel and event.type() == QEvent.Resize:
            self._position_corner_buttons()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._welcome_mode:
            # one frame later the layout (and the viewport size) is final;
            # re-centering directly here reads a stale viewport height
            QTimer.singleShot(0, self._center_welcome)

    def _position_corner_buttons(self) -> None:
        """Glue the corner buttons to the panel: pin right, settings left."""

        self._pin_btn.move(self.panel.width() - self._pin_btn.width() - 10, 10)
        self._settings_btn.move(10, 10)

    def _over_corner_button(self, pos: QPoint) -> bool:
        """True when *pos* (window coords) lies over a corner button."""

        for btn in (self._pin_btn, self._settings_btn):
            if not btn.isVisible():
                continue
            rect = QRect(btn.mapTo(self, QPoint(0, 0)), btn.size())
            if rect.contains(pos):
                return True
        return False

    def _context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setLayoutDirection(Qt.RightToLeft)

        lock_action = menu.addAction(
            "🔓 باز کردن قفل پنجره" if self._view_locked else "🔒 قفل پنجره"
        )
        lock_action.triggered.connect(self._toggle_view_lock)
        menu.addSeparator()

        copy_action = menu.addAction("کپی")
        copy_action.setEnabled(self.browser.textCursor().hasSelection())
        copy_action.triggered.connect(self.browser.copy)
        menu.addAction("جستجو").triggered.connect(self.toggle_search)
        menu.addAction("تنظیمات…").triggered.connect(
            self.settings_requested.emit
        )
        menu.addSeparator()
        menu.addAction("بستن").triggered.connect(self.close_view)
        return menu

    def _toggle_view_lock(self) -> None:
        self._apply_view_lock(not self._view_locked)

    def _on_pin_toggled(self, checked: bool) -> None:
        self._apply_view_lock(checked)

    def _apply_view_lock(self, locked: bool) -> None:
        """Single source of truth for the lock/pin state."""

        self._view_locked = locked
        was_blocked = self._pin_btn.blockSignals(True)
        self._pin_btn.setChecked(locked)
        self._pin_btn.blockSignals(was_blocked)
        self._pin_btn.setToolTip(
            "برداشتن پین" if locked else "پین کردن پنجره (بسته‌نشدن با کلیک بیرون)"
        )
        if locked:
            self._stop_click_watch()
        else:
            self._maybe_resume_click_watch()

    def _maybe_resume_click_watch(self) -> None:
        if (
            self.isVisible()
            and not self._is_hiding
            and self._settings.close_on_outside_click
            and not self._view_locked
        ):
            self._start_click_watch()

    def _start_click_watch(self) -> None:
        """Watch for mouse presses outside the popup while it is visible.

        The popup deliberately never takes focus on show (WS_EX_NOACTIVATE),
        so a Qt focus-out event cannot be used: the window usually has no
        focus to lose. Instead a cheap timer polls the global mouse-button
        state and closes the popup when a press lands outside its frame.
        """

        if self._click_timer is not None:
            return
        self._mouse_button_down = self._any_mouse_button_down()
        self._click_timer = QTimer(self)
        self._click_timer.setInterval(CLICK_WATCH_INTERVAL_MS)
        self._click_timer.timeout.connect(self._check_outside_click)
        self._click_timer.start()

    def _stop_click_watch(self) -> None:
        if self._click_timer is not None:
            self._click_timer.stop()
            self._click_timer.deleteLater()
            self._click_timer = None

    def _check_outside_click(self) -> None:
        if self._view_locked:
            return
        down = self._any_mouse_button_down()
        pressed = down and not self._mouse_button_down
        self._mouse_button_down = down
        if not pressed:
            return
        if not self.frameGeometry().contains(QCursor.pos()):
            self.close_view()

    @staticmethod
    def _any_mouse_button_down() -> bool:
        try:
            user32 = ctypes.windll.user32
            left = user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000
            right = user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000
            return bool(left) or bool(right)
        except Exception:  # pragma: no cover - non-Windows platforms
            return False

    # ------------------------------------------------------------------ #
    # windows interop
    # ------------------------------------------------------------------ #
    def _apply_no_activate(self) -> None:
        """Add ``WS_EX_NOACTIVATE`` so showing the popup keeps the user's focus."""

        if self.windowHandle() is None:
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)
        except Exception:  # pragma: no cover - non-Windows / permission issues
            pass

    def nativeEvent(self, event_type, message):  # noqa: N802
        if bytes(event_type) == b"windows_generic_MSG":
            try:
                msg = wintypes.MSG.from_address(int(message))
            except Exception:  # pragma: no cover
                return super().nativeEvent(event_type, message)

            if msg.message == WM_NCHITTEST:
                x = ctypes.c_short(msg.lParam & 0xFFFF).value
                y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
                local = self.mapFromGlobal(QPoint(x, y))
                if self._over_corner_button(local):
                    # corner buttons sit inside the resize/drag bands; let
                    # the click reach the button instead
                    return super().nativeEvent(event_type, message)
                edge = self._hit_edge(local)
                if edge:
                    return True, edge
                if self._in_drag_margin(local):
                    # empty padding around the content behaves as a title bar
                    return True, HTCAPTION
            elif msg.message == WM_MOUSEACTIVATE:
                # Clicks on the (non-activating) window should still focus it
                # so keyboard shortcuts work once the user interacts with it.
                return True, MA_ACTIVATE
        return super().nativeEvent(event_type, message)

    def _in_drag_margin(self, pos: QPoint) -> bool:
        """True for clicks on the empty padding around the content card.

        Those regions act as a draggable title bar; anything over the card
        (text, tables, scrollbars) or the search bar stays interactive.
        """

        card = self.browser.parentWidget()
        panel = card.parentWidget() if card is not None else None
        if card is None or panel is None:
            return False
        panel_rect = QRect(panel.mapTo(self, QPoint(0, 0)), panel.size())
        if not panel_rect.contains(pos):
            return False
        for child in (card, self.search_bar):
            if not child.isVisible():
                continue
            child_rect = QRect(child.mapTo(self, QPoint(0, 0)), child.size())
            if child_rect.contains(pos):
                return False
        return True

    def _hit_edge(self, pos: QPoint) -> int:
        """Map a cursor position to a native resize edge (0 = client area)."""

        left = pos.x() < RESIZE_BAND
        right = pos.x() > self.width() - RESIZE_BAND
        top = pos.y() < RESIZE_BAND
        bottom = pos.y() > self.height() - RESIZE_BAND
        if left and top:
            return HTTOPLEFT
        if right and top:
            return HTTOPRIGHT
        if left and bottom:
            return HTBOTTOMLEFT
        if right and bottom:
            return HTBOTTOMRIGHT
        if left:
            return HTLEFT
        if right:
            return HTRIGHT
        if top:
            return HTTOP
        if bottom:
            return HTBOTTOM
        return 0
