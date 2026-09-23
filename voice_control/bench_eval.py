"""Replicated Jev computer-use benchmark (lanes of steve8708/jev-browser-benchmark), run with goal mode.

Three lanes, each task checked from the page or app itself, never from Jev's done estimate:
  simple  - 42 local browser fixtures (goal-fixtures/bench/simple), served by bench_server
  long    - longer browser workflows: real public sites up to a safe checkpoint, the user's own Gmail and Drive
            (in their everyday Chrome, on test items prefixed "Jev bench"), local stand-ins for Notion and Figma
  native  - Windows desktop apps standing in for the Mac apps (Notepad, Word, PowerPoint, Excel) and real Spotify

Tasks marked "infeasible" need text the request does not contain (Jev must write it) or a password; they are
kept in the denominator. Checks marked "external" (Gmail, Drive) are judged after the run through those services'
APIs and recorded with --verdict.

    python -m voice_control.bench_eval --lane simple --split dev
    python -m voice_control.bench_eval --ids form_contact nav_tab --runs 2
    python -m voice_control.bench_eval --lane all --label baseline
    python -m voice_control.bench_eval --verdict BATCH TASK RUN pass "draft reply found"

Review prompts (Send, Save, Post...) are approved automatically only on local fixture pages, standing in for
a person approving them; on real sites the run stops at the review gate, which is the safe checkpoint.
This drives the mouse and keyboard. Don't use the computer while it runs.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil
import pythoncom
import win32con
import win32gui

from . import bench_server, goal, goal_eval
from .core import append_log, read_key
from .windows import open_windows

ROOT = goal_eval.ROOT
TASKS = Path(__file__).with_name("bench_tasks.json")
RESULTS = ROOT / "logs" / "bench-eval.jsonl"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE = goal_eval.FIXTURES / "chrome-profile"
SKIPPED = {"invalid_setup", "invalid_environment", "setup_error"}
USER_WINDOWS: list[int] = []  # windows a task opened in the user's own Chrome; only these are ever closed


# ------------------------------------------------------------------ setup
def _kill_profile_chrome() -> None:
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if (proc.info["name"] or "").lower() == "chrome.exe" and str(PROFILE) in " ".join(proc.info["cmdline"] or []):
                proc.kill()
        except psutil.Error:
            pass
    time.sleep(1.0)


def open_chrome(url: str, title: str | None = None) -> None:
    """A throwaway Chrome profile, so runs never touch the user's browser or each other."""
    _kill_profile_chrome()
    for saved in (PROFILE / "Default").glob("Web Data*"):  # autofill entries from earlier runs pop up over fields
        saved.unlink(missing_ok=True)
    before = {w["hwnd"] for w in open_windows()}
    subprocess.Popen([CHROME, f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check",
                      "--disable-session-crashed-bubble", "--hide-crash-restore-bubble", "--start-maximized",
                      # the user's everyday Chrome already has its accessibility tree on (the voice app keeps querying it)
                      "--force-renderer-accessibility",
                      "--new-window", url], creationflags=goal_eval.DETACHED, close_fds=True)
    deadline = time.monotonic() + 15
    pattern = re.escape(title) if title else "Google Chrome$"
    while time.monotonic() < deadline:
        fresh = [w for w in open_windows() if w["hwnd"] not in before and w["process"].lower() == "chrome.exe"
                 and re.search(pattern, w["title"])]
        if fresh:
            goal_eval._bring_to_front(fresh[0]["hwnd"])
            _warm(fresh[0]["hwnd"])
            return
        time.sleep(0.3)
    raise RuntimeError(f"setup: Chrome did not open {url}")


def _warm(hwnd: int) -> None:
    """Wait until the page's own controls are exposed, as they are in a browser that has been open a while."""
    from .windows import capture
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        time.sleep(0.7)
        _, controls = capture(hwnd)
        if any(c.path and "Address and search bar" not in c.name and c.role not in {"TabItem"} and
               not re.fullmatch(r"Minimize|Maximize|Restore|Close|Back|Forward|Reload|View site information|Bookmark this tab|"
                                r"You|Chrome|Tab search|New Tab|Extensions|Side panel.*", c.name) for c in controls):
            return


def open_user_chrome(url: str, title: str) -> None:
    """A new window in the user's everyday Chrome, for tasks in their signed-in accounts. Their other windows and
    tabs are never touched; the window is closed after the check."""
    before = {w["hwnd"] for w in open_windows()}
    subprocess.Popen([CHROME, "--new-window", url], creationflags=goal_eval.DETACHED, close_fds=True)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        fresh = [w for w in open_windows() if w["hwnd"] not in before and w["process"].lower() == "chrome.exe"]
        if fresh and fresh[0]["hwnd"] not in USER_WINDOWS:
            USER_WINDOWS.append(fresh[0]["hwnd"])
        if fresh and re.search(title, fresh[0]["title"], re.I):
            hwnd = fresh[0]["hwnd"]
            goal_eval._bring_to_front(hwnd)
            time.sleep(2.0)  # Gmail and Drive keep filling in after the title appears
            _warm(hwnd)
            # A dialog left by another window (such as "Leave site?") can keep the focus; the task must start here.
            if goal_eval.app_window(win32gui.GetForegroundWindow()) != hwnd:
                raise RuntimeError(f"setup: the new window did not come to the front ({win32gui.GetWindowText(win32gui.GetForegroundWindow())!r})")
            return
        time.sleep(0.5)
    raise RuntimeError(f"setup: no Chrome window matching {title!r} for {url}")


def close_user_windows() -> None:
    """Close the task's own windows. Gmail asks "Leave site?" when a reply was typed but never sent; leaving
    discards only that unsent test reply, in the task's own window."""
    while USER_WINDOWS:
        hwnd = USER_WINDOWS.pop()
        if not win32gui.IsWindow(hwnd):
            continue
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(1.5)
        if win32gui.IsWindow(hwnd):
            from pywinauto import Desktop
            dialogs = []
            win32gui.EnumWindows(lambda h, _: dialogs.append(h) if win32gui.GetWindowText(h) == "Leave site?"
                                 and win32gui.GetWindow(h, win32con.GW_OWNER) == hwnd else None, None)
            for dialog in dialogs:
                buttons = Desktop(backend="uia").window(handle=dialog).descendants(control_type="Button")
                next(b for b in buttons if b.window_text() == "Leave").invoke()
            time.sleep(1.5)
        if win32gui.IsWindow(hwnd):
            print(f"        warning: could not close task window {hwnd} ({win32gui.GetWindowText(hwnd)!r})")


def _page_title(path: str) -> str:
    html = (bench_server.ROOT / path).read_text(encoding="utf-8")
    return re.search(r"<title>(.*?)</title>", html).group(1)


def run_setup(task: dict[str, Any]) -> None:
    rest = []
    for step in task.get("setup", []):
        (op, arg), = step.items()
        if op == "bench_page":
            bench_server.reset(task["id"])
            open_chrome(f"{bench_server.start()}/{arg}", _page_title(arg))
        elif op == "chrome_url":
            open_chrome(arg)
        elif op == "user_chrome_url":
            open_user_chrome(arg["url"], arg["title"])
        elif op.startswith("native_"):
            from . import bench_native
            bench_native.setup(op, arg)
        else:
            rest.append(step)
    goal_eval.run_setup(rest)


# ------------------------------------------------------------------ checks
def matches(expected: Any, got: Any, goal: str = "") -> bool:
    if isinstance(expected, dict) and "composed" in expected:  # text Jev had to write itself
        from .bench_native import composed
        return isinstance(got, str) and composed(got, goal, expected["composed"])
    if isinstance(expected, dict) and "re" in expected:
        return isinstance(got, str) and re.fullmatch(expected["re"], got.strip(), re.I) is not None
    if isinstance(expected, dict):
        return isinstance(got, dict) and all(matches(v, got.get(k), goal) for k, v in expected.items())
    if isinstance(expected, list):  # the same items, in any order
        if not isinstance(got, list) or len(got) != len(expected):
            return False
        left = list(got)
        for item in expected:
            hit = next((i for i, g in enumerate(left) if matches(item, g, goal)), None)
            if hit is None:
                return False
            left.pop(hit)
        return True
    if isinstance(expected, str) and isinstance(got, str):
        return expected.strip().casefold() == got.strip().casefold()
    return expected == got


def check(task: dict[str, Any]) -> tuple[bool | None, str]:
    """None: judged later (--verdict), from the account's own API."""
    spec = dict(task["check"])
    if "external" in spec:
        return None, f"external: {spec['external']}"
    results = []
    if "foreground_field" in spec:  # a field's text in the window the run ended in, read before it is closed
        from .core import FIELD_ROLES
        from .windows import capture
        field = spec.pop("foreground_field")
        _, controls = capture(goal_eval.app_window(win32gui.GetForegroundWindow()))
        values = [c.value for c in controls if c.role in FIELD_ROLES and re.search(field["name"], c.name, re.I)]
        results.append((any(matches(field["value"], v, task["goal"]) for v in values), f"field {field['name']!r}: {values[:2]}"))
    if "bench_state" in spec:
        got = bench_server.state(task["id"])
        results.append((matches(spec.pop("bench_state"), got, task["goal"]), f"state {json.dumps(got)[:200]}"))
    for op in [k for k in spec if k.startswith("native_")]:
        from . import bench_native
        arg = spec.pop(op)
        results.append(bench_native.check(op, {**arg, "goal": task["goal"]} if isinstance(arg, dict) else arg))
    if spec:
        results.append(goal_eval.check(spec))
    return all(ok for ok, _ in results), "; ".join(detail for _, detail in results)


# ------------------------------------------------------------------ runner
def jev_cost(record: dict[str, Any]) -> float:
    return sum(float(((c.get("response") or {}).get("usage") or {}).get("cost") or 0)
               for s in record.get("steps", []) for c in s.get("jev_calls", []))


def run_task(key: str, task: dict[str, Any], driver: goal_eval.DesktopDriver) -> dict[str, Any]:
    record: dict[str, Any] = {"task": task["id"], "lane": task["lane"], "category": task.get("category"), "split": task["split"],
                              "goal": task["goal"], "infeasible": task.get("infeasible"), "site": task.get("site")}
    try:
        run_setup(task)
    except Exception as error:
        record.update(outcome="setup_error", passed=False, check=f"setup failed: {type(error).__name__}: {error}", seconds=0.0)
        close_user_windows()
        return record
    try:
        already, detail = check(task) if "external" not in task["check"] else (False, "")
    except Exception:
        already, detail = False, ""
    if already:
        record.update(outcome="invalid_setup", passed=False, check=f"already satisfied before the run: {detail}", seconds=0.0)
        close_user_windows()
        return record
    approvals = []

    def approve(step, state, controls) -> bool:
        if not goal.action_requires_review(step, state, controls):
            return True
        approvals.append(step.get("target", {}).get("description", ""))
        return bool(task.get("approve"))

    started = time.perf_counter()
    try:
        outcome = goal.run_goal(key, task["goal"], driver.observe, driver.act, record, allow_action=approve)
    except Exception as error:
        outcome = "harness_error"
        record["error"] = f"{type(error).__name__}: {error}"
    record["outcome"] = outcome
    record["seconds"] = round(time.perf_counter() - started, 1)
    record["approvals"] = len(approvals)
    time.sleep(5.0 if USER_WINDOWS else 1.0)  # Gmail saves a draft a few seconds after typing stops
    try:
        record["passed"], record["check"] = check(task)
    except Exception as error:
        record["passed"], record["check"] = False, f"check failed: {type(error).__name__}: {error}"
    close_user_windows()
    record["jev_calls"] = sum(len(s.get("jev_calls", [])) for s in record.get("steps", []))
    record["cost_usd"] = round(jev_cost(record), 6)
    return record


def set_verdict(batch: str, task: str, run: int, verdict: str, detail: str) -> None:
    """Record the result of an external check, judged from the account's own API after the run."""
    lines = RESULTS.read_text(encoding="utf-8").splitlines()
    hits = 0
    for i, line in enumerate(lines):
        record = json.loads(line)
        if record.get("batch") == batch and record["task"] == task and record.get("run") == run:
            record["passed"] = verdict == "pass"
            record["check"] = f"{record['check']} -> {verdict}: {detail}"
            lines[i] = json.dumps(record, ensure_ascii=False)
            hits += 1
    if hits != 1:
        raise SystemExit(f"expected one record for {batch} {task} run {run}, found {hits}")
    RESULTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{task} run {run}: {verdict}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lane", choices=["simple", "long", "native", "all"], default="all")
    parser.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--label", default="")
    parser.add_argument("--verdict", nargs=5, metavar=("BATCH", "TASK", "RUN", "pass|fail", "DETAIL"))
    args = parser.parse_args()
    if args.verdict:
        batch, task, run, verdict, detail = args.verdict
        assert verdict in {"pass", "fail"}
        set_verdict(batch, task, int(run), verdict, detail)
        return
    pythoncom.CoInitialize()
    bench_server.start()
    # Goals name local files as {fixtures}\..., so no machine's home path is committed.
    tasks = json.loads(TASKS.read_text(encoding="utf-8").replace("{fixtures}", json.dumps(str(goal_eval.FIXTURES))[1:-1]))
    if args.ids:
        tasks = [t for t in tasks if t["id"] in args.ids]
    else:
        tasks = [t for t in tasks if not t.get("retired") and (args.lane == "all" or t["lane"] == args.lane)
                 and (args.split == "all" or t["split"] == args.split)]
    key = read_key(ROOT / ".env.openrouter")
    driver = goal_eval.DesktopDriver()
    parked = [w["hwnd"] for w in open_windows() if win32gui.GetWindowLong(w["hwnd"], win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST]
    for hwnd in parked:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    batch = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    try:
        passed = total = 0
        for run in range(args.runs):
            for task in tasks:
                record = run_task(key, task, driver)
                record.update(batch=batch, run=run, label=args.label)
                append_log(RESULTS, record)
                skipped = record["outcome"] in SKIPPED
                if not skipped:
                    total += 1
                    passed += bool(record["passed"])
                status = "SKIP" if skipped else ("PEND" if record["passed"] is None else "PASS" if record["passed"] else "FAIL")
                print(f"{status} {task['lane']:6} {task['id']:26} {record['outcome']:15} {record.get('step_count', 0):2} steps "
                      f"{record['seconds']:5.1f}s ${record.get('cost_usd', 0):.4f} | {record['check'][:150]}", flush=True)
                for line in record.get("actions", []):
                    print(f"        {line[:180]}")
                last = record.get("steps", [{}])[-1] if record.get("steps") else {}
                if last and record["passed"] is not True:
                    print(f"        final: done_p={last.get('done_p')} verb={last.get('verb', {}).get('choice')} "
                          f"reason={last.get('reason') or last.get('error') or ''}")
        print(f"\n{passed}/{total} passed, external checks pending (batch {batch}, label {args.label!r})")
    finally:
        close_user_windows()
        _kill_profile_chrome()
        from . import bench_native
        bench_native.cleanup()
        for hwnd in parked:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)


if __name__ == "__main__":
    main()
