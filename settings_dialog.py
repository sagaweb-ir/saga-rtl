"""Settings dialog for RTL Viewer (opened from the tray menu).

Lets the user change, without editing JSON by hand:

* the global toggle hotkey -- press the capture button, then press the
  desired key combination (any order; pure modifier chords like
  ``Ctrl+Win+Alt`` work, and ``Esc`` cancels),
* whether clicking outside the popup closes it,
* the base font size (zoom) of the rendered content.

Every change applies immediately and is persisted to settings.json.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from hotkeys import HotkeyManager
from settings import (
    DEFAULT_HOTKEY,
    MAX_FONT_SCALE,
    MIN_FONT_SCALE,
    AppSettings,
)
from window import ViewerWindow
import autostart

DIALOG_QSS = """
QDialog {
    background: #161b22;
    border: 1px solid #30363d;
}
QLabel {
    color: #e6edf3;
    font-size: 13px;
}
QLabel#hint {
    color: #8b949e;
    font-size: 12px;
}
QPushButton {
    background: #21262d;
    color: #e6edf3;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 6px 14px;
    font-size: 13px;
}
QPushButton:hover { background: #30363d; }
QPushButton#captureBtn {
    font-family: 'JetBrains Mono', 'Cascadia Code', Consolas;
    min-width: 180px;
}
QCheckBox {
    color: #e6edf3;
    font-size: 13px;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px; height: 16px;
    border-radius: 4px;
    border: 1px solid #30363d;
    background: #0d1117;
}
QCheckBox::indicator:checked { background: #1f6feb; border-color: #1f6feb; }
QSlider::groove:horizontal {
    height: 6px; border-radius: 3px; background: #30363d;
}
QSlider::handle:horizontal {
    width: 16px; height: 16px; margin: -5px 0;
    border-radius: 8px; background: #1f6feb;
}
QSlider::sub-page:horizontal { background: #1f6feb; border-radius: 3px; }
"""


def pretty_hotkey(spec: str) -> str:
    """Human-friendly display form, e.g. ``Ctrl + Win + Alt + Right``."""

    return " + ".join(
        token.capitalize() if token in ("ctrl", "alt", "shift", "win") else token
        for token in spec.split("+")
        if token
    )


class SettingsDialog(QDialog):
    """Immediate-apply settings dialog."""

    def __init__(
        self,
        settings: AppSettings,
        hotkeys: HotkeyManager,
        window: Optional[ViewerWindow],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._hotkeys = hotkeys
        self._window = window

        self.setWindowTitle("تنظیمات RTL Viewer")
        self.setLayoutDirection(Qt.RightToLeft)
        self.setModal(True)
        # The popup is an always-on-top window; the dialog must carry the
        # same flag or it opens BEHIND it.
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setStyleSheet(DIALOG_QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        # ---- hotkey row ---- #
        hotkey_row = QHBoxLayout()
        hotkey_row.setSpacing(8)
        hotkey_label = QLabel("میانبر باز/بسته کردن:", self)
        self.capture_btn = QPushButton(pretty_hotkey(hotkeys.hotkey), self)
        self.capture_btn.setObjectName("captureBtn")
        reset_btn = QPushButton("پیش‌فرض", self)
        reset_btn.setToolTip(pretty_hotkey(DEFAULT_HOTKEY))
        hotkey_row.addWidget(hotkey_label)
        hotkey_row.addWidget(self.capture_btn, 1)
        hotkey_row.addWidget(reset_btn)
        root.addLayout(hotkey_row)

        self.hint = QLabel(
            "برای تغییر، دکمه را بزنید و کلید ترکیبی جدید را فشار دهید "
            "(Esc = انصراف)", self
        )
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        # ---- behaviour ---- #
        self.outside_click_cb = QCheckBox(
            "بستن پنجره با کلیک بیرون از آن", self
        )
        self.outside_click_cb.setChecked(settings.close_on_outside_click)
        root.addWidget(self.outside_click_cb)

        self.autostart_cb = QCheckBox(
            "اجرای خودکار برنامه با روشن شدن ویندوز", self
        )
        try:
            self.autostart_cb.setChecked(autostart.is_enabled())
        except OSError:  # registry unreadable — leave unchecked
            pass
        root.addWidget(self.autostart_cb)

        # ---- font size ---- #
        font_row = QHBoxLayout()
        font_row.setSpacing(8)
        font_label = QLabel("اندازه فونت:", self)
        self.size_slider = QSlider(Qt.Horizontal, self)
        # Grow the value left -> right (the standard mixer direction)
        # even though the dialog itself is RTL: Qt mirrors sliders under
        # RightToLeft, which reads as "larger font on the left".
        self.size_slider.setLayoutDirection(Qt.LeftToRight)
        self.size_slider.setRange(
            int(MIN_FONT_SCALE * 100), int(MAX_FONT_SCALE * 100)
        )
        self.size_slider.setValue(int(round(settings.font_scale * 100)))
        self.size_value = QLabel(f"{int(round(settings.font_scale * 100))}٪", self)
        self.size_value.setMinimumWidth(44)
        font_row.addWidget(font_label)
        font_row.addWidget(self.size_slider, 1)
        font_row.addWidget(self.size_value)
        root.addLayout(font_row)

        # ---- close ---- #
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("بستن", self)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)

        # ---- wiring ---- #
        self.capture_btn.clicked.connect(self._toggle_recording)
        reset_btn.clicked.connect(lambda: self._apply_hotkey(DEFAULT_HOTKEY))
        hotkeys.recorded.connect(self._on_recorded)
        self.outside_click_cb.toggled.connect(self._on_outside_click)
        self.autostart_cb.toggled.connect(self._on_autostart)
        self.size_slider.valueChanged.connect(
            lambda value: self.size_value.setText(f"{value}٪")
        )
        self.size_slider.sliderReleased.connect(self._on_font_size)
        close_btn.clicked.connect(self.close)

    # ------------------------------------------------------------------ #
    # hotkey capture
    # ------------------------------------------------------------------ #
    def _toggle_recording(self) -> None:
        if self._hotkeys.is_recording():
            self._hotkeys.cancel_recording()
            self._set_recording_ui(False, "انصراف شد")
            return
        self._hotkeys.start_recording()
        self.capture_btn.setText("… کلیدها را فشار دهید")
        self.hint.setText("میانبر دلخواه را بزنید — Esc انصراف است")

    def _set_recording_ui(self, recording: bool, hint: str) -> None:
        self.capture_btn.setText(
            "… کلیدها را فشار دهید" if recording
            else pretty_hotkey(self._hotkeys.hotkey)
        )
        self.hint.setText(hint)

    def _on_recorded(self, spec: str) -> None:
        if not spec:
            self._set_recording_ui(False, "انصراف شد")
            return
        if not self._apply_hotkey(spec):
            self._set_recording_ui(False, "این میانبر معتبر نیست؛ تغییر نکرد")
        else:
            self._set_recording_ui(False, "میانبر جدید ذخیره شد ✓")

    def _apply_hotkey(self, spec: str) -> bool:
        try:
            self._hotkeys.set_hotkey(spec)
        except (ValueError, RuntimeError):
            return False
        self._settings.hotkey = self._hotkeys.hotkey
        self._settings.save()
        return True

    # ------------------------------------------------------------------ #
    # other settings
    # ------------------------------------------------------------------ #
    def _on_outside_click(self, checked: bool) -> None:
        self._settings.close_on_outside_click = checked
        self._settings.save()

    def _on_autostart(self, checked: bool) -> None:
        """Register/unregister the Run key; revert the box on failure."""

        try:
            autostart.set_enabled(checked)
            self.hint.setText(
                "اجرای خودکار با ویندوز فعال شد ✓" if checked
                else "اجرای خودکار با ویندوز غیرفعال شد"
            )
        except OSError:
            self.autostart_cb.blockSignals(True)
            self.autostart_cb.setChecked(not checked)
            self.autostart_cb.blockSignals(False)
            self.hint.setText("تغییر اجرای خودکار ممکن نشد (خطای رجیستری)")

    def _on_font_size(self) -> None:
        scale = self.size_slider.value() / 100.0
        if scale == self._settings.font_scale:
            return
        self._settings.font_scale = scale
        if self._window is not None and self._window.isVisible():
            self._window._rerender_preserving_scroll()
        self._settings.save()

    # ------------------------------------------------------------------ #
    # Qt overrides
    # ------------------------------------------------------------------ #
    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._hotkeys.is_recording() and event.key() == Qt.Key_Escape:
            # recording cancellation is handled by the global hook
            event.accept()
            return
        super().keyPressEvent(event)
