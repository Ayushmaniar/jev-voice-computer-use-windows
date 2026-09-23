"""Native desktop lane of the replicated benchmark: setup and checks that read the apps themselves.

Stand-ins for the Mac apps in the original lane: Notepad for TextEdit, Word for Pages, PowerPoint for Keynote,
Excel for Numbers. Playback runs in the real Spotify app, signed in by the user (VLC with a local playlist was the
stand-in while it was signed out; that task is retired but kept).
Office documents are created fresh through COM and read back through COM, never from Jev's done estimate.
"""

from __future__ import annotations

import math
import os
import re
import struct
import subprocess
import time
import wave
from pathlib import Path
from typing import Any

import pythoncom
import win32com.client

from . import goal_eval
from .windows import open_windows

NATIVE = goal_eval.FIXTURES / "bench" / "native"
TRACKS = ["Morning Walk", "Harbor Lights", "Paper Planes"]
SPOTIFY_USED: set[str] = set()
LATEST: dict[str, Path] = {}  # fixture name -> the file the latest setup actually created


def _wait_window(pattern: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = [w for w in open_windows() if re.search(pattern, w["title"], re.I)]
        if found:
            goal_eval._bring_to_front(found[0]["hwnd"])
            time.sleep(1.2)
            return
        time.sleep(0.4)
    raise RuntimeError(f"setup: no window matching {pattern!r}")


OWN: dict[str, tuple[Any, set[int]]] = {}  # app -> (the COM instance this benchmark started, its process ids)
EXE = {"Word": "WINWORD.EXE", "Excel": "EXCEL.EXE", "PowerPoint": "POWERPNT.EXE"}


def _pids(app: str) -> set[int]:
    import psutil
    return {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").upper() == EXE[app]}


def _fresh_office(app: str):
    """A separate instance for each task, so a dialog left open by an earlier attempt (Excel's "reference isn't
    valid") cannot block the next one, and the user's own documents are never touched. Only processes this
    benchmark started are ever closed."""
    import psutil
    if app in OWN:
        _, pids = OWN.pop(app)
        for pid in pids:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
        time.sleep(1.0)
    before = _pids(app)
    instance = win32com.client.DispatchEx(f"{app}.Application")
    OWN[app] = (instance, _pids(app) - before)
    instance.DisplayAlerts = False  # a "replace the existing file?" prompt would block every COM call
    return instance


def _office(app: str):
    if app in OWN:
        return OWN[app][0]
    return win32com.client.GetActiveObject(f"{app}.Application")


def _close_office_doc(app: str, name: str) -> None:
    try:
        office = win32com.client.GetActiveObject(f"{app}.Application")
    except Exception:
        return
    collection = {"Word": "Documents", "Excel": "Workbooks", "PowerPoint": "Presentations"}[app]
    for doc in list(getattr(office, collection)):
        if doc.Name.lower() == name.lower():
            doc.Saved = True
            doc.Close(0) if app != "PowerPoint" else doc.Close()


def composed(text: str, goal: str, min_words: int) -> bool:
    """Text Jev had to write itself: long enough, and not just a stretch of the request typed back."""
    words = re.findall(r"[\w']+", text.casefold())
    return len(words) >= min_words and " ".join(words) not in " ".join(re.findall(r"[\w']+", goal.casefold()))


def _spotify_window() -> dict[str, Any] | None:
    return next((w for w in open_windows() if w["process"].lower() == "spotify.exe" and w["title"]), None)


def _spotify_button(name: str):
    window = _spotify_window()
    if not window:
        return None
    from pywinauto import Desktop
    return next(iter(Desktop(backend="uia").window(handle=window["hwnd"]).descendants(control_type="Button", title=name)), None)


def _spotify_press(name: str) -> None:
    button = _spotify_button(name)
    if button:
        try:
            button.invoke()
        except Exception:  # Spotify's Chromium UI refuses Invoke on some buttons
            button.click_input()
        time.sleep(0.5)


def _spotify_playing() -> bool:
    window = _spotify_window()  # the title is "Spotify Free" when idle or paused, "Artist - Song" while playing
    return bool(window) and not re.fullmatch(r"Spotify( Free| Premium)?", window["title"])


def _spotify_pause() -> None:
    if _spotify_playing():
        _spotify_press("Pause")


def cleanup() -> None:
    """Close the Office instances and the playlist VLC this benchmark started (never the user's). The VLC also
    holds the remote-control port that goal_eval's own VLC checks use."""
    import psutil
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if (proc.info["name"] or "").lower() == "vlc.exe" and "playlist.m3u" in " ".join(proc.info["cmdline"] or []):
                proc.kill()
        except psutil.Error:
            pass
    if SPOTIFY_USED:  # stop the song a run started; Spotify itself is the user's and stays open
        SPOTIFY_USED.clear()
        try:
            _spotify_pause()
        except Exception:
            pass
    for app in list(OWN):
        _, pids = OWN.pop(app)
        for pid in pids:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass


def _tone(path: Path, freq: float) -> None:
    with wave.open(str(path), "w") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(22050)
        out.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * i / 22050))) for i in range(22050 * 90)))


def setup(op: str, arg: Any) -> None:
    pythoncom.CoInitialize()
    NATIVE.mkdir(parents=True, exist_ok=True)
    if op == "native_notepad":
        # A fresh name each run: Notepad keeps an already open file's old text in its tab even after the file
        # on disk is rewritten, and typing would land after the previous run's line.
        path = NATIVE / f"{Path(arg).stem}-{time.strftime('%H%M%S')}{Path(arg).suffix}"
        LATEST[arg] = path
        path.write_text("Agenda\n", encoding="utf-8")
        subprocess.Popen(["notepad.exe", str(path)], creationflags=goal_eval.DETACHED, close_fds=True)
        _wait_window(re.escape(path.stem) + r".*Notepad")
    elif op == "native_word":
        word = _fresh_office("Word")
        word.Visible = True
        doc = word.Documents.Add()
        _close_office_doc("Word", arg)  # an instance left by an earlier run may still hold the file
        (NATIVE / arg).unlink(missing_ok=True)
        doc.SaveAs2(str(NATIVE / arg))
        doc.Activate()
        _wait_window(re.escape(Path(arg).stem) + r".*Word")
    elif op == "native_powerpoint":
        ppt = _fresh_office("PowerPoint")
        ppt.Visible = True
        deck = ppt.Presentations.Add()
        slide = deck.Slides.Add(1, 1)  # ppLayoutTitle
        slide.Shapes.Title.TextFrame.TextRange.Text = "Company update"
        _close_office_doc("PowerPoint", arg)  # an instance left by an earlier run may still hold the file
        (NATIVE / arg).unlink(missing_ok=True)
        deck.SaveAs(str(NATIVE / arg))
        _wait_window(re.escape(Path(arg).stem) + r".*PowerPoint")
    elif op == "native_excel":
        excel = _fresh_office("Excel")
        excel.Visible = True
        book = excel.Workbooks.Add()
        _close_office_doc("Excel", arg)  # an instance left by an earlier run may still hold the file
        (NATIVE / arg).unlink(missing_ok=True)
        book.SaveAs(str(NATIVE / arg))
        _wait_window(re.escape(Path(arg).stem) + r".*Excel")
    elif op == "native_vlc_playlist":
        files = []
        for i, name in enumerate(TRACKS):
            path = NATIVE / f"{name}.wav"
            if not path.exists():
                _tone(path, 330 + 110 * i)
            files.append(path)
        (NATIVE / "playlist.m3u").write_text("\n".join(str(f) for f in files), encoding="utf-8")
        goal_eval.run_setup([{"vlc": str((NATIVE / "playlist.m3u").relative_to(goal_eval.ROOT))}])
        time.sleep(0.5)
        goal_eval.vlc_rc("pause")
        _wait_window(r"VLC media player")
    elif op == "native_spotify":
        # The user's own signed-in Spotify: paused, on its home view, so no earlier search or song gives anything away.
        SPOTIFY_USED.add("spotify")
        if not _spotify_window():
            os.startfile("spotify:")
            deadline = time.monotonic() + 20
            while not _spotify_window() and time.monotonic() < deadline:
                time.sleep(0.5)
        _spotify_pause()
        _spotify_press("Home")
        goal_eval._bring_to_front(_spotify_window()["hwnd"])
        time.sleep(1.5)
    else:
        raise ValueError(f"unknown native setup {op}")


def check(op: str, arg: Any) -> tuple[bool, str]:
    pythoncom.CoInitialize()
    if op == "native_file_text":
        text = LATEST.get(arg["file"], NATIVE / arg["file"]).read_text(encoding="utf-8", errors="replace")
        return re.search(arg["re"], text, re.I | re.S) is not None, f"file {text[-120:]!r}"
    if op == "native_word_text":
        doc = next(d for d in _office("Word").Documents if d.Name.lower() == arg["file"].lower())
        text = doc.Content.Text
        return re.search(arg["re"], text, re.I | re.S) is not None, f"word {text[:160]!r}"
    if op == "native_ppt_titles":
        deck = next(d for d in _office("PowerPoint").Presentations if d.Name.lower() == arg["file"].lower())
        titles = []
        for slide in deck.Slides:
            texts = [s.TextFrame.TextRange.Text for s in slide.Shapes if s.HasTextFrame and s.TextFrame.HasText]
            titles.append(" | ".join(texts))
        ok = any(re.search(arg["re"], t, re.I) for t in titles[1:]) and len(titles) >= 2
        return ok, f"slides {titles}"
    if op == "native_excel_cells":
        book = next(b for b in _office("Excel").Workbooks if b.Name.lower() == arg["file"].lower())
        sheet = book.Worksheets(1)
        got = {cell: sheet.Range(cell).Value for cell in arg["cells"]}
        ok = all(str(v).rstrip("0").rstrip(".") == str(arg["cells"][c]) if isinstance(v, float) else str(v) == str(arg["cells"][c])
                 for c, v in got.items())
        return ok, f"cells {got}"
    if op == "native_vlc_title":
        title = goal_eval.vlc_rc("get_title")
        status = goal_eval.vlc_rc("status")
        playing = "state playing" in status or "play state: 3" in status
        return bool(re.search(arg, title, re.I)) and playing, f"vlc title {title.strip()[-60:]!r} playing={playing}"
    if op == "native_word_composed":
        doc = next(d for d in _office("Word").Documents if d.Name.lower() == arg["file"].lower())
        text = doc.Content.Text.strip()
        return composed(text, arg["goal"], arg["min_words"]), f"word {text[:160]!r}"
    if op == "native_spotify_title":
        # Spotify's window title turns into "Artist - Song" while a song plays. A free account may play an ad
        # first, so the check waits for it; a run passes only once the requested song itself is playing.
        deadline = time.monotonic() + arg.get("wait", 45)
        title = ""
        while time.monotonic() < deadline:
            window = _spotify_window()
            title = window["title"] if window else ""
            if re.search(arg["re"], title, re.I):
                return True, f"spotify title {title!r}"
            time.sleep(1.0)
        return False, f"spotify title {title!r}"
    raise ValueError(f"unknown native check {op}")
