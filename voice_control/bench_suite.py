"""Run several benchmark arms in a row and keep a live status file that bench_watch shows on screen.

A suite file lists the arms. Each arm runs one harness (bench_eval or goal_eval) on an explicit list of task ids, with
its own environment variables (for A/B switches) and a label for its records:

    {"name": "Settle",
     "arms": [{"name": "bench-1", "title": "Browser benchmark", "module": "voice_control.bench_eval",
               "label": "settle-target-check", "env": {}, "tasks": ["nav_settings_darkmode", "..."]}]}

    python -m voice_control.bench_suite logs/settle-ab/suite.json

Next to the suite file it writes <arm>.txt (the harness output) and status.json: which arm and task is running, with
the task's goal, and every finished task's result and time. Rerunning skips arms whose output already ends with a
summary line, so a suite can be resumed after it was stopped.

When the suite ends (finished or stopped with Ctrl+C) it closes every window the benchmark opened: the windows that
were not there when it started. Apps the benchmark started are ended; windows of apps that were already running
(the user's Chrome, Explorer, Settings) are asked to close. The starting snapshot is kept in status.json, so after the
runner itself was killed the same cleanup runs with:

    python -m voice_control.bench_suite --close-windows logs/settle-ab
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# Never closed, even if they opened during the run: this app, the progress window, and the runner's own Pythons.
KEEP_PROCESSES = {"claude.exe", "python.exe", "pythonw.exe", "windowsterminal.exe", "code.exe", "cursor.exe"}
# Shared hosts: even when started during the run they may hold the user's own windows, so only windows are closed.
CLOSE_ONLY = {"chrome.exe", "explorer.exe", "applicationframehost.exe"}
# "PASS simple nav_tab ... 4.4s" (bench_eval) and "PASS [easy  ] blue_folder ... 2.1s" (goal_eval)
RESULT = re.compile(r"^(PASS|FAIL|PEND|SKIP) (?:\[[^\]]*\]|\S+)\s+(\S+)\s+(\S+).*?(\d+\.\d)s")
SUMMARY = re.compile(r"^\d+/\d+ passed")


def goals() -> dict[str, str]:
    found = {}
    for name in ("bench_tasks.json", "goal_tasks.json"):
        for task in json.loads((Path(__file__).with_name(name)).read_text(encoding="utf-8")):
            found.setdefault(task["id"], task["goal"])
    return found


def desktop() -> dict[str, Any]:
    """The open windows and running processes, to tell afterwards what the benchmark opened."""
    import psutil
    from .windows import open_windows
    return {"windows": [w["hwnd"] for w in open_windows()], "pids": psutil.pids()}


def close_new_windows(before: dict[str, Any]) -> list[str]:
    """Close the windows opened since `before`. Returns the titles of any that stayed open."""
    import psutil
    import win32con
    import win32gui
    import win32process
    from .windows import open_windows
    old_windows, old_pids = set(before["windows"]), set(before["pids"])
    new = [w for w in open_windows() if w["hwnd"] not in old_windows and w["process"].lower() not in KEEP_PROCESSES]
    ended = set()
    for window in new:
        pid = win32process.GetWindowThreadProcessId(window["hwnd"])[1]
        if pid not in old_pids and window["process"].lower() not in CLOSE_ONLY:
            # an app the benchmark started: its documents are throwaway fixtures
            if pid not in ended:
                try:
                    psutil.Process(pid).kill()
                    ended.add(pid)
                except psutil.Error:
                    pass
        else:  # a window of an app that was already running, or of a shared host: close just this window
            try:
                win32gui.PostMessage(window["hwnd"], win32con.WM_CLOSE, 0, 0)
            except win32gui.error:
                pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        left = [w for w in new if win32gui.IsWindow(w["hwnd"]) and win32gui.IsWindowVisible(w["hwnd"])]
        if not left:
            break
        time.sleep(0.25)
    return [w["title"] for w in left]


class Status:
    def __init__(self, path: Path, suite: dict[str, Any]) -> None:
        self.path = path
        names = goals()
        self.data = {"suite": suite.get("name", path.parent.name), "pid": os.getpid(), "started": time.time(),
                     "finished": None, "state": "running", "current": None, "desktop": desktop(), "left_open": None,
                     "arms": [{"name": arm["name"], "title": arm.get("title", arm["name"]), "label": arm.get("label", ""),
                               "total": len(arm["tasks"]),
                               "tasks": [{"id": t, "goal": names.get(t, "")} for t in arm["tasks"]],
                               "results": [], "started": None, "finished": None, "error": None}
                              for arm in suite["arms"]]}
        self.write()

    def write(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(temporary, self.path)

    def begin_task(self, arm_index: int) -> None:
        arm = self.data["arms"][arm_index]
        done = len(arm["results"])
        task = arm["tasks"][done] if done < len(arm["tasks"]) else None
        self.data["current"] = {"arm": arm_index, "index": done, "task": task, "since": time.time()}
        self.write()


def run_arm(arm: dict[str, Any], index: int, status: Status, folder: Path) -> None:
    record = status.data["arms"][index]
    output = folder / f"{arm['name']}.txt"
    if output.exists() and any(SUMMARY.match(line) for line in output.read_text(encoding="utf-8", errors="replace").splitlines()):
        for line in output.read_text(encoding="utf-8", errors="replace").splitlines():
            if match := RESULT.match(line):
                record["results"].append({"id": match.group(2), "status": match.group(1), "outcome": match.group(3),
                                          "seconds": float(match.group(4))})
        record["started"] = record["finished"] = output.stat().st_mtime
        record["resumed"] = True
        status.write()
        return  # already finished in an earlier run of this suite
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", **{k: str(v) for k, v in arm.get("env", {}).items()}}
    command = [sys.executable, "-m", arm["module"], "--ids", *arm["tasks"], "--label", arm.get("label", "")]
    record["started"] = time.time()
    status.begin_task(index)
    with output.open("w", encoding="utf-8") as log, subprocess.Popen(
            command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1) as process:
        for line in process.stdout:
            log.write(line)
            log.flush()
            if match := RESULT.match(line):
                record["results"].append({"id": match.group(2), "status": match.group(1), "outcome": match.group(3),
                                          "seconds": float(match.group(4))})
                status.begin_task(index)
            elif "Traceback" in line:
                record["error"] = "crashed; see " + output.name
        process.wait()
        if process.returncode:
            record["error"] = record["error"] or f"exited with code {process.returncode}"
    record["finished"] = time.time()
    status.write()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("suite", type=Path, help="the suite file, or with --close-windows its folder")
    parser.add_argument("--close-windows", action="store_true",
                        help="only close the windows opened since the suite in this folder started")
    args = parser.parse_args()
    if args.close_windows:
        status = json.loads((args.suite / "status.json").read_text(encoding="utf-8"))
        left = close_new_windows(status["desktop"])
        print("closed the benchmark's windows" + (f"; still open: {left}" if left else ""))
        return
    suite = json.loads(args.suite.read_text(encoding="utf-8"))
    folder = args.suite.parent
    status = Status(folder / "status.json", suite)
    try:
        for index, arm in enumerate(suite["arms"]):
            run_arm(arm, index, status, folder)
        status.data["state"] = "finished"
    except KeyboardInterrupt:
        status.data["state"] = "stopped"
    finally:
        status.data["current"] = None
        if status.data["state"] == "running":  # a crash of the runner itself
            status.data["state"] = "stopped"
        try:
            status.data["left_open"] = close_new_windows(status.data["desktop"])
        except Exception as error:
            status.data["left_open"] = [f"cleanup failed: {type(error).__name__}: {error}"]
        status.data["finished"] = time.time()
        status.write()
        print(f"suite {status.data['state']} at {datetime.now(timezone.utc):%H:%M:%S} UTC")


if __name__ == "__main__":
    main()
