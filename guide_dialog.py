"""First-launch guide dialog for RTL Viewer.

A small always-on-top window that explains how the app works the first
time it is launched manually. The "don't show again" checkbox persists
``extra.guide_dismissed``; closing without the checkbox keeps the guide
coming back on the next launch.
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
    QVBoxLayout,
    QWidget,
)

from settings import AppSettings
from settings_dialog import DIALOG_QSS

#: (number, explanation) rows shown in the guide body.
GUIDE_LINES = (
    ("۱", "هر متنی را کپی کنید و کلید جهت راست (→) را بزنید تا در یک "
          "پاپ‌آپ تیره و زیبا همان‌جا رندر شود — فارسی راست‌چین، کد چپ‌چین."),
    ("۲", "بستن پاپ‌آپ: کلید Esc یا کلیک بیرون از پنجره."),
    ("۳", "نگه‌داشتن پاپ‌آپ روی صفحه: دکمهٔ 📌 گوشهٔ بالا-راست یا "
          "«قفل پنجره» در راست‌کلیک."),
    ("۴", "تغییر میانبر و بقیهٔ تنظیمات: دکمهٔ ⚙️ گوشهٔ بالا-چپ، راست‌کلیک ← "
          "«تنظیمات…»، یا راست‌کلیک روی آیکون برنامه کنار ساعت."),
)


class GuideDialog(QDialog):
    """One-time usage guide with a persistent "don't show again" tick."""

    def __init__(
        self,
        settings: AppSettings,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings

        self.setWindowTitle("راهنمای SAGA RTL")
        self.setLayoutDirection(Qt.RightToLeft)
        self.setModal(True)
        # The popup is always-on-top; the dialog must be too or it opens
        # behind it (same as the settings dialog).
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setStyleSheet(DIALOG_QSS)
        self.setMinimumWidth(480)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(10)

        title = QLabel("👋 خوش آمدید! این برنامه چطور کار می‌کند؟", self)
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        title.setWordWrap(True)
        root.addWidget(title)

        for number, text in GUIDE_LINES:
            line = QLabel(f"<b>{number}.</b> {text}", self)
            line.setWordWrap(True)
            root.addWidget(line)

        self.dont_show_cb = QCheckBox("این راهنما دیگر نشان داده نشود", self)
        root.addSpacing(4)
        root.addWidget(self.dont_show_cb)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        ok_btn = QPushButton("متوجه شدم", self)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        root.addLayout(btn_row)

    def accept(self) -> None:  # noqa: D102
        if self.dont_show_cb.isChecked():
            self._settings.extra["guide_dismissed"] = True
            self._settings.save()
        super().accept()
