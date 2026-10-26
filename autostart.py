"""Run-at-Windows-logon support for RTL Viewer.

Uses the per-user ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``
registry key (no administrator rights needed). The registered command
launches:

* the frozen ``RTLViewer.exe`` in PyInstaller builds, or
* the virtual-env ``pythonw.exe`` running ``main.py`` in source checkouts,
  so no console window flashes at logon.

State lives in the registry itself — it is deliberately NOT mirrored into
settings.json, so the two stores cannot drift apart.
"""

from __future__ import annotations

import sys
import winreg
from pathlib import Path
from typing import Optional

VALUE_NAME = "RTLViewer"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command() -> str:
    """Command line registered for autostart (quoted executable + args).

    The ``--autostart`` flag lets the app distinguish a logon launch
    (stay silent in the tray) from a manual one (greet with the welcome
    page).
    """

    if getattr(sys, "frozen", False):  # PyInstaller build
        return f'"{Path(sys.executable)}" --autostart'
    python = Path(sys.executable)
    if python.name.lower() == "python.exe":
        python = python.with_name("pythonw.exe")  # console-less interpreter
    main_py = Path(__file__).resolve().parent / "main.py"
    return f'"{python}" -X utf8 "{main_py}" --autostart'


def current_command(key_path: str = RUN_KEY) -> Optional[str]:
    """The registered command line, or None when not registered."""

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return value
    except OSError:
        return None


def refresh(key_path: str = RUN_KEY) -> None:
    """Rewrite the Run entry if its command is outdated (e.g. missing the
    ``--autostart`` flag after an app update); no-op when disabled."""

    if is_enabled(key_path) and current_command(key_path) != launch_command():
        set_enabled(True, key_path=key_path)


def is_enabled(key_path: str = RUN_KEY) -> bool:
    """True when the app is registered to start with Windows."""

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
            return True
    except OSError:
        return False


def set_enabled(enabled: bool, key_path: str = RUN_KEY) -> None:
    """Register/unregister the app for the current user.

    Raises ``OSError`` on registry problems so callers can report failure.
    """

    if not enabled:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            pass  # already disabled
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
