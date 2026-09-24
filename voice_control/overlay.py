"""Floating caption pill and activity panel for Jev Voice Control.

The pill never takes keyboard focus (WS_EX_NOACTIVATE), so the window the user
is talking about stays in the foreground while captions and results show on
top of it. The activity panel is shown without activation too; it only takes
focus when clicked, because its command field needs the keyboard.
"""

from __future__ import annotations

import ctypes
import math
import re
import time
import tkinter as tk
from datetime import datetime
from tkinter import font as tkfont
from typing import Any, Callable

import win32api
import win32con
import win32gui

C = {
    "bg": "#16161b", "bg2": "#202028", "card": "#1c1c23", "chip": "#2a2a34", "border": "#30303b",
    "text": "#f3f3f6", "muted": "#a3a3b2", "faint": "#6e6e7c",
    "listen": "#ff6369", "work": "#9d8cff", "ok": "#3ecf8e", "warn": "#f5b544", "accent": "#9d8cff", "off": "#3b3b47",
}
TIMING = (("transcribe", "Heard", "#5aa9ff"), ("state", "Screen", "#f5b544"),
          ("jev_total", "Jev", "#9d8cff"), ("execute", "Ran", "#3ecf8e"))
GLYPHS = {  # Segoe Fluent Icons / Segoe MDL2 Assets code points
    "up": "", "down": "", "check": "", "warn": "", "close": "", "info": "",
    "help": "", "mic": "", "send": "", "bolt": "", "target": "", "hide": "",
    "folder": "", "key": "", "power": "", "clock": "", "steps": "",
    "keyboard": "", "pointer": "", "arrow_up": "", "arrow_down": "",
    "arrow_left": "", "arrow_right": "", "pencil": "", "zoom_in": "", "zoom_out": "",
    "zoom": "", "minimize": "", "maximize": "", "switch": "", "launch": "",
    "block": "", "more": "",
}
MDL2_GLYPHS = {"target": ""}  # Windows 10's icon font has no target; a flag stands in
FALLBACK = {
    "up": "˄", "down": "˅", "check": "✓", "warn": "!", "close": "✕", "info": "i", "help": "?", "mic": "●", "send": "➤",
    "bolt": "⚡", "target": "◎", "hide": "◌", "folder": "▤", "key": "⚷", "power": "⏻", "clock": "◷", "steps": "≡",
    "keyboard": "⌨", "pointer": "↖", "arrow_up": "↑", "arrow_down": "↓", "arrow_left": "←", "arrow_right": "→",
    "pencil": "✎", "zoom_in": "+", "zoom_out": "−", "zoom": "⌕", "minimize": "–", "maximize": "□", "switch": "⇄",
    "launch": "↗", "block": "⊘", "more": "…",
}
VERB_GLYPH = {
    "left_click": "pointer", "right_click": "pointer", "double_click": "pointer", "hover": "pointer",
    "scroll_up": "arrow_up", "scroll_down": "arrow_down", "scroll_left": "arrow_left", "scroll_right": "arrow_right",
    "type_text": "pencil", "press_key": "keyboard", "key_chord": "keyboard", "zoom_in": "zoom_in", "zoom_out": "zoom_out",
    "zoom_reset": "zoom", "minimize_window": "minimize", "maximize_window": "maximize", "close_window": "close",
    "switch_window": "switch", "alt_tab": "switch", "launch_app": "launch", "no_action": "block",
}


def mix(color: str, base: str, amount: float) -> str:
    """`color` blended onto `base`: Tk has no alpha, so tints are precomputed."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(base[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(y + (x - y) * amount):02x}" for x, y in zip(a, b))


# ---------------------------------------------------------------- text helpers

VERB_NOW = {
    "left_click": "Click", "right_click": "Right-click", "double_click": "Double-click", "hover": "Hover over",
    "scroll_up": "Scroll up", "scroll_down": "Scroll down", "scroll_left": "Scroll left", "scroll_right": "Scroll right",
    "type_text": "Type into", "press_key": "Press", "key_chord": "", "zoom_in": "Zoom in", "zoom_out": "Zoom out",
    "zoom_reset": "Reset zoom", "minimize_window": "Minimize window", "maximize_window": "Maximize window",
    "close_window": "Close window", "switch_window": "Switch to", "alt_tab": "Go to previous window",
    "launch_app": "Open", "no_action": "Do nothing",
}
VERB_PAST = {
    "left_click": "Clicked", "right_click": "Right-clicked", "double_click": "Double-clicked", "hover": "Hovered over",
    "scroll_up": "Scrolled up", "scroll_down": "Scrolled down", "scroll_left": "Scrolled left", "scroll_right": "Scrolled right",
    "type_text": "Typed into", "press_key": "Pressed", "key_chord": "", "zoom_in": "Zoomed in", "zoom_out": "Zoomed out",
    "zoom_reset": "Reset zoom", "minimize_window": "Minimized window", "maximize_window": "Maximized window",
    "close_window": "Closed window", "switch_window": "Switched to", "alt_tab": "Went to previous window",
    "launch_app": "Opened", "no_action": "Did nothing",
}
TARGETLESS = {"scroll_up", "scroll_down", "scroll_left", "scroll_right", "zoom_in", "zoom_out", "zoom_reset",
              "alt_tab", "no_action"}
FRIENDLY_ERRORS = (
    ("Speech was not recognized", "Didn't catch that"),
    ("verb confidence", "Not sure what you meant, try rephrasing"),
    ("No sufficiently confident target", "Couldn't find that on screen"),
    ("disagreed with spatial", "Not sure which one you meant, so nothing ran"),
    ("say 'type <text>'", "Say “type” followed by the text"),
    ("did not become foreground", "The window lost focus, so nothing ran"),
    ("not in captured controls", "That item changed before it could be clicked"),
    ("OPENROUTER_API_KEY", "No API key yet: right-click the pill, then API key"),
    ("401 Client Error", "Jev rejected the API key: right-click the pill, then API key"),
    ("ConnectionError", "Couldn't reach Jev, check your connection"),
    ("Timeout", "Jev took too long to answer"),
    ("HTTPError", "Jev returned an error"),
)


def target_label(target: dict[str, Any] | None) -> str:
    """Short human name of a planned target: the quoted control/window/app name, or the key."""
    if not target or target.get("kind") in {None, "none", "current"}:
        return ""
    if target.get("kind") in {"key", "chord"}:
        return str(target.get("key", "")).replace("_", " ")
    description = target.get("description", "")
    match = re.search(r'"(.*?)"(?= at |\s\(|$)', description) or re.search(r'"(.*?)"', description)
    return match.group(1) if match else description


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def action_phrase(verb: str | None, target: dict[str, Any] | None, past: bool = False) -> str:
    if not verb:
        return "Nothing planned"
    label = target_label(target)
    if verb == "key_chord":
        return (label or "Shortcut").capitalize()
    words = (VERB_PAST if past else VERB_NOW).get(verb, verb.replace("_", " ").capitalize())
    if verb in TARGETLESS or not label:
        return words
    if verb == "press_key":
        return f"{words} {label}"
    return f"{words} “{shorten(label, 48)}”"


def friendly_error(error: str | None) -> str:
    error = error or ""
    for needle, text in FRIENDLY_ERRORS:
        if needle.lower() in error.lower():
            return text
    return shorten(error.split(": ", 1)[-1] or "Something went wrong", 70)


def wrap_words(text: str, measure: Callable[[str], int], width: int) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if line and measure(candidate) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def ellipsize(text: str, measure: Callable[[str], int], width: int) -> str:
    if measure(text) <= width:
        return text
    while text and measure(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


def fit_tail(text: str, measure: Callable[[str], int], width: int, max_lines: int) -> list[str]:
    """Subtitle-style wrap: keep the newest words, marking dropped text with a leading ellipsis."""
    lines = [ellipsize(line, measure, width) for line in wrap_words(text, measure, width)]
    if len(lines) <= max_lines:
        return lines
    lines = lines[-max_lines:]
    words = lines[0].split()
    while len(words) > 1 and measure("… " + " ".join(words)) > width:
        words.pop(0)
    lines[0] = "… " + " ".join(words)
    return lines


def format_ms(ms: float | None) -> str:
    if ms is None:
        return "—"
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{int(ms)} ms"


STATUS_BY_OUTCOME = {"executed": "done", "execution_error": "error", "error": "error", "discarded": "discarded",
                     "planned": "review", "planning": "working", "done": "done", "cancelled": "discarded",
                     "review_required": "review", "blocked": "error", "stuck": "error", "step_limit": "error"}


def entry_from_record(record: dict[str, Any], when: str | None = None) -> dict[str, Any]:
    """Activity-list entry from a log record (live or loaded from the JSONL log)."""
    verb = record.get("verb") or {}
    target = record.get("target") or {}
    if when is None:
        try:
            when = datetime.fromisoformat(record["timestamp"]).astimezone().strftime("%H:%M:%S")
        except (KeyError, ValueError):
            when = datetime.now().strftime("%H:%M:%S")
    return {
        "id": record.get("utterance_id", ""),
        "time": when,
        "transcript": record.get("transcript", ""),
        "window": ((record.get("state_summary") or {}).get("activeWindow") or {}).get("title", ""),
        "verb": verb.get("choice"),
        "verb_p": (verb.get("probabilities") or {}).get(verb.get("choice")),
        "target": target,
        "target_p": target.get("probability"),
        "status": STATUS_BY_OUTCOME.get(record.get("outcome"), "working"),
        "outcome": record.get("outcome"),
        "result": record.get("result"),
        "error": record.get("error"),
        "timings": dict(record.get("timings_ms") or {}),
        "source": record.get("source"),
        "mode": record.get("mode"),
        "actions": list(record.get("actions") or []),
        "next_step": next((s.get("summary") or action_phrase((s.get("verb") or {}).get("choice"), s.get("target"))
                           for s in reversed(record.get("steps") or []) if s.get("reason")), None),
    }


def entry_title(entry: dict[str, Any]) -> str:
    status = entry["status"]
    if entry.get("mode") == "goal":
        if status == "done":
            return f"Goal reached in {len(entry.get('actions') or [])} steps"
        if status == "discarded":
            return "Goal stopped"
        if status == "review":
            return "Goal needs review"
        if status == "error":
            return "Goal stopped before completion"
        return "Working on goal…"
    if status == "done":
        return action_phrase(entry["verb"], entry["target"], past=True)
    if status == "error":
        return friendly_error(entry["error"])
    if status in {"review", "discarded"}:
        return action_phrase(entry["verb"], entry["target"])
    return "Working…"


FILLER = re.compile(r"^(?:(?:okay|ok|uh+|um+|so|now|hey|alright|all right|well|and)\b[\s,.!]*)+", re.I)


def spoken_request(transcript: str) -> str:
    """What was asked, without the leading "okay, uh, now" filler."""
    text = FILLER.sub("", transcript.strip()).strip() or transcript.strip()
    return text[:1].upper() + text[1:]


def entry_headline(entry: dict[str, Any]) -> str:
    """The one big line of an activity card: what happened for single actions, what was asked for goals."""
    status, transcript = entry["status"], entry.get("transcript") or ""
    if entry.get("mode") == "goal" or (status in {"error", "working"} and transcript):
        return spoken_request(transcript) if transcript else entry_title(entry)
    return entry_title(entry)


GOAL_ENDINGS = {"blocked": "Blocked", "stuck": "Got stuck", "step_limit": "Too many steps", "cancelled": "Stopped",
                "discarded": "Skipped", "review_required": "Waiting for you"}


def entry_reason(entry: dict[str, Any]) -> tuple[str, str] | None:
    """A short (text, color-key) note under the headline when something didn't simply work, else None."""
    status = entry["status"]
    if status == "error":
        if entry.get("error") or entry.get("mode") != "goal":
            return friendly_error(entry.get("error")), "warn"
        return GOAL_ENDINGS.get(entry.get("outcome"), "Stopped"), "warn"
    if status == "review":
        return (f"Next: {entry['next_step']}" if entry.get("next_step") else "Waiting for you"), "accent"
    if status == "discarded":
        return GOAL_ENDINGS.get(entry.get("outcome"), "Skipped"), "faint"
    return None


def entry_glyph(entry: dict[str, Any]) -> str:
    status = entry["status"]
    if status == "error":
        return "warn"
    if status == "review":
        return "help"
    if status == "discarded":
        return "block"
    if status == "working":
        return "more"
    if entry.get("mode") == "goal":
        return "check"
    return VERB_GLYPH.get(entry.get("verb") or "", "check")


# ---------------------------------------------------------------- win32 helpers

def set_ex_style(hwnd: int, add: int = 0, remove: int = 0) -> None:
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, (style | add) & ~remove)
    win32gui.SetWindowPos(hwnd, 0, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOZORDER
                          | win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED)


def round_corners(hwnd: int) -> None:
    """Windows 11 rounded corners and a hairline border; silently a square window elsewhere."""
    try:
        dwm = ctypes.windll.dwmapi
        preference = ctypes.c_int(2)  # DWMWCP_ROUND
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(preference), 4)
        r, g, b = (int(C["border"][i:i + 2], 16) for i in (1, 3, 5))
        border = ctypes.c_uint(r | g << 8 | b << 16)
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), 4)
    except (AttributeError, OSError):
        pass


def show_no_activate(hwnd: int, visible: bool) -> None:
    if visible:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
    else:
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)


def work_area(point: tuple[int, int]) -> tuple[int, int, int, int]:
    monitor = win32api.MonitorFromPoint((int(point[0]), int(point[1])), win32con.MONITOR_DEFAULTTONEAREST)
    return win32api.GetMonitorInfo(monitor)["Work"]


def on_a_monitor(point: tuple[int, int]) -> bool:
    return bool(win32api.MonitorFromPoint((int(point[0]), int(point[1])), win32con.MONITOR_DEFAULTTONULL))


def round_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, r: float, **kw: Any) -> int:
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
              x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(points, smooth=True, **kw)


class Theme:
    def __init__(self, root: tk.Misc):
        self.s = root.winfo_fpixels("1i") / 96
        families = set(tkfont.families(root))

        def first(*names: str) -> str | None:
            return next((n for n in names if n in families), None)

        text = first("Segoe UI Variable Text", "Segoe UI") or "TkDefaultFont"
        display = first("Segoe UI Variable Display", "Segoe UI") or text
        self.icon_family = first("Segoe Fluent Icons", "Segoe MDL2 Assets")
        icon = self.icon_family or text
        self.fonts = {
            "cap": tkfont.Font(root, family=display, size=12),
            "capb": tkfont.Font(root, family=display, size=12, weight="bold"),
            "body": tkfont.Font(root, family=text, size=10),
            "bodyb": tkfont.Font(root, family=text, size=10, weight="bold"),
            "small": tkfont.Font(root, family=text, size=9),
            "tiny": tkfont.Font(root, family=text, size=8),
            "title": tkfont.Font(root, family=display, size=13, weight="bold"),
            "icon": tkfont.Font(root, family=icon, size=9),
            "iconl": tkfont.Font(root, family=icon, size=11),
            "iconxl": tkfont.Font(root, family=icon, size=15),
            "iconxxl": tkfont.Font(root, family=icon, size=26),
            "head": tkfont.Font(root, family=display, size=12, weight="bold"),
        }

    def px(self, value: float) -> int:
        return int(round(value * self.s))

    def glyph(self, name: str) -> str:
        if not self.icon_family:
            return FALLBACK[name]
        return MDL2_GLYPHS.get(name, GLYPHS[name]) if self.icon_family == "Segoe MDL2 Assets" else GLYPHS[name]


# ---------------------------------------------------------------- caption pill

class Pill:
    """Bottom-center status pill: idle dot, live captions, progress, review buttons, result."""

    RESULT_MS = {"done": 2600, "error": 4500, "hint": 1800}

    def __init__(self, root: tk.Tk, theme: Theme, *, on_panel: Callable[[], None], on_menu: Callable[[int, int], None],
                 on_run: Callable[[], None], on_cancel: Callable[[], None], on_moved: Callable[[tuple[int, int]], None],
                 get_level: Callable[[], float], anchor: tuple[int, int] | None = None):
        self.root, self.t = root, theme
        self.on_panel, self.on_menu, self.on_run, self.on_cancel, self.on_moved = on_panel, on_menu, on_run, on_cancel, on_moved
        self.get_level = get_level
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=C["bg"])
        self.canvas = tk.Canvas(root, bg=C["bg"], highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.state, self.data = "idle", {}
        self.model, self.panel_open, self.hover, self.hide_when_idle = "loading", False, False, False
        self.hwnd, self.visible, self.level, self.listen_w = 0, True, 0.0, 0
        self.app_name, self.app_icon = "", None
        self.hits: dict[str, tuple[float, float, float, float]] = {}
        self._tick_job = self._collapse_job = None
        self._press: tuple[int, int, tuple[int, int]] | None = None
        self._dragging = False
        if anchor is None or not on_a_monitor(anchor):
            left, _, right, bottom = work_area((0, 0))
            anchor = ((left + right) // 2, bottom - theme.px(18))
        self.anchor = anchor
        for sequence, handler in (("<Enter>", self._enter), ("<Leave>", self._leave), ("<ButtonPress-1>", self._down),
                                  ("<B1-Motion>", self._drag), ("<ButtonRelease-1>", self._up),
                                  ("<Button-3>", lambda e: self.on_menu(e.x_root, e.y_root))):
            self.canvas.bind(sequence, handler)
        self._layout()
        self.w, self.h = self.tw, self.th
        self._place()
        self._draw()

    # -- public API
    def attach(self) -> None:
        """Call once the window is mapped: no focus stealing, no taskbar button, rounded corners."""
        self.hwnd = int(self.root.wm_frame(), 16)
        set_ex_style(self.hwnd, add=win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW, remove=win32con.WS_EX_APPWINDOW)
        round_corners(self.hwnd)
        self._refresh_visibility()

    def show(self, state: str, **data: Any) -> None:
        if state == "listening" and self.state != "listening":
            self.listen_w = 0
        self.state, self.data = state, data
        if self._collapse_job:
            self.root.after_cancel(self._collapse_job)
            self._collapse_job = None
        if state in self.RESULT_MS:
            self._collapse_job = self.root.after(self.RESULT_MS[state], self._collapse)
        self._relayout()

    def update(self, **data: Any) -> None:
        self.data.update(data)
        self._relayout()

    def set_app(self, name: str, image: Any) -> None:
        """The app in front, shown beside the idle dot: its icon (a PIL image), else its initial."""
        from PIL import ImageTk

        self.app_name = name
        self.app_icon = ImageTk.PhotoImage(image, master=self.root) if image is not None else None
        self._relayout()

    def set_model(self, model: str) -> None:
        self.model = model
        self._relayout()

    def set_panel_open(self, is_open: bool) -> None:
        self.panel_open = is_open
        self._relayout()

    def set_hide_when_idle(self, hide: bool) -> None:
        self.hide_when_idle = hide
        self._relayout()

    def set_click_through(self, enabled: bool) -> None:
        """While an action runs, clicks aimed at a control under the pill must reach that control."""
        if not self.hwnd:
            return
        if enabled:
            set_ex_style(self.hwnd, add=win32con.WS_EX_LAYERED)
            win32gui.SetLayeredWindowAttributes(self.hwnd, 0, 255, win32con.LWA_ALPHA)
            set_ex_style(self.hwnd, add=win32con.WS_EX_TRANSPARENT)
        else:
            set_ex_style(self.hwnd, remove=win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED)

    def top_edge(self) -> tuple[int, int]:
        return self.anchor[0], int(self.anchor[1] - self.h)

    # -- layout and drawing
    def _collapse(self) -> None:
        self._collapse_job = None
        if self.hover:
            self._collapse_job = self.root.after(700, self._collapse)
        else:
            self.show("idle")

    def _relayout(self) -> None:
        self._layout()
        self._refresh_visibility()
        if self._tick_job is None:
            self._tick()

    def _refresh_visibility(self) -> None:
        visible = not (self.hide_when_idle and self.state == "idle" and not self.panel_open and self.model == "ready")
        if self.hwnd and visible != self.visible:
            show_no_activate(self.hwnd, visible)
        self.visible = visible

    def _rows(self, maxw: int) -> tuple[str, list[tuple[str, str, str]], str]:
        f, st, d = self.t.fonts, self.state, self.data
        cap, small = f["cap"].measure, f["small"].measure
        if st == "idle":
            if self.model == "loading":
                return "dot", [("Loading speech model…", "small", C["muted"])], "chevron"
            if self.model == "error":
                return "dot", [("Speech model failed, open activity", "small", C["warn"])], "chevron"
            hint = f"Hold Right Ctrl to control {self.app_name}" if self.app_name else "Hold Right Ctrl and speak"
            return "dot", ([(ellipsize(hint, small, maxw), "small", C["text"])] if self.hover else []), "chevron"
        if st == "listening":
            caption = d.get("caption", "")
            rows = [(line, "cap", C["text"]) for line in fit_tail(caption, cap, maxw, 2)] if caption else \
                [("Listening…", "cap", C["muted"])]
            return "bars", rows, "chevron"
        if st == "working":
            transcript = d.get("transcript", "")
            rows = [(line, "cap", C["text"]) for line in fit_tail(transcript, cap, maxw, 2)] if transcript else []
            return "spinner", rows + [(ellipsize(d.get("step", ""), small, maxw), "small", C["muted"])], "stop" if d.get("can_stop") else "chevron"
        if st == "review":
            return "review", [(ellipsize(d.get("title", ""), f["capb"].measure, maxw), "capb", C["text"]),
                              (ellipsize(f"“{d.get('detail', '')}”", small, maxw), "small", C["muted"])], "buttons"
        rows = [(ellipsize(d.get("title", ""), f["capb"].measure if st != "hint" else f["body"].measure, maxw),
                 "capb" if st != "hint" else "body", C["text"])]
        if d.get("detail"):
            rows.append((ellipsize(d["detail"], small, maxw), "small", C["muted"]))
        return {"done": "ok", "error": "error"}.get(st, "info"), rows, "chevron"

    def _layout(self) -> None:
        px, f = self.t.px, self.t.fonts
        maxw = min(px(520), int(self.root.winfo_screenwidth() * 0.5))
        icon, rows, right = self._rows(maxw)
        icon_w = {"dot": px(8) + (px(26) if self.app_name else 0), "bars": px(27), "spinner": px(24)}.get(icon, px(24))
        text_w = max((f[font].measure(text) for text, font, _ in rows), default=0)
        run_w = f["bodyb"].measure("Run") + px(26)
        right_w = px(30) if right == "chevron" else (px(86) if right == "stop" else run_w + px(40) + px(30))
        pad = px(13)
        tw = pad + icon_w + (px(10) + text_w + px(6) if rows else px(4)) + right_w
        spacing = px(1)
        content_h = sum(f[font].metrics("linespace") for _, font, _ in rows) + spacing * max(0, len(rows) - 1)
        th = max(px(34) if self.state == "idle" else px(48), content_h + px(20))
        if self.state == "listening":
            tw = self.listen_w = max(tw, px(280), self.listen_w)
        self.L = {"icon": icon, "rows": rows, "right": right, "pad": pad, "icon_w": icon_w,
                  "content_h": content_h, "spacing": spacing, "run_w": run_w}
        self.tw, self.th = tw, th

    def _place(self) -> None:
        w, h = int(round(self.w)), int(round(self.h))
        self.root.geometry(f"{w}x{h}+{int(self.anchor[0] - w / 2)}+{int(self.anchor[1] - h)}")

    def _tick(self) -> None:
        self._tick_job = None
        for attr, target in (("w", self.tw), ("h", self.th)):
            value = getattr(self, attr)
            value += (target - value) * 0.38
            setattr(self, attr, target if abs(target - value) < 0.6 else value)
        level = self.get_level() if self.state == "listening" else 0.0
        self.level += (level - self.level) * (0.5 if level > self.level else 0.2)
        self._place()
        self._draw()
        moving = self.w != self.tw or self.h != self.th
        live = self.state in {"listening", "working"} or (self.state == "idle" and self.model == "loading")
        if moving or live:
            self._tick_job = self.root.after(33, self._tick)

    def _draw(self) -> None:
        c, px, f, L = self.canvas, self.t.px, self.t.fonts, self.L
        c.delete("all")
        w, h = self.w, self.h
        now = time.monotonic()
        cy = h / 2
        x = L["pad"]
        icon = L["icon"]
        if icon == "dot":
            color = {"ready": C["ok"], "error": C["warn"]}.get(self.model, C["faint"])
            if self.model == "loading":
                color = C["muted"] if math.sin(now * 5) > 0 else C["faint"]
            r = px(4)
            c.create_oval(x, cy - r, x + 2 * r, cy + r, fill=color, outline="")
            if self.app_icon is not None:
                c.create_image(x + px(16), cy, image=self.app_icon, anchor="w")
            elif self.app_name:
                ax = x + px(16)
                round_rect(c, ax, cy - px(9), ax + px(18), cy + px(9), px(5), fill=C["chip"], outline="")
                c.create_text(ax + px(9), cy, text=self.app_name[0].upper(), font=f["tiny"], fill=C["text"])
        elif icon == "bars":
            bar, gap, tallest = px(3), px(3), px(22)
            for i in range(5):
                wobble = 0.55 + 0.45 * math.sin(now * 9 + i * 1.7)
                height = max(px(3), tallest * (0.12 + 0.88 * self.level * wobble))
                bx = x + i * (bar + gap) + bar / 2
                c.create_line(bx, cy - height / 2, bx, cy + height / 2, width=bar, fill=C["listen"], capstyle="round")
        elif icon == "spinner":
            for i in range(3):
                lift = px(4) * max(0.0, math.sin(now * 7 - i * 0.9))
                dx = x + px(3) + i * px(9)
                c.create_oval(dx - px(3), cy - lift - px(3), dx + px(3), cy - lift + px(3), fill=C["work"], outline="")
        else:
            fill, name = {"ok": (C["ok"], "check"), "error": (C["warn"], "warn"),
                          "review": (C["accent"], "help"), "info": (C["chip"], "info")}[icon]
            r = px(12)
            c.create_oval(x, cy - r, x + 2 * r, cy + r, fill=fill, outline="")
            c.create_text(x + r, cy, text=self.t.glyph(name), font=f["icon"], fill="#111116" if icon != "info" else C["text"])
        tx = x + L["icon_w"] + px(10)
        y = (h - L["content_h"]) / 2
        for text, font, color in L["rows"]:
            c.create_text(tx, y, text=text, anchor="nw", font=f[font], fill=color)
            y += f[font].metrics("linespace") + L["spacing"]
        self.hits = {"chevron": (w - px(32), 0, w, h)}
        glyph = self.t.glyph("down" if self.panel_open else "up")
        c.create_text(w - px(16), cy, text=glyph, font=f["icon"], fill=C["muted"])
        if L["right"] == "buttons":
            x2 = w - px(34)
            x1 = x2 - px(28)
            round_rect(c, x1, cy - px(14), x2, cy + px(14), px(14), fill=C["chip"], outline="")
            c.create_text((x1 + x2) / 2, cy, text=self.t.glyph("close"), font=f["icon"], fill=C["text"])
            self.hits["cancel"] = (x1, 0, x2, h)
            x2, x1 = x1 - px(6), x1 - px(6) - L["run_w"]
            round_rect(c, x1, cy - px(14), x2, cy + px(14), px(14), fill=C["accent"], outline="")
            c.create_text((x1 + x2) / 2, cy, text="Run", font=f["bodyb"], fill="#111116")
            self.hits["run"] = (x1, 0, x2, h)
        elif L["right"] == "stop":
            x2 = w - px(34)
            x1 = x2 - px(52)
            round_rect(c, x1, cy - px(14), x2, cy + px(14), px(14), fill=C["chip"], outline="")
            c.create_text((x1 + x2) / 2, cy, text="Stop", font=f["small"], fill=C["text"])
            self.hits["cancel"] = (x1, 0, x2, h)

    # -- mouse
    def _enter(self, _: tk.Event) -> None:
        self.hover = True
        if self.state == "idle":
            self._relayout()

    def _leave(self, _: tk.Event) -> None:
        self.hover = False
        if self.state == "idle":
            self._relayout()

    def _down(self, event: tk.Event) -> None:
        self._press = (event.x_root, event.y_root, self.anchor)
        self._dragging = False

    def _drag(self, event: tk.Event) -> None:
        if not self._press:
            return
        x0, y0, (ax, ay) = self._press
        if not self._dragging and math.hypot(event.x_root - x0, event.y_root - y0) > self.t.px(4):
            self._dragging = True
        if self._dragging:
            self.anchor = (ax + event.x_root - x0, ay + event.y_root - y0)
            self._place()

    def _up(self, event: tk.Event) -> None:
        self._press = None
        if self._dragging:
            self._dragging = False
            self.on_moved(self.anchor)
            return
        hit = next((name for name, (x1, y1, x2, y2) in self.hits.items() if x1 <= event.x <= x2 and y1 <= event.y <= y2), None)
        if hit == "run":
            self.on_run()
        elif hit == "cancel":
            self.on_cancel()
        elif hit == "chevron" or self.state in {"idle", "done", "error", "hint"}:
            self.on_panel()


# ---------------------------------------------------------------- activity panel

class Tooltip:
    """One shared hover tip for the panel. Shown without activation, so the panel keeps the keyboard."""

    DELAY_MS = 450

    def __init__(self, root: tk.Misc, theme: Theme):
        self.t = theme
        self.win = tk.Toplevel(root, bg=C["border"])
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry("+-20000+-20000")
        self.label = tk.Label(self.win, text="", font=theme.fonts["small"], fg=C["text"], bg=C["bg2"], justify="left",
                              padx=theme.px(9), pady=theme.px(5), wraplength=theme.px(260))
        self.label.pack(padx=1, pady=1)
        self.hwnd = 0
        self._show_job = self._hide_job = None

    def attach(self) -> None:
        self.hwnd = int(self.win.wm_frame(), 16)
        set_ex_style(self.hwnd, add=win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW, remove=win32con.WS_EX_APPWINDOW)
        show_no_activate(self.hwnd, False)

    def bind(self, widgets: list[tk.Misc], text: str | Callable[[], str]) -> None:
        """Show `text` under the first widget while the pointer is over any of them."""
        for widget in widgets:
            widget.bind("<Enter>", lambda _, anchor=widgets[0]: self._schedule(anchor, text), add="+")
            widget.bind("<Leave>", lambda _: self._leave(), add="+")
            widget.bind("<ButtonPress>", lambda _: self.hide(), add="+")

    def _cancel(self) -> None:
        for job in (self._show_job, self._hide_job):
            if job:
                self.win.after_cancel(job)
        self._show_job = self._hide_job = None

    def _schedule(self, widget: tk.Misc, text: str | Callable[[], str]) -> None:
        self._cancel()
        self._show_job = self.win.after(self.DELAY_MS, lambda: self._show(widget, text() if callable(text) else text))

    def _leave(self) -> None:
        self._cancel()
        self._hide_job = self.win.after(60, self.hide)

    def _show(self, widget: tk.Misc, text: str) -> None:
        self._show_job = None
        if not self.hwnd or not text or not widget.winfo_viewable():
            return
        px = self.t.px
        self.label.configure(text=text)
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        x, y = widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height() + px(6)
        left, _, right, bottom = work_area((x, y))
        x = min(max(x, left + px(4)), right - w - px(4))
        if y + h > bottom:
            y = widget.winfo_rooty() - h - px(6)
        show_no_activate(self.hwnd, True)
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST, x, y, w, h, win32con.SWP_NOACTIVATE)

    def hide(self) -> None:
        self._hide_job = None
        if self.hwnd:
            show_no_activate(self.hwnd, False)


class Tile(tk.Canvas):
    """A setting as a big icon tile: tinted when on, with a switch dot in the corner."""

    def __init__(self, parent: tk.Misc, theme: Theme, variable: tk.BooleanVar, glyph: str, text: str, width: int):
        self.t, self.variable, self.glyph, self.text, self.w = theme, variable, glyph, text, width
        super().__init__(parent, width=width, height=theme.px(66), bg=C["bg"], highlightthickness=0, cursor="hand2")
        self.bind("<Button-1>", lambda _: variable.set(not variable.get()))
        variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _draw(self) -> None:
        px, f, on = self.t.px, self.t.fonts, self.variable.get()
        self.delete("all")
        round_rect(self, 1, 1, self.w - 1, px(66) - 1, px(12), fill=mix(C["accent"], C["bg"], 0.2) if on else C["card"],
                   outline=C["accent"] if on else C["border"])
        self.create_text(self.w / 2, px(26), text=self.t.glyph(self.glyph), font=f["iconxl"],
                         fill=C["accent"] if on else C["faint"])
        self.create_text(self.w / 2, px(50), text=self.text, font=f["small"], fill=C["text"] if on else C["muted"])
        x, y, r = self.w - px(13), px(13), px(4)
        self.create_oval(x - r, y - r, x + r, y + r, fill=C["accent"] if on else "", outline=C["accent"] if on else C["faint"])


class ActivityPanel:
    """Settings tiles, a typed-command field, and recent commands as icon cards; clicking a card shows its details."""

    WIDTH, HEIGHT, KEEP = 440, 600, 80
    HOW_TO = ("Hold Right Ctrl, speak, and release. Pressing another key while holding cancels, "
              "so Right Ctrl shortcuts keep working.")

    def __init__(self, root: tk.Tk, theme: Theme, *, auto_var: tk.BooleanVar, goal_var: tk.BooleanVar, hide_var: tk.BooleanVar,
                 on_command: Callable[[str], None], on_open_logs: Callable[[], None], on_api_key: Callable[[], None],
                 on_quit: Callable[[], None], on_close: Callable[[], None], logo: Any = None):
        self.t = theme
        px, f = theme.px, theme.fonts
        self.on_command, self.on_close = on_command, on_close
        self.width, self.height = px(self.WIDTH), px(self.HEIGHT)
        self.win = tk.Toplevel(root, bg=C["bg"])
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{self.width}x{self.height}+-20000+-20000")
        self.hwnd, self.is_open = 0, False
        self.cards: dict[str, tuple[tk.Frame, dict[str, Any]]] = {}
        self.order: list[str] = []
        self.expanded: set[str] = set()
        self.app_images: dict[str, Any] = {}
        self.status_text = ""
        self.inner_w = self.width - px(32)
        self.tip = Tooltip(root, theme)

        outer = tk.Frame(self.win, bg=C["bg"], padx=px(16), pady=px(14))
        outer.pack(fill="both", expand=True)

        head = tk.Frame(outer, bg=C["bg"])
        head.pack(fill="x")
        self.logo = None
        if logo is not None:
            from PIL import ImageTk
            self.logo = ImageTk.PhotoImage(logo.resize((px(26), px(26))), master=self.win)
            tk.Label(head, image=self.logo, bg=C["bg"]).pack(side="left", padx=(0, px(8)))
        tk.Label(head, text="Jev Voice", font=f["title"], fg=C["text"], bg=C["bg"]).pack(side="left")
        close = tk.Label(head, text=theme.glyph("close"), font=f["iconl"], fg=C["muted"], bg=C["bg"], cursor="hand2", padx=px(4))
        close.pack(side="right")
        close.bind("<Button-1>", lambda _: on_close())
        self.chip = tk.Canvas(head, height=px(24), width=px(80), bg=C["bg"], highlightthickness=0)
        self.chip.pack(side="right", padx=(0, px(8)))
        self.tip.bind([self.chip], lambda: self.status_text)

        self.howto = tk.Canvas(outer, height=px(40), width=self.inner_w, bg=C["bg"], highlightthickness=0)
        self.howto.pack(fill="x", pady=px(12))
        self._draw_howto()
        self.tip.bind([self.howto], self.HOW_TO)

        tiles = tk.Frame(outer, bg=C["bg"])
        tiles.pack(fill="x")
        tile_w = (self.inner_w - 2 * px(8)) // 3
        for i, (var, glyph, text, tip) in enumerate((
                (auto_var, "bolt", "Auto-run", "Run actions right away. When off, Jev shows the action and waits for Run."),
                (goal_var, "target", "Multi-step", "Let Jev take several steps to reach a goal, like “unmute the video”."),
                (hide_var, "hide", "Hide pill", "Hide the pill at the bottom of the screen while nothing is happening."))):
            tile = Tile(tiles, theme, var, glyph, text, tile_w)
            tile.pack(side="left", padx=(0 if i == 0 else px(8), 0))
            self.tip.bind([tile], tip)

        box = tk.Frame(outer, bg=C["bg2"], highlightthickness=1, highlightbackground=C["border"], highlightcolor=C["accent"])
        box.pack(fill="x", pady=(px(12), 0))
        tk.Label(box, text=theme.glyph("keyboard"), font=f["iconl"], fg=C["faint"], bg=C["bg2"]).pack(side="left", padx=(px(12), 0))
        self.placeholder = "Type a command"
        self.entry = tk.Entry(box, bg=C["bg2"], fg=C["faint"], insertbackground=C["text"], relief="flat", bd=0, font=f["cap"])
        self.entry.insert(0, self.placeholder)
        self.entry.pack(side="left", fill="x", expand=True, padx=px(8), pady=px(9))
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        self.entry.bind("<Return>", lambda _: self._submit())
        self.entry.bind("<Button-1>", lambda _: (self.win.focus_force(), self.entry.focus_set()))
        send = tk.Canvas(box, width=px(34), height=px(34), bg=C["bg2"], highlightthickness=0, cursor="hand2")
        round_rect(send, 0, 0, px(34), px(34), px(10), fill=C["accent"], outline="")
        send.create_text(px(17), px(17), text=theme.glyph("send"), font=f["iconl"], fill="#111116")
        send.pack(side="right", padx=px(5), pady=px(5))
        send.bind("<Button-1>", lambda _: self._submit())
        self.tip.bind([send], "Run the typed command, as if you had said it")

        tk.Label(outer, text="Recent", font=f["bodyb"], fg=C["muted"], bg=C["bg"]).pack(anchor="w", pady=(px(16), px(8)))

        foot = tk.Frame(outer, bg=C["bg"])
        foot.pack(side="bottom", fill="x", pady=(px(10), 0))
        for glyph, text, command, side, tip in (
                ("folder", "Logs", on_open_logs, "left", "Open the folder with Jev Voice's logs"),
                ("key", "API key", on_api_key, "left", "Change the OpenRouter or TypeSafe AI key"),
                ("power", "Quit", on_quit, "right", "Quit Jev Voice")):
            button = tk.Frame(foot, bg=C["bg"], cursor="hand2")
            button.pack(side=side, padx=(0, px(18)) if side == "left" else 0)
            icon = tk.Label(button, text=theme.glyph(glyph), font=f["iconl"], fg=C["muted"], bg=C["bg"])
            icon.pack(side="left")
            word = tk.Label(button, text=text, font=f["small"], fg=C["muted"], bg=C["bg"])
            word.pack(side="left", padx=(px(6), 0))
            parts = [button, icon, word]
            for widget in parts:
                widget.bind("<Button-1>", lambda _, cmd=command: cmd())
                widget.bind("<Enter>", lambda _, ws=(icon, word): [w.configure(fg=C["text"]) for w in ws], add="+")
                widget.bind("<Leave>", lambda _, ws=(icon, word): [w.configure(fg=C["muted"]) for w in ws], add="+")
            self.tip.bind(parts, tip)

        self.list = tk.Canvas(outer, bg=C["bg"], highlightthickness=0, bd=0)
        self.list.pack(fill="both", expand=True)
        self.inner = tk.Frame(self.list, bg=C["bg"])
        self.list.create_window(0, 0, window=self.inner, anchor="nw", width=self.inner_w)
        self.inner.bind("<Configure>", lambda _: self.list.configure(scrollregion=self.list.bbox("all")))
        self.empty = tk.Frame(self.inner, bg=C["bg"])
        tk.Label(self.empty, text=theme.glyph("mic"), font=f["iconxxl"], fg=C["faint"], bg=C["bg"]).pack(pady=(px(24), px(8)))
        tk.Label(self.empty, text="Hold Right Ctrl and speak", font=f["cap"], fg=C["text"], bg=C["bg"]).pack()
        tk.Label(self.empty, text="Try “scroll down”", font=f["small"], fg=C["faint"], bg=C["bg"]).pack(pady=(px(2), 0))
        self.empty.pack(fill="x")
        self.win.bind("<MouseWheel>", lambda e: self.list.yview_scroll(int(-e.delta / 120) * 2, "units"))
        self.win.bind("<Escape>", lambda _: on_close())
        self.set_status("")

    def attach(self) -> None:
        self.hwnd = int(self.win.wm_frame(), 16)
        set_ex_style(self.hwnd, add=win32con.WS_EX_TOOLWINDOW, remove=win32con.WS_EX_APPWINDOW)
        round_corners(self.hwnd)
        show_no_activate(self.hwnd, False)
        self.tip.attach()

    def open(self, pill_top: tuple[int, int]) -> None:
        px = self.t.px
        left, top, right, _ = work_area(pill_top)
        x = min(max(pill_top[0] - self.width // 2, left + px(8)), right - self.width - px(8))
        y = max(pill_top[1] - self.height - px(10), top + px(8))
        # Show first: Tk re-applies its remembered position when a hidden window is shown again.
        show_no_activate(self.hwnd, True)
        self.win.geometry(f"{self.width}x{self.height}+{x}+{y}")
        win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST, x, y, self.width, self.height, win32con.SWP_NOACTIVATE)
        self.is_open = True

    def close(self) -> None:
        show_no_activate(self.hwnd, False)
        self.tip.hide()
        self.is_open = False

    def set_status(self, text: str) -> None:
        """Speech-model status as a chip (GPU, CPU, Loading or Error); the full text is its tooltip."""
        self.status_text = text
        px, f, c = self.t.px, self.t.fonts, self.chip
        if not text or text.startswith("Loading"):
            word, color = "Loading", C["faint"]
        elif text.startswith("Speech model failed"):
            word, color = "Error", C["warn"]
        else:
            word, color = ("GPU" if " on CUDA" in text else "CPU"), C["ok"]
        width = f["small"].measure(word) + px(32)
        c.configure(width=width)
        c.delete("all")
        round_rect(c, 1, 1, width - 1, px(24) - 1, px(11), fill=C["card"], outline=C["border"])
        c.create_oval(px(10), px(8), px(18), px(16), fill=color, outline="")
        c.create_text(px(24), px(12), text=word, anchor="w", font=f["small"], fill=C["muted"])

    def remember_app(self, name: str, image: Any) -> None:
        """Icon (a PIL image on the card color) shown on cards for commands given while `name` was in front."""
        if name and image is not None and name not in self.app_images:
            from PIL import ImageTk
            self.app_images[name] = ImageTk.PhotoImage(image, master=self.win)

    def _app_image(self, e: dict[str, Any]) -> Any:
        if e.get("app") in self.app_images:
            return self.app_images[e["app"]]
        window = (e.get("window") or "").lower()  # entries from the log: titles like "Video - YouTube - Google Chrome"
        return next((image for name, image in self.app_images.items() if window.endswith(name.lower())), None)

    def _draw_howto(self) -> None:
        """How to talk, as a picture: a Right Ctrl keycap, a plus, and a microphone."""
        px, f, c = self.t.px, self.t.fonts, self.howto
        w, h = self.inner_w, px(40)
        round_rect(c, 1, 1, w - 1, h - 1, px(12), fill=C["card"], outline=C["border"])
        key = "Right Ctrl"
        kw = f["bodyb"].measure(key) + px(22)
        x = px(8)
        round_rect(c, x, px(8), x + kw, h - px(5), px(6), fill=C["border"], outline="")
        round_rect(c, x, px(6), x + kw, h - px(8), px(6), fill=C["chip"], outline=C["faint"])
        c.create_text(x + kw / 2, (h - px(2)) / 2, text=key, font=f["bodyb"], fill=C["text"])
        x += kw + px(12)
        c.create_text(x, h / 2, text="+", font=f["cap"], fill=C["faint"], anchor="w")
        x += f["cap"].measure("+") + px(12)
        mic = self.t.glyph("mic")
        c.create_text(x, h / 2, text=mic, font=f["iconxl"], fill=C["listen"], anchor="w")
        x += f["iconxl"].measure(mic) + px(10)
        c.create_text(x, h / 2, text="Hold and speak", font=f["body"], fill=C["muted"], anchor="w")
        c.create_text(w - px(16), h / 2, text=self.t.glyph("info"), font=f["icon"], fill=C["faint"])

    # -- command field
    def _focus_in(self, _: tk.Event) -> None:
        if self.entry.get() == self.placeholder:
            self.entry.delete(0, "end")
            self.entry.configure(fg=C["text"])

    def _focus_out(self, _: tk.Event) -> None:
        if not self.entry.get().strip():
            self.entry.delete(0, "end")
            self.entry.insert(0, self.placeholder)
            self.entry.configure(fg=C["faint"])

    def _submit(self) -> None:
        text = self.entry.get().strip()
        if text and text != self.placeholder:
            self.on_command(text)

    # -- cards
    def upsert(self, entry: dict[str, Any]) -> None:
        self.empty.pack_forget()
        old = self.cards.get(entry["id"])
        card = self._card(entry)
        if old:
            card.pack(fill="x", pady=(0, self.t.px(8)), before=old[0])
            old[0].destroy()
        else:
            first = self.cards[self.order[0]][0] if self.order else None
            card.pack(fill="x", pady=(0, self.t.px(8)), **({"before": first} if first else {}))
            self.order.insert(0, entry["id"])
            self.list.yview_moveto(0)
        self.cards[entry["id"]] = (card, entry)
        while len(self.order) > self.KEEP:
            self.cards.pop(self.order.pop())[0].destroy()

    def _toggle(self, entry_id: str) -> None:
        self.expanded.symmetric_difference_update({entry_id})
        if entry_id in self.cards:
            self.upsert(self.cards[entry_id][1])

    def _card(self, e: dict[str, Any]) -> tk.Frame:
        """One big line and an outcome badge; the rest waits behind a click."""
        px, f = self.t.px, self.t.fonts
        status, is_open = e["status"], e["id"] in self.expanded
        color = {"done": C["ok"], "error": C["warn"], "review": C["accent"], "discarded": C["faint"]}.get(status, C["work"])
        card = tk.Frame(self.inner, bg=C["card"], highlightthickness=1, cursor="hand2",
                        highlightbackground=mix(color, C["card"], 0.5) if is_open else C["border"])
        widgets: list[tk.Misc] = [card]

        def label(parent: tk.Misc, text: str, font: str, fg: str, **kw: Any) -> tk.Label:
            widget = tk.Label(parent, text=text, font=f[font], fg=fg, bg=kw.pop("bg", C["card"]), justify="left", **kw)
            widgets.append(widget)
            return widget

        def frame(parent: tk.Misc, **kw: Any) -> tk.Frame:
            widget = tk.Frame(parent, bg=C["card"], **kw)
            widgets.append(widget)
            return widget

        row = frame(card, padx=px(10), pady=px(10))
        row.pack(fill="x")
        size = px(38)
        badge = tk.Canvas(row, width=size, height=size, bg=C["card"], highlightthickness=0)
        widgets.append(badge)
        badge.create_oval(0, 0, size, size, fill=mix(color, C["card"], 0.2), outline="")
        badge.create_text(size / 2, size / 2, text=self.t.glyph(entry_glyph(e)), font=f["iconxl"], fill=color)
        badge.pack(side="left", anchor="n")

        side = frame(row)
        side.pack(side="right", fill="y", padx=(px(8), 0))
        label(side, e["time"][:5], "tiny", C["faint"]).pack(anchor="e")
        label(side, self.t.glyph("up" if is_open else "down"), "icon", C["faint"]).pack(side="bottom", anchor="e")

        text = frame(row)
        text.pack(side="left", fill="x", expand=True, padx=(px(12), 0))
        width = self.inner_w - size - px(20 + 12 + 50)
        lines = wrap_words(entry_headline(e), f["head"].measure, width)
        if len(lines) > 2:
            lines = [lines[0], ellipsize(" ".join(lines[1:]), f["head"].measure, width)]
        label(text, "\n".join(lines), "head", C["text"], anchor="w").pack(fill="x")

        meta = frame(text)
        meta.pack(fill="x", pady=(px(4), 0))
        image = self._app_image(e)
        if image is not None:
            label(meta, "", "small", C["muted"], image=image).pack(side="left", padx=(0, px(8)))
        timings = e.get("timings") or {}
        parts = [(timings[key], name, tint) for key, name, tint in TIMING if isinstance(timings.get(key), (int, float))]
        reason = entry_reason(e)
        if reason:
            label(meta, ellipsize(reason[0], f["small"].measure, width - px(26)), "small", C[reason[1]]).pack(side="left")
        else:
            stats = [("clock", format_ms(sum(value for value, _, _ in parts)))] if parts else []
            if e.get("mode") == "goal":
                stats.append(("steps", str(len(e.get("actions") or []))))
            if e.get("source") == "typed_test":
                stats.append(("keyboard", "typed"))
            for glyph, value in stats:
                label(meta, self.t.glyph(glyph), "icon", C["faint"]).pack(side="left")
                label(meta, value, "small", C["muted"]).pack(side="left", padx=(px(4), px(12)))

        if is_open:
            self._details(card, e, parts, label, frame, widgets)
        for widget in widgets:
            widget.bind("<Button-1>", lambda _, entry_id=e["id"]: self._toggle(entry_id))
        return card

    def _details(self, card: tk.Frame, e: dict[str, Any], parts: list[tuple[float, str, str]],
                 label: Callable[..., tk.Label], frame: Callable[..., tk.Frame], widgets: list[tk.Misc]) -> None:
        """The expanded half of a card: what was said, where the time went, each step, and the raw error."""
        px, f = self.t.px, self.t.fonts
        wrap = self.inner_w - px(26)
        rule = tk.Frame(card, bg=C["border"], height=1)
        widgets.append(rule)
        rule.pack(fill="x", padx=px(10))
        body = frame(card, padx=px(12), pady=px(10))
        body.pack(fill="x")
        if e.get("transcript") and entry_headline(e) != spoken_request(e["transcript"]):
            label(body, f"“{e['transcript']}”", "body", C["text"], wraplength=wrap, anchor="w").pack(fill="x")
        if parts:
            total = sum(value for value, _, _ in parts) or 1
            bar = tk.Canvas(body, width=wrap, height=px(6), bg=C["card"], highlightthickness=0)
            widgets.append(bar)
            bar.pack(anchor="w", pady=(px(10), px(4)))
            x = 0.0
            for value, _, tint in parts:
                seg = max(px(2), (wrap - px(2) * (len(parts) - 1)) * value / total)
                round_rect(bar, x, 0, x + seg, px(6), px(3), fill=tint, outline="")
                x += seg + px(2)
            legend = frame(body)
            legend.pack(fill="x")
            for value, name, tint in parts:
                label(legend, "●", "tiny", tint).pack(side="left")
                label(legend, f"{name} {format_ms(value)}", "tiny", C["muted"]).pack(side="left", padx=(px(2), px(10)))
        lines = []
        for i, action in enumerate(e.get("actions") or [], 1):
            lines.append(f"{i}.  {action}")
        if e.get("next_step"):
            lines.append(f"Next:  {e['next_step']}")
        if (e.get("target") or {}).get("description"):
            lines.append(f"Target:  {e['target']['description']}")
        if isinstance(e.get("verb_p"), (int, float)):
            lines.append(f"Confidence:  {float(e['verb_p']):.0%}")
        if e.get("window"):
            lines.append(f"Window:  {e['window']}")
        if lines:
            label(body, "\n".join(lines), "small", C["muted"], wraplength=wrap, anchor="w").pack(fill="x", pady=(px(8), 0))
        if e.get("error"):
            label(body, e["error"], "tiny", C["warn"], wraplength=wrap, anchor="w").pack(fill="x", pady=(px(6), 0))
        label(body, f"id {e['id'][:12]}", "tiny", C["faint"], anchor="w").pack(fill="x", pady=(px(6), 0))


# ---------------------------------------------------------------- API key dialog

KEY_LINKS = (("Get an OpenRouter key", "https://openrouter.ai/keys"), ("Get a TypeSafe AI key", "https://console.typesafe.ai/"))


def key_provider(key: str) -> str:
    """Which service a key is for, as core.jev_route decides it."""
    key = key.strip()
    return "" if not key else "OpenRouter key" if key.startswith("sk-or-") else "TypeSafe AI key"


class KeyDialog:
    """A small window for pasting the OpenRouter or TypeSafe key. Unlike the pill it takes focus: it needs the keyboard."""

    def __init__(self, root: tk.Tk, theme: Theme, *, on_save: Callable[[str], str | None], first_run: bool,
                 found: list[dict[str, str]] = ()):
        """`found`: keys already on this PC (configure.find_keys), offered with where each came from."""
        import webbrowser
        self.t, self.on_save = theme, on_save
        self.found, self.choice = list(found), -1
        px, f = theme.px, theme.fonts
        self.win = tk.Toplevel(root, bg=C["bg"])
        self.win.title("Jev Voice: API key")
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        width = px(420)
        outer = tk.Frame(self.win, bg=C["bg"], padx=px(22), pady=px(18))
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text="Connect Jev Voice" if first_run else "Change API key", font=f["title"], fg=C["text"],
                 bg=C["bg"]).pack(anchor="w")
        intro = ("Jev needs an API key to understand what's on your screen. Paste an OpenRouter key (it starts with "
                 "sk-or-) or a TypeSafe AI key.")
        tk.Label(outer, text=intro, font=f["small"], fg=C["muted"], bg=C["bg"], wraplength=width - px(44),
                 justify="left").pack(anchor="w", pady=(px(6), px(14)))

        box = tk.Frame(outer, bg=C["bg2"], highlightthickness=1, highlightbackground=C["border"], highlightcolor=C["accent"])
        box.pack(fill="x")
        self.key = tk.StringVar()
        self.entry = tk.Entry(box, textvariable=self.key, show="•", bg=C["bg2"], fg=C["text"], insertbackground=C["text"],
                              relief="flat", bd=0, font=f["body"], width=40)
        self.entry.pack(side="left", fill="x", expand=True, padx=px(10), pady=px(8))
        self.reveal = tk.Label(box, text="Show", font=f["small"], fg=C["muted"], bg=C["bg2"], cursor="hand2", padx=px(8))
        self.reveal.pack(side="right")
        self.reveal.bind("<Button-1>", lambda _: self._toggle_reveal())
        self.note = tk.Label(outer, text="", font=f["tiny"], fg=C["faint"], bg=C["bg"], anchor="w", justify="left",
                             wraplength=width - px(44))
        self.note.pack(fill="x", pady=(px(4), 0))
        self.key.trace_add("write", lambda *_: self._describe())
        self.use_found = tk.Label(outer, text="", font=f["small"], fg=C["accent"], bg=C["bg"], cursor="hand2", anchor="w")
        if self.found:
            self.use_found.pack(fill="x", pady=(px(6), 0))
            self.use_found.bind("<Button-1>", lambda _: self._use((self.choice + 1) % len(self.found)))
            self._label_use_found()

        links = tk.Frame(outer, bg=C["bg"])
        links.pack(fill="x", pady=(px(10), 0))
        for i, (text, url) in enumerate(KEY_LINKS):
            link = tk.Label(links, text=text, font=f["small"], fg=C["accent"], bg=C["bg"], cursor="hand2")
            link.pack(side="left", padx=(0 if i == 0 else px(16), 0))
            link.bind("<Button-1>", lambda _, u=url: webbrowser.open(u))

        buttons = tk.Frame(outer, bg=C["bg"])
        buttons.pack(fill="x", pady=(px(18), 0))
        save = tk.Label(buttons, text="Save", font=f["bodyb"], fg="#111116", bg=C["accent"], padx=px(16), pady=px(5), cursor="hand2")
        save.pack(side="right")
        save.bind("<Button-1>", lambda _: self._save())
        cancel = tk.Label(buttons, text="Not now" if first_run else "Cancel", font=f["body"], fg=C["muted"], bg=C["bg"],
                          padx=px(12), pady=px(5), cursor="hand2")
        cancel.pack(side="right")
        cancel.bind("<Button-1>", lambda _: self.close())
        self.win.bind("<Return>", lambda _: self._save())
        self.win.bind("<Escape>", lambda _: self.close())

        self.win.update_idletasks()
        try:  # dark title bar (DWMWA_USE_IMMERSIVE_DARK_MODE) to match the window
            ctypes.windll.dwmapi.DwmSetWindowAttribute(int(self.win.wm_frame(), 16), 20, ctypes.byref(ctypes.c_int(1)), 4)
        except (AttributeError, OSError):
            pass
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        left, top, right, bottom = work_area(win32api.GetCursorPos())
        self.win.geometry(f"+{(left + right - w) // 2}+{(top + bottom - h) // 2}")
        if self.found and first_run:
            self._use(0)
        self.win.after(50, self.focus)

    def _use(self, index: int) -> None:
        self.choice = index
        self.key.set(self.found[index]["key"])
        self._label_use_found()

    def _label_use_found(self) -> None:
        n = len(self.found)
        self.use_found.configure(text="Use a key found on this PC" if self.choice < 0 else
                                 f"Use another key found on this PC ({self.choice + 1} of {n})" if n > 1 else "")

    def _describe(self) -> None:
        key = self.key.get().strip()
        text = key_provider(key)
        if 0 <= self.choice < len(self.found) and key == self.found[self.choice]["key"]:
            text += f", found in {self.found[self.choice]['source']}"
        self.note.configure(text=text, fg=C["faint"])

    def focus(self) -> None:
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self.entry.focus_set()

    def _toggle_reveal(self) -> None:
        hidden = self.entry.cget("show") != ""
        self.entry.configure(show="" if hidden else "•")
        self.reveal.configure(text="Hide" if hidden else "Show")

    def _save(self) -> None:
        key = self.key.get().strip()
        if not key:
            self.note.configure(text="Paste your key first.", fg=C["warn"])
            return
        if any(ch.isspace() for ch in key):
            self.note.configure(text="A key has no spaces; check what was pasted.", fg=C["warn"])
            return
        error = self.on_save(key)
        if error:
            self.note.configure(text=error, fg=C["warn"])
        else:
            self.close()

    def close(self) -> None:
        if self.win.winfo_exists():
            self.win.destroy()

    @property
    def is_open(self) -> bool:
        return bool(self.win.winfo_exists())
