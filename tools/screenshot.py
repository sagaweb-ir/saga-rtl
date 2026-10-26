# -*- coding: utf-8 -*-
"""Generate README screenshots of the popup (run from the project root).

Uses the real platform (not offscreen) so Vazirmatn renders exactly as
users see it. The popup flashes briefly on screen while grabbing.

    .venv/Scripts/python.exe -X utf8 tools/screenshot.py
"""

import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from PySide6.QtWidgets import QApplication

import main as app_main
from markdown_renderer import FontFamilies, Renderer
from settings import AppSettings
from window import ViewerWindow

DEMO = """# گزارش تست SAGA RTL

سلام! این یک **پیش‌نمایش زنده** از رندر Markdown با پاراگراف فارسیِ راست‌چین است.

## قابلیت‌ها

- رندر کامل Markdown مثل گیت‌هاب
- `کد درون‌خطی` و بلوک کد چپ‌چین با Syntax Highlight
- فلش‌ها در متن فارسی خودکار آینه می‌شوند: شروع ← پایان
- جدول، نقل‌قول، تسک‌لیست و لینک

| مورد | نتیجه |
| --- | --- |
| شروع برنامه | ✅ ~۱۷۰ms |
| مصرف RAM | ✅ ۷۵MB |
| جهت متن | ✅ فارسی RTL / کد LTR |

> نقل‌قول: متن کپی‌شده هرجا که باشد، با یک کلید زیبا نمایش داده می‌شود.

```python
def hello_rtl():
    return "SAGA RTL Clipboard Viewer"
```
"""


class StubClipboard:
    def __init__(self, text: str) -> None:
        self._text = text

    def read_text(self) -> str:
        return self._text


def main() -> int:
    app = QApplication(sys.argv)
    if family := app_main.load_fonts():
        from PySide6.QtGui import QFont

        app.setFont(QFont(family, 11))

    docs = PROJ / "docs"
    docs.mkdir(exist_ok=True)
    settings = AppSettings(window_width=880, window_height=660,
                           window_x=120, window_y=90)

    def build() -> ViewerWindow:
        return ViewerWindow(
            settings=settings,
            clipboard=StubClipboard(""),
            renderer_factory=lambda: Renderer(
                FontFamilies(code=app_main.resolve_code_font())
            ),
        )

    # 1) welcome page
    win = build()
    win.show_welcome()
    for _ in range(12):
        app.processEvents()
    win.panel.grab().save(str(docs / "screenshot-welcome.png"), "PNG")

    # 2) rendered markdown content
    win.close_view()
    win._clipboard = StubClipboard(DEMO)
    win.open_view()
    for _ in range(12):
        app.processEvents()
    win.panel.grab().save(str(docs / "screenshot-content.png"), "PNG")

    win.close_view()
    for _ in range(12):
        app.processEvents()
    print("saved:", docs / "screenshot-welcome.png")
    print("saved:", docs / "screenshot-content.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
