"""Persistent application settings for RTL Viewer.

Settings are stored as a small JSON document under the user's profile
(``%APPDATA%/RTLViewer/settings.json`` on Windows) so the portable ``exe``
build and the plain ``python main.py`` run share the same state.

The module intentionally depends only on the standard library so it can be
unit-tested without a Qt application instance.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

APP_DIR_NAME = "RTLViewer"
SETTINGS_FILE_NAME = "settings.json"

#: Valid zoom range applied to every rendered font size.
MIN_FONT_SCALE = 0.5
MAX_FONT_SCALE = 2.5

#: Default / clamped popup size (the visible panel is slightly smaller
#: because the frameless window keeps a margin for the drop shadow).
DEFAULT_WIDTH = 900
DEFAULT_HEIGHT = 700
MIN_WIDTH = 600
MIN_HEIGHT = 400
MAX_WIDTH = 3840
MAX_HEIGHT = 2160


#: Default global toggle hotkey (may be changed from the settings dialog):
#: the Right-arrow key alone.
DEFAULT_HOTKEY = "right"


def config_dir() -> Path:
    """Return the directory that stores user configuration for the app."""

    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / APP_DIR_NAME


@dataclass
class AppSettings:
    """User-adjustable application state persisted between runs."""

    font_scale: float = 1.0
    window_width: int = DEFAULT_WIDTH
    window_height: int = DEFAULT_HEIGHT
    #: Last popup position (None = center on the active screen).
    window_x: Optional[int] = None
    window_y: Optional[int] = None
    #: Close the popup when the user clicks anywhere outside it.
    close_on_outside_click: bool = True
    #: Global toggle hotkey, e.g. ``"ctrl+win+alt"`` or ``"ctrl+alt+space"``.
    hotkey: str = DEFAULT_HOTKEY
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def _clamp(cls, value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppSettings":
        """Build settings from a raw dictionary, ignoring invalid values."""

        known = {f.name for f in fields(cls)} - {"extra"}
        cleaned = {k: v for k, v in data.items() if k in known}
        settings = cls(**cleaned)  # type: ignore[arg-type]
        settings.font_scale = float(
            cls._clamp(round(settings.font_scale, 2), MIN_FONT_SCALE, MAX_FONT_SCALE)
        )
        settings.window_width = int(
            cls._clamp(settings.window_width, MIN_WIDTH, MAX_WIDTH)
        )
        settings.window_height = int(
            cls._clamp(settings.window_height, MIN_HEIGHT, MAX_HEIGHT)
        )
        settings.extra = dict(data.get("extra", {}))
        return settings

    @property
    def path(self) -> Path:
        """Full path of the JSON file backing these settings."""

        return config_dir() / SETTINGS_FILE_NAME

    @classmethod
    def load(cls) -> "AppSettings":
        """Load settings from disk, falling back to defaults on any error."""

        try:
            raw = json.loads(cls._path_or_none().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        return cls.from_dict(raw)

    @classmethod
    def _path_or_none(cls) -> Path:
        return config_dir() / SETTINGS_FILE_NAME

    def save(self) -> None:
        """Persist settings to disk; silently ignore IO failures."""

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = asdict(self)
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    #: Fields the app mutates by itself while running (window geometry);
    #: preferences such as the hotkey are only written by the settings
    #: dialog, at the moment the user changes them.
    RUNTIME_FIELDS = ("window_width", "window_height", "window_x", "window_y")

    def save_runtime(self) -> None:
        """Persist only runtime-mutated fields, merged onto the file on disk.

        A full save from a stale in-memory copy (e.g. an instance started
        before the user changed the hotkey, saving on exit) would silently
        revert user preferences to old values. This merge keeps whatever
        is on disk for every preference field.
        """

        try:
            disk = AppSettings.load()
        except Exception:
            disk = AppSettings()
        for name in self.RUNTIME_FIELDS:
            setattr(disk, name, getattr(self, name))
        disk.save()
