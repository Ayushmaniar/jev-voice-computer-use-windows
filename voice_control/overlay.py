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
GLYPHS = {"up": "", "down": "", "check": "", "warn": "", "close": "",
          "info": "", "help": ""}
FALLBACK = {"up": "˄", "down": "˅", "check": "✓", "warn": "!", "close": "✕", "info": "i", "help": "?"}

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
    ("Say 'type <text>'", "Say “type” followed by the text"),
    ("did not become foreground", "The window lost focus, so nothing ran"),
    ("not in captured controls", "That item changed before it could be clicked"),
    ("OPENROUTER_API_KEY", "No OpenRouter or TypeSafe API key found"),
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
        if needle in error:
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
        }

    def px(self, value: float) -> int:
        return int(round(value * self.s))

    def glyph(self, name: str) -> str:
        return GLYPHS[name] if self.icon_family else FALLBACK[name]


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

class Toggle(tk.Frame):
    def __init__(self, parent: tk.Misc, theme: Theme, variable: tk.BooleanVar, text: str):
        super().__init__(parent, bg=C["bg"], cursor="hand2")
        px = theme.px
        self.variable, self.px = variable, px
        self.track = tk.Canvas(self, width=px(32), height=px(18), bg=C["bg"], highlightthickness=0)
        self.track.pack(side="left")
        label = tk.Label(self, text=text, font=theme.fonts["small"], fg=C["text"], bg=C["bg"])
        label.pack(side="left", padx=(px(7), 0))
        for widget in (self, self.track, label):
            widget.bind("<Button-1>", lambda _: variable.set(not variable.get()))
        variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _draw(self) -> None:
        px, c, on = self.px, self.track, self.variable.get()
        c.delete("all")
        round_rect(c, 0, 0, px(32), px(18), px(9), fill=C["accent"] if on else C["off"], outline="")
        knob = px(23) if on else px(9)
        c.create_oval(knob - px(6), px(3), knob + px(6), px(15), fill="#ffffff", outline="")


class ActivityPanel:
    """History of commands as cards, plus settings and a typed-command field."""

    WIDTH, HEIGHT, KEEP = 440, 580, 80

    def __init__(self, root: tk.Tk, theme: Theme, *, auto_var: tk.BooleanVar, goal_var: tk.BooleanVar, hide_var: tk.BooleanVar,
                 on_command: Callable[[str], None], on_open_logs: Callable[[], None], on_quit: Callable[[], None],
                 on_close: Callable[[], None]):
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
        self.inner_w = self.width - px(32)

        outer = tk.Frame(self.win, bg=C["bg"], padx=px(16), pady=px(14))
        outer.pack(fill="both", expand=True)
        head = tk.Frame(outer, bg=C["bg"])
        head.pack(fill="x")
        tk.Label(head, text="Jev Voice", font=f["title"], fg=C["text"], bg=C["bg"]).pack(side="left")
        self.status = tk.Label(head, text="", font=f["small"], fg=C["muted"], bg=C["bg"])
        self.status.pack(side="left", padx=(px(10), 0), pady=(px(3), 0))
        close = tk.Label(head, text=theme.glyph("close"), font=f["icon"], fg=C["muted"], bg=C["bg"], cursor="hand2", padx=px(4))
        close.pack(side="right")
        close.bind("<Button-1>", lambda _: on_close())
        tk.Label(outer, text="Hold Right Ctrl, speak, and release. Pressing another key while holding cancels, "
                             "so Right Ctrl shortcuts keep working.", font=f["small"], fg=C["faint"], bg=C["bg"],
                 wraplength=self.inner_w, justify="left").pack(anchor="w", pady=(px(4), px(12)))

        toggles = tk.Frame(outer, bg=C["bg"])
        toggles.pack(fill="x", pady=(0, px(12)))
        Toggle(toggles, theme, auto_var, "Run actions automatically").pack(side="left")
        Toggle(toggles, theme, goal_var, "Multi-step goals").pack(side="left", padx=(px(18), 0))
        Toggle(outer, theme, hide_var, "Hide pill when idle").pack(anchor="w", pady=(0, px(10)))

        box = tk.Frame(outer, bg=C["bg2"], highlightthickness=1, highlightbackground=C["border"], highlightcolor=C["accent"])
        box.pack(fill="x")
        self.placeholder = "Type a command to test, e.g. scroll down"
        self.entry = tk.Entry(box, bg=C["bg2"], fg=C["faint"], insertbackground=C["text"], relief="flat", bd=0, font=f["body"])
        self.entry.insert(0, self.placeholder)
        self.entry.pack(side="left", fill="x", expand=True, padx=px(10), pady=px(8))
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        self.entry.bind("<Return>", lambda _: self._submit())
        self.entry.bind("<Button-1>", lambda _: (self.win.focus_force(), self.entry.focus_set()))
        run = tk.Label(box, text="Run", font=f["bodyb"], fg="#111116", bg=C["accent"], padx=px(12), pady=px(3), cursor="hand2")
        run.pack(side="right", padx=px(5))
        run.bind("<Button-1>", lambda _: self._submit())

        heading = tk.Frame(outer, bg=C["bg"])
        heading.pack(fill="x", pady=(px(16), px(8)))
        tk.Label(heading, text="Activity", font=f["bodyb"], fg=C["text"], bg=C["bg"]).pack(side="left")
        legend = tk.Frame(heading, bg=C["bg"])
        legend.pack(side="right")
        for _, name, color in TIMING:
            tk.Label(legend, text="■", font=f["tiny"], fg=color, bg=C["bg"]).pack(side="left")
            tk.Label(legend, text=name, font=f["tiny"], fg=C["faint"], bg=C["bg"]).pack(side="left", padx=(0, px(6)))

        foot = tk.Frame(outer, bg=C["bg"])
        foot.pack(side="bottom", fill="x", pady=(px(10), 0))
        for text, command, side in (("Open logs folder", on_open_logs, "left"), ("Quit Jev Voice", on_quit, "right")):
            link = tk.Label(foot, text=text, font=f["small"], fg=C["muted"], bg=C["bg"], cursor="hand2")
            link.pack(side=side)
            link.bind("<Button-1>", lambda _, cmd=command: cmd())

        self.list = tk.Canvas(outer, bg=C["bg"], highlightthickness=0, bd=0)
        self.list.pack(fill="both", expand=True)
        self.inner = tk.Frame(self.list, bg=C["bg"])
        self.list.create_window(0, 0, window=self.inner, anchor="nw", width=self.inner_w)
        self.inner.bind("<Configure>", lambda _: self.list.configure(scrollregion=self.list.bbox("all")))
        self.empty = tk.Label(self.inner, text="Nothing yet. Hold Right Ctrl and say something like “scroll down”.",
                              font=f["small"], fg=C["faint"], bg=C["bg"], wraplength=self.inner_w, justify="left")
        self.empty.pack(anchor="w", pady=px(8))
        self.win.bind("<MouseWheel>", lambda e: self.list.yview_scroll(int(-e.delta / 120) * 2, "units"))
        self.win.bind("<Escape>", lambda _: on_close())

    def attach(self) -> None:
        self.hwnd = int(self.win.wm_frame(), 16)
        set_ex_style(self.hwnd, add=win32con.WS_EX_TOOLWINDOW, remove=win32con.WS_EX_APPWINDOW)
        round_corners(self.hwnd)
        show_no_activate(self.hwnd, False)

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
        self.is_open = False

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

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
        px, f = self.t.px, self.t.fonts
        status = e["status"]
        color = {"done": C["ok"], "error": C["warn"], "review": C["accent"], "discarded": C["faint"]}.get(status, C["work"])
        wrap = self.inner_w - px(34)
        card = tk.Frame(self.inner, bg=C["card"], highlightthickness=1, highlightbackground=C["border"], cursor="hand2")
        tk.Frame(card, bg=color, width=px(3)).pack(side="left", fill="y")
        body = tk.Frame(card, bg=C["card"], padx=px(12), pady=px(9))
        body.pack(side="left", fill="both", expand=True)
        widgets: list[tk.Widget] = [card, body]

        def label(parent: tk.Misc, text: str, font: str, fg: str, **kw: Any) -> tk.Label:
            widget = tk.Label(parent, text=text, font=f[font], fg=fg, bg=kw.pop("bg", C["card"]), justify="left", **kw)
            widgets.append(widget)
            return widget

        top = tk.Frame(body, bg=C["card"])
        top.pack(fill="x")
        widgets.append(top)
        state_word = {"done": "", "error": "Failed", "review": "Waiting for review", "discarded": "Discarded",
                      "working": "Working"}.get(status, "")
        label(top, " · ".join(x for x in (state_word, e["time"]) if x), "tiny", C["faint"]).pack(side="right", anchor="n")
        label(top, shorten(entry_title(e), 52), "bodyb", C["text"]).pack(side="left", anchor="w")
        if e.get("transcript"):
            label(body, f"“{e['transcript']}”", "small", C["muted"], wraplength=wrap, anchor="w").pack(fill="x", pady=(px(2), 0))

        timings = e.get("timings") or {}
        parts = [(timings.get(key), name, tint) for key, name, tint in TIMING if isinstance(timings.get(key), (int, float))]
        chips = tk.Frame(body, bg=C["card"])
        widgets.append(chips)
        chip_text = []
        if parts:
            chip_text.append(format_ms(sum(value for value, _, _ in parts)))
        if e.get("mode") == "goal":
            chip_text.append(f"{len(e.get('actions') or [])} steps")
        elif e.get("verb"):
            chip_text.append(e["verb"].replace("_", " "))
        targetless = e.get("verb") in TARGETLESS
        if isinstance(e.get("target_p"), (int, float)) and not targetless:
            chip_text.append(f"{float(e['target_p']):.0%} sure")
        elif isinstance(e.get("verb_p"), (int, float)):
            chip_text.append(f"{float(e['verb_p']):.0%} sure")
        if e.get("window"):
            chip_text.append(shorten(e["window"], 28))
        if e.get("source") == "typed_test":
            chip_text.append("typed")
        for text in chip_text:
            label(chips, text, "tiny", C["muted"], bg=C["chip"], padx=px(6), pady=px(1)).pack(side="left", padx=(0, px(5)))
        if chip_text:
            chips.pack(fill="x", pady=(px(6), 0))

        if parts:
            total = sum(value for value, _, _ in parts) or 1
            bar_w = wrap
            bar = tk.Canvas(body, width=bar_w, height=px(5), bg=C["card"], highlightthickness=0)
            widgets.append(bar)
            bar.pack(anchor="w", pady=(px(7), 0))
            x = 0.0
            for value, _, tint in parts:
                width = max(px(2), (bar_w - px(2) * (len(parts) - 1)) * value / total)
                bar.create_rectangle(x, 0, x + width, px(5), fill=tint, outline="")
                x += width + px(2)
            summary = " · ".join(f"{name} {format_ms(value)}" for value, name, _ in parts)

        if e["id"] in self.expanded:
            details = [f"{summary}   total {format_ms(total)}"] if parts else []
            if e.get("error"):
                details.append(e["error"])
            if e.get("result"):
                details.append(f"Result: {e['result']}")
            if e.get("actions"):
                details.extend(e["actions"])
            if e.get("next_step"):
                details.append(f"Next: {e['next_step']}")
            if (e.get("target") or {}).get("description"):
                details.append(f"Target: {e['target']['description']}")
            if isinstance(e.get("verb_p"), (int, float)):
                details.append(f"Action confidence {float(e['verb_p']):.0%}")
            details.append(f"id {e['id'][:12]}")
            label(body, "\n".join(details), "tiny", C["muted"], wraplength=wrap, anchor="w").pack(fill="x", pady=(px(6), 0))
        elif status == "error" and e.get("error"):
            label(body, shorten(e["error"], 110), "tiny", C["warn"], wraplength=wrap, anchor="w").pack(fill="x", pady=(px(4), 0))

        for widget in widgets:
            widget.bind("<Button-1>", lambda _, entry_id=e["id"]: self._toggle(entry_id))
        return card
