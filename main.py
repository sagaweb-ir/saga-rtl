"""RTL Clipboard Viewer — application entry point.

Wires the settings, clipboard service, markdown renderer, popup window and
global hotkeys together, loads the bundled fonts, applies the dark theme and
provides a minimal tray icon so the background process can be quit.

Run with::

    python main.py
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

APP_NAME = "RTL Viewer"

#: Candidate code fonts, first one installed wins.
_CODE_FONT_CANDIDATES = ("JetBrains Mono", "Cascadia Code", "Consolas", "Courier New")

_APP_QSS = """
QMenu { background: #161b22; color: #e6edf3; border: 1px solid #30363d; }
QMenu::item { padding: 6px 24px; }
QMenu::item:selected { background: #1f6feb; }
"""


def resources_dir() -> Path:
    """Locate the ``resources`` folder (source checkout or PyInstaller bundle)."""

    if hasattr(sys, "_MEIPASS"):  # PyInstaller --onefile
        return Path(sys._MEIPASS) / "resources"
    return Path(__file__).resolve().parent / "resources"


def load_fonts() -> Optional[str]:
    """Register bundled fonts; return the family to use for prose (or None)."""

    families: list = []
    for file_name in (
        "Vazirmatn-Regular.ttf",
        "JetBrainsMono-Regular.ttf",
    ):
        path = resources_dir() / file_name
        if path.exists():
            font_id = QFontDatabase.addApplicationFont(str(path))
            if font_id != -1:
                families.extend(QFontDatabase.applicationFontFamilies(font_id))
    return families[0] if families else None


def resolve_code_font() -> str:
    """Pick the first monospace font available on this machine."""

    installed = set(QFontDatabase.families())
    for candidate in _CODE_FONT_CANDIDATES:
        if candidate in installed:
            return candidate
    return "Consolas"


def build_tray_icon() -> QIcon:
    """Draw a small rounded-square "RTL" badge for the system tray."""

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setBrush(QColor("#1f6feb"))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(4, 4, 56, 56, 16, 16)
    painter.setPen(QColor("#ffffff"))
    font = QFont("Segoe UI", 20, QFont.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "ر")
    painter.end()
    return QIcon(pixmap)


def _open_settings(window, hotkeys, settings) -> None:
    """Open the (modal) settings dialog on top of the popup."""

    from settings_dialog import SettingsDialog

    dialog = SettingsDialog(settings, hotkeys, window)
    # Clicks inside the dialog land outside the popup's frame — pause the
    # outside-click watcher so the popup survives while settings are open
    # (the font slider live-previews on the visible popup).
    window._stop_click_watch()
    dialog.exec()
    window._maybe_resume_click_watch()


def _create_tray(app: QApplication, window, hotkeys, settings) -> None:
    """Minimal tray icon so the always-running process can be quit."""

    from settings_dialog import pretty_hotkey

    if not QSystemTrayIcon.isSystemTrayAvailable():
        # When autostarted at logon the taskbar tray may not be ready yet.
        # Retry for a couple of minutes instead of leaving the app running
        # with no tray icon (which would make it unreachable).
        attempts = getattr(app, "_rtl_tray_attempts", 0) + 1
        app._rtl_tray_attempts = attempts  # type: ignore[attr-defined]
        if attempts <= 12:
            QTimer.singleShot(
                10_000, lambda: _create_tray(app, window, hotkeys, settings)
            )
        return
    tray = QSystemTrayIcon(build_tray_icon(), parent=app)
    tray.setToolTip(f"{APP_NAME} — {pretty_hotkey(hotkeys.hotkey)}")

    menu = QMenu()
    toggle_action = QAction("نمایش / مخفی‌کردن", menu)
    toggle_action.triggered.connect(window.toggle)
    settings_action = QAction("تنظیمات…", menu)
    settings_action.triggered.connect(
        lambda: _open_settings(window, hotkeys, settings)
    )
    quit_action = QAction("خروج", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(toggle_action)
    menu.addAction(settings_action)
    menu.addSeparator()
    menu.addAction(quit_action)
    menu.setStyleSheet(_APP_QSS)

    tray.setContextMenu(menu)
    # Left click toggles the popup as well.
    tray.activated.connect(
        lambda reason: window.toggle()
        if reason == QSystemTrayIcon.ActivationReason.Trigger
        else None
    )
    tray.show()
    app.setQuitOnLastWindowClosed(False)
    # Keep references alive for the process lifetime.
    app._rtl_tray = tray  # type: ignore[attr-defined]
    app._rtl_tray_menu = menu  # type: ignore[attr-defined]


def _preload_renderer_modules() -> None:
    """Import heavy renderers off-thread so the first popup opens fast."""

    def worker() -> None:
        try:
            import markdown_renderer  # noqa: F401
        except Exception:  # pragma: no cover
            pass

    threading.Thread(target=worker, name="preload", daemon=True).start()


def main() -> int:
    from PySide6.QtCore import QLockFile
    from settings import config_dir

    # Single-instance guard: without it, launching the app again while an
    # older copy is still in the tray keeps two processes with two hotkey
    # hooks alive — and the stale copy saving its (old) settings on exit
    # reverts the hotkey the user recorded in the newer one. A crashed
    # process releases the lock automatically (native Windows locking).
    if not os.environ.get("RTL_VIEWER_ALLOW_SECOND_INSTANCE"):
        try:
            config_dir().mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        lock = QLockFile(str(config_dir() / "instance.lock"))
        if not lock.tryLock(100):
            return 0  # already running — keep the existing tray instance
        globals()["_instance_lock"] = lock  # hold for the process lifetime

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    # NOTE: the app-level layout direction stays LTR on purpose. Setting it to
    # RightToLeft leaks into QTextBrowser's document default direction and
    # right-aligns English text and code blocks. RTL is applied per-widget
    # (e.g. the search bar) instead; rendered text direction comes from the
    # HTML dir/align attributes.
    app.setStyleSheet(_APP_QSS)

    text_family = load_fonts()
    if text_family:
        app.setFont(QFont(text_family, 11))

    _preload_renderer_modules()

    from clipboard import ClipboardService
    from hotkeys import HotkeyManager
    from markdown_renderer import FontFamilies, Renderer
    from settings import DEFAULT_HOTKEY, AppSettings
    from window import ViewerWindow

    settings = AppSettings.load()
    clipboard = ClipboardService()
    fonts = FontFamilies(code=resolve_code_font())
    window = ViewerWindow(
        settings=settings,
        clipboard=clipboard,
        renderer_factory=lambda: Renderer(fonts),
    )

    hotkeys = HotkeyManager(parent=app)
    try:
        hotkeys.set_hotkey(settings.hotkey)
    except (ValueError, RuntimeError):
        # Fall back at RUNTIME only — never overwrite the user's saved
        # spec: a transient parse failure (e.g. keyboard layout not ready
        # right after boot) would otherwise permanently revert the hotkey
        # to the default on the next settings save.
        try:
            hotkeys.set_hotkey(DEFAULT_HOTKEY)
        except (ValueError, RuntimeError):  # pragma: no cover
            pass
    hotkeys.toggle_requested.connect(window.toggle)
    hotkeys.escape_requested.connect(window.close_view)
    window.escape_hook_wanted.connect(hotkeys.set_escape_enabled)
    window.settings_requested.connect(
        lambda: _open_settings(window, hotkeys, settings)
    )
    if not os.environ.get("RTL_VIEWER_DISABLE_HOTKEYS"):
        hotkeys.start()

    _create_tray(app, window, hotkeys, settings)

    # Keep the autostart registry command current (e.g. after this update
    # adds the --autostart flag to it).
    try:
        import autostart

        autostart.refresh()
    except Exception:  # pragma: no cover - registry unavailable
        pass

    # A manual launch greets with the branded welcome page; a logon
    # autostart (registry Run entry passes --autostart) stays silent.
    if "--autostart" not in sys.argv:
        window.show_welcome()
        if not settings.extra.get("guide_dismissed"):
            from guide_dialog import GuideDialog

            # Clicks on the dialog land outside the popup's frame — pause
            # the outside-click watcher while it is up (same as settings).
            window._stop_click_watch()
            GuideDialog(settings, window).exec()
            window._maybe_resume_click_watch()

    def _on_quit() -> None:
        hotkeys.stop()
        settings.save_runtime()

    app.aboutToQuit.connect(_on_quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
