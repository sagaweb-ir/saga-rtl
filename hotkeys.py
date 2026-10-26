"""Global hotkey management.

Uses the :mod:`keyboard` library (Windows low-level hook) and bridges its
background-thread callbacks into the Qt main thread through signals, so Qt
widgets are never touched from a foreign thread.

The toggle shortcut is user-configurable (settings dialog / settings.json).
It is described by a string such as ``"ctrl+win+alt"`` or ``"ctrl+alt+space"``
and matched on *scan codes* rather than key names, which keeps it working on
any keyboard layout (e.g. Persian layouts can leave key names unresolved).
Modifiers may be pressed in any order; a pure-modifier chord fires once per
press and is immune to key auto-repeat.

The manager can also *record* a new shortcut: while recording, the next key
combination the user presses is reported through the :attr:`recorded` signal
(``""`` means cancelled).

Only *toggle* and *escape* are system-wide shortcuts; everything else
(Ctrl+F, Ctrl+C, F11, ...) is a regular window shortcut because grabbing
those globally would break the operating system shortcuts.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List, Optional

from PySide6.QtCore import QObject, Signal

from settings import DEFAULT_HOTKEY

try:  # pragma: no cover - exercised only on exotic setups
    import keyboard
except ImportError as exc:  # pragma: no cover
    keyboard = None
    _IMPORT_ERROR = exc

# --------------------------------------------------------------------------- #
# scan-code tables
# --------------------------------------------------------------------------- #
#: Positive values are physical scan codes; negative ones are the hook's
#: ``-vk`` fallback used for VK-injected events.
_CTRL_SCANS = frozenset({29, 285, -162, -163})           # left/right ctrl
_ALT_SCANS = frozenset({56, 312, -164, -165})            # left/right alt
_SHIFT_SCANS = frozenset({42, 54, -160, -161})           # left/right shift
_WIN_SCANS = frozenset({91, 92, 347, 348, -91, -92})     # left/right windows

_MODIFIER_GROUPS: Dict[str, FrozenSet[int]] = {
    "ctrl": _CTRL_SCANS,
    "alt": _ALT_SCANS,
    "shift": _SHIFT_SCANS,
    "win": _WIN_SCANS,
}

_ALIASES = {
    "control": "ctrl",
    "windows": "win",
    "left windows": "win",
    "right windows": "win",
    "super": "win",
    "meta": "win",
    "cmd": "win",
    "alt gr": "alt",
}


def normalize_key_name(name: str) -> str:
    """Map raw hook names (``left ctrl``…) to canonical tokens (``ctrl``…)."""

    token = (name or "").strip().lower()
    if token in _ALIASES:
        return _ALIASES[token]
    if "ctrl" in token:
        return "ctrl"
    if "windows" in token or token in ("win", "super", "meta"):
        return "win"
    if "shift" in token:
        return "shift"
    if "alt" in token:
        return "alt"
    return token


def parse_hotkey(spec: str) -> List[FrozenSet[int]]:
    """Translate a hotkey string into a list of scan-code groups.

    Each group must have at least one key held for the hotkey to fire.
    Raises :class:`ValueError` with a Persian message for invalid specs.
    """

    if keyboard is None:  # pragma: no cover
        raise RuntimeError(
            "The 'keyboard' package is required for global hotkeys: "
            f"{_IMPORT_ERROR}"
        )
    tokens = [t.strip().lower() for t in spec.split("+") if t.strip()]
    if not tokens:
        raise ValueError("میانبر خالی است")
    if len(tokens) == 1 and tokens[0] in _MODIFIER_GROUPS:
        raise ValueError(
            "یک کلید تغییردهنده به‌تنهایی میانبر نمی‌شود؛ "
            "کلید دیگری هم اضافه کنید"
        )
    groups: List[FrozenSet[int]] = []
    for token in tokens:
        token = _ALIASES.get(token, token)
        group = _MODIFIER_GROUPS.get(token)
        if group is None:
            try:
                group = frozenset(keyboard.key_to_scan_codes(token))
            except Exception:
                raise ValueError(f"کلید ناشناخته: «{token}»") from None
            if not group:
                raise ValueError(f"کلید ناشناخته: «{token}»")
        groups.append(group)
    return groups


class HotkeyManager(QObject):
    """Owns the global hotkeys and emits thread-safe Qt signals."""

    #: Emitted (from the hook thread, delivered queued on the main thread)
    #: when the user presses the configured show/hide hotkey.
    toggle_requested = Signal()
    #: Emitted when the user presses Escape while the popup is visible.
    escape_requested = Signal()
    #: Result of shortcut recording (``""`` = cancelled).
    recorded = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._hook: Optional[int] = None
        self._escape_handle: Optional[int] = None
        self._pressed_scans: set = set()
        self._combo_active = False
        self._hotkey_spec: str = DEFAULT_HOTKEY
        self._groups: List[FrozenSet[int]] = list(_MODIFIER_GROUPS.values())[:0]
        self._recording = False
        self._rec_scans: Dict[int, str] = {}
        self._rec_names: List[str] = []
        try:
            self.set_hotkey(DEFAULT_HOTKEY)
        except (ValueError, RuntimeError):  # pragma: no cover
            self._groups = [_CTRL_SCANS, _ALT_SCANS, _WIN_SCANS]

    # ------------------------------------------------------------------ #
    # configuration
    # ------------------------------------------------------------------ #
    def set_hotkey(self, spec: str) -> None:
        """Validate *spec* and make it the live toggle hotkey."""

        groups = parse_hotkey(spec)
        tokens = [t.strip().lower() for t in spec.split("+") if t.strip()]
        self._groups = groups
        self._hotkey_spec = "+".join(tokens)

    @property
    def hotkey(self) -> str:
        """The currently active hotkey specification."""

        return self._hotkey_spec

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Install the global keyboard hook."""

        if keyboard is None:  # pragma: no cover
            raise RuntimeError(
                "The 'keyboard' package is required for global hotkeys: "
                f"{_IMPORT_ERROR}"
            )
        if self._hook is None:
            self._hook = keyboard.hook(self._on_key_event)

    def stop(self) -> None:
        """Unhook everything registered by this manager."""

        if keyboard is None:  # pragma: no cover
            return
        if self._hook is not None:
            try:
                keyboard.unhook(self._hook)
            except (KeyError, ValueError):  # pragma: no cover
                pass
            self._hook = None
        if self._escape_handle is not None:
            try:
                keyboard.remove_hotkey(self._escape_handle)
            except (KeyError, ValueError):  # pragma: no cover
                pass
            self._escape_handle = None

    # ------------------------------------------------------------------ #
    # hook callback
    # ------------------------------------------------------------------ #
    def _on_key_event(self, event) -> None:
        scan = getattr(event, "scan_code", None)
        if scan is None:
            return
        down = event.event_type == "down"
        if down:
            self._pressed_scans.add(scan)
        else:
            self._pressed_scans.discard(scan)

        if self._recording:
            self._handle_recording(event, scan, down)
            return

        all_held = all(self._pressed_scans & group for group in self._groups)
        if all_held and not self._combo_active:
            self._combo_active = True
            self.toggle_requested.emit()
        elif not all_held:
            self._combo_active = False

    # ------------------------------------------------------------------ #
    # recording
    # ------------------------------------------------------------------ #
    def start_recording(self) -> None:
        """Begin listening for the next key combination."""

        self._recording = True
        self._rec_scans.clear()
        self._rec_names.clear()

    def cancel_recording(self) -> None:
        self._recording = False
        self._rec_scans.clear()
        self._rec_names.clear()

    def is_recording(self) -> bool:
        return self._recording

    def _handle_recording(self, event, scan: int, down: bool) -> None:
        raw_name = (getattr(event, "name", "") or "").strip().lower()
        if down:
            if raw_name == "esc":
                self.cancel_recording()
                self.recorded.emit("")
                return
            name = normalize_key_name(raw_name)
            if name in _MODIFIER_GROUPS:
                if scan not in self._rec_scans:
                    self._rec_scans[scan] = name
                    if name not in self._rec_names:
                        self._rec_names.append(name)
                return
            # a non-modifier key completes the combination
            combo = "+".join(self._rec_names + [name])
            self.cancel_recording()
            self.recorded.emit(combo)
        else:
            self._rec_scans.pop(scan, None)
            if not self._rec_scans and len(self._rec_names) >= 2:
                # every pressed key released -> pure modifier chord
                combo = "+".join(self._rec_names)
                self.cancel_recording()
                self.recorded.emit(combo)

    # ------------------------------------------------------------------ #
    # dynamic escape hook
    # ------------------------------------------------------------------ #
    def set_escape_enabled(self, enabled: bool) -> None:
        """Register/unregister a global Escape hook.

        The hook exists only while the popup is visible (and the in-window
        search bar is closed), so Escape keeps its normal meaning for every
        other application the rest of the time.
        """

        if keyboard is None:  # pragma: no cover
            return
        if enabled and self._escape_handle is None:
            self._escape_handle = keyboard.add_hotkey(
                "esc", lambda: self.escape_requested.emit()
            )
        elif not enabled and self._escape_handle is not None:
            try:
                keyboard.remove_hotkey(self._escape_handle)
            except (KeyError, ValueError):  # pragma: no cover
                pass
            self._escape_handle = None
