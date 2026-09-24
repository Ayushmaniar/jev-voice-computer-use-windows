"""Offline tests for sliders: the categorical changes, the three ways of moving one, and how Jev is asked."""

import unittest
from unittest.mock import patch

from . import goal, sliders
from .core import Control, eligible_targets
from .sliders import Reading, target_value


class FakeSlider:
    """A slider as UI Automation shows it. `pattern` offers RangeValue; `focusable` takes keys; the track maps a click
    at fraction f of its length to low + (f - pad) / (1 - 2 pad) of the range, like a styled track with end padding."""

    def __init__(self, value=40.0, low=0.0, high=100.0, small=1.0, pattern=True, writable=True, focusable=True,
                 readable=True, pad=0.0, rect=(100, 10, 300, 30)):
        self.value, self.low, self.high, self.small = value, low, high, small
        self.pattern, self.writable, self.focusable, self.readable, self.pad = pattern, writable, focusable, readable, pad
        self.rect = rect
        self.keys, self.clicks = [], []
        outer = self

        class Range:
            @property
            def CurrentValue(self): return outer.value
            CurrentMinimum, CurrentMaximum, CurrentSmallChange, CurrentLargeChange = low, high, small, small * 10
            CurrentIsReadOnly = not writable

            def SetValue(self, value):
                outer.value = min(outer.high, max(outer.low, value))

        class Value:
            @property
            def CurrentValue(self): return f"{outer.value:g}" if outer.readable else ""

        class Element:
            CurrentIsKeyboardFocusable = focusable
            CurrentOrientation = 1

        class Info:
            element = Element()

        self._range, self._value, self.element_info = Range(), Value(), Info()

    @property
    def iface_range_value(self):
        if not self.pattern:
            raise AttributeError("no RangeValue")
        return self._range

    @property
    def iface_value(self):
        return self._value

    def set_focus(self):
        pass

    # keyboard and mouse, as sliders.py drives them
    def press(self, keys):
        self.keys.append(keys)
        if keys == "{END}":
            self.value = self.high
        elif keys == "{HOME}":
            self.value = self.low
        else:
            direction, count = keys.strip("{}").split()
            self.value = min(self.high, max(self.low, self.value + (1 if direction == "RIGHT" else -1) * int(count) * self.small))

    def click(self, coords):
        x = coords[0]
        fraction = (x - self.rect[0] - 1) / (self.rect[2] - self.rect[0] - 2)
        inner = min(1.0, max(0.0, (fraction - self.pad) / (1 - 2 * self.pad)))
        self.clicks.append(round(fraction, 3))
        self.value = round(self.low + inner * (self.high - self.low))


def move(slider, change):
    with patch("pywinauto.keyboard.send_keys", slider.press), patch("pywinauto.mouse.click", slider.click), \
            patch.object(sliders.time, "sleep"):
        return sliders.move(slider, slider.rect, change)


class ChangeTests(unittest.TestCase):
    def test_steps_are_a_share_of_the_slider_s_own_range(self):
        self.assertEqual(target_value("increase_10", Reading(40, 0, 100)), 50)
        self.assertEqual(target_value("decrease_25", Reading(0.6, 0, 1)), 0.35)
        self.assertEqual(target_value("increase_25", Reading(90, 0, 100)), 100)  # clamped
        self.assertEqual(target_value("max", Reading(3, 1, 5)), 5)
        self.assertEqual(target_value("middle", Reading(3, 0, 200)), 100)
        self.assertIsNone(target_value("increase_5", Reading(None)))  # relative, with nothing to start from

    def test_a_number_the_user_said_is_a_value_or_a_share_of_the_range(self):
        self.assertEqual(target_value("set_30", Reading(40, 0, 100)), 30)
        self.assertEqual(target_value("set_50pct", Reading(0, 0, 200)), 100)
        self.assertEqual(target_value("set_70", Reading(0.2, 0, 1)), 0.7)  # "70" on a 0-1 slider means 70%

    def test_numbers_in_the_request_become_set_choices(self):
        criteria = sliders.change_criteria("set the volume to 30 and brightness to 80%")
        self.assertIn("set_30", criteria)
        self.assertIn("set_80pct", criteria)
        self.assertIn("increase_10", criteria)
        self.assertIn("none", criteria)
        self.assertTrue(sliders.valid_change("set_80pct"))
        self.assertFalse(sliders.valid_change("increase_7"))


class MoveTests(unittest.TestCase):
    def test_range_value_is_set_directly(self):
        slider = FakeSlider(value=40)
        self.assertIn("from 40 (0 to 100) to 100 (0 to 100) (value)", move(slider, "max"))
        self.assertEqual(slider.keys, [])

    def test_a_slider_that_ignores_set_value_is_moved_with_keys(self):
        slider = FakeSlider(value=40, writable=False)
        self.assertIn("(keyboard)", move(slider, "increase_10"))
        self.assertEqual(slider.value, 50)
        self.assertEqual(slider.keys, ["{RIGHT 10}"])

    def test_a_player_slider_is_clicked_and_corrected_from_what_it_reads_back(self):
        # VLC's volume: no RangeValue, no focus, a track with padding at both ends, value only as text.
        slider = FakeSlider(value=70, pattern=False, focusable=False, pad=0.08)
        result = move(slider, "set_25")
        self.assertIn("(mouse)", result)
        self.assertLessEqual(abs(slider.value - 25), 2)
        self.assertGreater(len(slider.clicks), 1)  # the first click, at 25% of the track, landed on 20

    def test_max_on_a_player_slider_clicks_the_end_of_the_track(self):
        slider = FakeSlider(value=25, pattern=False, focusable=False, pad=0.08)
        move(slider, "max")
        self.assertEqual(slider.value, 100)

    def test_already_there_does_nothing(self):
        slider = FakeSlider(value=100)
        self.assertEqual(move(slider, "max"), "already at 100 (0 to 100)")

    def test_a_slider_that_never_moves_is_reported(self):
        slider = FakeSlider(value=40, writable=False, focusable=False)
        slider.click = lambda coords: None  # the app ignores the click too
        with self.assertRaises(RuntimeError):
            move(slider, "set_70")

    def test_unnamed_sliders_are_described_by_length_and_place(self):
        frame = (0, 0, 1000, 600)
        self.assertEqual(sliders.unnamed_label((850, 540, 950, 560), frame),
                         "unnamed short horizontal slider, bottom right of the window")
        self.assertEqual(sliders.unnamed_label((50, 500, 800, 520), frame),
                         "unnamed long horizontal slider, bottom center of the window")


class JevChoiceTests(unittest.TestCase):
    def controls(self):
        return [Control("c0", "Brightness", "Slider", (0, 0, 200, 20), "Display", value="40 (0 to 100)"),
                Control("c1", "Save", "Button", (0, 40, 60, 60), "Display")]

    def test_sliders_are_set_not_clicked(self):
        controls = self.controls()
        self.assertEqual([t["id"] for t in eligible_targets("set_slider", controls, [], [], 1)], ["none", "c0"])
        self.assertNotIn("c0", [t["id"] for t in eligible_targets("left_click", controls, [], [], 1)])

    def test_goal_mode_asks_how_far_in_the_same_request(self):
        controls = self.controls()
        state = {"activeWindow": {"hwnd": 1, "title": "Display", "process": "chrome.exe"}, "openWindows": [], "texts": []}
        asked = {}

        def ask(_key, _goal, _view, questions, _trace):
            asked.update(questions)
            return {"goal_done": {"noul": 0.02},
                    "next_action": {"choice": "set_slider", "probabilities": {"set_slider": 0.9, "left_click": 0.1}},
                    "slider_target": {"choice": "c0", "probabilities": {"c0": 0.95, "none": 0.05}},
                    "control_target": {"choice": "c1", "probabilities": {"c1": 0.9}},
                    "slider_change": {"choice": "max", "probabilities": {"max": 0.9, "increase_25": 0.1}}}, 3.0

        step = {}
        with patch.object(goal, "ask_questions", ask):
            decision = goal.plan_step("key", "Turn the brightness all the way up", [], state, state, controls, [], step)
        self.assertEqual(decision, "act")
        self.assertIn("slider_change", asked)
        self.assertEqual(step["target"]["id"], "c0")
        self.assertEqual(step["target"]["change"], "max")

    def test_an_unclear_amount_is_not_guessed(self):
        controls = self.controls()
        state = {"activeWindow": {"hwnd": 1, "title": "Display", "process": "chrome.exe"}, "openWindows": [], "texts": []}

        def ask(*_args):
            return {"goal_done": {"noul": 0.02},
                    "next_action": {"choice": "set_slider", "probabilities": {"set_slider": 0.95}},
                    "slider_target": {"choice": "c0", "probabilities": {"c0": 0.95}},
                    "slider_change": {"choice": "increase_10", "probabilities": {"increase_10": 0.4, "increase_25": 0.38}}}, 3.0

        step = {}
        with patch.object(goal, "ask_questions", ask):
            decision = goal.plan_step("key", "adjust the brightness", [], state, state, controls, [], step)
        self.assertEqual(decision, "blocked")
        self.assertNotIn("change", step.get("target", {}))


if __name__ == "__main__":
    unittest.main()
