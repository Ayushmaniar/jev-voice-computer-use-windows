"""Offline tests for settling and stale decisions: scripted signal sources and a fake clock, no windows."""

import dataclasses
import unittest
from unittest import mock

from . import goal, settle, uia_watch
from .core import Control
from .settle import Armed, Settler, settle_page


class FakePage:
    """Signal source as a function of time since the action: `script(t)` returns (poll, counters)."""

    def __init__(self, script):
        self.script = script
        self.t = 0.0

    def poll(self):
        return self.script(self.t)[0]

    def counters(self, _now):
        return self.script(self.t)[1]

    def frames(self):
        pass


def run(page, verb="left_click", ambient=False, idle_only=False, quick=None, until_change=None):
    def sleep(seconds):
        page.t += seconds

    armed = Armed(page, requests=0, navigations=0, count=0, ambient=ambient)
    reason, facts = settle_page(page, armed, verb, idle_only, quick, until_change=until_change,
                                clock=lambda: page.t, sleep=sleep)
    return reason, round(page.t, 3), facts


def poll(count=0, quiet=10.0, ready="complete", visible=True):
    return {"count": count, "quietMs": quiet * 1000, "installedMs": 10_000, "ready": ready, "visible": visible}


def counters(requests=0, navigations=0, loading=False, inflight=0, network_quiet=float("inf")):
    return {"requests": requests, "navigations": navigations, "loading": loading, "inflight": inflight,
            "network_quiet": network_quiet}


class SettleTests(unittest.TestCase):
    def test_nothing_happening_returns_after_the_quick_window(self):
        reason, waited, _ = run(FakePage(lambda t: (poll(), counters())))
        self.assertEqual(reason, "nothing changed")
        self.assertAlmostEqual(waited, settle.QUICK_DEFAULT_S, delta=settle.POLL_S)

    def test_typing_waits_out_a_debounce_before_concluding_nothing_changed(self):
        # Suggestions are fetched 0.3 s after typing and arrive 0.2 s later.
        def script(t):
            if t < 0.3:
                return poll(), counters()
            if t < 0.5:
                return poll(), counters(requests=1, inflight=1, network_quiet=0)
            return poll(count=5, quiet=t - 0.5), counters(requests=1, network_quiet=t - 0.5)
        reason, waited, _ = run(FakePage(script), verb="type_text")
        self.assertEqual(reason, "requests finished")
        self.assertGreaterEqual(waited, 0.5 + settle.QUIET_S)
        self.assertLess(waited, 0.5 + settle.QUIET_S + 0.1)

    def test_a_menu_opening_waits_only_for_the_window_to_go_quiet(self):
        reason, waited, _ = run(FakePage(lambda t: (poll(count=3, quiet=max(0.0, t - 0.05)), counters())))
        self.assertEqual(reason, "window changed")
        self.assertLess(waited, 0.3)

    def test_a_navigation_waits_for_load_then_quiet(self):
        # Old document goes at 0.1 s; the new one loads until 2.0 s and keeps changing until 2.2 s.
        def script(t):
            if t < 0.1:
                return poll(), counters()
            if t < 2.0:
                return None, counters(requests=4, navigations=1, loading=True, inflight=2, network_quiet=0)
            return poll(count=1, quiet=max(0.0, t - 2.2)), counters(requests=4, navigations=1, network_quiet=t - 2.0)
        reason, waited, facts = run(FakePage(script))
        self.assertEqual(reason, "page loaded")
        self.assertTrue(facts["navigated"])
        self.assertGreaterEqual(waited, 2.2 + settle.QUIET_S)
        self.assertLess(waited, 2.2 + settle.QUIET_S + 0.1)

    def test_an_animation_alone_stops_blocking_at_the_cap(self):
        reason, waited, _ = run(FakePage(lambda t: (poll(count=int(t * 100) + 1, quiet=0), counters())))
        self.assertEqual(reason, "still animating")
        self.assertAlmostEqual(waited, settle.DOM_CAP_S, delta=settle.POLL_S)

    def test_an_ambient_window_ignores_its_own_animation(self):
        reason, waited, _ = run(FakePage(lambda t: (poll(count=int(t * 100) + 1, quiet=0), counters())), ambient=True)
        self.assertEqual(reason, "nothing changed")
        self.assertLess(waited, 0.2)

    def test_a_visible_loading_indicator_holds_the_step_past_the_animation_cap(self):
        # A "Loading…" label shows from 0.05 s to 2.5 s while the window keeps changing; then the content settles.
        def script(t):
            loading = 0.05 <= t < 2.5
            return poll(count=int(t * 20) if t < 2.6 else 52, quiet=0 if t < 2.6 else t - 2.6), counters(loading=loading)
        reason, waited, facts = run(FakePage(script))
        self.assertEqual(reason, "loading finished")
        self.assertTrue(facts["loading"])
        self.assertGreaterEqual(waited, 2.6 + settle.QUIET_S)

    def test_a_long_request_hits_the_cap(self):
        reason, waited, _ = run(FakePage(lambda t: (poll(), counters(requests=1, inflight=1, network_quiet=0))))
        self.assertEqual(reason, "timed out")
        self.assertAlmostEqual(waited, settle.CAP_S, delta=settle.POLL_S)

    def test_leaving_the_window_hands_back_to_the_window_level_settle(self):
        reason, _, _ = run(FakePage(lambda t: (poll(visible=t < 0.05), counters())))
        self.assertEqual(reason, "left the window")

    def test_the_wait_verb_waits_for_a_page_that_was_already_loading(self):
        def script(t):
            if t < 1.0:
                return poll(ready="interactive", quiet=0), counters(inflight=1, network_quiet=0)
            return poll(quiet=t - 1.0), counters(network_quiet=t - 1.0)
        reason, waited, _ = run(FakePage(script), verb="wait", idle_only=True)
        self.assertEqual(reason, "window quiet")
        self.assertGreaterEqual(waited, 1.0 + settle.IDLE_QUIET_S)

    def test_the_wait_verb_waits_for_the_change_jev_expects(self):
        # Results land 1.2 s into the wait (a request UI Automation could not see); the wait covers them in one step.
        def script(t):
            return (poll(count=4, quiet=t - 1.2) if t >= 1.2 else poll()), counters()
        reason, waited, _ = run(FakePage(script), verb="wait", idle_only=True, until_change=settle.WAIT_FOR_CHANGE_S)
        self.assertEqual(reason, "window quiet")
        self.assertGreaterEqual(waited, 1.2 + settle.IDLE_QUIET_S)
        self.assertLess(waited, 1.2 + settle.IDLE_QUIET_S + 0.1)

    def test_the_wait_verb_gives_up_when_nothing_comes(self):
        reason, waited, _ = run(FakePage(lambda t: (poll(), counters())), verb="wait", idle_only=True,
                                until_change=settle.WAIT_FOR_CHANGE_S)
        self.assertEqual(reason, "nothing changed")
        self.assertAlmostEqual(waited, settle.WAIT_FOR_CHANGE_S, delta=settle.POLL_S)


class QuickWindowTests(unittest.TestCase):
    def test_ui_automation_gives_links_and_enter_time_to_start_a_navigation(self):
        link = Control("c1", "Next page", "Hyperlink", (0, 0, 10, 10), "")
        button = Control("c2", "Menu", "Button", (0, 0, 10, 10), "")
        self.assertEqual(Settler._quick("left_click", {"id": "c1"}, [link, button]), settle.LINK_S)
        self.assertEqual(Settler._quick("left_click", {"id": "c2"}, [link, button]), settle.QUICK_DEFAULT_S)
        self.assertEqual(Settler._quick("press_key", {"key": "Enter"}, []), settle.ENTER_S)
        self.assertEqual(Settler._quick("type_text", {"id": "c1"}, []), settle.QUICK_S["type_text"])


class SettlerActTests(unittest.TestCase):
    def make(self, signals=None):
        return Settler(screen=mock.Mock(signals=mock.Mock(return_value=signals)))

    def act(self, settler, verb="left_click", target=None):
        state = {"activeWindow": {"hwnd": 1}}
        with mock.patch("voice_control.windows.execute", return_value="clicked") as execute, \
                mock.patch("voice_control.windows.settle", return_value=453), \
                mock.patch("voice_control.windows._top_level_picture", return_value=()):
            return settler.act(verb, target or {"id": "c1"}, state, [], [], ""), execute

    def test_without_any_signal_source_the_window_level_settle_is_used(self):
        result, _ = self.act(self.make())
        self.assertEqual(result, "clicked (settled in 453 ms)")

    def test_ui_automation_settles_inside_the_window(self):
        result, _ = self.act(self.make(signals=FakePage(lambda t: (poll(), counters()))))
        self.assertRegex(result, r"^clicked \(settled in \d+ ms: nothing changed\)$")

    def test_tab_switching_chords_and_window_verbs_are_not_followed_inside_the_window(self):
        settler = self.make(signals=FakePage(lambda t: (poll(), counters())))
        for verb, target in (("key_chord", {"key": "next_tab"}), ("switch_window", {"id": "w1"})):
            result, _ = self.act(settler, verb, target)
            self.assertEqual(result, "clicked (settled in 453 ms)")

    def test_wait_follows_the_window_instead_of_running_the_one_second_sleep(self):
        with mock.patch.object(settle, "settle_page", return_value=("window quiet", {})) as settled:
            result, execute = self.act(self.make(signals=FakePage(lambda t: (poll(), counters()))), "wait", {"id": "current"})
        execute.assert_not_called()
        self.assertTrue(settled.call_args.args[3])  # idle_only
        self.assertEqual(settled.call_args.kwargs["until_change"], settle.WAIT_FOR_CHANGE_S)
        self.assertRegex(result, r"^waited until the window was idle \(settled in \d+ ms: window quiet\)$")


class StaleMarkTests(unittest.TestCase):
    def watch(self):
        watch = uia_watch.ScreenWatch(subscribe=False)  # no COM thread; the counters are driven by hand
        watch.enabled = True
        watch.hwnd = 5
        watch.generation = 1
        return watch

    def test_a_change_after_the_mark_is_stale(self):
        watch = self.watch()
        mark = uia_watch.Mark(generation=1, count=3, at=0.0, ambient=False)
        watch.count = 3
        self.assertFalse(watch.stale(mark))
        watch._event(1)
        self.assertTrue(watch.stale(mark))

    def test_only_steady_change_counts_as_changing_on_its_own(self):
        watch = self.watch()
        watch.since = 0.0
        watch.recent.extend([9.0 + 0.05 * i for i in range(20)])  # a ticker: events all through the last second
        self.assertTrue(watch.continuous(10.0))
        watch.recent.clear()
        watch.recent.extend([9.8, 9.82, 9.85, 9.9])  # the burst that the last action caused
        self.assertFalse(watch.continuous(10.0))

    def test_a_window_that_changes_on_its_own_is_never_stale(self):
        watch = self.watch()
        watch.count = 9
        self.assertFalse(watch.stale(uia_watch.Mark(generation=1, count=3, at=0.0, ambient=True)))

    def test_events_from_an_earlier_window_do_not_count(self):
        watch = self.watch()
        mark = uia_watch.Mark(generation=1, count=0, at=0.0, ambient=False)
        watch.generation = 2
        watch._event(1)
        watch._event(2)
        self.assertFalse(watch.stale(mark))

    def test_names_that_say_loading_are_tracked_until_they_change_back(self):
        watch = self.watch()
        sender = mock.Mock(GetRuntimeId=mock.Mock(return_value=[42, 7]))
        watch._name_changed(1, sender, "Loading results…")
        self.assertEqual(watch.loading, {(42, 7)})
        watch._name_changed(1, sender, "Results")
        self.assertEqual(watch.loading, set())


class CaptureLoadingTests(unittest.TestCase):
    def readings(self, *loading_per_reading):
        controls = [Control(f"c{i}", f"Item {i}", "Button", (0, 0, 10, 10), "") for i in range(20)]
        return [({"loading": list(loading), "n": n}, controls) for n, loading in enumerate(loading_per_reading)]

    def settled(self, hwnd, readings):
        from . import windows
        calls = iter(readings)
        with mock.patch.object(windows, "capture", lambda _hwnd: next(calls)), mock.patch.object(windows.time, "sleep"):
            return windows.capture_settled(hwnd)[0]["n"]

    def test_a_new_loading_indicator_is_read_again_until_it_goes(self):
        from . import windows
        windows._LOADING_SEEN.pop(901, None)
        self.assertEqual(self.settled(901, self.readings(['Text "Loading…"'], ['Text "Loading…"'], [])), 2)

    def test_an_indicator_that_stays_holds_only_the_first_reading(self):
        from . import windows
        windows._LOADING_SEEN[902] = {'ProgressBar "Download"'}
        self.assertEqual(self.settled(902, self.readings(['ProgressBar "Download"'], [])), 0)


class StaleReplanTests(unittest.TestCase):
    def screen(self, title, items, focused=None):
        """items: (name, top) pairs, one button each, 20 px tall; ids follow the order, as in a real reading."""
        controls = [Control(f"c{i}", name, "Button", (0, top, 100, top + 20), title,
                            state="focused" if name == focused else "") for i, (name, top) in enumerate(items)]
        state = {"activeWindow": {"hwnd": 1, "title": title, "process": "app.exe"}, "openWindows": [],
                 "controls": [{"id": c.id} for c in controls], "texts": []}
        return state, controls, []

    def run_goal(self, before, after, verb="left_click", name=None, rounds=2, stale=(True, False)):
        """One goal run: Jev decides on `before` (clicking `name`, else the last control); the window reads as
        `after` from then on."""
        observations = iter([after] * 3)
        stale_answers = iter(stale)
        decisions, acted = [], []

        def plan(key, goal_text, steps, initial, state, controls, apps, step, prompts=None):
            decisions.append([c.name for c in controls])
            step.setdefault("timings_ms", {})["jev"] = 700
            step["verb"] = {"choice": verb}
            chosen = next(c for c in controls if c.name == name) if name else controls[-1]
            step["target"] = {"id": chosen.id, "kind": "control", "description": f'Button "{chosen.name}"'}
            step["done_p"] = 0.1
            return "act" if len(decisions) < rounds else "done"

        def act(verb_, target, state, controls, *rest):
            acted.append(next(c.name for c in controls if c.id == target["id"]))
            return "clicked"

        record = {}
        with mock.patch.object(goal, "plan_step", plan):
            goal.run_goal("key", "goal", lambda: next(observations), act, record, initial_observation=before,
                          stale=lambda: next(stale_answers, False))
        return record["steps"][0], decisions, acted

    def test_a_click_on_a_control_that_is_gone_is_decided_again_on_a_fresh_read(self):
        loading = self.screen("Search", [("Show more", 0)])
        loaded = self.screen("Search", [("First result", 0), ("Second result", 20)])
        first, decisions, _ = self.run_goal(loading, loaded, rounds=3)
        self.assertEqual(decisions[:2], [["Show more"], ["First result", "Second result"]])
        self.assertEqual(first["target"]["id"], "c1")  # acted on the fresh screen, not the vanished Show more
        self.assertEqual(len(first["stale_replans"]), 1)
        self.assertEqual(first["stale_replans"][0]["target"], "c0")
        self.assertEqual(first["timings_ms"]["jev"], 1400)

    def test_a_change_elsewhere_keeps_the_decision_without_asking_jev_again(self):
        before = self.screen("Shop", [("Search", 0), ("Filters", 40)])
        after = self.screen("Shop", [("Cookie banner", 200), ("Search", 0), ("Filters", 40)])  # ids shift by one
        first, decisions, acted = self.run_goal(before, after, name="Filters")
        self.assertEqual(len(decisions), 2)  # this step's decision and the next step's, no second one for this step
        self.assertEqual(first["target"]["id"], "c2")  # the same control under its id in the fresh reading
        self.assertEqual(first["stale_kept"], ["c1"])
        self.assertNotIn("stale_replans", first)
        self.assertEqual(first["timings_ms"]["jev"], 700)
        self.assertEqual(acted[0], "Filters")

    def test_something_new_over_the_target_is_decided_again(self):
        before = self.screen("Shop", [("Search", 0), ("Filters", 40)])
        after = self.screen("Shop", [("Search", 0), ("Filters", 40), ("Sort menu", 30)])  # covers Filters' center
        first, _, _ = self.run_goal(before, after, name="Filters", rounds=3)
        self.assertEqual(len(first["stale_replans"]), 1)

    def test_a_target_that_moved_or_changed_state_is_decided_again(self):
        before = self.screen("Shop", [("Search", 0), ("Filters", 40)])
        moved = self.screen("Shop", [("Search", 0), ("Filters", 80)])
        self.assertEqual(len(self.run_goal(before, moved, name="Filters", rounds=3)[0]["stale_replans"]), 1)
        state, controls, apps = self.screen("Shop", [("Search", 0), ("Filters", 40)])
        controls[1] = dataclasses.replace(controls[1], state="expanded")
        changed = (state, controls, apps)
        self.assertEqual(len(self.run_goal(before, changed, name="Filters", rounds=3)[0]["stale_replans"]), 1)

    def test_a_wait_is_decided_again_because_what_it_waited_for_may_have_come(self):
        before = self.screen("Flights", [("Search", 0)])
        after = self.screen("Flights", [("Search", 0), ("Results", 100)])
        first, _, _ = self.run_goal(before, after, verb="wait", rounds=3)
        self.assertEqual(len(first["stale_replans"]), 1)

    def test_keys_keep_their_decision_only_while_the_same_field_has_focus(self):
        step = {"verb": {"choice": "press_key"}, "target": {"id": "kEnter", "kind": "key", "key": "Enter"}}
        before = self.screen("Chat", [("Message", 0), ("Search", 40)], focused="Message")
        same = self.screen("Chat", [("New message", 80), ("Message", 0), ("Search", 40)], focused="Message")
        moved = self.screen("Chat", [("Message", 0), ("Search", 40)], focused="Search")
        self.assertEqual(goal.still_valid(step, before[0], before[1], same[0], same[1]), {})
        self.assertIsNone(goal.still_valid(step, before[0], before[1], moved[0], moved[1]))

    def test_another_active_window_or_a_closed_target_window_is_decided_again(self):
        before = self.screen("Chat", [("Message", 0)])
        other = self.screen("Other", [("Message", 0)])
        other[0]["activeWindow"]["hwnd"] = 2
        click = {"verb": {"choice": "left_click"}, "target": {"id": "c0", "kind": "control"}}
        self.assertIsNone(goal.still_valid(click, before[0], before[1], other[0], other[1]))
        switch = {"verb": {"choice": "switch_window"}, "target": {"id": "w7", "kind": "window"}}
        after = self.screen("Chat", [("Message", 0)])
        after[0]["openWindows"] = [{"hwnd": 1}, {"hwnd": 8}]
        self.assertIsNone(goal.still_valid(switch, before[0], before[1], after[0], after[1]))
        after[0]["openWindows"].append({"hwnd": 7})
        self.assertEqual(goal.still_valid(switch, before[0], before[1], after[0], after[1]), {})

    def test_a_window_that_keeps_changing_is_replanned_at_most_twice(self):
        screen = self.screen("Busy", [("Go", 0)])
        calls = []

        def plan(key, goal_text, steps, initial, state, controls, apps, step, prompts=None):
            calls.append(1)
            step.setdefault("timings_ms", {})["jev"] = 1
            step["verb"] = {"choice": "no_action"}
            step["reason"] = "nothing to do"
            return "blocked"

        with mock.patch.object(goal, "plan_step", plan):
            outcome = goal.run_goal("key", "go", lambda: screen, lambda *args: "", {}, initial_observation=screen,
                                    stale=lambda: True)
        self.assertEqual(outcome, "blocked")
        self.assertEqual(len(calls), 1 + goal.STALE_REPLANS)

    def test_a_kept_decision_is_rechecked_at_most_twice_too(self):
        screen = self.screen("Busy", [("Go", 0)])
        calls = []

        def plan(key, goal_text, steps, initial, state, controls, apps, step, prompts=None):
            calls.append(1)
            step.setdefault("timings_ms", {})["jev"] = 1
            step["verb"] = {"choice": "left_click"}
            step["target"] = {"id": "c0", "kind": "control", "description": 'Button "Go"'}
            return "act" if len(calls) == 1 else "done"

        record = {}
        answers = iter([True] * goal.STALE_REPLANS + [False] * 10)
        with mock.patch.object(goal, "plan_step", plan):
            goal.run_goal("key", "go", lambda: screen, lambda *args: "clicked", record, initial_observation=screen,
                          stale=lambda: next(answers, False))
        self.assertEqual(record["steps"][0]["stale_kept"], ["c0"] * goal.STALE_REPLANS)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
