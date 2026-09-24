"""Live evaluation of goal mode on the real desktop, with independent success checks.

Each task in goal_tasks.json has a spoken-style goal, a deterministic setup
(open a folder, start VLC, focus a window...), and a check that reads the
result from the system itself (window titles, UI Automation, VLC's own remote
control interface), never from Jev's done probability. Tasks are split into
`dev` (used while tuning prompts) and `holdout` (only measured, to catch
overfitting).

    python -m voice_control.goal_eval --split dev
    python -m voice_control.goal_eval --ids gmail vlc_subs_en --runs 2
    python -m voice_control.goal_eval --split holdout

This drives the mouse and keyboard. Don't use the computer while it runs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pythoncom
import win32con
import win32gui
import win32process
import psutil

from . import goal
from .settle import Settler
from .core import append_log, read_key
from .windows import _bring_to_front, app_window, capture, capture_settled, installed_apps, open_windows

ROOT = Path(__file__).resolve().parents[1]
TASKS = Path(__file__).with_name("goal_tasks.json")
RESULTS = ROOT / "logs" / "goal-eval.jsonl"
FIXTURES = ROOT / "goal-fixtures"
VLC = next((p for p in (Path(os.environ.get("ProgramFiles(x86)", "")) / "VideoLAN/VLC/vlc.exe",
                        Path(os.environ.get("ProgramFiles", "")) / "VideoLAN/VLC/vlc.exe") if p.is_file()), None)
VLC_RC = ("127.0.0.1", 4212)
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


class DesktopDriver:
    """observe/act for goal.run_goal against the live desktop."""

    def __init__(self) -> None:
        self.apps = installed_apps()
        self.settler = Settler()

    def observe(self):
        hwnd = app_window(win32gui.GetForegroundWindow())
        if not hwnd or win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid():
            raise RuntimeError("No usable foreground window")
        self.settler.before_capture(hwnd)
        state, controls = capture_settled(hwnd)
        return state, controls, self.apps

    def act(self, verb, target, state, controls, apps, text):
        return self.settler.act(verb, target, state, controls, apps, "", None, text)

    def stale(self) -> bool:
        return self.settler.stale()


# ------------------------------------------------------------------ setup operations
def _windows_matching(pattern: str) -> list[dict[str, Any]]:
    return [w for w in open_windows() if re.search(pattern, w["title"], re.I)]


def _focus(pattern: str) -> None:
    matches = _windows_matching(pattern)
    if not matches:
        raise RuntimeError(f"setup: no window matches {pattern!r}")
    _bring_to_front(matches[0]["hwnd"])
    time.sleep(0.4)
    if win32gui.GetForegroundWindow() != matches[0]["hwnd"]:
        _bring_to_front(matches[0]["hwnd"])
        time.sleep(0.4)
    if win32gui.GetForegroundWindow() != matches[0]["hwnd"]:
        raise RuntimeError(f"setup: could not focus window matching {pattern!r}")


def _kill(process: str) -> None:
    for proc in psutil.process_iter(["name"]):
        if (proc.info["name"] or "").lower() == process.lower():
            try:
                proc.kill()
            except psutil.Error:
                pass
    time.sleep(0.8)


def _explorer_at(path: str) -> None:
    folder = (ROOT / path).resolve()
    name = folder.name
    existing = [w for w in open_windows() if w["process"] == "explorer.exe" and w["title"].startswith(name + " - File Explorer")]
    if existing:
        _bring_to_front(existing[0]["hwnd"])
    else:
        os.startfile(folder)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and not _windows_matching("^" + re.escape(name) + " - File Explorer"):
            time.sleep(0.2)
        _focus("^" + re.escape(name) + " - File Explorer")
    time.sleep(0.5)


def _vlc(media: str) -> None:
    if VLC is None:
        raise RuntimeError("VLC is not installed")
    movie = (ROOT / media).resolve()
    # Keep the user's unrelated VLC windows open. Only replace a previous
    # process that this harness launched for the same fixture movie.
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if (proc.info["name"] or "").casefold() == "vlc.exe" and str(movie).casefold() in " ".join(proc.info["cmdline"] or []).casefold():
                proc.kill()
        except psutil.Error:
            pass
    time.sleep(0.8)
    process = subprocess.Popen([str(VLC), "--no-one-instance", "--extraintf=oldrc",
                                f"--rc-host={VLC_RC[0]}:{VLC_RC[1]}", "--rc-quiet",
                                "--no-qt-privacy-ask", "--no-video-title-show", str(movie)],
                               creationflags=DETACHED, close_fds=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any(win32process.GetWindowThreadProcessId(w["hwnd"])[1] == process.pid and
               w["title"].endswith("VLC media player") and " - " in w["title"] for w in open_windows()):
            break
        time.sleep(0.3)
    time.sleep(1.0)
    # VLC keeps its last volume across launches, so every "turn it down" run started lower than the one before
    # (154, 141, 77, 38, then 0, where nothing is lower). Each run starts at 100% (256 in the rc interface's units).
    vlc_rc("volume 256")
    time.sleep(0.3)
    BASELINE["vlc_volume"] = _vlc_volume()


BASELINE: dict[str, Any] = {}


def _vlc_time() -> int:
    numbers = re.findall(r"^\s*(\d+)\s*$", vlc_rc("get_time"), re.M)
    return int(numbers[-1]) if numbers else -1


def _vlc_volume() -> int:
    found = re.findall(r"audio volume: (\d+)", vlc_rc("volume"))  # the reply also ends in "returned 0"
    return int(found[-1]) if found else -1


def _ensure(process: str, command: str) -> None:
    if not any(w["process"].lower() == process.lower() for w in open_windows()):
        os.startfile(command)
        time.sleep(3)


def run_setup(steps: list[dict[str, Any]]) -> None:
    for step in steps:
        (op, arg), = step.items()
        if op == "kill":
            _kill(arg)
        elif op == "explorer":
            _explorer_at(arg)
        elif op == "vlc":
            _vlc(arg)
        elif op == "focus":
            _focus(arg)
        elif op == "ensure":
            _ensure(*arg)
        elif op == "close":
            for w in _windows_matching(arg):
                win32gui.PostMessage(w["hwnd"], win32con.WM_CLOSE, 0, 0)
            time.sleep(0.8)
        elif op == "minimize":
            for w in _windows_matching(arg):
                win32gui.ShowWindow(w["hwnd"], win32con.SW_MINIMIZE)
            time.sleep(0.3)
        elif op == "chrome_new_window":  # a throwaway profile: runs never touch the user's browser or each other
            profile = FIXTURES / "chrome-profile"
            for proc in psutil.process_iter(["name", "cmdline"]):
                if (proc.info["name"] or "").lower() == "chrome.exe" and str(profile) in " ".join(proc.info["cmdline"] or []):
                    try:
                        proc.kill()
                    except psutil.Error:
                        pass
            time.sleep(1.0)
            before = {w["hwnd"] for w in open_windows()}
            subprocess.Popen([arg, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                              "--new-window", "chrome://newtab"], creationflags=DETACHED, close_fds=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not [w for w in _windows_matching(r"New Tab - Google Chrome$") if w["hwnd"] not in before]:
                time.sleep(0.2)
            time.sleep(1.0)
        elif op == "sleep":
            time.sleep(arg)
        else:
            raise ValueError(f"unknown setup op {op}")


# ------------------------------------------------------------------ checks (read the system, not the agent)
def vlc_rc(command: str) -> str:
    with socket.create_connection(VLC_RC, timeout=3) as sock:
        sock.settimeout(1.0)
        sock.sendall((command + "\n").encode())
        time.sleep(0.4)
        data = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
                if len(chunk) < 65536:
                    break
        except socket.timeout:
            pass
    return data.decode(errors="replace")


def check(spec: dict[str, Any]) -> tuple[bool, str]:
    results = []
    for op, arg in spec.items():
        if op == "foreground_title":
            title = win32gui.GetWindowText(app_window(win32gui.GetForegroundWindow()))
            results.append((bool(re.search(arg, title, re.I)), f"foreground {title!r}"))
        elif op == "window_title":
            results.append((bool(_windows_matching(arg)), f"window {arg!r} {'open' if _windows_matching(arg) else 'missing'}"))
        elif op == "foreground_control":  # a control in the foreground window whose name matches
            _, controls = capture(app_window(win32gui.GetForegroundWindow()))
            names = [c.name for c in controls if re.search(arg, c.name, re.I)]
            results.append((bool(names), f"control {arg!r}: {names[:3]}"))
        elif op == "foreground_selected_control":
            _, controls = capture(app_window(win32gui.GetForegroundWindow()))
            names = [c.name for c in controls if re.search(arg, c.name, re.I) and "selected" in c.state]
            results.append((bool(names), f"selected control {arg!r}: {names[:3]}"))
        elif op == "foreground_text":  # any UIA element name (including static text) in the foreground window
            from pywinauto import Desktop
            root = Desktop(backend="uia").window(handle=app_window(win32gui.GetForegroundWindow())).wrapper_object()
            names = [d.element_info.name for d in root.descendants() if d.element_info.name and re.search(arg, d.element_info.name, re.I)]
            results.append((bool(names), f"text {arg!r}: {names[:3]}"))
        elif op == "vlc_subtitle":
            listing = vlc_rc("strack")
            current = next((line for line in listing.splitlines() if line.rstrip().endswith("*")), "")
            results.append((bool(re.search(arg, current, re.I)), f"subtitle {current.strip()!r}"))
        elif op == "vlc_paused":  # is_playing stays 1 while paused; status reports the pause
            status = vlc_rc("status")
            paused = "Type 'pause' to continue" in status or "play state: 4" in status
            results.append((paused == arg, f"paused {paused}"))
        elif op == "vlc_rate_above":  # oldrc cannot report the rate, so time how fast playback advances
            first, started = _vlc_time(), time.monotonic()
            time.sleep(6)
            rate = (_vlc_time() - first) / (time.monotonic() - started)
            results.append((rate > arg, f"measured rate {rate:.2f}"))
        elif op == "vlc_volume_decreased":
            volume = _vlc_volume()
            results.append((0 <= volume < BASELINE.get("vlc_volume", -1), f"volume {BASELINE.get('vlc_volume')} -> {volume}"))
        else:
            raise ValueError(f"unknown check {op}")
    return all(ok for ok, _ in results), "; ".join(detail for _, detail in results)


# ------------------------------------------------------------------ runner
def run_task(key: str, task: dict[str, Any], driver: DesktopDriver) -> dict[str, Any]:
    record: dict[str, Any] = {"task": task["id"], "goal": task["goal"], "split": task["split"], "level": task["level"]}
    try:
        run_setup(task.get("setup", []))
    except Exception as error:
        denied = isinstance(error, PermissionError) or getattr(error, "winerror", None) == 5
        record.update(outcome="invalid_environment" if denied else "setup_error", passed=False,
                      check=f"setup failed: {type(error).__name__}: {error}", seconds=0.0)
        return record
    if not win32gui.GetForegroundWindow():
        record.update(outcome="invalid_environment", passed=False,
                      check="Windows has no foreground window; an interactive desktop is required", seconds=0.0)
        return record
    try:
        already, detail = check(task["check"]) if not task.get("check_needs_action") else (False, "")
    except Exception:
        already, detail = False, ""
    if already:  # the goal is satisfied before anything ran: the setup failed to reset, so the run would prove nothing
        record.update(outcome="invalid_setup", passed=False, check=f"already satisfied before the run: {detail}", seconds=0.0)
        return record
    started = time.perf_counter()
    try:
        outcome = goal.run_goal(key, task["goal"], driver.observe, driver.act, record, stale=driver.stale)
    except Exception as error:
        outcome = "harness_error"
        record["error"] = f"{type(error).__name__}: {error}"
    record["outcome"] = outcome
    record["seconds"] = round(time.perf_counter() - started, 1)
    time.sleep(0.5)
    try:
        record["passed"], record["check"] = check(task["check"])
    except Exception as error:
        record["passed"], record["check"] = False, f"check failed: {type(error).__name__}: {error}"
    record["jev_calls"] = sum(len(s.get("jev_calls", [])) for s in record.get("steps", []))
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=["dev", "holdout", "challenge", "all"], default="dev")
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--label", default="", help="free text stored with each result, e.g. the prompt variant")
    args = parser.parse_args()
    pythoncom.CoInitialize()
    tasks = json.loads(TASKS.read_text(encoding="utf-8"))
    if args.ids:
        tasks = [t for t in tasks if t["id"] in args.ids]
    elif args.split != "all":
        tasks = [t for t in tasks if t["split"] == args.split]
    key = read_key(ROOT / ".env.openrouter")
    driver = DesktopDriver()
    # An always-on-top window (such as a chat app watching this run) would sit over every target.
    parked = [w["hwnd"] for w in open_windows() if win32gui.GetWindowLong(w["hwnd"], win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST]
    for hwnd in parked:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    try:
        run_all(key, tasks, driver, args)
    finally:
        for hwnd in parked:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)


def run_all(key: str, tasks: list[dict[str, Any]], driver: DesktopDriver, args: argparse.Namespace) -> None:
    batch = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    passed = 0
    total = 0
    unavailable = False
    for run in range(args.runs):
        for task in tasks:
            record = run_task(key, task, driver)
            record.update(batch=batch, run=run, label=args.label)
            append_log(RESULTS, record)
            if record["outcome"] not in {"invalid_setup", "invalid_environment", "setup_error"}:
                total += 1
                passed += record["passed"]
            status = "SKIP" if record["outcome"] in {"invalid_setup", "invalid_environment", "setup_error"} else ("PASS" if record["passed"] else "FAIL")
            print(f"{status} [{task['level']:6}] {task['id']:22} {record['outcome']:10} "
                  f"{record.get('step_count', 0)} steps {record['seconds']:5.1f}s  | {record['check']}", flush=True)
            if record["outcome"] == "invalid_environment":
                unavailable = True
                break
            for line in record.get("actions", []):
                print(f"        {line[:200]}")
            last = record.get("steps", [{}])[-1] if record.get("steps") else {}
            if last:
                print(f"        final: done_p={last.get('done_p')} verb={last.get('verb', {}).get('choice')} "
                      f"reason={last.get('reason') or last.get('error') or ''}")
        if unavailable:
            break
    print(f"\n{passed}/{total} passed ({args.split if not args.ids else 'selected'}; batch {batch})")


if __name__ == "__main__":
    main()
