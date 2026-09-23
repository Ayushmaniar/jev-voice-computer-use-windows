"""Background push-to-talk app. Right Ctrl is the global hold-to-talk key.

The UI is a small always-on-top pill (overlay.Pill) that shows live captions
while you hold Right Ctrl, then progress and the result, and collapses back
to a dot. The chevron opens the activity panel with the history of commands.
"""

from __future__ import annotations

import ctypes
import copy
import json
import os
import queue
import time
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import tkinter as tk

import numpy as np
import sounddevice as sd
import win32api
import win32event
import win32gui
import win32process
import pythoncom
from pynput import keyboard as global_keyboard

from .core import (PROMPTS, append_log, asr_hotwords, collapse_repeats, plan_command, read_key, should_auto_run,
                   snapshot_inputs)
from . import goal
from .overlay import (C, STATUS_BY_OUTCOME, VERB_NOW, ActivityPanel, Pill, Theme, action_phrase, entry_from_record,
                      friendly_error)
from .windows import capture, capture_settled, execute, installed_apps, window_app, app_window, settle, _top_level_picture

ROOT = Path(__file__).resolve().parents[1]
ICON = Path(__file__).resolve().parent / "assets" / "jev-voice-logo.png"
LOG = ROOT / "logs" / "voice-actions.jsonl"
AUDIO_DIR = ROOT / "logs" / "audio"


def save_clip(audio: np.ndarray, uid: str) -> str:
    """Keep the exact clip Whisper heard so transcription changes can be replayed against real speech."""
    import wave
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIO_DIR / f"{uid}.wav"
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(SAMPLE_RATE)
        file.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return str(path.relative_to(ROOT))
SETTINGS = ROOT / ".voice-settings.json"
REPLAY_FIELDS = {"inputs", "prompts", "jev_calls"}
SAMPLE_RATE = 16000


def _without_replay_payload(record: dict) -> dict:
    """Final records share the plan record's utterance_id; the replay payload is stored once, on the plan record."""
    return {k: v for k, v in record.items() if k not in REPLAY_FIELDS}


def enable_cuda_dlls() -> None:
    """CTranslate2 loads cuBLAS/cuDNN by name; the pip NVIDIA wheels put them in nvidia/*/bin, which Windows doesn't search."""
    try:
        import nvidia
    except ImportError:
        return
    for root in nvidia.__path__:
        for folder in Path(root).glob("*/bin"):
            os.add_dll_directory(str(folder))
            os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")


def load_settings() -> dict:
    settings = {"auto_run": True, "goal_mode": True, "hide_when_idle": False, "pill_anchor": None}
    try:
        settings.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    settings["auto_run"] = True  # every launch starts with auto-run on; turning it off lasts one session
    return settings


def recent_entries(path: Path, limit: int = 40, max_bytes: int = 4_000_000) -> list[dict]:
    """Newest `limit` commands from the log, oldest first; a final record replaces its plan record."""
    if not path.exists():
        return []
    with path.open("rb") as file:
        size = file.seek(0, 2)
        file.seek(max(0, size - max_bytes))
        lines = file.read().decode("utf-8", "replace").splitlines()
    if size > max_bytes:
        lines = lines[1:]
    entries: dict[str, dict] = {}
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("utterance_id") and record.get("outcome") in STATUS_BY_OUTCOME:
            entries.pop(record["utterance_id"], None)
            entries[record["utterance_id"]] = entry_from_record(record)
    return [e for e in entries.values() if e["status"] != "working"][-limit:]


class VoiceApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Jev Voice Control")
        if ICON.exists():
            self.root.iconphoto(True, tk.PhotoImage(file=str(ICON)))
        LOG.parent.mkdir(parents=True, exist_ok=True)
        self.settings = load_settings()
        self.last_external_hwnd: int | None = None
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jev-voice", initializer=pythoncom.CoInitialize)
        self.model = None
        self.model_device = "loading"
        self.gpu_model_name = os.environ.get("JEV_VOICE_MODEL", "large-v3-turbo")
        self.cpu_model_name = os.environ.get("JEV_VOICE_CPU_MODEL", "base.en")
        self.model_name = self.gpu_model_name
        # Screen capture runs on its own thread from the moment Right Ctrl goes
        # down, so on-screen names can prime Whisper and planning needn't wait.
        self.capturer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jev-capture", initializer=pythoncom.CoInitialize)
        self.scene = None
        self.apps_cache: list[dict[str, str]] = []
        self.apps_cached_at = 0.0
        self.recording = False
        self.audio_blocks: list[np.ndarray] = []
        self.samples = 0
        self.rms = 0.0
        self.stream = None
        self.pending = None
        self.busy = False
        self.goal_stop = threading.Event()
        self.goal_active = False
        self.goal_review_pending: tuple[dict, threading.Event, str] | None = None
        self.key_held = False
        self.key_chorded = False
        self.take = 0
        self.partial_busy = False
        self.partial_samples = 0
        self.caption = ""
        self.entries: dict[str, dict] = {}
        self.tray = None
        self.key_listener = global_keyboard.Listener(on_press=self._key_press, on_release=self._key_release)

        self.theme = Theme(self.root)
        self.auto = tk.BooleanVar(value=bool(self.settings["auto_run"]))
        self.goal_mode = tk.BooleanVar(value=bool(self.settings["goal_mode"]))
        self.hide_idle = tk.BooleanVar(value=bool(self.settings["hide_when_idle"]))
        anchor = self.settings.get("pill_anchor")
        self.pill = Pill(self.root, self.theme, on_panel=self.toggle_panel, on_menu=self._show_menu,
                         on_run=self.execute_pending, on_cancel=self.cancel_pending, on_moved=self._pill_moved,
                         get_level=self._level, anchor=tuple(anchor) if anchor else None)
        self.panel = ActivityPanel(self.root, self.theme, auto_var=self.auto, goal_var=self.goal_mode, hide_var=self.hide_idle,
                                   on_command=self.plan_typed_command, on_open_logs=self.open_logs,
                                   on_quit=self._close, on_close=self.toggle_panel)
        self.auto.trace_add("write", lambda *_: self._save_settings())
        self.goal_mode.trace_add("write", lambda *_: self._save_settings())
        self.hide_idle.trace_add("write", lambda *_: (self.pill.set_hide_when_idle(self.hide_idle.get()), self._save_settings()))
        self.root.update_idletasks()
        self.root.update()
        self.pill.attach()
        self.panel.attach()
        self.app_hwnd = self.pill.hwnd
        self.pill.set_hide_when_idle(self.hide_idle.get())
        self.panel.set_status("Loading speech model…")
        for entry in recent_entries(LOG):
            self.panel.upsert(entry)
        self._start_tray()
        self.key_listener.start()
        self.root.after(60, self._poll)
        self.root.after(200, self._track_foreground)
        self.worker.submit(self._load_model)

    # ------------------------------------------------------------ settings and chrome
    def _save_settings(self) -> None:
        self.settings.update(auto_run=self.auto.get(), goal_mode=self.goal_mode.get(), hide_when_idle=self.hide_idle.get())
        try:
            saved = {k: v for k, v in self.settings.items() if k != "auto_run"}
            SETTINGS.write_text(json.dumps(saved, indent=2), encoding="utf-8")
        except OSError:
            pass
        if self.tray is not None:
            self.tray.update_menu()

    def _pill_moved(self, anchor: tuple[int, int]) -> None:
        self.settings["pill_anchor"] = [int(anchor[0]), int(anchor[1])]
        self._save_settings()
        if self.panel.is_open:
            self.panel.open(self.pill.top_edge())

    def _reset_pill(self) -> None:
        self.settings["pill_anchor"] = None
        self._save_settings()
        left, _, right, bottom = win32api.GetMonitorInfo(win32api.MonitorFromPoint((0, 0)))["Work"]
        self.pill.anchor = ((left + right) // 2, bottom - self.theme.px(18))
        self._pill_moved(self.pill.anchor)
        self.pill.update()

    def toggle_panel(self) -> None:
        if self.panel.is_open:
            self.panel.close()
        else:
            self.panel.open(self.pill.top_edge())
        self.pill.set_panel_open(self.panel.is_open)

    def open_logs(self) -> None:
        os.startfile(LOG.parent)

    def _show_menu(self, x: int, y: int) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Hide activity" if self.panel.is_open else "Show activity", command=self.toggle_panel)
        menu.add_checkbutton(label="Run actions automatically", variable=self.auto)
        menu.add_checkbutton(label="Multi-step goal mode", variable=self.goal_mode)
        if self.goal_active:
            menu.add_command(label="Stop current goal", command=self.stop_goal)
        menu.add_checkbutton(label="Hide pill when idle", variable=self.hide_idle)
        menu.add_command(label="Move pill back to the bottom", command=self._reset_pill)
        menu.add_separator()
        menu.add_command(label="Open logs folder", command=self.open_logs)
        menu.add_command(label="Quit Jev Voice", command=self._close)
        menu.tk_popup(x, y)

    def _start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image
        except ImportError:
            return
        with Image.open(ICON) as source:
            image = source.convert("RGBA")
            image.thumbnail((64, 64), Image.Resampling.LANCZOS)

        def post(name: str):
            return lambda *_: self.events.put(("ui", name))

        menu = pystray.Menu(
            pystray.MenuItem("Show activity", post("panel"), default=True),
            pystray.MenuItem("Run actions automatically", post("auto"), checked=lambda _: bool(self.settings["auto_run"])),
            pystray.MenuItem("Multi-step goal mode", post("goal_mode"), checked=lambda _: bool(self.settings["goal_mode"])),
            pystray.MenuItem("Stop current goal", post("stop_goal")),
            pystray.MenuItem("Hide pill when idle", post("hide_idle"), checked=lambda _: bool(self.settings["hide_when_idle"])),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open logs folder", post("logs")),
            pystray.MenuItem("Quit Jev Voice", post("quit")),
        )
        self.tray = pystray.Icon("jev-voice", image, "Jev Voice: hold Right Ctrl to talk", menu)
        self.tray.run_detached()

    def _is_own(self, hwnd: int) -> bool:
        try:
            return win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid()
        except Exception:
            return False

    def _track_foreground(self) -> None:
        try:
            hwnd = win32gui.GetForegroundWindow()
            if hwnd and not self._is_own(hwnd):
                if hwnd != self.last_external_hwnd:
                    try:
                        self.pill.set_app(*window_app(hwnd, self.theme.px(18), C["bg"]))
                    except Exception:
                        self.pill.set_app("", None)
                self.last_external_hwnd = hwnd
        finally:
            self.root.after(200, self._track_foreground)

    # ------------------------------------------------------------ hotkey
    def _key_press(self, key: object) -> None:
        if key == global_keyboard.Key.ctrl_r and not self.key_held:
            self.key_held = True
            self.key_chorded = False
            self.events.put(("start", None))
        elif self.key_held and key != global_keyboard.Key.ctrl_r and not self.key_chorded:
            self.key_chorded = True
            self.events.put(("cancel", None))

    def _key_release(self, key: object) -> None:
        if key == global_keyboard.Key.ctrl_r and self.key_held:
            self.key_held = False
            if not self.key_chorded:
                self.events.put(("stop", None))
            self.key_chorded = False

    # ------------------------------------------------------------ speech
    def _load_model(self) -> None:
        try:
            enable_cuda_dlls()
            from faster_whisper import WhisperModel
            for name, device, compute in ((self.gpu_model_name, "cuda", "float16"), (self.cpu_model_name, "cpu", "int8")):
                try:
                    model = WhisperModel(name, device=device, compute_type=compute, download_root=str(ROOT / ".voice-model-cache"))
                    # Model construction alone does not prove CUDA DLLs are
                    # usable; force one encoder pass before advertising GPU.
                    if device == "cuda":
                        segments, _ = model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1, language="en", vad_filter=False)
                        list(segments)
                    self.model = model
                    self.model_device = device
                    self.model_name = name
                    self.events.put(("model_ready", f"{name} on {device.upper()}"))
                    return
                except Exception:
                    if device == "cpu":
                        raise
        except Exception as error:
            self.events.put(("model_error", str(error)))

    def _level(self) -> float:
        return min(1.0, (self.rms / 0.05) ** 0.7) if self.recording else 0.0

    def start_recording(self) -> None:
        if self.recording:
            return
        if self.model is None:
            if self.model_device == "error":
                self.pill.show("error", title="Speech model failed to load", detail="Open activity for details")
            else:
                self.pill.show("hint", title="Speech model is still loading…")
            return
        if self.busy:
            return  # the pill is already showing progress for the previous command
        self.discard_pending()
        self.audio_blocks, self.samples, self.rms, self.caption = [], 0, 0.0, ""
        try:
            self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=self._audio_callback)
            self.stream.start()
        except Exception as error:
            self.pill.show("error", title="Microphone unavailable", detail=str(error))
            return
        self.recording = True
        self.take += 1
        hwnd = self.last_external_hwnd or win32gui.GetForegroundWindow()
        self.scene = (hwnd, self.capturer.submit(self._capture_scene, hwnd))
        self.partial_busy, self.partial_samples = False, 0
        take = self.take
        # Reveal after a beat so a Right Ctrl shortcut (cancelled by the next key) doesn't flash the pill.
        self.root.after(120, lambda: self.recording and self.take == take and self.pill.show("listening", caption=self.caption))
        self.root.after(400, lambda: self._partial_tick(take))

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info: object, status: object) -> None:
        block = indata[:, 0].copy()
        self.audio_blocks.append(block)
        self.samples += len(block)
        self.rms = float(np.sqrt(np.mean(block * block)))

    def _partial_tick(self, take: int) -> None:
        """Live captions: re-transcribe the growing buffer whenever the previous pass is done."""
        if not self.recording or take != self.take:
            return
        if not self.partial_busy and self.samples >= SAMPLE_RATE // 2 and self.samples - self.partial_samples >= SAMPLE_RATE // 4:
            audio = np.concatenate(list(self.audio_blocks))[-SAMPLE_RATE * 20:]
            self.partial_busy, self.partial_samples = True, self.samples
            self.worker.submit(self._partial, audio, take)
        self.root.after(120, lambda: self._partial_tick(take))

    def _partial(self, audio: np.ndarray, take: int) -> None:
        try:
            text = collapse_repeats(self._transcribe(audio, partial=True, hotwords=self._ready_hotwords()))
        except Exception:
            text = None
        self.events.put(("partial", (take, text)))

    def _stop_stream(self) -> None:
        self.recording = False
        self.rms = 0.0
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def stop_recording(self) -> None:
        if not self.recording:
            return
        self._stop_stream()
        audio = np.concatenate(self.audio_blocks) if self.audio_blocks else np.zeros(0, dtype=np.float32)
        self.audio_blocks = []
        if len(audio) < 4000:
            reason = "no_audio" if not len(audio) else "too_short"
            append_log(LOG, {"utterance_id": uuid.uuid4().hex, "event": "capture_failed", "transcript": "",
                             "audio_duration_s": round(len(audio) / SAMPLE_RATE, 2), "outcome": reason})
            self.pill.show("hint", title="No audio from the microphone" if reason == "no_audio" else "Hold Right Ctrl while you speak")
            return
        self.busy = True
        self.goal_stop.clear()
        self.goal_active = self.goal_mode.get()
        uid = uuid.uuid4().hex
        self._begin_entry(uid, self.caption, "microphone")
        self.pill.show("working", transcript=self.caption, step="Finishing transcription…", can_stop=self.goal_active)
        release_scene = None
        if self.goal_active:
            hwnd = self.last_external_hwnd or win32gui.GetForegroundWindow()
            release_scene = self.capturer.submit(self._capture_scene, hwnd)
        self.worker.submit(self._plan_audio, audio, self.scene, None, uid, self.goal_active, self.auto.get(), release_scene)

    def cancel_recording(self) -> None:
        if self.recording:
            self._stop_stream()
            self.audio_blocks = []
            append_log(LOG, {"utterance_id": uuid.uuid4().hex, "event": "capture_discarded", "transcript": "", "outcome": "right_ctrl_used_as_shortcut"})
            if self.pill.state == "listening":
                self.pill.show("idle")

    def plan_typed_command(self, utterance: str) -> None:
        if self.busy or self.recording:
            return
        self.discard_pending()
        self.busy = True
        self.goal_stop.clear()
        self.goal_active = self.goal_mode.get()
        uid = uuid.uuid4().hex
        self._begin_entry(uid, utterance, "typed_test")
        self.pill.show("working", transcript=utterance, step="Reading the screen…", can_stop=self.goal_active)
        hwnd = self.last_external_hwnd or win32gui.GetForegroundWindow()
        self.worker.submit(self._plan_audio, None, (hwnd, self.capturer.submit(self._capture_scene, hwnd)), utterance, uid,
                           self.goal_active, self.auto.get())

    def _capture_scene(self, hwnd: int) -> dict:
        """UIA state, app catalog, and Whisper hints for the window the user is talking to."""
        started = time.perf_counter()
        state, controls = capture(hwnd)
        refreshed = time.monotonic() - self.apps_cached_at > 60
        if refreshed:
            self.apps_cache = installed_apps()
            self.apps_cached_at = time.monotonic()
        return {"state": state, "controls": controls, "apps": self.apps_cache, "app_catalog_refreshed": refreshed,
                "capture_ms": round((time.perf_counter() - started) * 1000), "hotwords": asr_hotwords(state, controls)}

    def _ready_hotwords(self) -> str | None:
        future = self.scene[1] if self.scene else None
        if future is None or not future.done() or future.exception():
            return None
        return future.result()["hotwords"]

    def _transcribe(self, audio: np.ndarray, partial: bool = False, hotwords: str | None = None) -> str:
        # The final pass is the one that gets acted on, so it gets beam search;
        # captions stay greedy to keep up with speech.
        options = {"beam_size": 1 if partial else 5, "language": "en", "vad_filter": True, "condition_on_previous_text": False, "hotwords": hotwords}
        if partial:
            # Captions are disposable: no temperature-fallback retries (a looping
            # decode otherwise costs seconds) and a length cap tied to the audio.
            options.update(without_timestamps=True, temperature=0.0, max_new_tokens=int(len(audio) / SAMPLE_RATE * 8) + 8)
        try:
            segments, _ = self.model.transcribe(audio, **options)
            return " ".join(segment.text.strip() for segment in segments).strip()
        except Exception:
            if self.model_device != "cuda":
                raise
            from faster_whisper import WhisperModel
            self.model = WhisperModel(self.cpu_model_name, device="cpu", compute_type="int8", download_root=str(ROOT / ".voice-model-cache"))
            self.model_device = "cpu"
            self.model_name = self.cpu_model_name
            self.events.put(("model_ready", f"{self.model_name} on CPU (CUDA failed)"))
            segments, _ = self.model.transcribe(audio, **options)
            return " ".join(segment.text.strip() for segment in segments).strip()

    # ------------------------------------------------------------ planning and execution
    def _plan_audio(self, audio: np.ndarray | None, scene: tuple, typed: str | None = None, uid: str | None = None,
                    goal_mode: bool = False, auto_run: bool = True, release_scene: Future | None = None) -> None:
        record: dict = {"utterance_id": uid or uuid.uuid4().hex, "source": "typed_test" if typed is not None else "microphone", "audio_duration_s": round(len(audio) / SAMPLE_RATE, 2) if audio is not None else None, "asr_device": self.model_device if audio is not None else None, "timings_ms": {}, "outcome": "planning"}
        uid = record["utterance_id"]
        if audio is not None:
            try:
                record["audio_path"] = save_clip(audio, uid)
            except OSError as error:
                record["audio_path_error"] = str(error)
        try:
            # Capture started at key press; it usually finishes while the user is still talking,
            # so "state" is only the time planning actually waited for it.
            started = time.perf_counter()
            try:
                captured, capture_error = scene[1].result(), None
            except Exception as error:
                captured, capture_error = None, error
            record["timings_ms"]["state"] = round((time.perf_counter() - started) * 1000)
            hotwords = captured["hotwords"] if captured else None
            started = time.perf_counter()
            utterance = typed if typed is not None else self._transcribe(audio, hotwords=hotwords)
            record["timings_ms"]["transcribe"] = round((time.perf_counter() - started) * 1000) if audio is not None else None
            record["transcript"] = utterance
            record["asr_device"] = self.model_device if audio is not None else None
            record["asr_model"] = self.model_name if audio is not None else None
            record["asr_hotwords"] = hotwords if audio is not None else None
            self.events.put(("transcript", (uid, utterance)))
            if not utterance:
                raise RuntimeError("Speech was not recognized")
            if capture_error:
                raise capture_error
            state, controls, apps = captured["state"], captured["controls"], captured["apps"]
            record["app_catalog_refreshed"] = captured["app_catalog_refreshed"]
            record["timings_ms"]["state_capture"] = captured["capture_ms"]
            record["state_summary"] = {"activeWindow": state["activeWindow"], "openWindowCount": len(state["openWindows"]), "controlCount": state["controlCount"], "visitedNodes": state["visitedNodes"], "truncatedTraversal": state["truncatedTraversal"], "installedAppCount": len(apps)}
            # Replay inputs, the prompts in force, and (via plan_command) every
            # exact Jev request/response, so replay.py can vary prompts alone.
            record["inputs"] = snapshot_inputs(utterance, state, apps)
            record["prompts"] = goal.GOAL_PROMPTS if goal_mode else PROMPTS
            key = read_key(ROOT / ".env.openrouter")
            if goal_mode:
                current_captured = None
                if release_scene is not None:
                    started = time.perf_counter()
                    try:
                        current_captured = release_scene.result()
                        record["timings_ms"]["release_capture"] = current_captured["capture_ms"]
                    except Exception as error:
                        record["release_capture_error"] = f"{type(error).__name__}: {error}"
                    record["timings_ms"]["release_state_wait"] = round((time.perf_counter() - started) * 1000)
                self._run_goal(key, utterance, captured, record, auto_run, current_captured)
                return
            self.events.put(("step", (uid, "Choosing an action…")))

            def chosen(verb: str) -> None:
                if verb not in {"no_action"}:
                    self.events.put(("step", (uid, f"{VERB_NOW.get(verb) or 'Shortcut'}: finding the target…")))

            plan_command(key, utterance, state, controls, apps, record, progress=chosen)
            record["outcome"] = "planned"
            record["event"] = "plan"
            append_log(LOG, record)
            self.events.put(("plan", (record, state, controls, apps, utterance)))
        except Exception as error:
            record["outcome"] = "error"
            record["event"] = "error"
            record["error"] = f"{type(error).__name__}: {error}"
            append_log(LOG, record)
            self.events.put(("failed", record))

    def _run_goal(self, key: str, utterance: str, captured: dict, record: dict, auto_run: bool,
                  current_captured: dict | None = None) -> None:
        """Use the same goal loop as the live harness, starting from the speech-time capture."""
        uid = record["utterance_id"]
        record["mode"] = "goal"
        first = (captured["state"], captured["controls"], captured["apps"])
        current = ((current_captured["state"], current_captured["controls"], current_captured["apps"])
                   if current_captured else None)
        apps = (current or first)[2]

        def observe():
            hwnd = app_window(win32gui.GetForegroundWindow())
            if not hwnd or self._is_own(hwnd):
                hwnd = self.last_external_hwnd or first[0]["activeWindow"]["hwnd"]
            state, controls = capture_settled(hwnd)
            return state, controls, apps

        def click_through(enabled: bool) -> None:
            ready = threading.Event()
            self.events.put(("goal_click_through", (enabled, ready)))
            if not ready.wait(0.5) and enabled:
                raise RuntimeError("The goal overlay did not clear the screen in time")

        def act(verb, target, state, controls, available_apps, text):
            before = _top_level_picture()
            try:
                click_through(True)
                result = execute(verb, target, state, controls, available_apps, utterance, self.app_hwnd, text)
                waited = settle(verb, before)
                return f"{result} (settled in {waited} ms)"
            finally:
                click_through(False)

        def progress(phase, step):
            self.events.put(("goal_progress", (uid, phase, copy.deepcopy(step),
                                                [s.get("summary") for s in record["steps"] if s.get("summary")])))

        def allow_action(step, state, controls):
            if auto_run and not goal.action_requires_review(step, state, controls):
                return True
            decision = {"approved": False}
            ready = threading.Event()
            self.events.put(("goal_review", (uid, copy.deepcopy(step), decision, ready)))
            while not ready.wait(0.1):
                if self.goal_stop.is_set():
                    return False
            if decision["approved"] and not self.goal_stop.is_set():
                step["reviewed"] = True
                return True
            return False

        foreground = app_window(win32gui.GetForegroundWindow())
        started_window = (current or first)[0]["activeWindow"]
        refresh_first = bool(foreground and not self._is_own(foreground) and
                             (foreground != started_window["hwnd"] or
                              win32gui.GetWindowText(foreground) != started_window["title"]))

        outcome = goal.run_goal(key, utterance, observe, act, record, progress=progress,
                                should_stop=self.goal_stop.is_set,
                                allow_action=allow_action,
                                initial_observation=first, current_observation=current, refresh_first=refresh_first)
        record["event"] = "final"
        record["timings_ms"]["jev_total"] = sum(s.get("timings_ms", {}).get("jev", 0) for s in record["steps"])
        record["timings_ms"]["execute"] = sum(s.get("timings_ms", {}).get("execute", 0) for s in record["steps"])
        if outcome in {"error", "blocked", "stuck", "step_limit"}:
            record["error"] = next((s.get("error") or s.get("observe_error") or s.get("reason")
                                    for s in reversed(record["steps"])
                                    if s.get("error") or s.get("observe_error") or s.get("reason")),
                                   "Goal stopped before completion")
        append_log(LOG, record)
        self.events.put(("goal_finished", record))

    def stop_goal(self) -> None:
        if self.goal_active:
            self.goal_stop.set()
            if self.goal_review_pending:
                _, ready, _ = self.goal_review_pending
                ready.set()
                self.goal_review_pending = None
                self.pill.show("working", step="Stopping goal…", can_stop=False)
            else:
                self.pill.update(step="Stopping after the current step…")

    def execute_pending(self) -> None:
        if self.goal_review_pending:
            decision, ready, uid = self.goal_review_pending
            self.goal_review_pending = None
            decision["approved"] = True
            ready.set()
            self.pill.show("working", transcript=self.entries.get(uid, {}).get("transcript", ""),
                           step="Running the approved step…", can_stop=True)
            return
        if self.pending is None:
            return
        record, state, controls, apps, utterance = self.pending
        self.pending = None
        self.busy = True
        self.pill.show("working", transcript=utterance, step=f"{action_phrase(record['verb']['choice'], record['target'])}…")
        self.pill.set_click_through(True)
        self.worker.submit(self._execute, record, state, controls, apps, utterance)

    def _execute(self, record: dict, state: dict, controls: list, apps: list, utterance: str) -> None:
        start = time.perf_counter()
        try:
            foreground = win32gui.GetForegroundWindow()
            own = foreground if self._is_own(foreground) else self.app_hwnd
            result = execute(record["verb"]["choice"], record["target"], state, controls, apps, utterance, own)
            record["outcome"] = "executed"
            record["result"] = result
        except Exception as error:
            record["outcome"] = "execution_error"
            record["error"] = f"{type(error).__name__}: {error}"
        record["timings_ms"]["execute"] = round((time.perf_counter() - start) * 1000)
        record["event"] = "final"
        append_log(LOG, _without_replay_payload(record))
        self.events.put(("executed", record))

    def discard_pending(self) -> None:
        if self.pending is not None:
            record = self.pending[0]
            record["outcome"] = "discarded"
            record["event"] = "final"
            append_log(LOG, _without_replay_payload(record))
            self.pending = None
            self._update_entry(record)

    def cancel_pending(self) -> None:
        if self.goal_active:
            self.stop_goal()
            return
        self.discard_pending()
        self.pill.show("idle")

    # ------------------------------------------------------------ activity entries
    def _begin_entry(self, uid: str, transcript: str, source: str) -> None:
        entry = entry_from_record({"utterance_id": uid, "transcript": transcript, "source": source, "outcome": "planning"},
                                  when=datetime.now().strftime("%H:%M:%S"))
        self.entries[uid] = entry
        self.panel.upsert(entry)

    def _update_entry(self, record: dict) -> None:
        uid = record["utterance_id"]
        when = self.entries.get(uid, {}).get("time")
        entry = entry_from_record(record, when=when or datetime.now().strftime("%H:%M:%S"))
        self.entries[uid] = entry
        self.panel.upsert(entry)

    # ------------------------------------------------------------ event loop
    def _poll(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "start":
                self.start_recording()
            elif kind == "stop":
                self.stop_recording()
            elif kind == "cancel":
                self.cancel_recording()
            elif kind == "partial":
                take, text = value
                if take == self.take:
                    self.partial_busy = False
                    if self.recording and text:
                        self.caption = text
                        if self.pill.state == "listening":
                            self.pill.update(caption=text)
            elif kind == "model_ready":
                self.pill.set_model("ready")
                self.panel.set_status(str(value))
            elif kind == "model_error":
                self.model_device = "error"
                self.pill.set_model("error")
                self.panel.set_status(f"Speech model failed: {value}")
            elif kind == "transcript":
                uid, text = value
                if uid in self.entries:
                    self.entries[uid]["transcript"] = text
                    self.panel.upsert(self.entries[uid])
                if self.pill.state == "working":
                    self.pill.update(transcript=text, step="Reading the screen…")
            elif kind == "step":
                if self.pill.state == "working":
                    self.pill.update(step=value[1])
            elif kind == "goal_click_through":
                enabled, ready = value
                self.pill.set_click_through(enabled)
                ready.set()
            elif kind == "goal_review":
                uid, step, decision, ready = value
                if self.goal_stop.is_set() or not self.goal_active:
                    ready.set()
                    continue
                self.goal_review_pending = (decision, ready, uid)
                phrase = action_phrase(step["verb"]["choice"], step.get("target"))
                if uid in self.entries:
                    self.entries[uid]["status"] = "review"
                    self.entries[uid]["next_step"] = phrase
                    self.panel.upsert(self.entries[uid])
                self.pill.show("review", title=phrase, detail=self.entries.get(uid, {}).get("transcript", ""))
            elif kind == "goal_progress":
                uid, phase, step, actions = value
                if uid in self.entries:
                    self.entries[uid]["actions"] = actions
                    self.entries[uid]["mode"] = "goal"
                    self.entries[uid]["status"] = "working"
                    self.entries[uid]["next_step"] = None
                    self.panel.upsert(self.entries[uid])
                if self.pill.state == "working" and not self.goal_stop.is_set():
                    if phase == "acting":
                        phrase = action_phrase(step["verb"]["choice"], step.get("target"))
                        label = f"Step {step['index']}: {phrase}…"
                    elif phase == "acted":
                        label = f"Step {step['index']} complete; checking the result…"
                    else:
                        label = f"Step {step['index']}: choosing the next action…"
                    self.pill.update(step=label)
            elif kind == "goal_finished":
                self.goal_active = False
                self.goal_review_pending = None
                self.busy = False
                self.pill.set_click_through(False)
                self._update_entry(value)
                outcome = value["outcome"]
                count = value.get("step_count", 0)
                if outcome == "done":
                    self.pill.show("done", title=f"Goal reached in {count} step{'s' if count != 1 else ''}", detail=value["transcript"])
                elif outcome == "cancelled":
                    self.pill.show("hint", title=f"Stopped after {count} steps")
                elif outcome == "review_required":
                    self.pill.show("hint", title="Stopped before a sensitive action", detail="Review the next step in Activity")
                else:
                    self.pill.show("error", title=f"Goal stopped: {outcome.replace('_', ' ')}", detail=value.get("error", ""))
            elif kind == "plan":
                record, state, controls, apps, utterance = value
                self.pending = value
                self.busy = False
                self._update_entry(record)
                verb = record["verb"]["choice"]
                if should_auto_run(self.auto.get(), verb):
                    self.execute_pending()
                else:
                    self.pill.show("review", title=action_phrase(verb, record["target"]), detail=utterance)
            elif kind == "failed":
                self.goal_active = False
                self.goal_review_pending = None
                self.busy = False
                self._update_entry(value)
                self.pill.show("error", title=friendly_error(value.get("error")), detail=value.get("transcript") and f"“{value['transcript']}”")
            elif kind == "executed":
                self.busy = False
                self.pill.set_click_through(False)
                self._update_entry(value)
                detail = f"“{value.get('transcript', '')}”"
                if value["outcome"] == "executed":
                    self.pill.show("done", title=action_phrase(value["verb"]["choice"], value["target"], past=True), detail=detail)
                else:
                    self.pill.show("error", title=friendly_error(value.get("error")), detail=detail)
            elif kind == "ui":
                {"panel": self.toggle_panel, "auto": lambda: self.auto.set(not self.auto.get()),
                 "goal_mode": lambda: self.goal_mode.set(not self.goal_mode.get()), "stop_goal": self.stop_goal,
                 "hide_idle": lambda: self.hide_idle.set(not self.hide_idle.get()),
                 "logs": self.open_logs, "quit": self._close}[value]()
                if value == "quit":
                    return
        self.root.after(40, self._poll)

    def _close(self) -> None:
        self.goal_stop.set()
        if self.goal_review_pending:
            self.goal_review_pending[1].set()
        self.key_listener.stop()
        if self.tray is not None:
            self.tray.stop()
        if self.stream is not None:
            self._stop_stream()
        self.discard_pending()
        self.worker.shutdown(wait=False, cancel_futures=True)
        self.capturer.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    mutex = win32event.CreateMutex(None, False, "Local\\JevVoiceControl")
    if win32api.GetLastError() == 183:  # ERROR_ALREADY_EXISTS: two instances would both act on Right Ctrl
        ctypes.windll.user32.MessageBoxW(0, "Jev Voice is already running. Look for the pill at the bottom of the "
                                            "screen or the icon in the system tray.", "Jev Voice", 0x40)
        return
    try:
        VoiceApp().run()
    finally:
        win32api.CloseHandle(mutex)


if __name__ == "__main__":
    main()
