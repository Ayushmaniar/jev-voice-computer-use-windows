"""A small always-on-top window showing a benchmark suite's progress (reads the status.json of bench_suite).

    python -m voice_control.bench_watch logs/settle-ab

While the suite runs the window is click-through and never takes focus, so the harness's clicks pass straight through
it and it cannot become the foreground window (the harness also leaves click-through windows unparked). It is also
excluded from screen capture: people see it, but the pixels the harness samples are those of the windows beneath, so
its ticking timer never reads as a change on the page. Once the suite has stopped or finished it becomes clickable,
with a close button.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

import psutil
import win32api
import win32con
import win32gui

from .overlay import C, Theme, round_corners, set_ex_style

WIDTH = 460
MARGIN = 16
TITLE = "Jev benchmark progress"


def alive(pid: int | None) -> bool:
    try:
        return bool(pid) and psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def clock(seconds: float | None) -> str:
    return datetime.fromtimestamp(seconds).strftime("%H:%M") if seconds else "--:--"


def elapsed(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{max(1, minutes)} min"


def summary(status: dict) -> dict:
    """Everything the window shows, computed from status.json; a new value means a redraw."""
    arms = status["arms"]
    running = status["state"] == "running" and alive(status.get("pid"))
    state = "running" if running else ("finished" if status["state"] == "finished" else "stopped")
    done = sum(len(a["results"]) for a in arms)
    total = sum(a["total"] for a in arms)
    # Wall-clock time per finished task so far (setup and settling included), for the estimate. Arms carried over
    # from an earlier, resumed run have no duration here and are left out.
    timed = [a for a in arms if a["started"] and a["finished"] and not a.get("error") and not a.get("resumed")]
    spent = sum(a["finished"] - a["started"] for a in timed)
    finished_tasks = sum(len(a["results"]) for a in timed)
    current = status.get("current") if running else None
    if current:
        arm = arms[current["arm"]]
        spent += max(0.0, current["since"] - arm["started"])
        finished_tasks += current["index"]
    per_task = spent / finished_tasks if finished_tasks else None
    eta = current["since"] + per_task * (total - done) if current and per_task else None
    rows = []
    for index, arm in enumerate(arms):
        passed = sum(r["status"] == "PASS" for r in arm["results"])
        counted = sum(r["status"] in {"PASS", "FAIL"} for r in arm["results"])
        mark = ("▶" if current and current["arm"] == index else "✓" if arm["finished"] and not arm["error"]
                else "!" if arm["error"] else "·")
        score = f"{passed}/{counted} passed" if counted else ""
        rows.append((mark, arm.get("title", arm["name"]), f"{len(arm['results'])}/{arm['total']}", score,
                     "current" if mark == "▶" else "done" if mark == "✓" else "error" if mark == "!" else "pending"))
    task = None
    if current and current["task"]:
        arm = arms[current["arm"]]
        task = {"arm": f"Arm {current['arm'] + 1} of {len(arms)} · {arm.get('title', arm['name'])}",
                "count": f"Task {current['index'] + 1} of {arm['total']}", "id": current["task"]["id"],
                "goal": current["task"]["goal"], "since": clock(current["since"])}
    last = None
    for arm in reversed(arms):
        if arm["results"]:
            result = arm["results"][-1]
            last = f"Last: {result['status']} {result['id']} ({result['seconds']:.0f} s)"
            break
    return {"state": state, "suite": status["suite"], "done": done, "total": total, "task": task, "rows": rows,
            "last": last, "eta": (f"Done around {clock(eta)} · about {duration(eta - current['since'])} left"
                                  if eta else "Estimating the finish time after the first tasks…"),
            "ended": clock(status.get("finished")), "started": status.get("started"),
            "took": duration(status["finished"] - status["started"]) if status.get("finished") and status.get("started") else None,
            "left_open": status.get("left_open")}


class Watch:
    def __init__(self, folder: Path) -> None:
        self.path = folder / "status.json"
        try:  # sharp text and window coordinates that match the screen's at any display scale
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.title(TITLE)
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.97)
        self.root.configure(bg=C["bg"])
        self.theme = Theme(self.root)
        self.base = self.theme.fonts["body"].actual()["family"]
        self.width = self.theme.px(WIDTH)
        self.shown = None
        self.clickable = None
        self.timer = None
        self.frame = tk.Frame(self.root, bg=C["bg"], padx=self.theme.px(14), pady=self.theme.px(12))
        self.frame.pack(fill="both", expand=True)
        self.root.after(0, self.refresh)

    def font(self, size: int, weight: str = "normal") -> tuple:
        return (self.base, size, weight)

    def label(self, parent, text: str, color: str = "text", size: int = 9, weight: str = "normal", wrap: int = 0, **grid):
        widget = tk.Label(parent, text=text, fg=C[color], bg=C["bg"], font=self.font(size, weight), anchor="w",
                          justify="left", wraplength=wrap)
        widget.grid(sticky="w", **grid)
        return widget

    def render(self, view: dict) -> None:
        for child in self.frame.winfo_children():
            child.destroy()
        self.frame.columnconfigure(0, weight=1)
        dot = {"running": "ok", "finished": "accent", "stopped": "warn"}[view["state"]]
        head = tk.Frame(self.frame, bg=C["bg"])
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(1, weight=1)
        tk.Label(head, text="●", fg=C[dot], bg=C["bg"], font=self.font(10)).grid(row=0, column=0)
        title = {"running": "Benchmark running", "finished": f"Benchmark finished at {view['ended']}",
                 "stopped": f"Benchmark stopped at {view['ended']}"}[view["state"]]
        self.label(head, f" {title}", weight="bold", size=10, row=0, column=1)
        if view["state"] != "running":
            close = tk.Label(head, text="✕", fg=C["muted"], bg=C["bg"], font=self.font(10), cursor="hand2")
            close.grid(row=0, column=2)
            close.bind("<Button-1>", lambda _e: self.root.destroy())
        row = 1
        if view["state"] == "running" and view["started"]:
            self.timer = self.label(self.frame, "", "muted", row=row, pady=(2, 0)); row += 1
            self.tick()
        else:
            self.timer = None
            if view["took"]:
                self.label(self.frame, f"Took {view['took']} (started {clock(view['started'])})", "muted", row=row,
                           pady=(2, 0)); row += 1
            if view["left_open"]:
                self.label(self.frame, "Still open: " + ", ".join(view["left_open"])[:120], "warn",
                           wrap=self.width - self.theme.px(32), row=row); row += 1
            elif view["left_open"] is not None:
                self.label(self.frame, "Closed the windows the benchmark opened", "faint", row=row); row += 1
        if view["task"]:
            task = view["task"]
            self.label(self.frame, f"{task['arm']}  ·  {task['count']}", "muted", row=row, pady=(8, 0)); row += 1
            self.label(self.frame, task["id"], "accent", size=10, weight="bold", row=row, pady=(4, 0)); row += 1
            self.label(self.frame, f"“{task['goal']}”", wrap=self.width - self.theme.px(32), row=row); row += 1
        elif view["state"] == "running":
            self.label(self.frame, "Starting the next arm…", "muted", row=row, pady=(8, 0)); row += 1
        inner, thick = self.width - self.theme.px(28), self.theme.px(6)
        bar = tk.Canvas(self.frame, height=thick, bg=C["chip"], highlightthickness=0, bd=0, width=inner)
        bar.grid(row=row, column=0, sticky="ew", pady=(self.theme.px(10), 2)); row += 1
        bar.create_rectangle(0, 0, inner * view["done"] / max(1, view["total"]), thick, fill=C["ok"], width=0)
        self.label(self.frame, f"{view['done']} of {view['total']} task runs done", "muted", row=row); row += 1
        if view["state"] == "running":
            self.label(self.frame, view["eta"], row=row); row += 1
        if view["last"]:
            self.label(self.frame, view["last"], "faint", row=row); row += 1
        table = tk.Frame(self.frame, bg=C["bg"])
        table.grid(row=row, column=0, sticky="ew", pady=(10, 0))
        colors = {"current": "text", "done": "muted", "error": "warn", "pending": "faint"}
        for i, (mark, name, count, score, kind) in enumerate(view["rows"]):
            for column, (text, width) in enumerate(((mark, 2), (name, 34), (count, 6), (score, 13))):
                tk.Label(table, text=text, fg=C[colors[kind]], bg=C["bg"], font=self.font(9, "bold" if kind == "current" else "normal"),
                         width=width, anchor="w").grid(row=i, column=column, sticky="w")

    def tick(self) -> None:
        """Only the timer's text changes each second; the rest redraws when a task starts or finishes."""
        if self.timer is not None and self.shown and self.shown["started"]:
            self.timer.configure(text=f"Running for {elapsed(time.time() - self.shown['started'])}"
                                      f"  ·  started {clock(self.shown['started'])}")

    def place(self) -> None:
        self.root.update_idletasks()
        left, top, right, bottom = win32api.GetMonitorInfo(win32api.MonitorFromPoint((0, 0)))["Work"]
        height = self.root.winfo_reqheight()
        margin = self.theme.px(MARGIN)
        self.root.geometry(f"{self.width}x{height}+{right - self.width - margin}+{top + margin}")

    def set_clickable(self, clickable: bool) -> None:
        if clickable == self.clickable:
            return
        self.clickable = clickable
        hwnd = win32gui.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        add = win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_LAYERED
        if clickable:
            set_ex_style(hwnd, add=add, remove=win32con.WS_EX_TRANSPARENT)
        else:  # the harness's clicks go through it, and the harness does not park click-through windows
            set_ex_style(hwnd, add=add | win32con.WS_EX_TRANSPARENT)
        round_corners(hwnd)
        try:  # WDA_EXCLUDEFROMCAPTURE (Windows 10 2004+): visible on screen, absent from screen captures
            import ctypes
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)
        except Exception:
            pass

    def refresh(self) -> None:
        try:
            view = summary(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            view = None
        if view is not None and view != self.shown:
            self.shown = view
            self.render(view)
            self.place()
            self.root.deiconify()
            self.set_clickable(view["state"] != "running")
        self.tick()
        self.root.after(1000, self.refresh)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, nargs="?", default=Path(__file__).resolve().parents[1] / "logs" / "settle-ab")
    Watch(parser.parse_args().folder).run()


if __name__ == "__main__":
    main()
