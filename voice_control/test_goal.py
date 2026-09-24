"""Offline tests for the multi-step loop and its voice safety boundary."""

import unittest
import json
import re
import queue
import threading
import tempfile
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from . import app, core, goal, goal_eval, goal_probe, goal_replay, goal_sim, overlay, windows
from .core import Control


def screen(title, controls=()):
    active = {"hwnd": 10, "title": title, "process": "test.exe"}
    return {"activeWindow": active, "openWindows": [active], "texts": [], "controls": [],
            "controlCount": len(controls), "visitedNodes": len(controls),
            "truncatedTraversal": False}, list(controls), []


class GoalLoopTests(unittest.TestCase):
    def test_simulated_text_goal_requires_the_exact_literal(self):
        case = next(c for c in json.loads(goal_sim.CASES.read_text(encoding="utf-8"))
                    if c["id"] == "notepad_message")

        def plan(_key, _goal, _steps, _initial, state, _controls, _apps, step, _prompts):
            if state["activeWindow"]["process"] == "explorer.exe":
                step["verb"] = {"choice": "launch_app"}
                step["target"] = {"id": "a0", "kind": "app", "description": "Notepad"}
            else:
                step["verb"] = {"choice": "type_text"}
                step["target"] = {"id": "editor", "kind": "control", "description": "Text editor"}
                step["text"] = "wrong text"
            step["timings_ms"] = {}
            return "act"

        with patch.object(goal, "plan_step", plan):
            result = goal_sim.run_case("key", case)
        self.assertFalse(result["passed"])
        self.assertEqual(result["outcome"], "error")
        self.assertIn("did not match", result["reason"])

    def test_stop_recording_starts_release_capture_for_goal_mode(self):
        voice = app.VoiceApp.__new__(app.VoiceApp)
        voice.settler = Mock()  # the Settler follows live windows; these tests fake the goal loop
        voice.recording = True
        voice.stream = None
        voice.audio_blocks = [np.ones(5000, dtype=np.float32)]
        voice.goal_mode = Mock(get=Mock(return_value=True))
        voice.auto = Mock(get=Mock(return_value=True))
        voice.goal_stop = threading.Event()
        voice.caption = "open Gmail"
        voice.pill = Mock()
        voice.worker = Mock()
        voice.capturer = Mock()
        voice.scene = (10, Future())
        voice.last_external_hwnd = 10
        release = Future()
        voice.capturer.submit.return_value = release
        with patch.object(voice, "_begin_entry"):
            voice.stop_recording()
        self.assertEqual(voice.capturer.submit.call_args.args[1], 10)
        submitted = voice.worker.submit.call_args.args
        self.assertEqual(submitted[2], voice.scene)
        self.assertIs(submitted[-1], release)

    def test_release_observation_updates_first_state_and_keeps_origin(self):
        started = screen("Browser loading")
        current = screen("Browser loading", [Control("link", "Gmail", "Hyperlink", (0, 0, 100, 20), "Browser")])
        seen = []

        def plan(_key, _goal, _steps, initial, state, controls, _apps, _step, _prompts):
            seen.append((initial["activeWindow"]["title"], state["activeWindow"]["title"],
                         [control.name for control in controls]))
            return "done"

        with patch.object(goal, "plan_step", plan):
            outcome = goal.run_goal("key", "open Gmail", lambda: self.fail("unneeded capture"),
                                    lambda *_args: self.fail("unneeded action"), {},
                                    initial_observation=started, current_observation=current)
        self.assertEqual(outcome, "done")
        self.assertEqual(seen, [("Browser loading", "Browser loading", ["Gmail"])])

    def test_voice_planner_forwards_release_capture_to_goal(self):
        initial_state, initial_controls, apps = screen("New Tab")
        current_state, current_controls, _ = screen("New Tab", [Control("gmail", "Gmail", "Hyperlink",
                                                                         (0, 0, 100, 20), "New Tab")])
        def captured(state, controls):
            return {"state": state, "controls": controls, "apps": apps, "hotwords": "Gmail",
                    "capture_ms": 12, "app_catalog_refreshed": False}
        first, release = Future(), Future()
        first.set_result(captured(initial_state, initial_controls))
        release.set_result(captured(current_state, current_controls))
        voice = app.VoiceApp.__new__(app.VoiceApp)
        voice.settler = Mock()  # the Settler follows live windows; these tests fake the goal loop
        voice.events = queue.Queue()
        voice.model_device = "cpu"
        voice.model_name = "base.en"
        with (patch.object(voice, "_transcribe", return_value="open Gmail"),
              patch.object(voice, "_run_goal") as run,
              patch.object(app, "save_clip", return_value="clip.wav"),
              patch.object(app, "read_key", return_value="key")):
            voice._plan_audio(np.ones(5000, dtype=np.float32), (10, first), None, "uid", True, True, release)
        self.assertEqual(run.call_args.args[2]["controls"], initial_controls)
        self.assertEqual(run.call_args.args[5]["controls"], current_controls)

    def test_stop_after_review_approval_prevents_execution(self):
        stopped = False
        acted = []

        def plan(_key, _goal, _steps, _initial, _state, _controls, _apps, step, _prompts):
            step["verb"] = {"choice": "press_key"}
            step["target"] = {"id": "kEnter", "kind": "key", "key": "Enter", "description": "Enter"}
            step["timings_ms"] = {}
            return "act"

        def approve(_step, _state, _controls):
            nonlocal stopped
            stopped = True
            return True

        with patch.object(goal, "plan_step", plan):
            result = goal.run_goal("key", "open item", lambda: screen("Home"),
                                   lambda *_args: acted.append(True), {},
                                   should_stop=lambda: stopped, allow_action=approve)
        self.assertEqual(result, "cancelled")
        self.assertEqual(acted, [])

    def test_credential_fields_reject_guessed_or_secret_text(self):
        offered = []

        def choose(goal_text, field_name, value):
            field = Control("entry", field_name, "Edit", (0, 0, 100, 20), "Sign in")
            state = screen("Sign in", [field])[0]
            text_id = next(key for key, label in goal.text_candidates(goal_text).items()
                           if label == f'Type "{value}"')

            def ask(_key, _goal, _state, questions, _trace=None):
                if "text_to_type" in questions:
                    offered.append(list(questions["text_to_type"]["criteria"].values()))
                return {"goal_done": {"noul": 0.05},
                        "next_action": {"choice": "type_text", "probabilities": {"type_text": 0.99}},
                        "field_target": {"choice": "entry", "probabilities": {"entry": 0.99, "none": 0.01}},
                        "text_to_type": {"choice": text_id, "probabilities": {text_id: 0.99}}}, 3.0

            with patch.object(goal, "ask_questions", ask):
                step = {}
                decision = goal.plan_step("key", goal_text, [], state, state, [field], [], step)
            return decision, step

        decision, step = choose("Open Gmail for me", "Email or phone", "Gmail")
        self.assertEqual(decision, "blocked")
        self.assertIn("no email address or phone number", step["reason"])
        self.assertNotIn("text", step)

        decision, step = choose("Type the password secret", "Password", "secret")
        self.assertEqual(decision, "blocked")
        self.assertIn("yourself", step["reason"])
        self.assertNotIn("text", step)

        decision, step = choose("Sign in with user@example.com", "Email or phone", "user@example.com")
        self.assertEqual(decision, "act")
        self.assertEqual(step["text"], "user@example.com")

        # A contact form's Email field is offered only the address, not "the email test.user@example.com".
        offered.clear()
        decision, step = choose("Send a message with the name Test User, the email test.user@example.com, and the message "
                                "Please call me back", "Email", "test.user@example.com")
        self.assertEqual(decision, "act")
        self.assertEqual(step["text"], "test.user@example.com")
        self.assertEqual(offered, [['Type "test.user@example.com"']])

    def test_completion_gate_keeps_working_until_the_last_screen(self):
        memory = Control("memory", "Memory", "ListItem", (0, 0, 100, 20), "Task Manager")
        state = screen("Task Manager", [memory])[0]

        def next_action(done_p):
            def ask(*_args):
                return {"goal_done": {"noul": done_p},
                        "next_action": {"choice": "left_click", "probabilities": {"left_click": 0.99}},
                        "control_target": {"choice": "memory", "probabilities": {"memory": 0.95, "none": 0.05}}}, 3.0
            return ask

        with patch.object(goal, "ask_questions", next_action(0.70)):
            step = {}
            self.assertEqual(goal.plan_step("key", "Show the Memory graph", [], state, state,
                                            [memory], [], step), "act")
            self.assertEqual(step["target"]["id"], "memory")
        with patch.object(goal, "ask_questions", next_action(0.78)):
            step = {}
            self.assertEqual(goal.plan_step("key", "Show the Memory graph", [], state, state,
                                            [memory], [], step), "done")

    def test_synthetic_probe_checks_the_actual_next_choice(self):
        case = json.loads(goal_probe.CASES.read_text(encoding="utf-8"))[0]
        state, controls = goal_probe.state_for(case)
        self.assertEqual(state["activeWindow"]["title"], "Sound settings")
        self.assertEqual([control.id for control in controls], ["add", "paired"])

        def plan(_key, _goal, _steps, _initial, _state, _controls, _apps, step, _prompts):
            step["verb"] = {"choice": "left_click"}
            step["target"] = {"id": "paired"}
            return "act"

        with patch.object(goal, "plan_step", plan):
            result = goal_probe.probe("key", case)
        self.assertTrue(result["matched"])
        self.assertEqual(result["target"], "paired")

    def test_clear_menu_target_does_not_probe_runner_up(self):
        controls = [Control("add", "Add a new audio device", "MenuItem", (0, 0, 100, 20), "Open menu > Sound"),
                    Control("paired", "Paired output devices", "MenuItem", (0, 20, 100, 40), "Open menu > Sound")]
        state = screen("Sound", controls)[0]

        def ask(*_args):
            return {"goal_done": {"noul": 0.01},
                    "next_action": {"choice": "left_click", "probabilities": {"left_click": 0.99}},
                    "control_target": {"choice": "add", "probabilities": {"add": 0.81, "paired": 0.18, "none": 0.01}}}, 3.0

        with patch.object(goal, "ask_questions", ask):
            step = {}
            decision = goal.plan_step("key", "pair a new speaker", [], state, state, controls, [], step)
        self.assertEqual(decision, "act")
        self.assertEqual(step["verb"]["choice"], "left_click")
        self.assertEqual(step["target"]["id"], "add")

    def test_long_and_quoted_text_remain_candidates(self):
        quoted = goal.text_candidates('Type exactly "Hello, world!" in Notepad')
        self.assertEqual(next(iter(quoted.values())), 'Type "Hello, world!"')
        self.assertIn('Type "Hello, world!"', goal.text_candidates("Type 'Hello, world!' in Notepad").values())
        self.assertIn('Type "hello, world!"', goal.text_candidates("Type hello, world!").values())
        message = " ".join(f"word{i}" for i in range(40)) + "."
        candidates = goal.text_candidates(f"In Notepad, write this message: {message}")
        self.assertIn(f'Type "{message}"', candidates.values())
        self.assertLessEqual(len(candidates), 254)

    def test_uncertain_text_choice_does_not_type(self):
        control = Control("edit", "Search", "Edit", (0, 0, 100, 20), "Browser")
        state = screen("Browser", [control])[0]
        def ask(*_args):
            return {"goal_done": {"noul": 0.02},
                    "next_action": {"choice": "type_text", "probabilities": {"type_text": 0.9}},
                    "field_target": {"choice": "edit", "probabilities": {"edit": 0.9}},
                    "text_to_type": {"choice": "t0", "probabilities": {"t0": 0.28, "t1": 0.25}}}, 3.0
        with patch.object(goal, "ask_questions", ask):
            step = {}
            decision = goal.plan_step("key", "search for weather", [], state, state, [control], [], step)
        self.assertEqual(decision, "blocked")
        self.assertNotIn("text", step)

    def test_multiple_actions_use_new_observation_and_stop_at_done(self):
        observations = iter([screen("Home"), screen("Menu"), screen("Target")])
        acted = []
        seen = []

        def plan(_key, _goal, steps, _initial, state, _controls, _apps, step, _prompts):
            seen.append((state["activeWindow"]["title"], len(steps)))
            if state["activeWindow"]["title"] == "Target":
                step["done_p"] = 0.95
                return "done"
            step["verb"] = {"choice": "press_key"}
            step["target"] = {"id": "key-enter", "kind": "key", "key": "Enter", "description": "Enter"}
            step["timings_ms"] = {}
            return "act"

        with patch.object(goal, "plan_step", plan):
            record = {}
            outcome = goal.run_goal("key", "open target", lambda: next(observations),
                                    lambda *args: acted.append(args[2]["activeWindow"]["title"]) or "pressed Enter", record)
        self.assertEqual(outcome, "done")
        self.assertEqual(acted, ["Home", "Menu"])
        self.assertEqual(seen, [("Home", 0), ("Menu", 1), ("Target", 2)])
        self.assertEqual(record["step_count"], 2)

    def test_changed_window_refreshes_first_decision_but_keeps_started_in(self):
        started = screen("Explorer")
        current = screen("Chrome")
        observed = []
        def plan(_key, _goal, _steps, initial, state, _controls, _apps, _step, _prompts):
            observed.append((initial["activeWindow"]["title"], state["activeWindow"]["title"]))
            return "done"
        with patch.object(goal, "plan_step", plan):
            outcome = goal.run_goal("key", "open Gmail", lambda: current,
                                    lambda *args: self.fail("already done"), {},
                                    initial_observation=started, refresh_first=True)
        self.assertEqual(outcome, "done")
        self.assertEqual(observed, [("Explorer", "Chrome")])

    def test_stop_after_decision_prevents_action(self):
        stopped = False
        def plan(*args):
            nonlocal stopped
            stopped = True
            step = args[-2]
            step["verb"] = {"choice": "press_key"}
            step["target"] = {"id": "enter", "key": "Enter"}
            step["timings_ms"] = {}
            return "act"
        with patch.object(goal, "plan_step", plan):
            record = {}
            outcome = goal.run_goal("key", "task", lambda: screen("Home"),
                                    lambda *args: self.fail("acted after stop"), record,
                                    should_stop=lambda: stopped)
        self.assertEqual(outcome, "cancelled")

    def test_review_gate_prevents_action(self):
        def plan(*args):
            step = args[-2]
            step["verb"] = {"choice": "left_click"}
            step["target"] = {"id": "send", "kind": "control", "description": "Send"}
            step["timings_ms"] = {}
            return "act"
        with patch.object(goal, "plan_step", plan):
            record = {}
            outcome = goal.run_goal("key", "send", lambda: screen("Mail"),
                                    lambda *args: self.fail("acted past review"), record,
                                    allow_action=lambda step, state, controls: False)
        self.assertEqual(outcome, "review_required")
        self.assertEqual(record["step_count"], 0)

    def test_review_cancel_marks_goal_cancelled(self):
        stopped = threading.Event()
        def plan(*args):
            step = args[-2]
            step["verb"] = {"choice": "left_click"}
            step["target"] = {"id": "send", "kind": "control", "description": "Send"}
            step["timings_ms"] = {}
            return "act"
        def deny(*_args):
            stopped.set()
            return False
        with patch.object(goal, "plan_step", plan):
            outcome = goal.run_goal("key", "send", lambda: screen("Mail"),
                                    lambda *args: self.fail("acted after cancel"), {},
                                    should_stop=stopped.is_set, allow_action=deny)
        self.assertEqual(outcome, "cancelled")

    def test_waiting_on_an_unchanged_screen_stops_before_the_step_limit(self):
        waits = 0
        def plan(*args):
            nonlocal waits
            waits += 1
            step = args[-2]
            step["verb"] = {"choice": "wait"}
            step["target"] = {"id": "current", "kind": "current"}
            step["timings_ms"] = {}
            return "act"
        picker = screen("Google Flights", [Control("c1", "Departure", "Edit", (0, 0, 90, 30), "", state="focused"),
                                           Control("c2", "Done.", "Button", (0, 40, 90, 70), "")])
        with patch.object(goal, "plan_step", plan):
            record = {}
            outcome = goal.run_goal("key", "find flights", lambda: picker, lambda *args: "waited 1 s", record)
        self.assertEqual(outcome, "stuck")
        self.assertEqual(waits, goal.WAIT_LIMIT)
        self.assertIn("waited", record["steps"][-1]["reason"])

    def test_a_wait_that_loads_something_does_not_count_toward_the_idle_limit(self):
        waiting = {"verb": {"choice": "wait"}, "content_changed": False}
        loaded = {"verb": {"choice": "wait"}, "content_changed": True}
        self.assertFalse(goal._idle([waiting, loaded, waiting]))
        self.assertFalse(goal._idle([waiting, waiting]))
        self.assertTrue(goal._idle([loaded] + [waiting] * goal.WAIT_LIMIT))

    def test_a_ticking_player_clock_does_not_count_as_waiting_progress(self):
        playing = screen("YouTube Music")[0]
        later = {**playing, "texts": ["Did you mean:", "3:00", "2:36 / 2:55"]}
        playing = {**playing, "texts": ["Did you mean:", "3:00", "2:34 / 2:55"]}
        self.assertEqual(goal.signature(playing, [], ambient=False), goal.signature(later, [], ambient=False))
        self.assertNotEqual(goal.signature(playing, [], ambient=False),
                            goal.signature({**playing, "texts": ["Did you mean:", "3:00", "No results"]}, [], ambient=False))
        self.assertNotEqual(goal.signature(playing, []), goal.signature(later, []))  # seeking a video moves only the clock

    def test_background_tab_title_does_not_count_as_waiting_progress_but_a_new_tab_does(self):
        state = screen("YouTube Music")[0]
        music = Control("t1", "YouTube Music", "TabItem", (0, 0, 90, 30), "", state="selected")
        video = Control("t2", "Gemma 4 12B - YouTube - Memory usage - 274 MB", "TabItem", (90, 0, 180, 30), "")
        renamed = Control("t2", "(1) Gemma 4 12B - YouTube - Memory usage - 185 MB", "TabItem", (90, 0, 180, 30), "")
        opened = Control("t3", "New Tab", "TabItem", (180, 0, 270, 30), "")
        self.assertEqual(goal.signature(state, [music, video], ambient=False), goal.signature(state, [music, renamed], ambient=False))
        self.assertNotEqual(goal.signature(state, [music, video], ambient=False),
                            goal.signature(state, [music, video, opened], ambient=False))
        self.assertNotEqual(goal.signature(state, [music, video]), goal.signature(state, [music, renamed]))

    def test_seeking_twice_is_not_stuck_when_only_the_clock_moves(self):
        clock = iter(["10:00 / 45:00", "10:10 / 45:00", "10:20 / 45:00", "10:30 / 45:00"])
        presses = []

        def observe():
            state, controls, apps = screen("VLC media player")
            return {**state, "texts": [next(clock)]}, controls, apps

        def plan(*args):
            step = args[-2]
            if len(presses) == 2:
                step["done_p"] = 0.9
                return "done"
            step.update(verb={"choice": "press_key"}, target={"id": "kRight", "kind": "key", "key": "Right"}, timings_ms={})
            return "act"
        with patch.object(goal, "plan_step", plan):
            outcome = goal.run_goal("key", "skip ahead 20 seconds", observe, lambda *args: presses.append(1) or "pressed Right", {})
        self.assertEqual((outcome, len(presses)), ("done", 2))

    def test_waits_while_music_plays_still_stop_as_idle(self):
        clock = iter(f"2:{s:02d} / 2:55" for s in range(30, 60))

        def observe():
            state, controls, apps = screen("YouTube Music")
            return {**state, "texts": ["Did you mean:", next(clock)]}, controls, apps

        def plan(*args):
            args[-2].update(verb={"choice": "wait"}, target={"id": "current", "kind": "current"}, timings_ms={})
            return "act"
        with patch.object(goal, "plan_step", plan):
            record = {}
            self.assertEqual(goal.run_goal("key", "play a sad song", observe, lambda *args: "waited 1 s", record), "stuck")
        self.assertEqual(len(record["steps"]), goal.WAIT_LIMIT)

    def test_typed_field_text_counts_as_a_screen_change(self):
        state = screen("Google Flights")[0]
        before = [Control("c1", "Where from?", "ComboBox", (0, 0, 90, 30), "", value="Ahmedabad")]
        after = [Control("c1", "Where from?", "ComboBox", (0, 0, 90, 30), "", value="Los Angeles")]
        self.assertNotEqual(goal.signature(state, before), goal.signature(state, after))

    def test_field_value_is_shown_to_jev(self):
        field = Control("c1", "Where from?", "ComboBox", (0, 0, 90, 30), "", state="collapsed", value="Ahmedabad")
        self.assertEqual(core.describe_control(field), 'ComboBox "Where from?" = "Ahmedabad" [collapsed]')
        restored = core.restore_inputs({"utterance": "x", "activeWindow": {"hwnd": 1}, "openWindows": [], "installedApps": [],
                                        "controls": [{"id": "c1", "name": "Where from?", "role": "ComboBox", "rect": [0, 0, 90, 30],
                                                      "path": "", "enabled": True, "value": "Ahmedabad"}]})[2]
        self.assertEqual(restored[0].value, "Ahmedabad")

    def test_execution_failure_stops_instead_of_retrying(self):
        calls = 0
        def plan(*args):
            step = args[-2]
            step["verb"] = {"choice": "press_key"}
            step["target"] = {"id": "enter", "key": "Enter"}
            step["timings_ms"] = {}
            return "act"
        def act(*args):
            nonlocal calls
            calls += 1
            raise RuntimeError("window closed")
        with patch.object(goal, "plan_step", plan):
            outcome = goal.run_goal("key", "task", lambda: screen("Home"), act, {})
        self.assertEqual(outcome, "error")
        self.assertEqual(calls, 1)

    def test_review_gate_only_catches_deletion_like_actions(self):
        state, _, _ = screen("Mail")
        for label in ("Delete", "Delete message", "Remove item", "Erase data", "Discard draft", "Move to Trash", "Uninstall app"):
            with self.subTest(label=label):
                control = Control("action", label, "Button", (0, 0, 10, 10), "Mail")
                click = {"verb": {"choice": "left_click"}, "target": {"id": "action"}}
                self.assertTrue(goal.action_requires_review(click, state, [control]))
        for label in ("Save draft", "Send message", "Submit form", "Post", "Publish", "Confirm", "Purchase", "Search"):
            with self.subTest(label=label):
                control = Control("action", label, "Button", (0, 0, 10, 10), "Mail")
                click = {"verb": {"choice": "left_click"}, "target": {"id": "action"}}
                self.assertFalse(goal.action_requires_review(click, state, [control]))

        send = Control("send", "Send message", "Button", (0, 0, 10, 10), "Mail")
        enter = {"verb": {"choice": "press_key"}, "target": {"key": "Enter"}}
        self.assertFalse(goal.action_requires_review(enter, state, [send]))
        terminal = screen("PowerShell")[0]
        terminal["activeWindow"]["process"] = "pwsh.exe"
        self.assertFalse(goal.action_requires_review(enter, terminal, []))
        self.assertTrue(goal.action_requires_review({"verb": {"choice": "press_key"}, "target": {"key": "Delete"}}, state, []))
        for verb, key in (("close_window", None), ("key_chord", "close_tab"), ("key_chord", "paste"), ("key_chord", "save")):
            with self.subTest(verb=verb, key=key):
                self.assertFalse(goal.action_requires_review({"verb": {"choice": verb}, "target": {"key": key}}, state, []))

    def test_large_screen_can_choose_control_after_first_254(self):
        controls = [Control(f"c{i:04}", f"Item {i}", "Button", (0, i, 10, i + 1), "Page")
                    for i in range(300)]
        state = screen("Large page", controls)[0]
        def ask(_key, _goal, _view, questions, _trace):
            self.assertNotIn("control_target", questions)
            answers = {"goal_done": {"noul": 0.03},
                       "next_action": {"choice": "left_click", "probabilities": {"left_click": 0.9}}}
            return answers, 5.0
        grouped_calls = []
        def ask_group(_key, question, _task, criteria, context, _prompt, _trace):
            grouped_calls.append((question, context))
            self.assertEqual(context["stepsTaken"], ["1. opened the page"])
            if question == "group":
                self.assertIn("g1", criteria)
                return {"choice": "g1", "probabilities": {"g1": 0.9}}, 2.0
            self.assertIn("c0299", criteria)
            return {"choice": "c0299", "probabilities": {"c0299": 0.9}}, 2.0
        with patch.object(goal, "ask_questions", ask), patch.object(core, "ask_jev", ask_group):
            step = {}
            decision = goal.plan_step("key", "open item 299", [{"summary": "1. opened the page"}],
                                      state, state, controls, [], step)
        self.assertEqual(decision, "act")
        self.assertEqual(step["target"]["id"], "c0299")
        self.assertEqual(step["timings_ms"]["jev"], 9)
        self.assertEqual([call[0] for call in grouped_calls], ["group", "target"])

    def test_existing_menu_option_wins_close_file_picker_tie(self):
        controls = [Control("add", "Add Subtitle File", "MenuItem", (0, 0, 100, 20), "Open menu > Subtitle"),
                    Control("track", "Sub Track", "MenuItem", (0, 20, 100, 40), "Open menu > Subtitle")]
        state = screen("Movie", controls)[0]
        def ask(_key, _goal, _view, questions, _trace):
            return {"goal_done": {"noul": 0.02},
                    "next_action": {"choice": "left_click", "probabilities": {"left_click": 0.9}},
                    "control_target": {"choice": "add", "probabilities": {"add": 0.6, "track": 0.35}}}, 5.0
        with patch.object(goal, "ask_questions", ask):
            step = {}
            decision = goal.plan_step("key", "load English subtitles", [], state, state, controls, [], step)
        self.assertEqual(decision, "act")
        self.assertEqual(step["verb"]["choice"], "left_click")
        self.assertEqual(step["target"]["id"], "track")
        self.assertEqual(step["target"]["selection_rule"], "prefer existing choices to file import")

    def test_typed_voice_path_enters_shared_goal_loop(self):
        voice = app.VoiceApp.__new__(app.VoiceApp)
        voice.settler = Mock()  # the Settler follows live windows; these tests fake the goal loop
        voice.events = queue.Queue()
        voice.model_device = "cpu"
        voice.model_name = "test"
        ready = Future()
        state, controls, apps = screen("Home")
        captured = {"state": state, "controls": controls, "apps": apps, "hotwords": None,
                    "app_catalog_refreshed": False, "capture_ms": 1}
        ready.set_result(captured)
        with patch.object(app, "read_key", return_value="key"), patch.object(voice, "_run_goal") as run:
            voice._plan_audio(None, (10, ready), "open target", "test-uid", True, False)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[1:3], ("open target", captured))
        self.assertFalse(run.call_args.args[-1])
        events = list(voice.events.queue)
        self.assertTrue(any(kind == "transcript" for kind, _ in events))

    def test_activity_entry_shows_completed_and_review_steps(self):
        record = {"utterance_id": "u", "transcript": "send the note", "mode": "goal",
                  "outcome": "review_required", "actions": ["1. switch_window Mail"],
                  "steps": [{"verb": {"choice": "left_click"}, "target": {"kind": "control", "description": "Send message"},
                             "reason": "review"}]}
        entry = overlay.entry_from_record(record)
        self.assertEqual(entry["actions"], ["1. switch_window Mail"])
        self.assertEqual(entry["next_step"], "Click “Send message”")
        self.assertEqual(overlay.entry_title(entry), "Goal needs review")

    def test_voice_review_can_approve_and_resume_goal(self):
        state, controls, apps = screen("Mail")
        captured = {"state": state, "controls": controls, "apps": apps}
        voice = app.VoiceApp.__new__(app.VoiceApp)
        voice.settler = Mock()  # the Settler follows live windows; these tests fake the goal loop
        voice.events = queue.Queue()
        voice.goal_stop = threading.Event()
        voice.goal_active = True
        voice.goal_review_pending = None
        voice.entries = {"uid": {"transcript": "send the note"}}
        voice.pill = Mock()
        voice.root = Mock()
        voice.panel = Mock()
        record = {"utterance_id": "uid", "transcript": "send the note", "timings_ms": {}}

        def fake_loop(_key, _utterance, _observe, _act, result, **kwargs):
            step = {"index": 1, "verb": {"choice": "left_click"},
                    "target": {"kind": "control", "description": "Send"}, "timings_ms": {}}
            result["steps"] = [step]
            self.assertTrue(kwargs["allow_action"](step, state, controls))
            result.update(outcome="done", actions=["approved"], step_count=1)
            return "done"

        with (patch.object(goal, "run_goal", fake_loop),
              patch.object(goal, "action_requires_review", return_value=True),
              patch.object(app, "append_log")):
            worker = threading.Thread(target=voice._run_goal,
                                      args=("key", "send the note", captured, record, True))
            worker.start()
            kind, (uid, step, decision, ready) = voice.events.get(timeout=2)
            self.assertEqual(kind, "goal_review")
            self.assertEqual(uid, "uid")
            self.assertFalse(ready.is_set())
            voice.events.put((kind, (uid, step, decision, ready)))
            voice._poll()
            self.assertEqual(voice.goal_review_pending, (decision, ready, uid))
            voice.pill.show.assert_called_with("review", title="Click “Send”", detail="send the note")
            voice.execute_pending()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(record["steps"][0]["reviewed"])
        self.assertEqual(record["outcome"], "done")

    def test_live_harness_skips_when_desktop_has_no_foreground(self):
        task = {"id": "sample", "goal": "open sample", "split": "challenge", "level": "hard", "setup": [],
                "check": {"foreground_title": "Sample"}}
        with (patch.object(goal_eval, "run_setup"),
              patch.object(goal_eval.win32gui, "GetForegroundWindow", return_value=0),
              patch.object(goal_eval, "check", side_effect=AssertionError("check should not run"))):
            result = goal_eval.run_task("key", task, Mock())
        self.assertEqual(result["outcome"], "invalid_environment")
        self.assertFalse(result["passed"])

    def test_live_harness_records_setup_failures_without_scoring_jev(self):
        task = {"id": "sample", "goal": "open sample", "split": "challenge", "level": "hard", "setup": [],
                "check": {"foreground_title": "Sample"}}
        for error, expected in ((PermissionError("desktop denied"), "invalid_environment"),
                                (ValueError("bad fixture"), "setup_error")):
            with (self.subTest(error=type(error).__name__),
                  patch.object(goal_eval, "run_setup", side_effect=error),
                  patch.object(goal_eval, "check", side_effect=AssertionError("check should not run"))):
                result = goal_eval.run_task("key", task, Mock())
            self.assertEqual(result["outcome"], expected)
            self.assertEqual(result["seconds"], 0)

    def test_manual_goal_mode_reviews_even_safe_steps(self):
        state, controls, apps = screen("Calculator")
        voice = app.VoiceApp.__new__(app.VoiceApp)
        voice.settler = Mock()  # the Settler follows live windows; these tests fake the goal loop
        voice.events = queue.Queue()
        voice.goal_stop = threading.Event()
        record = {"utterance_id": "uid", "transcript": "open calculator", "timings_ms": {}}
        step = {"index": 1, "verb": {"choice": "left_click"},
                "target": {"kind": "control", "description": "Calculator"}, "timings_ms": {}}
        def fake_loop(_key, _utterance, _observe, _act, result, **kwargs):
            result["steps"] = [step]
            self.assertTrue(kwargs["allow_action"](step, state, controls))
            result.update(outcome="done", actions=["approved"], step_count=1)
            return "done"
        with (patch.object(goal, "run_goal", fake_loop),
              patch.object(goal, "action_requires_review", return_value=False),
              patch.object(app, "append_log")):
            worker = threading.Thread(target=voice._run_goal,
                                      args=("key", "open calculator", {"state": state, "controls": controls, "apps": apps},
                                            record, False))
            worker.start()
            kind, (_, _, decision, ready) = voice.events.get(timeout=2)
            self.assertEqual(kind, "goal_review")
            decision["approved"] = True
            ready.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(step["reviewed"])

    def test_voice_goal_refreshes_changed_foreground(self):
        state, controls, apps = screen("Explorer")
        voice = app.VoiceApp.__new__(app.VoiceApp)
        voice.settler = Mock()  # the Settler follows live windows; these tests fake the goal loop
        voice.events = queue.Queue()
        voice.goal_stop = threading.Event()
        record = {"utterance_id": "uid", "transcript": "open Gmail", "timings_ms": {}}
        def fake_loop(_key, _goal, _observe, _act, result, **kwargs):
            self.assertTrue(kwargs["refresh_first"])
            self.assertEqual(kwargs["initial_observation"][0]["activeWindow"]["title"], "Explorer")
            result.update(steps=[], actions=[], step_count=0, outcome="done")
            return "done"
        with (patch.object(goal, "run_goal", fake_loop),
              patch.object(app, "append_log"),
              patch.object(app.win32gui, "GetForegroundWindow", return_value=20),
              patch.object(app, "app_window", return_value=20),
              patch.object(app.win32gui, "GetWindowText", return_value="Chrome"),
              patch.object(voice, "_is_own", return_value=False)):
            voice._run_goal("key", "open Gmail", {"state": state, "controls": controls, "apps": apps}, record, True)
        self.assertEqual(record["outcome"], "done")

    def test_voice_goal_exposes_block_reason(self):
        state, controls, apps = screen("Browser")
        voice = app.VoiceApp.__new__(app.VoiceApp)
        voice.settler = Mock()  # the Settler follows live windows; these tests fake the goal loop
        voice.events = queue.Queue()
        voice.goal_stop = threading.Event()
        record = {"utterance_id": "uid", "transcript": "search", "timings_ms": {}}
        def fake_loop(_key, _goal, _observe, _act, result, **_kwargs):
            result.update(steps=[{"reason": "text to type is unclear", "timings_ms": {}}],
                          actions=[], step_count=0, outcome="blocked")
            return "blocked"
        with (patch.object(goal, "run_goal", fake_loop), patch.object(app, "append_log"),
              patch.object(app.win32gui, "GetForegroundWindow", return_value=0)):
            voice._run_goal("key", "search", {"state": state, "controls": controls, "apps": apps}, record, True)
        self.assertEqual(record["error"], "text to type is unclear")

    def test_vlc_fixture_setup_preserves_unrelated_vlc(self):
        movie = str((goal_eval.ROOT / "goal-fixtures/media/Sample Movie.mkv").resolve())
        personal = Mock(info={"name": "vlc.exe", "cmdline": ["vlc.exe", "C:\\Users\\me\\my-movie.mkv"]})
        fixture = Mock(info={"name": "vlc.exe", "cmdline": ["vlc.exe", movie]})
        with (patch.object(goal_eval, "VLC", Path("vlc.exe")),
              patch.object(goal_eval.psutil, "process_iter", return_value=[personal, fixture]),
              patch.object(goal_eval.subprocess, "Popen", return_value=Mock(pid=123)) as popen,
              patch.object(goal_eval, "open_windows", return_value=[{"hwnd": 77, "title": "Sample Movie.mkv - VLC media player"}]),
              patch.object(goal_eval.win32process, "GetWindowThreadProcessId", return_value=(0, 123)),
              patch.object(goal_eval, "_vlc_volume", return_value=192),
              patch.object(goal_eval, "vlc_rc") as rc,
              patch.object(goal_eval.time, "sleep")):
            goal_eval._vlc("goal-fixtures/media/Sample Movie.mkv")
        personal.kill.assert_not_called()
        fixture.kill.assert_called_once()
        self.assertIn("--no-one-instance", popen.call_args.args[0])
        rc.assert_called_with("volume 256")  # every run starts at 100%, whatever the last run left

    def test_goal_replay_uses_latest_saved_step_with_history(self):
        state, controls, apps = screen("Menu")
        saved = {"inputs": core.snapshot_inputs("open target", state, apps), "window": state["activeWindow"]}
        original = {"task": "demo", "goal": "open target", "batch": "old", "outcome": "blocked",
                    "initial_window": state["activeWindow"],
                    "steps": [{"summary": "1. opened menu"}, saved]}
        invalid = {"task": "demo", "batch": "new", "outcome": "invalid_environment", "steps": []}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "results.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in (original, invalid)), encoding="utf-8")
            selected = goal_replay.saved_step(path, "demo", 2)
        self.assertEqual(selected["batch"], "old")
        with patch.object(goal, "plan_step", return_value="done") as plan:
            decision, _ = goal_replay.replan("key", selected, 2)
        self.assertEqual(decision, "done")
        self.assertEqual(plan.call_args.args[2], [{"summary": "1. opened menu"}])

    def test_switch_window_to_a_tab_becomes_a_tab_click(self):
        tab = Control("tab", "(31) WhatsApp", "TabItem", (500, 0, 700, 50), "Chrome")
        state = screen("Gemini - Google Chrome", [tab])[0]
        state["openWindows"] = [state["activeWindow"], {"hwnd": 20, "title": "Claude", "process": "claude.exe"}]

        def ask(*_args):
            return {"goal_done": {"noul": 0.03},
                    "next_action": {"choice": "switch_window", "probabilities": {"switch_window": 0.88, "left_click": 0.08}},
                    "control_target": {"choice": "tab", "probabilities": {"tab": 0.96, "none": 0.04}},
                    "window_target": {"choice": "none", "probabilities": {"none": 0.66, "w20": 0.34}}}, 3.0

        with patch.object(goal, "ask_questions", ask):
            step = {}
            decision = goal.plan_step("key", "go to WhatsApp and open a chat", [], state, state, [tab], [], step)
        self.assertEqual(decision, "act")
        self.assertEqual((step["verb"]["choice"], step["target"]["id"]), ("left_click", "tab"))

    def test_menu_bar_title_opens_on_a_close_call_but_commands_do_not(self):
        view = Control("view", "View", "MenuItem", (0, 0, 40, 20), "Player")
        video = Control("video", "Video", "MenuItem", (40, 0, 80, 20), "Player")
        state = screen("Player", [view, video])[0]

        def ask(*_args):
            return {"goal_done": {"noul": 0.1},
                    "next_action": {"choice": "left_click", "probabilities": {"left_click": 0.9}},
                    "control_target": {"choice": "view", "probabilities": {"view": 0.49, "video": 0.37, "none": 0.14}}}, 3.0

        with patch.object(goal, "ask_questions", ask):
            step = {}
            self.assertEqual(goal.plan_step("key", "go full screen", [], state, state, [view, video], [], step), "act")
        self.assertEqual(step["target"]["id"], "view")
        opened = [Control(c.id, c.name, c.role, c.rect, "Open menu > View") for c in (view, video)]
        with patch.object(goal, "ask_questions", ask):
            step = {}
            self.assertEqual(goal.plan_step("key", "go full screen", [], state, state, opened, [], step), "blocked")

    def test_close_call_between_open_field_suggestions_picks_the_likelier(self):
        page = Control("result", "Los Angeles travel guide", "ListItem", (0, 0, 90, 20), "Page")
        field = Control("field", "Where else?", "ComboBox", (0, 30, 90, 50), "Page", state="expanded", value="Los Angeles")
        city = Control("city", "Los Angeles, California", "ListItem", (0, 60, 90, 80), "Page > Enter your destination")
        lax = Control("lax", "Los Angeles International Airport (LAX)", "ListItem", (0, 90, 90, 110), "Page > Enter your destination")

        def plan(controls, pick="city", second="lax"):
            def ask(*_args):
                return {"goal_done": {"noul": 0.1},
                        "next_action": {"choice": "left_click", "probabilities": {"left_click": 0.9}},
                        "control_target": {"choice": pick, "probabilities": {pick: 0.48, second: 0.40, "none": 0.12}}}, 3.0
            state = screen("Google Flights", controls)[0]
            with patch.object(goal, "ask_questions", ask):
                step = {}
                return goal.plan_step("key", "flights to Los Angeles", [], state, state, controls, [], step), step

        decision, step = plan([page, field, city, lax])
        self.assertEqual((decision, step["target"]["id"]), ("act", "city"))
        self.assertEqual(step["target"]["selection_rule"], "pick the likelier of an open field's suggestions")
        collapsed = Control("field", "Where to?", "ComboBox", (0, 30, 90, 50), "Page", state="collapsed")
        self.assertEqual(plan([page, collapsed, city, lax])[0], "blocked")  # no list is open
        self.assertEqual(plan([page, field, city, lax], "city", "result")[0], "blocked")  # runner-up is not a suggestion
        self.assertEqual(plan([page, field, city, lax], "result", "city")[0], "blocked")  # listed before the field

    def test_play_button_nested_in_its_same_named_link_is_one_choice(self):
        page = "YouTube Music"
        link = Control("c0060", "Play Aal Izz Well - Sonu Nigam", "Hyperlink", (487, 442, 612, 567), page)
        button = Control("c0061", "Play Aal Izz Well - Sonu Nigam", "Button", (514, 469, 585, 540),
                         f"{page} > Play Aal Izz Well - Sonu Nigam")
        other = Control("c0069", "Behti Hawa Sa Tha Woh", "Hyperlink", (567, 650, 782, 675), page)

        def plan(controls, probabilities):
            def ask(*_args):
                return {"goal_done": {"noul": 0.1},
                        "next_action": {"choice": "left_click", "probabilities": {"left_click": 0.9}},
                        "control_target": {"choice": max(probabilities, key=probabilities.get),
                                           "probabilities": probabilities}}, 3.0
            state = screen(page, controls)[0]
            with patch.object(goal, "ask_questions", ask):
                step = {}
                return goal.plan_step("key", "play the most romantic song", [], state, state, controls, [], step), step

        # The logged Three Idiots screen: 0.25 + 0.21 on one control, 0.20 on the next song.
        decision, step = plan([link, button, other], {"c0061": 0.25, "c0060": 0.21, "c0069": 0.20, "none": 0.04})
        self.assertEqual((decision, step["target"]["id"]), ("act", "c0061"))
        self.assertAlmostEqual(step["target"]["probability"], 0.46)
        self.assertEqual(step["target"]["runner_up"]["id"], "c0069")
        self.assertEqual(step["target"]["selection_rule"], "same control nested in its own container")
        # Merged, it still needs the usual margin over the next different target.
        self.assertEqual(plan([link, button, other], {"c0061": 0.25, "c0060": 0.21, "c0069": 0.40, "none": 0.04})[0], "blocked")

    def test_differently_named_or_side_by_side_controls_are_not_merged(self):
        row = Control("row", "Invoice from Acme", "Hyperlink", (0, 0, 800, 40), "Inbox")
        delete = Control("delete", "Delete", "Button", (700, 5, 730, 35), "Inbox > Invoice from Acme")
        self.assertFalse(goal._nested_twin("delete", "row", [row, delete]))
        first = Control("p1", "Play", "Button", (0, 0, 30, 30), "Liked Music")
        second = Control("p2", "Play", "Button", (0, 40, 30, 70), "Episodes for Later")
        self.assertFalse(goal._nested_twin("p1", "p2", [first, second]))
        song = Control("song", "Jaan Se Guzarte Hain", "Hyperlink", (567, 736, 1070, 760), "YouTube Music")
        play = Control("play", "Play Jaan Se Guzarte Hain - Shashwat Sachdev", "Button", (492, 742, 532, 783), "YouTube Music")
        self.assertFalse(goal._nested_twin("play", "song", [song, play]))

    def test_a_click_that_changed_nothing_is_retried_as_a_real_click(self):
        def step(target, changed=None):
            return {"verb": {"choice": "left_click"}, "target": {"id": target}, "screen_changed": changed}
        self.assertTrue(goal._repeat_of_silent_click([step("switch", False), step("switch")]))
        self.assertFalse(goal._repeat_of_silent_click([step("switch", True), step("switch")]))
        self.assertFalse(goal._repeat_of_silent_click([step("switch", False), step("other")]))

    def test_waiting_on_a_click_that_never_registered_clicks_again(self):
        search = Control("c9", "Search", "Button", (0, 0, 50, 20), "Flights")
        send = Control("c9", "Send", "Button", (0, 0, 50, 20), "Mail")
        dark = Control("c9", "Dark mode", "Button", (0, 0, 50, 20), "Settings", state="unchecked")

        def steps(name, *waits):
            click = {"verb": {"choice": "left_click"}, "target": {"id": "c3", "description": f'Button "{name}" at (0, 0, 50, 20)'},
                     "screen_changed": True}
            return [click] + [{"verb": {"choice": "wait"}, "content_changed": w} for w in waits] + [{"verb": {"choice": "wait"}}]
        self.assertTrue(goal._waiting_on_a_silent_click(steps("Search", False), [search]))
        self.assertFalse(goal._waiting_on_a_silent_click(steps("Search"), [search]))  # a first wait is allowed
        self.assertFalse(goal._waiting_on_a_silent_click(steps("Search", True), [search]))  # something is loading
        self.assertFalse(goal._waiting_on_a_silent_click(steps("Send", False), [send]))
        self.assertFalse(goal._waiting_on_a_silent_click(steps("Dark mode", False), [dark]))
        self.assertIs(goal._still_there({"description": 'Button "Search" at (1, 2, 3, 4)'}, [search]), search)

    def test_saving_with_the_keyboard_runs_without_review(self):
        state, controls, _ = screen("Docs")
        step = {"verb": {"choice": "key_chord"}, "target": {"id": "ch_save", "key": "save"}}
        self.assertFalse(goal.action_requires_review(step, state, controls))
        self.assertIn("save", core.CHORDS)

    def test_select_text_picks_a_literal_that_is_in_the_field(self):
        note = Control("note", "Note body", "Edit", (0, 0, 300, 90), "Writer", value="This is urgent: send the slides before Friday.")

        def plan(goal_text, word):
            text_id = next(k for k, label in goal.text_candidates(goal_text).items() if label == f'Type "{word}"')

            def ask(*_args):
                return {"goal_done": {"noul": 0.05},
                        "next_action": {"choice": "select_text", "probabilities": {"select_text": 0.9}},
                        "field_target": {"choice": "note", "probabilities": {"note": 0.95, "none": 0.05}},
                        "text_to_type": {"choice": text_id, "probabilities": {text_id: 0.9}}}, 3.0
            state = screen("Writer", [note])[0]
            with patch.object(goal, "ask_questions", ask):
                step = {}
                return goal.plan_step("key", goal_text, [], state, state, [note], [], step), step

        decision, step = plan("Make the word urgent bold", "urgent")
        self.assertEqual((decision, step["verb"]["choice"], step["text"]), ("act", "select_text", "urgent"))
        decision, step = plan("Make the word calm bold", "calm")  # not in the note: never selected
        self.assertEqual(decision, "blocked")
        self.assertNotIn("text", step)

    def test_text_is_chosen_after_its_field_and_names_that_field(self):
        origin = Control("from", "Where from?", "ComboBox", (0, 0, 100, 20), "Flights", value="Dubai")
        destination = Control("to", "Where to?", "ComboBox", (0, 30, 100, 50), "Flights")
        goal_text = "find me flights from Dubai to Los Angeles"
        ids = {label[len('Type "'):-1]: k for k, label in goal.text_candidates(goal_text).items()}
        requests = []

        def ask(_key, _goal, view, questions, _trace):
            requests.append((view, questions))
            if "text_to_type" in questions:
                return {"text_to_type": {"choice": ids["Los Angeles"], "probabilities": {ids["Los Angeles"]: 0.9}}}, 1.0
            return {"goal_done": {"noul": 0.05},
                    "next_action": {"choice": "type_text", "probabilities": {"type_text": 0.9}},
                    "field_target": {"choice": "to", "probabilities": {"to": 0.9, "from": 0.1}}}, 3.0

        state = screen("Flights", [origin, destination])[0]
        with patch.object(goal, "ask_questions", ask):
            step = {}
            decision = goal.plan_step("key", goal_text, [], state, state, [origin, destination], [], step)
        self.assertEqual((decision, step["target"]["id"], step["text"]), ("act", "to", "Los Angeles"))
        self.assertNotIn("text_to_type", requests[0][1])  # not guessed before the field is known
        self.assertEqual(list(requests[1][1]), ["text_to_type"])
        self.assertIn('"Where to?"', requests[1][0]["targetField"])
        self.assertEqual(step["timings_ms"]["jev"], 4)

    def test_value_typed_into_another_field_can_be_typed_again(self):
        # "billing and shipping city: Pune" puts one value in two fields; the choice is Jev's, not filtered out.
        billing = Control("bill", "Billing city", "Edit", (0, 0, 100, 20), "Checkout", value="Pune")
        shipping = Control("ship", "Shipping city", "Edit", (0, 30, 100, 50), "Checkout")
        goal_text = "Set the billing and shipping city: Pune"
        pune = next(k for k, label in goal.text_candidates(goal_text).items() if label == 'Type "Pune"')
        typed = {"verb": {"choice": "type_text"}, "text": "Pune", "result": "typed", "summary": '1. type_text Edit "Billing city" text "Pune"',
                 "target": {"id": "bill", "description": 'Edit "Billing city" at (0, 0, 100, 20)'}}

        def ask(_key, _goal, _view, questions, _trace):
            if "text_to_type" in questions:
                return {"text_to_type": {"choice": pune, "probabilities": {pune: 0.9}}}, 1.0
            return {"goal_done": {"noul": 0.05},
                    "next_action": {"choice": "type_text", "probabilities": {"type_text": 0.9}},
                    "field_target": {"choice": "ship", "probabilities": {"ship": 0.9}}}, 3.0

        state = screen("Checkout", [billing, shipping])[0]
        with patch.object(goal, "ask_questions", ask):
            step = {}
            decision = goal.plan_step("key", goal_text, [typed], state, state, [billing, shipping], [], step)
        self.assertEqual((decision, step["target"]["id"], step["text"]), ("act", "ship", "Pune"))

    def test_repeated_action_with_flat_progress_stops(self):
        observations = iter([screen(f"Page {i}") for i in range(20)])

        def plan(_key, _goal, _steps, _initial, _state, _controls, _apps, step, _prompts):
            step.update(verb={"choice": "scroll_down"}, target={"id": "current", "kind": "current"}, done_p=0.05, timings_ms={})
            return "act"

        with patch.object(goal, "plan_step", plan):
            record = {}
            outcome = goal.run_goal("key", "keep clicking links until bikes", lambda: next(observations),
                                    lambda *_: "scrolled", record)
        self.assertEqual(outcome, "stuck")
        self.assertEqual(record["step_count"], goal.REPEAT_LIMIT)


OPENROUTER_GOAL = ("Can you search for recent emails I got from Open Router, and then if you find any email related to "
                   "billing, then can you please open that?")
# Jev's text answers for the "Ask Gmail" box on both steps of the logged run (2026-09-23 18:28 UTC), spans with p > 0.
# The first entry of each is the whole rest of the request, a "search for" candidate that no longer exists.
OPENROUTER_SPLITS = [
    {"recent emails I got from Open Router, and then if you find any email related to billing, then can you please open that?": 0.05,
     "Open Router": 0.36, "recent emails I got from Open Router": 0.31, "search for recent emails I got from Open Router": 0.05,
     "from Open Router": 0.03, "email related to billing": 0.02, "I got from Open Router": 0.02,
     "for recent emails I got from Open Router": 0.02, "emails I got from Open Router": 0.02, "related to billing": 0.02,
     "any email related to billing": 0.02, "recent emails": 0.02, "Open Router, and then if": 0.02,
     "you search for recent emails": 0.01, "you please open that": 0.01, "search for recent emails I": 0.01,
     "Can you search for recent": 0.01},
    {"recent emails I got from Open Router, and then if you find any email related to billing, then can you please open that?": 0.05,
     "Open Router": 0.35, "recent emails I got from Open Router": 0.31, "from Open Router": 0.03,
     "any email related to billing": 0.03, "search for recent emails I got from Open Router": 0.03,
     "from Open Router, and then if you find any email related": 0.02,
     "Open Router, and then if you find any email related to billing": 0.02, "emails I got from Open Router": 0.02,
     "email related to billing": 0.02, "related to billing": 0.02, "recent emails": 0.02, "billing": 0.02,
     "Open Router, and then if": 0.02, "I got from Open Router": 0.02, "search for recent emails I": 0.01},
]
NEVER = re.compile(r"(?!)")  # a SEARCH_FIELD that matches no field: the behavior before search boxes merged nested text


def text_answer(goal_text, split):
    """A text_to_type answer over the goal's real candidates, from {text: probability}; spans no longer offered drop out."""
    ids = {label[len('Type "'):-1]: key for key, label in goal.text_candidates(goal_text).items()}
    probabilities = {ids[text]: p for text, p in split.items() if text in ids}
    return {"choice": max(probabilities, key=probabilities.get), "probabilities": probabilities}


def sim_controls(case, name):
    """The controls goal_sim builds for one state of a case, and the screen Jev is shown for it."""
    scene = case["states"][name]
    controls = [Control(item[0], item[1], item[2], (20, 60 + i * 28, 300, 84 + i * 28), scene["active"]["title"],
                        state=item[3] if len(item) > 3 else "", value=item[4] if len(item) > 4 else "")
                for i, item in enumerate(scene["controls"])]
    return controls, (scene["active"]["title"], tuple(goal.control_label(c) for c in controls), tuple(scene.get("texts", [])))


def scripted_jev(case, script, splits=None):
    """A stand-in for Jev on a goal_sim case: in each state it answers with the scripted (verb, target, text), and
    reports done only in the success state. splits gives a state's text answer as {text: probability} instead."""
    screens = {}
    for name in case["states"]:
        signature = sim_controls(case, name)[1]
        assert signature not in screens, f"{name} and {screens.get(signature)} look the same to Jev"
        screens[signature] = name

    def ask(_key, goal_text, view, questions, _trace):
        name = screens[(view["activeWindow"]["title"], tuple(view["exposedControls"]), tuple(view["visibleText"]))]
        if "text_to_type" in questions:
            split = (splits or {}).get(name) or {script[name][2]: 0.9}
            return {"text_to_type": text_answer(goal_text, split)}, 1.0
        done = name == case["success"]
        verb, target, _text = script.get(name, ("no_action", None, None))
        answers = {"goal_done": {"noul": 0.95 if done else 0.03},
                   "next_action": {"choice": verb, "probabilities": {verb: 0.9}}}
        for question, spec in questions.items():
            if question.endswith("_target"):
                pick = target if question == f"{goal.HEAD_OF.get(verb)}_target" else "none"
                assert pick in spec["criteria"], f"{pick} is not offered in {name} for {question}"
                answers[question] = {"choice": pick, "probabilities": {pick: 0.9, **({"none": 0.05} if pick != "none" else {})}}
        return answers, 3.0
    return ask


SIM_CASES = {c["id"]: c for c in json.loads(goal_sim.CASES.read_text(encoding="utf-8"))}
# One working path through each case that tests search boxes, flights, and sending email.
SIM_PATHS = {
    "gmail_search_sender_open_billing": {"inbox": ("type_text", "ask", "Open Router"), "typed": ("press_key", "kEnter", None),
                                         "results": ("left_click", "o2", None)},
    "gmail_search_box_billing_from_sender": {"inbox": ("type_text", "ask", "Open Router"), "typed": ("left_click", "search_btn", None),
                                             "results": ("left_click", "o2", None)},
    "flights_dubai_to_los_angeles_today": {
        "home": ("type_text", "from", "Dubai"), "from_typed": ("left_click", "dxb", None),
        "origin_set": ("type_text", "to", "Los Angeles"), "to_typed": ("left_click", "lax", None),
        "route_set": ("left_click", "departure", None), "date_picker": ("left_click", "sep23", None),
        "date_chosen": ("left_click", "done", None), "ready": ("left_click", "search", None)},
    "gmail_reply_offsite_send": {"inbox": ("left_click", "t1", None), "thread": ("left_click", "reply", None),
                                 "reply_open": ("type_text", "body", "Sounds good, see you there"),
                                 "reply_typed": ("left_click", "send", None)},
    "form_contact_send": {"blank": ("type_text", "name", "Test User"),
                          "filled_name": ("type_text", "email", "test.user@example.com"),
                          "filled_email_name": ("type_text", "message", "Please call me back"),
                          "filled_email_message_name": ("left_click", "send", None)},
    "gmail_compose_new_email_send": {"inbox": ("left_click", "compose", None), "blank": ("type_text", "to", "contact@example.com"),
                                     "filled_to": ("type_text", "subject", "Offsite"),
                                     "filled_subject_to": ("type_text", "body", "See you on Friday"),
                                     "filled_body_subject_to": ("left_click", "send", None)},
    "wikipedia_search_ships": {"new_tab": ("type_text", "omnibox", "wikipedia.com"), "url_typed": ("press_key", "kEnter", None),
                               "wiki_home": ("type_text", "wsearch", "ships"), "wiki_typed": ("press_key", "kEnter", None)},
}


class SearchTextTests(unittest.TestCase):
    """A search box takes Jev's text choice when its top answers are longer and shorter spans of one query."""

    def plan(self, field, split, goal_text=OPENROUTER_GOAL, others=(), verbs=None, click=None):
        """One plan_step on a screen with field (and others), Jev choosing type_text into field and text by split."""
        controls = [field, *others]
        answer = text_answer(goal_text, split)
        asked = []

        def ask(_key, _goal, view, questions, _trace):
            asked.append(list(questions))
            if "text_to_type" in questions:
                return {"text_to_type": answer}, 1.0
            answers = {"goal_done": {"noul": 0.04},
                       "next_action": {"choice": "type_text", "probabilities": verbs or {"type_text": 0.93, "left_click": 0.04}},
                       "field_target": {"choice": field.id, "probabilities": {field.id: 0.88, "none": 0.1}}}
            if "control_target" in questions:
                answers["control_target"] = {"choice": click or "none", "probabilities": {click or "none": 0.74}}
            return answers, 3.0

        state = screen("Inbox - Gmail - Google Chrome", controls)[0]
        with patch.object(goal, "ask_questions", ask):
            step = {}
            decision = goal.plan_step("key", goal_text, [], state, state, controls, [], step)
        return decision, step, asked

    def test_logged_openrouter_splits_type_the_query_into_ask_gmail(self):
        ask_gmail = Control("c0037", "Ask Gmail", "Edit", (411, 242, 1183, 268), "Gmail")
        for split in OPENROUTER_SPLITS:
            decision, step, _ = self.plan(ask_gmail, split)
            self.assertEqual((decision, step["verb"]["choice"], step["text"]), ("act", "type_text", "Open Router"))
            self.assertEqual(step["text_rule"], "longer and shorter spans of one query")
            self.assertGreater(step["text_p"], 0.75)
            self.assertNotIn("tried", step)

    def test_before_the_merge_the_logged_split_was_unclear(self):
        ask_gmail = Control("c0037", "Ask Gmail", "Edit", (411, 242, 1183, 268), "Gmail")
        with patch.object(goal, "SEARCH_FIELD", NEVER):
            decision, step, _ = self.plan(ask_gmail, OPENROUTER_SPLITS[1])
        self.assertEqual((decision, step["reason"]), ("blocked", "text to type is unclear"))
        self.assertNotIn("text", step)
        self.assertNotIn("text_rule", step)

    def test_logged_step_types_instead_of_falling_back_to_the_search_button(self):
        # Step 3 of the logged run: type_text 0.78, left_click 0.19; unclear text made it click "Search mail" instead.
        ask_gmail = Control("c0037", "Ask Gmail", "Edit", (411, 242, 1183, 268), "Gmail")
        button = Control("c0039", "Search mail", "Button", (343, 225, 413, 283), "Gmail")
        verbs = {"type_text": 0.78, "left_click": 0.19}
        decision, step, _ = self.plan(ask_gmail, OPENROUTER_SPLITS[0], others=[button], verbs=verbs, click="c0039")
        self.assertEqual((decision, step["verb"]["choice"], step["target"]["id"], step["text"]),
                         ("act", "type_text", "c0037", "Open Router"))
        with patch.object(goal, "SEARCH_FIELD", NEVER):
            decision, step, _ = self.plan(ask_gmail, OPENROUTER_SPLITS[0], others=[button], verbs=verbs, click="c0039")
        self.assertEqual((decision, step["verb"]["choice"], step["target"]["id"]), ("act", "left_click", "c0039"))
        self.assertEqual(step["tried"][0]["reason"], "text to type is unclear")

    def test_every_kind_of_search_box_merges(self):
        for name, role in [("Ask Gmail", "Edit"), ("Search mail", "Edit"), ("Address and search bar", "Edit"),
                           ("Search Google or type a URL", "ComboBox"), ("Search Wikipedia", "ComboBox"),
                           ("Search", "ComboBox"), ("Find what:", "Edit"), ("Look up", "Edit"), ("Filter", "Edit"),
                           ("Search query", "Edit")]:
            with self.subTest(name=name):
                decision, step, _ = self.plan(Control("q", name, role, (0, 0, 300, 20), "Page"), OPENROUTER_SPLITS[0])
                self.assertEqual((decision, step.get("text")), ("act", "Open Router"))

    def test_other_fields_keep_the_strict_margin(self):
        # A message, subject, recipient, name, or city loses words when a shorter span is typed, so no merge there.
        for name, role in [("Message Body", "Edit"), ("Subject", "Edit"), ("To recipients", "ComboBox"),
                           ("Where from?", "ComboBox"), ("Where to?", "ComboBox"), ("Task name", "Edit"),
                           ("Text editor", "Edit"), ("Name", "Edit")]:
            with self.subTest(name=name):
                self.assertIsNone(goal.SEARCH_FIELD.search(name))
                decision, step, _ = self.plan(Control("f", name, role, (0, 0, 300, 20), "Page"), OPENROUTER_SPLITS[0])
                self.assertEqual((decision, step["reason"]), ("blocked", "text to type is unclear"))

    def test_email_body_is_never_cut_short(self):
        goal_text = "Reply to the offsite email saying Sounds good, see you there"
        body = Control("body", "Message Body", "Edit", (0, 0, 600, 300), "Gmail", state="focused")
        decision, step, _ = self.plan(body, {"Sounds good": 0.4, "Sounds good, see you there": 0.35, "the offsite email": 0.25},
                                      goal_text)
        self.assertEqual(decision, "blocked")
        self.assertNotIn("text", step)
        decision, step, _ = self.plan(body, {"Sounds good, see you there": 0.8, "Sounds good": 0.2}, goal_text)
        self.assertEqual((decision, step["text"]), ("act", "Sounds good, see you there"))
        self.assertNotIn("text_rule", step)  # confident on its own: the merge is never consulted

    def test_flight_fields_are_not_search_boxes(self):
        # "Dubai" inside "flights from Dubai to Los Angeles" is nested, but the origin box must get the city alone.
        goal_text = "Now help me search for flights from Dubai to Los Angeles for today."
        origin = Control("from", "Where from?", "ComboBox", (0, 0, 200, 20), "Flights", state="collapsed", value="Ahmedabad")
        decision, _, _ = self.plan(origin, {"flights from Dubai to Los Angeles": 0.4, "Dubai": 0.35, "Los Angeles": 0.25},
                                   goal_text)
        self.assertEqual(decision, "blocked")
        # The logged run typed "Dubai" at 0.48 (under 0.5, over twice the runner-up): unchanged.
        decision, step, _ = self.plan(origin, {"Dubai": 0.48, "flights from Dubai": 0.2, "Los Angeles": 0.12}, goal_text)
        self.assertEqual((decision, step["text"]), ("act", "Dubai"))
        self.assertNotIn("text_rule", step)

    def test_the_chosen_span_is_typed_even_when_it_is_the_longer_one(self):
        field = Control("q", "Ask Gmail", "Edit", (0, 0, 300, 20), "Gmail")
        decision, step, _ = self.plan(field, {"recent emails I got from Open Router": 0.36, "Open Router": 0.31,
                                              "from Open Router": 0.1, "billing": 0.08})
        self.assertEqual((decision, step["text"]), ("act", "recent emails I got from Open Router"))
        self.assertAlmostEqual(step["text_p"], 0.77)

    def test_unrelated_runner_up_is_still_unclear(self):
        field = Control("q", "Ask Gmail", "Edit", (0, 0, 300, 20), "Gmail")
        decision, step, _ = self.plan(field, {"Open Router": 0.36, "billing": 0.31, "recent emails": 0.2})
        self.assertEqual((decision, step["reason"]), ("blocked", "text to type is unclear"))
        self.assertNotIn("text_rule", step)
        # A longer span holding the choice agrees with it even when the runner-up is unrelated: 0.36 + 0.2 = 0.56.
        decision, step, _ = self.plan(field, {"Open Router": 0.36, "billing": 0.31, "from Open Router": 0.2})
        self.assertEqual((decision, step["text"]), ("act", "Open Router"))
        self.assertAlmostEqual(step["text_p"], 0.56)

    def test_logged_google_flights_address_bar_splits(self):
        # Live replays of "go to Google Flights and then ... search for flights from Dubai to Los Angeles where ..."
        # in the address bar. "go to Google Flights" agrees with "Google Flights"; the flight query is a rival.
        goal_text = ("Can you please go to Google Flights and then can you please search for flights from Dubai to Los "
                     "Angeles where the departure is today and the return is two days after today. The date today is "
                     "23rd of September, 2026.")
        query = ("flights from Dubai to Los Angeles where the departure is today and the return is two days after "
                 "today. The date today is 23rd of September, 2026.")
        bar = Control("bar", "Address and search bar", "Edit", (0, 0, 900, 30), "Chrome", state="focused")
        rest = {"flights from Dubai to Los Angeles": 0.08, "Dubai to Los Angeles": 0.05}
        decision, step, _ = self.plan(bar, {"Google Flights": 0.35, query: 0.19, "go to Google Flights": 0.08, **rest}, goal_text)
        self.assertEqual((decision, step["text"], step["text_rule"]),
                         ("act", "Google Flights", "longer and shorter spans of one query"))
        self.assertAlmostEqual(step["text_p"], 0.43)
        with patch.object(goal, "SEARCH_FIELD", NEVER):  # 0.35 is under twice 0.19 on its own
            self.assertEqual(self.plan(bar, {"Google Flights": 0.35, query: 0.19, "go to Google Flights": 0.08, **rest},
                                       goal_text)[0], "blocked")
        # 0.30 + 0.09 against 0.22 stays unclear: Jev really is split between the site and the flight search.
        decision, _, _ = self.plan(bar, {"Google Flights": 0.30, query: 0.22, "go to Google Flights": 0.09, **rest}, goal_text)
        self.assertEqual(decision, "blocked")

    def test_a_runner_up_that_runs_into_the_next_instruction_does_not_agree(self):
        field = Control("q", "Ask Gmail", "Edit", (0, 0, 300, 20), "Gmail")
        decision, step, _ = self.plan(field, {"Open Router": 0.36, "Open Router, and then if": 0.31, "billing": 0.1})
        self.assertEqual((decision, step["reason"]), ("blocked", "text to type is unclear"))

    def test_other_spans_inside_a_long_choice_are_rivals(self):
        # Choosing "recent emails I got from Open Router" over "Open Router": "recent emails" is a different search.
        spans = goal.text_candidates(OPENROUTER_GOAL)
        answer = text_answer(OPENROUTER_GOAL, {"recent emails I got from Open Router": 0.4, "Open Router": 0.3,
                                               "from Open Router": 0.1, "recent emails": 0.15, "billing": 0.05})
        probability, rival = goal._nested_text(answer, spans)
        self.assertAlmostEqual(probability, 0.8)
        self.assertEqual((spans[rival["id"]], rival["probability"]), ('Type "recent emails"', 0.15))

    def test_merged_texts_still_need_the_margin_over_a_different_text(self):
        field = Control("q", "Search mail", "Edit", (0, 0, 300, 20), "Gmail")
        # 0.25 + 0.20 = 0.45 is under 0.5 and under twice "billing" (0.30).
        split = {"Open Router": 0.25, "from Open Router": 0.20, "billing": 0.30, "recent emails": 0.25}
        decision, step, _ = self.plan(field, split)
        self.assertEqual((decision, step["reason"]), ("blocked", "text to type is unclear"))
        split = {"Open Router": 0.25, "from Open Router": 0.20, "billing": 0.20, "recent emails": 0.1}
        decision, step, _ = self.plan(field, split)
        self.assertEqual((decision, step["text"]), ("act", "Open Router"))
        self.assertAlmostEqual(step["text_p"], 0.45)

    def test_a_choice_that_runs_into_the_next_instruction_is_not_typed(self):
        field = Control("q", "Ask Gmail", "Edit", (0, 0, 300, 20), "Gmail")
        decision, step, _ = self.plan(field, {"Open Router, and then if": 0.36, "Open Router": 0.31, "from Open Router": 0.2})
        self.assertEqual((decision, step["reason"]), ("blocked", "text to type is unclear"))
        self.assertNotIn("text", step)

    def test_spans_nested_with_only_one_of_the_top_two_are_rivals(self):
        answer = text_answer(OPENROUTER_GOAL, OPENROUTER_SPLITS[1])
        spans = goal.text_candidates(OPENROUTER_GOAL)
        probability, rival = goal._nested_text(answer, spans)
        # Counted: the two top answers and the spans around both; not "Open Router, and then if you find...".
        self.assertAlmostEqual(probability, 0.35 + 0.31 + 0.03 + 0.03 + 0.02 + 0.02)
        self.assertEqual(rival["probability"], 0.03)
        self.assertEqual(spans[rival["id"]], 'Type "any email related to billing"')

    def test_nested_text_needs_a_nested_runner_up(self):
        spans = goal.text_candidates(OPENROUTER_GOAL)
        self.assertIsNone(goal._nested_text(text_answer(OPENROUTER_GOAL, {"Open Router": 1.0}), spans))
        self.assertIsNone(goal._nested_text(text_answer(OPENROUTER_GOAL, {"Open Router": 0.5, "billing": 0.5}), spans))
        self.assertIsNone(goal._nested_text({"choice": "t0", "probabilities": {"t0": 0.5, "t9999": 0.5}},
                                            {"t0": 'Type "Open Router"'}))  # runner-up is not a candidate

    def test_nested_span_compares_whole_words(self):
        nested = goal._nested_span
        self.assertTrue(nested("Open Router", "recent emails I got from Open Router"))
        self.assertTrue(nested("recent emails I got from Open Router", "Open Router"))
        self.assertTrue(nested("open router", "Open Router, and then"))  # case and punctuation do not matter
        self.assertTrue(nested("Open Router", "Open Router"))
        self.assertFalse(nested("art", "party"))
        self.assertFalse(nested("Router", "Routers of the world"))
        self.assertFalse(nested("New York", "York New"))
        self.assertFalse(nested("Open Router", "Open the Router"))
        self.assertFalse(nested("", "Open Router"))
        self.assertFalse(nested("...", "Open Router"))

    def test_search_cue_candidate_stops_before_the_next_instruction(self):
        cases = {
            OPENROUTER_GOAL: "recent emails I got from Open Router",
            "search for ships and then open the first result": "ships",
            "search for ships, then open the first result": "ships",
            "Search for flights from Dubai to Los Angeles; pick the cheapest": "flights from Dubai to Los Angeles",
            "look up Alan Turing then read his biography": "Alan Turing",
            "search for weather in San Diego": "weather in San Diego",
        }
        for goal_text, query in cases.items():
            with self.subTest(goal=goal_text):
                texts = [label[len('Type "'):-1] for label in goal.text_candidates(goal_text).values()]
                self.assertEqual(texts[0], query)
                self.assertFalse(any(re.search(r"\bthen\b", t) and len(t.split()) > 14 for t in texts))
        texts = goal.text_candidates(OPENROUTER_GOAL).values()
        self.assertNotIn(f'Type "{OPENROUTER_GOAL.split("search for ", 1)[1]}"', texts)

    def test_dictation_cues_keep_the_whole_text(self):
        # "then" inside dictated text is part of the message, not a new instruction.
        message = "buy milk and then call mom, then pick up the kids"
        self.assertIn(f'Type "{message}"', goal.text_candidates(f"Type {message}").values())
        self.assertIn(f'Type "{message}"', goal.text_candidates(f"Write {message}").values())
        self.assertIn(f'Type "{message}"', goal.text_candidates(f"In Notepad, dictate {message}").values())

    def test_merge_is_recorded_only_when_it_decides(self):
        field = Control("q", "Ask Gmail", "Edit", (0, 0, 300, 20), "Gmail")
        # Merged but still short of the margin: the rule is not left on the step.
        decision, step, _ = self.plan(field, {"Open Router": 0.2, "from Open Router": 0.15, "billing": 0.3, "recent emails": 0.35})
        self.assertEqual(decision, "blocked")
        self.assertNotIn("text_rule", step)
        self.assertNotIn("text_p", step)


class SimCaseTests(unittest.TestCase):
    """goal_sim cases are well formed, and the new ones complete with a scripted Jev through the real planner."""

    def test_every_case_is_well_formed_and_reachable(self):
        for case in SIM_CASES.values():
            with self.subTest(case=case["id"]):
                states = case["states"]
                self.assertIn(case["start"], states)
                self.assertIn(case["success"], states)
                texts = {label[len('Type "'):-1] for label in goal.text_candidates(case["goal"]).values()}
                seen, frontier = {case["start"]}, [case["start"]]
                while frontier:
                    for transition, rule in states[frontier.pop()]["transitions"].items():
                        destination = rule["next"] if isinstance(rule, dict) else rule
                        self.assertIn(destination, states, transition)
                        if isinstance(rule, dict):
                            literals = rule["text"] if isinstance(rule["text"], list) else [rule["text"]]
                            for literal in literals:  # Jev can only type a span of the request
                                self.assertIn(literal, texts, f"{transition} needs text the request does not contain")
                        if destination not in seen:
                            seen.add(destination)
                            frontier.append(destination)
                self.assertIn(case["success"], seen)
                for name, scene in states.items():
                    for control in scene["controls"]:
                        self.assertIn(control[2], windows.ROLES, name)

    def test_scripted_paths_complete_the_new_cases(self):
        for case_id, path in SIM_PATHS.items():
            with self.subTest(case=case_id):
                case = SIM_CASES[case_id]
                with patch.object(goal, "ask_questions", scripted_jev(case, path)):
                    result = goal_sim.run_case("key", case)
                self.assertTrue(result["passed"], result)
                self.assertEqual(len(result["steps"]), len(path) + 1)  # every scripted action, then done

    def test_logged_openrouter_split_completes_the_simulated_gmail_search(self):
        case = SIM_CASES["gmail_search_sender_open_billing"]
        path = SIM_PATHS[case["id"]]
        for split in OPENROUTER_SPLITS:
            with patch.object(goal, "ask_questions", scripted_jev(case, path, {"inbox": split})):
                result = goal_sim.run_case("key", case)
            self.assertTrue(result["passed"], result)
            self.assertIn('text "Open Router"', result["steps"][0])
            with patch.object(goal, "ask_questions", scripted_jev(case, path, {"inbox": split})), \
                    patch.object(goal, "SEARCH_FIELD", NEVER):
                result = goal_sim.run_case("key", case)
            self.assertEqual((result["outcome"], result["reason"]), ("blocked", "text to type is unclear"))

    def test_the_longer_query_also_reaches_the_billing_email(self):
        case = SIM_CASES["gmail_search_sender_open_billing"]
        split = {"recent emails I got from Open Router": 0.4, "Open Router": 0.3, "billing": 0.1}
        with patch.object(goal, "ask_questions", scripted_jev(case, SIM_PATHS[case["id"]], {"inbox": split})):
            result = goal_sim.run_case("key", case)
        self.assertTrue(result["passed"], result)
        self.assertIn('text "recent emails I got from Open Router"', result["steps"][0])

    def test_a_wrong_text_or_target_fails_the_simulated_case(self):
        case = SIM_CASES["gmail_search_sender_open_billing"]
        path = {**SIM_PATHS[case["id"]], "results": ("left_click", "o1", None)}  # "New models", not the receipt
        with patch.object(goal, "ask_questions", scripted_jev(case, path)):
            result = goal_sim.run_case("key", case)
        self.assertFalse(result["passed"])
        self.assertEqual(result["outcome"], "error")
        case = SIM_CASES["gmail_reply_offsite_send"]
        path = {**SIM_PATHS[case["id"]], "reply_open": ("type_text", "body", "Sounds good")}  # cut short
        with patch.object(goal, "ask_questions", scripted_jev(case, path)):
            result = goal_sim.run_case("key", case)
        self.assertIn("did not match", result["reason"])

    def test_flight_fields_get_one_city_each(self):
        case = SIM_CASES["flights_dubai_to_los_angeles_today"]
        path = {**SIM_PATHS[case["id"]], "home": ("type_text", "from", "Los Angeles")}  # destination into origin
        with patch.object(goal, "ask_questions", scripted_jev(case, path)):
            result = goal_sim.run_case("key", case)
        self.assertFalse(result["passed"])
        path = {**SIM_PATHS[case["id"]], "route_set": ("type_text", "departure", "today"),
                "date_typed": ("press_key", "kEnter", None)}  # typing "today" as the logged run did
        del path["date_picker"], path["date_chosen"]
        with patch.object(goal, "ask_questions", scripted_jev(case, path)):
            result = goal_sim.run_case("key", case)
        self.assertTrue(result["passed"], result)

    def test_contact_form_fields_can_be_filled_in_any_order(self):
        case = SIM_CASES["form_contact_send"]
        path = {"blank": ("type_text", "message", "Please call me back"),
                "filled_message": ("type_text", "email", "test.user@example.com"),
                "filled_email_message": ("type_text", "name", "Test User"),
                "filled_email_message_name": ("left_click", "send", None)}
        with patch.object(goal, "ask_questions", scripted_jev(case, path)):
            result = goal_sim.run_case("key", case)
        self.assertTrue(result["passed"], result)
        # Send before every field is filled has no transition.
        with patch.object(goal, "ask_questions", scripted_jev(case, {"blank": ("left_click", "send", None)})):
            self.assertFalse(goal_sim.run_case("key", case)["passed"])


class FakeElement:
    """Just enough of a pywinauto UIA wrapper for the collector and target re-resolution."""

    def __init__(self, role, name="", rect=(0, 0, 100, 100), children=(), focused=False, value=None, password=False,
                 read_only=False):
        self.element_info = type("Info", (), {"name": name, "control_type": role,
                                              "element": type("Raw", (), {"CurrentIsPassword": password})()})()
        self._rect, self._children, self._focused = rect, list(children), focused
        if value is not None:
            self.iface_value = type("Value", (), {"CurrentValue": value, "CurrentIsReadOnly": read_only})()

    def rectangle(self):
        left, top, right, bottom = self._rect
        return type("Rect", (), {"left": left, "top": top, "right": right, "bottom": bottom})()

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def children(self):
        return self._children

    def descendants(self):
        found = []
        for child in self._children:
            found += [child] + child.descendants()
        return found

    def has_keyboard_focus(self):
        return self._focused


def fake_desktop(root):
    window = type("Window", (), {"wrapper_object": lambda self: root})()
    return lambda backend: type("Desktop", (), {"window": lambda self, handle: window})()


class CaptureTests(unittest.TestCase):
    def collect(self, root):
        controls, texts = [], []
        with patch.object(windows, "Desktop", fake_desktop(root)):
            windows._collect(1, "", controls, 0, 3000, own=True, texts=texts)
        return controls, texts

    def test_card_text_is_kept_as_detail_and_deep_controls_are_found(self):
        card = FakeElement("Hyperlink", "Text-to-Image • 7B", (10, 10, 90, 40),
                           [FakeElement("Text", "Qwen/Qwen-Image-2.1", (12, 12, 80, 20)), FakeElement("Text", "7B", (12, 22, 30, 30))])
        deep = FakeElement("ListItem", "US Re-entry", (10, 50, 90, 60))
        for _ in range(30):  # a web app nests its file tiles far below the old depth cap of 20
            deep = FakeElement("Group", "", (0, 0, 100, 100), [deep])
        controls, texts = self.collect(FakeElement("Window", "Page", (0, 0, 100, 100), [card, deep]))
        by_name = {c.name: c for c in controls}
        self.assertEqual(by_name["Text-to-Image • 7B"].detail, "Qwen/Qwen-Image-2.1")
        self.assertIn("US Re-entry", by_name)
        self.assertIn("Qwen/Qwen-Image-2.1", texts)

    def test_prefilled_field_values_are_captured_but_passwords_are_not(self):
        origin = FakeElement("ComboBox", "Where from?", (0, 10, 90, 40), value="Ahmedabad")
        empty = FakeElement("ComboBox", "Where to?", (0, 40, 90, 70), value="")
        secret = FakeElement("Edit", "Password", (0, 70, 90, 95), value="hunter2", password=True)
        controls, _ = self.collect(FakeElement("Window", "Page", (0, 0, 100, 100), [origin, empty, secret]))
        self.assertEqual([(c.name, c.value) for c in controls], [("Where from?", "Ahmedabad"), ("Where to?", ""), ("Password", "")])

    def type_into(self, field):
        wrapper = Mock()
        typed = Mock()
        with (patch.object(windows, "_wait_for_foreground"), patch.object(windows, "_ensure_uncovered"),
              patch.object(windows, "_find_fresh_wrapper", return_value=wrapper),
              patch.object(windows.keyboard, "send_keys") as keys,
              patch("pynput.keyboard.Controller", return_value=typed), patch.object(windows.time, "sleep")):
            result = windows.execute("type_text", {"id": field.id}, {"activeWindow": {"hwnd": 1}}, [field], [], "", text="Los Angeles")
        self.assertEqual("".join(c.args[0] for c in typed.type.call_args_list), "Los Angeles")
        return result, [c.args[0] for c in keys.call_args_list]

    def test_typing_replaces_a_prefilled_single_line_field(self):
        origin = Control("c1", "Where from?", "ComboBox", (254, 758, 607, 828), "", value="Ahmedabad")
        result, keys = self.type_into(origin)
        self.assertEqual(keys, ["^a"])
        self.assertEqual(result, 'replaced "Ahmedabad" with 11 characters in Where from?')

    def test_typing_appends_in_a_document_sized_field(self):
        body = Control("c1", "Message body", "Edit", (0, 0, 800, 600), "", value="Dear team,")
        result, keys = self.type_into(body)
        self.assertEqual(keys, [])
        self.assertEqual(result, "typed 11 characters into Message body")

    def test_repeated_names_carry_the_text_read_just_before_them(self):
        rows = [FakeElement("Group", "", (0, 10 + 20 * i, 100, 30 + 20 * i),
                            [FakeElement("Text", name, (0, 10 + 20 * i, 50, 30 + 20 * i)),
                             FakeElement("Button", "Rename", (60, 10 + 20 * i, 90, 30 + 20 * i))])
                for i, name in enumerate(["budget.xlsx", "draft.txt"])]
        controls, _ = self.collect(FakeElement("Window", "Files", (0, 0, 100, 100), rows + [FakeElement("Button", "Help", (0, 80, 30, 95))]))
        self.assertEqual([(c.name, c.context) for c in controls], [("Rename", "budget.xlsx"), ("Rename", "draft.txt"), ("Help", "")])
        self.assertEqual(core.describe_control(controls[1]), 'Button "Rename" near "draft.txt"')

    def test_only_an_editable_document_is_a_typing_target(self):
        editor = FakeElement("Document", "Text editor", (0, 10, 100, 90), value="Agenda")
        page = FakeElement("Document", "Expense report", (0, 10, 100, 90), value="http://127.0.0.1/", read_only=True)
        controls, _ = self.collect(FakeElement("Window", "Notepad", (0, 0, 100, 100), [editor]))
        self.assertEqual([(c.role, c.value) for c in controls], [("Document", "Agenda")])
        self.assertEqual(self.collect(FakeElement("Window", "Chrome", (0, 0, 100, 100), [page]))[0], [])

    def test_a_combo_box_arrow_named_open_is_not_a_target(self):
        types = FakeElement("ComboBox", "Files of type:", (0, 10, 60, 30), [FakeElement("Button", "Open", (50, 12, 58, 28))], value="All Files")
        dialog = FakeElement("Window", "Open", (0, 0, 100, 100), [types, FakeElement("SplitButton", "Open", (60, 40, 90, 60))])
        controls, _ = self.collect(dialog)
        self.assertEqual([(c.role, c.name) for c in controls], [("ComboBox", "Files of type:"), ("SplitButton", "Open")])

    def test_subtrees_outside_the_window_are_skipped(self):
        below = FakeElement("Group", "", (0, 500, 100, 900), [FakeElement("Hyperlink", "Far below", (0, 600, 50, 620))])
        edge = FakeElement("Group", "", (0, 80, 100, 300), [FakeElement("Hyperlink", "Half shown", (0, 90, 50, 110))])
        field = FakeElement("ComboBox", "Where from?", (0, 20, 50, 40), focused=True)
        controls, _ = self.collect(FakeElement("Window", "Page", (0, 0, 100, 100), [below, edge, field]))
        self.assertEqual([c.name for c in controls], ["Half shown", "Where from?"])
        self.assertIn("focused", controls[1].state)

    def test_duplicate_or_moved_target_still_resolves(self):
        chosen = Control("c0", "youtube", "Hyperlink", (10, 10, 50, 30), "")
        twice = [FakeElement("Hyperlink", "youtube", (11, 10, 51, 30)) for _ in range(3)]
        with patch.object(windows, "Desktop", fake_desktop(FakeElement("Window", "", children=twice))):
            self.assertIs(windows._find_fresh_wrapper(1, chosen), twice[0])
        moved = FakeElement("Button", "Hansin Patwa", (10, 300, 90, 340))
        with patch.object(windows, "Desktop", fake_desktop(FakeElement("Window", "", children=[moved]))):
            self.assertIs(windows._find_fresh_wrapper(1, Control("c1", "Hansin Patwa", "Button", (10, 100, 90, 140), "")), moved)
        two = [FakeElement("Button", "Reply", (10, 300, 90, 340)), FakeElement("Button", "Reply", (10, 500, 90, 540))]
        with patch.object(windows, "Desktop", fake_desktop(FakeElement("Window", "", children=two))), \
                self.assertRaises(RuntimeError):
            windows._find_fresh_wrapper(1, Control("c2", "Reply", "Button", (10, 100, 90, 140), ""))


if __name__ == "__main__":
    unittest.main()
