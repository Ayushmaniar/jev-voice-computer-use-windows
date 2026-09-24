import os
import tempfile
import unittest
import tkinter as tk
from queue import Queue
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pynput import keyboard
import win32gui

import copy
import json

import numpy as np

from .app import VoiceApp, _without_replay_payload, recent_entries
from . import overlay
from . import app, core, replay
from . import windows as window_backend
from .core import (SURFACE_ID, Control, MAX_CHOICES, VERBS, append_log, describe_control, dictation_text,
                   eligible_targets, reference_hint, restore_inputs, should_auto_run)


class FakeJev:
    """Offline stand-in for requests.post: answers each question from a script and keeps the bodies."""

    def __init__(self, answers):
        self.answers = answers
        self.bodies = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.bodies.append(json)
        question = next(iter(json["questions"]))
        choice, probability = self.answers[question]
        body = {"answers": {question: {"choice": choice, "probabilities": {choice: probability}}}}
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: body)


class ReplayLoggingTests(unittest.TestCase):
    def setUp(self):
        self.controls = [Control("c0000", "Kavya Rao", "ListItem", (306, 443, 870, 526), "Slack > DMs"),
                         Control("c0001", "Search", "Button", (500, 10, 700, 40), "Slack > Toolbar")]
        active = {"hwnd": 7, "title": "DMs - Slack", "process": "slack.exe"}
        self.state = {"activeWindow": active, "openWindows": [active],
                      "controls": [{"id": c.id, "role": c.role, "name": c.name, "rect": c.rect, "path": c.path, "enabled": c.enabled} for c in self.controls]}
        self.apps = [{"name": "Slack", "path": r"C:\private\Slack.lnk"}]

    def logged_record(self):
        """What the app writes for one planned command, after a JSON round trip."""
        record = {"utterance_id": "u1", "event": "plan", "outcome": "planned", "transcript": "go to Cavia Rao",
                  "inputs": core.snapshot_inputs("go to Cavia Rao", self.state, self.apps), "prompts": core.PROMPTS}
        with patch.object(core.requests, "post", FakeJev({"verb": ("left_click", 0.9), "target": ("c0000", 0.9)})):
            core.plan_command("secret-key", "go to Cavia Rao", self.state, self.controls, self.apps, record)
        return json.loads(json.dumps(record, default=str))

    def test_trace_stores_exact_request_without_key(self):
        record = self.logged_record()
        self.assertEqual([c["question"] for c in record["jev_calls"]], ["verb", "target"])
        self.assertEqual(record["jev_calls"][0]["request"]["state"]["task"], "go to Cavia Rao")
        self.assertNotIn("secret-key", json.dumps(record))
        self.assertNotIn(r"C:\private", json.dumps(record["inputs"]))

    def test_failed_call_is_still_logged(self):
        def failing(url, headers=None, json=None, timeout=None):
            def fail():
                raise RuntimeError("HTTP 500")
            return SimpleNamespace(status_code=500, raise_for_status=fail)
        record = {}
        with patch.object(core.requests, "post", failing), self.assertRaises(RuntimeError):
            core.plan_command("k", "scroll down", self.state, self.controls, self.apps, record)
        self.assertEqual(record["jev_calls"][0]["http_status"], 500)
        self.assertIn("request", record["jev_calls"][0])

    def test_logged_state_rebuilds_identical_requests(self):
        ok, detail = replay.fidelity_check(self.logged_record())
        self.assertTrue(ok, detail)

    def test_prompt_change_leaves_state_untouched(self):
        record = self.logged_record()
        utterance, state, controls, apps = core.restore_inputs(record["inputs"])
        changed = copy.deepcopy(record["prompts"])
        changed["verb"] = changed["target"] = "Different instructions."
        fake = FakeJev({"verb": ("left_click", 0.9), "target": ("c0000", 0.9)})
        with patch.object(core.requests, "post", fake):
            core.plan_command("k", utterance, state, controls, apps, {}, changed)

        def strip(body):
            body = copy.deepcopy(body)
            for question in body["questions"].values():
                question.pop("instructions")
            return replay.canonical(body)

        self.assertEqual([strip(b) for b in fake.bodies], [strip(c["request"]) for c in record["jev_calls"]])
        self.assertNotEqual(fake.bodies[0]["questions"]["verb"]["instructions"], record["jev_calls"][0]["request"]["questions"]["verb"]["instructions"])

    def test_tampered_state_fails_fidelity(self):
        record = self.logged_record()
        record["inputs"]["controls"][0]["name"] = "Someone Else"
        self.assertFalse(replay.fidelity_check(record)[0])

    def test_final_record_omits_replay_payload(self):
        final = _without_replay_payload({"utterance_id": "u1", "outcome": "executed", "inputs": {}, "prompts": {}, "jev_calls": []})
        self.assertEqual(final, {"utterance_id": "u1", "outcome": "executed"})


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.controls = [
            Control("c0", "Home", "Hyperlink", (0, 0, 90, 30), "Navigation"),
            Control("c1", "Notifications", "Hyperlink", (0, 40, 90, 70), "Navigation"),
            Control("c2", "Chat", "Hyperlink", (0, 80, 90, 110), "Navigation"),
            Control("c3", "Search", "Edit", (200, 0, 400, 30), "Toolbar"),
        ]

    def test_first_link_follows_tree_order(self):
        self.assertEqual(reference_hint("open the first link", self.controls), "c0")

    def test_spatial_below_uses_bounds_and_role_independence(self):
        self.assertEqual(reference_hint("the button below Notifications button", self.controls), "c2")

    def test_spatial_ambiguity_fails_closed(self):
        controls = self.controls + [Control("c4", "Other", "Button", (0, 81, 90, 111), "Navigation")]
        self.assertIsNone(reference_hint("button below Notifications", controls))

    def test_role_filter(self):
        left = eligible_targets("left_click", self.controls, [], [], 1)
        double = eligible_targets("double_click", self.controls, [], [], 1)
        edit = eligible_targets("type_text", self.controls, [], [], 1)
        hover = eligible_targets("hover", self.controls, [], [], 1)
        chords = eligible_targets("key_chord", self.controls, [], [], 1)
        self.assertEqual(len(left), 5)
        # A sparse window also offers its content area to double- and right-clicks, never to left clicks.
        self.assertEqual([item["id"] for item in double][-1], SURFACE_ID)
        self.assertEqual(len(double), 5)
        self.assertEqual([item["id"] for item in edit], ["none", "c3"])
        self.assertEqual(len(hover), 5)
        self.assertIn("ch_back", [item["id"] for item in chords])

    def test_fields_include_search_boxes_and_spinners(self):
        controls = [Control("c0", "Where from?", "ComboBox", (0, 0, 9, 9), "", state="expanded, focused"),
                    Control("c1", "Hours", "Spinner", (0, 10, 9, 19), ""), Control("c2", "OK", "Button", (0, 20, 9, 29), "")]
        self.assertEqual([t["id"] for t in eligible_targets("type_text", controls, [], [], 1)], ["none", "c0", "c1"])

    def test_surface_target_only_on_sparse_windows_without_open_menu(self):
        busy = [Control(f"c{i}", str(i), "Button", (0, i, 20, i + 1), "") for i in range(40)]
        self.assertNotIn(SURFACE_ID, [t["id"] for t in eligible_targets("right_click", busy, [], [], 1)])
        self.assertIn(SURFACE_ID, [t["id"] for t in eligible_targets("right_click", [], [], [], 1)])
        menu = [Control("c0", "Subtitle", "MenuItem", (0, 0, 9, 9), "Open menu")]
        self.assertNotIn(SURFACE_ID, [t["id"] for t in eligible_targets("right_click", menu, [], [], 1)])

    def test_detail_is_described_and_restored(self):
        card = Control("c0", "Text-to-Image • 7B", "Hyperlink", (0, 0, 9, 9), "", detail="Qwen/Qwen-Image-2.1")
        self.assertIn('(shows "Qwen/Qwen-Image-2.1")', describe_control(card))
        inputs = {"utterance": "x", "activeWindow": {}, "openWindows": [], "installedApps": [],
                  "controls": [{"id": "c0", "name": card.name, "role": card.role, "rect": [0, 0, 9, 9], "path": "",
                                "enabled": True, "detail": card.detail}]}
        self.assertEqual(restore_inputs(inputs)[2][0].detail, "Qwen/Qwen-Image-2.1")

    def test_no_silent_truncation(self):
        controls = [Control(f"c{i}", str(i), "Button", (0, i, 20, i + 1), "") for i in range(MAX_CHOICES)]
        targets = eligible_targets("left_click", controls, [], [], 1)
        self.assertEqual(len(targets), MAX_CHOICES + 1)
        self.assertEqual(targets[-1]["id"], f"c{MAX_CHOICES-1}")

    def test_auto_run_covers_every_executable_verb(self):
        for verb in VERBS:
            self.assertEqual(should_auto_run(True, verb), verb != "no_action", verb)
            self.assertFalse(should_auto_run(False, verb), verb)
        self.assertFalse(should_auto_run(True, "unknown_verb"))

    def test_dictation_and_log(self):
        self.assertEqual(dictation_text("type Hello, world!"), "Hello, world!")
        self.assertIsNone(dictation_text("open a link"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.jsonl"
            append_log(path, {"transcript": "hello", "outcome": "discarded"})
            text = path.read_text(encoding="utf-8")
            self.assertIn('"transcript": "hello"', text)
            self.assertIn('"timestamp":', text)

    def test_right_ctrl_alone_and_shortcut_cancellation(self):
        fake = SimpleNamespace(key_held=False, key_chorded=False, events=Queue())
        VoiceApp._key_press(fake, keyboard.Key.ctrl_r)
        self.assertEqual(fake.events.get_nowait()[0], "start")
        VoiceApp._key_release(fake, keyboard.Key.ctrl_r)
        self.assertEqual(fake.events.get_nowait()[0], "stop")
        VoiceApp._key_press(fake, keyboard.Key.ctrl_l)
        self.assertTrue(fake.events.empty())
        VoiceApp._key_press(fake, keyboard.Key.ctrl_r)
        self.assertEqual(fake.events.get_nowait()[0], "start")
        VoiceApp._key_press(fake, keyboard.KeyCode.from_char("c"))
        self.assertEqual(fake.events.get_nowait()[0], "cancel")
        VoiceApp._key_release(fake, keyboard.Key.ctrl_r)
        self.assertTrue(fake.events.empty())

    def test_reviewed_execute_refocuses_its_original_window(self):
        state = {"activeWindow": {"hwnd": 123}}
        with (patch.object(window_backend.win32gui, "GetForegroundWindow", side_effect=[456, 123, 123]),
              patch.object(window_backend.win32gui, "IsWindow", return_value=True),
              patch.object(window_backend.win32gui, "ShowWindow") as show,
              patch.object(window_backend.win32gui, "SetForegroundWindow") as focus):
            self.assertEqual(window_backend.execute("minimize_window", {"id": "current"}, state, [], [], "minimize", app_hwnd=456), "minimized current window")
            focus.assert_called_once_with(123)
            self.assertEqual(show.call_count, 2)

    def test_close_window_can_target_a_named_background_window(self):
        windows = [{"hwnd": 123, "title": "Claude", "process": "claude.exe"}, {"hwnd": 789, "title": "VALORANT", "process": "valorant.exe"}]
        self.assertEqual([t["id"] for t in eligible_targets("close_window", [], windows, [], 123)], ["none", "current", "w789"])
        with (patch.object(window_backend, "open_windows", return_value=windows),
              patch.object(window_backend.win32gui, "GetForegroundWindow", return_value=123),
              patch.object(window_backend.win32gui, "GetWindowText", return_value="VALORANT"),
              patch.object(window_backend.win32gui, "PostMessage") as post):
            self.assertEqual(window_backend.execute("close_window", {"id": "w789"}, {"activeWindow": {"hwnd": 123}}, [], [], "close valorant"), 'requested window "VALORANT" close')
            post.assert_called_once_with(789, window_backend.win32con.WM_CLOSE, 0, 0)

    def test_switch_window_retries_with_alt_when_focus_is_locked(self):
        denied = window_backend.win32gui.error(0, "SetForegroundWindow", "No error message is available")
        with (patch.object(window_backend, "open_windows", return_value=[{"hwnd": 789}]),
              patch.object(window_backend.win32gui, "GetForegroundWindow", side_effect=[123, 789]),
              patch.object(window_backend.win32gui, "ShowWindow"),
              patch.object(window_backend.win32gui, "SetForegroundWindow", side_effect=[denied, None]) as focus,
              patch.object(window_backend.win32api, "keybd_event") as key):
            self.assertEqual(window_backend.execute("switch_window", {"id": "w789"}, {"activeWindow": {"hwnd": 123}}, [], [], "switch"), "activated window 789")
            self.assertEqual(focus.call_count, 2)
            self.assertEqual(key.call_count, 2)

    def test_tk_outer_window_id_only_after_mapping(self):
        root = tk.Tk()
        try:
            root.update_idletasks()
            root.update()
            self.assertEqual(int(root.wm_frame(), 16), win32gui.GetAncestor(root.winfo_id(), 2))
        finally:
            root.destroy()


class OverlayTextTests(unittest.TestCase):
    def test_action_phrases(self):
        control = {"id": "c1", "kind": "control", "description": 'ListItem "Blue Folder" at (1, 2, 3, 4), under Explorer'}
        self.assertEqual(overlay.action_phrase("double_click", control, past=True), "Double-clicked “Blue Folder”")
        self.assertEqual(overlay.action_phrase("left_click", control), "Click “Blue Folder”")
        self.assertEqual(overlay.action_phrase("scroll_down", {"id": "current", "kind": "current"}, past=True), "Scrolled down")
        self.assertEqual(overlay.action_phrase("key_chord", {"id": "ch_next_tab", "kind": "chord", "key": "next_tab"}), "Next tab")
        self.assertEqual(overlay.action_phrase("press_key", {"id": "kEscape", "kind": "key", "key": "Escape"}, past=True), "Pressed Escape")

    def test_friendly_errors(self):
        self.assertEqual(overlay.friendly_error("RuntimeError: No sufficiently confident target"), "Couldn't find that on screen")
        self.assertEqual(overlay.friendly_error("RuntimeError: Speech was not recognized"), "Didn't catch that")
        self.assertEqual(overlay.friendly_error("ValueError: odd thing"), "odd thing")

    def test_captions_keep_the_newest_words(self):
        text = " ".join(f"w{i}" for i in range(30))
        lines = overlay.fit_tail(text, len, 20, 2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("… "))
        self.assertTrue(lines[-1].endswith("w29"))
        self.assertTrue(all(len(line) <= 20 for line in lines))
        self.assertEqual(overlay.fit_tail("open the blue", len, 20, 2), ["open the blue"])

    def test_history_merges_plan_and_final_records(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.jsonl"
            target = {"id": "c1", "kind": "control", "description": 'Button "Send" at (1, 2, 3, 4), under Chat', "probability": 0.9}
            append_log(path, {"utterance_id": "a", "event": "plan", "outcome": "planned", "transcript": "click send",
                              "verb": {"choice": "left_click", "probabilities": {"left_click": 0.8}}, "target": target,
                              "timings_ms": {"transcribe": 400, "state": 300, "jev_total": 900}})
            append_log(path, {"utterance_id": "a", "event": "final", "outcome": "executed", "transcript": "click send",
                              "verb": {"choice": "left_click", "probabilities": {"left_click": 0.8}}, "target": target,
                              "timings_ms": {"transcribe": 400, "state": 300, "jev_total": 900, "execute": 50}})
            append_log(path, {"utterance_id": "b", "event": "capture_failed", "outcome": "too_short"})
            with path.open("a", encoding="utf-8") as file:
                file.write("not json\n")
            entries = recent_entries(path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "done")
        self.assertEqual(overlay.entry_title(entries[0]), "Clicked “Send”")
        self.assertEqual(entries[0]["timings"]["execute"], 50)


class SpeechHintTests(unittest.TestCase):
    def test_hotwords_put_on_screen_names_after_command_words(self):
        controls = [Control("c0", "Line up", "Button", (0, 0, 1, 1), ""),
                    Control("c1", "Send", "Button", (0, 0, 1, 1), ""),
                    Control("c2", "Marisol Grey", "ListItem", (0, 0, 1, 1), ""),
                    Control("c3", "Documents (pinned)", "ListItem", (0, 0, 1, 1), ""),
                    Control("c4", "are you coming to the meeting later today or not", "ListItem", (0, 0, 1, 1), ""),
                    Control("c5", "marisol grey", "Hyperlink", (0, 0, 1, 1), "")]
        text = core.asr_hotwords({"openWindows": [{"title": "DMs - Northwind - Slack"}]}, controls)
        self.assertTrue(text.startswith(core.ASR_COMMAND_WORDS))
        names = text[len(core.ASR_COMMAND_WORDS):]
        self.assertLess(names.index("Marisol Grey"), names.index("Send"))  # list items before buttons
        self.assertEqual(names.lower().count("marisol grey"), 1)
        self.assertIn("Documents,", names)
        self.assertIn("Slack", names)
        self.assertNotIn("Line up", names)
        self.assertNotIn("meeting", names)

    def test_hotwords_respect_length_limit(self):
        controls = [Control(f"c{i}", f"Person Number{i}", "ListItem", (0, 0, 1, 1), "") for i in range(200)]
        self.assertLessEqual(len(core.asr_hotwords({"openWindows": []}, controls)), 700)

    def test_save_clip_round_trips_audio(self):
        import tempfile, wave
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "HOME", Path(folder)), patch.object(app, "AUDIO_DIR", Path(folder) / "audio"):
            path = app.save_clip(np.array([0.0, 0.5, -1.5], dtype=np.float32), "abc")
            with wave.open(str(Path(folder) / path)) as file:
                self.assertEqual(file.getframerate(), app.SAMPLE_RATE)
                self.assertEqual(np.frombuffer(file.readframes(3), dtype=np.int16).tolist(), [0, 16383, -32767])

    def test_hotwords_keep_open_windows_when_controls_fill_the_limit(self):
        controls = [Control(f"c{i}", f"Person Number{i}", "ListItem", (0, 0, 1, 1), "") for i in range(200)]
        self.assertIn("Riot Client", core.asr_hotwords({"openWindows": [{"title": "Riot Client"}]}, controls))

    def test_collapse_repeats(self):
        self.assertEqual(core.collapse_repeats("Open the blue. Open the blue. Open the blue."), "Open the blue.")
        self.assertEqual(core.collapse_repeats("Scroll down. Then scroll up."), "Scroll down. Then scroll up.")
        self.assertEqual(core.collapse_repeats(""), "")


class TargetGateTests(unittest.TestCase):
    def test_clear_winner_below_half_is_accepted(self):
        # Hollow Knight run: 0.48 on the video link, 0.14 on "none", 0.12 on the search box
        answer = {"choice": "c0098", "probabilities": {"c0098": 0.48, "none": 0.14, "c0036": 0.12, "c0008": 0.08}}
        self.assertEqual(core.runner_up(answer), {"id": "none", "probability": 0.14})
        self.assertTrue(core.target_accepted(0.48, core.runner_up(answer)))

    def test_close_race_is_rejected(self):
        self.assertFalse(core.target_accepted(0.45, {"id": "c2", "probability": 0.3}))

    def test_floor_and_absolute_thresholds(self):
        self.assertFalse(core.target_accepted(0.31, None))
        self.assertTrue(core.target_accepted(0.5, {"id": "c2", "probability": 0.45}))
        self.assertFalse(core.target_accepted(None, None))

    def test_planner_uses_margin_gate(self):
        control = Control("c0000", "Hollow Knight: Silksong (Original Soundtrack)", "Hyperlink", (1, 2, 3, 4), "YouTube")
        active = {"hwnd": 7, "title": "YouTube - Google Chrome", "process": "chrome.exe"}
        state = {"activeWindow": active, "openWindows": [active], "controls": []}
        with patch.object(core.requests, "post", FakeJev({"verb": ("left_click", 0.96), "target": ("c0000", 0.48)})):
            record = {}
            core.plan_command("k", "play the Hollow Knight soundtrack", state, [control], [], record)
        self.assertEqual(record["target"]["id"], "c0000")


class ProviderTests(unittest.TestCase):
    def ask(self, key):
        urls, fake = [], FakeJev({"verb": ("scroll_down", 0.9)})

        def post(url, headers=None, json=None, timeout=None):
            urls.append((url, headers["Authorization"]))
            return fake(url, headers, json, timeout)
        with patch.object(core.requests, "post", post):
            core.choose_verb(key, "scroll down", {"activeWindow": {}})
        return urls[0], fake.bodies[0]["model"]

    def test_openrouter_key_goes_to_openrouter(self):
        (url, auth), model = self.ask("sk-or-v1-abc")
        self.assertEqual((url, auth, model), (core.ENDPOINT, "Bearer sk-or-v1-abc", "~typesafe/jev-latest"))

    def test_typesafe_key_goes_to_typesafe(self):
        (url, auth), model = self.ask("ts-abc")
        self.assertEqual((url, auth, model), ("https://api.typesafe.ai/v1/systemone", "Bearer ts-abc", "jev-latest"))

    def test_read_key_accepts_either_provider(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {}, clear=True):
            root = Path(tmp)
            with self.assertRaises(RuntimeError):
                core.read_key(root / ".env.openrouter")
            (root / ".env.typesafe").write_text('TYPESAFE_API_KEY="ts-file"\n', encoding="utf-8")
            self.assertEqual(core.read_key(root / ".env.openrouter"), "ts-file")
            (root / ".env.openrouter").write_text("OPENROUTER_API_KEY=sk-or-file\n", encoding="utf-8")
            self.assertEqual(core.read_key(root / ".env.openrouter"), "sk-or-file")
            with patch.dict("os.environ", {"TYPESAFE_API_KEY": "ts-env"}):
                self.assertEqual(core.read_key(root / ".env.openrouter"), "ts-env")

    def test_save_key_replaces_the_other_provider(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {}, clear=True):
            path = Path(tmp) / "new" / ".env.openrouter"
            self.assertEqual(core.save_key(path, " sk-or-one \n"), path)
            self.assertEqual(core.read_key(path), "sk-or-one")
            self.assertEqual(core.save_key(path, "ts-two"), path.with_name(".env.typesafe"))
            self.assertFalse(path.exists())
            self.assertEqual(core.read_key(path), "ts-two")
            core.save_key(path, "sk-or-three")
            self.assertFalse(path.with_name(".env.typesafe").exists())
            self.assertEqual(core.read_key(path), "sk-or-three")

    def test_keys_in_env_files(self):
        from . import configure
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("# OPENROUTER_API_KEY=sk-or-commented\nexport TYPESAFE_API_KEY='ts-quoted'\n"
                           "OPENAI_API_KEY=sk-or-v1-other # via OpenRouter\nOPENAI_API_KEY=sk-openai\n"
                           "OPENROUTER_API_KEY=\nEMPTY=\"\"\n", encoding="utf-8")
            self.assertEqual(configure.keys_in_file(env), ["ts-quoted", "sk-or-v1-other"])
            self.assertEqual(configure.keys_in_file(Path(tmp) / "missing"), [])

    def test_find_keys_orders_saved_env_then_newest_file(self):
        from . import configure
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"TYPESAFE_API_KEY": "ts-env"}, clear=True):
            root = Path(tmp)
            saved = root / "home" / ".env.openrouter"
            core.save_key(saved, "sk-or-saved")
            old, new = root / "p1" / ".env", root / "p2" / ".env.local"
            for path, key in ((old, "sk-or-old"), (new, "sk-or-new")):
                path.parent.mkdir()
                path.write_text(f"OPENROUTER_API_KEY={key}\nOTHER=sk-or-saved\n", encoding="utf-8")
            os.utime(old, (1, 1))
            with patch.object(configure, "KEY_FILE", saved), \
                    patch.object(configure, "search_folders", lambda: [root / "p1", root / "p2"]):
                found = configure.find_keys()
        self.assertEqual([f["key"] for f in found], ["sk-or-saved", "ts-env", "sk-or-new", "sk-or-old"])
        self.assertEqual(found[1]["source"], "environment variable TYPESAFE_API_KEY")
        self.assertEqual(configure.mask("sk-or-v1-abcdef123456"), "sk-or-...3456")

    def test_openrouter_log_still_replays(self):
        tests = ReplayLoggingTests()
        tests.setUp()
        record = {"inputs": core.snapshot_inputs("go to Cavia Rao", tests.state, tests.apps), "prompts": core.PROMPTS}
        with patch.object(core.requests, "post", FakeJev({"verb": ("left_click", 0.9), "target": ("c0000", 0.9)})):
            core.plan_command("sk-or-v1-abc", "go to Cavia Rao", tests.state, tests.controls, tests.apps, record)
        record = json.loads(json.dumps(record, default=str))
        self.assertEqual(record["jev_calls"][0]["request"]["model"], core.MODEL)
        ok, detail = replay.fidelity_check(record)
        self.assertTrue(ok, detail)


if __name__ == "__main__":
    unittest.main()
