"""When an action is finished: wait for what it started, then move on, in any app, with nothing installed.

windows.settle only sees top-level windows, so inside an app it is a fixed ~450 ms pause: too long when nothing
happened, too short while content is still arriving, after which Jev spends whole steps choosing `wait`. The Settler
instead watches the window through UI Automation events (uia_watch.py): structure and state changes, menus and
windows the app opens, controls named "Loading…", the busy cursor, and pixels for windows with almost no controls.

settle_page decides: nothing started -> move on after a short per-action window; changes -> wait until they stop;
loading -> wait until it finishes. Window-level actions (switch, launch, close) keep the window-level settle, and so
does any window UI Automation cannot follow.

UI Automation cannot see a request that has not changed the screen yet (a slow server behind a link). The Settler
also answers stale(): the window changed after it was last read, so goal.run_goal checks whether its decision still
holds on a fresh reading.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

QUIET_S = 0.15         # no change for this long counts as settled
IDLE_QUIET_S = 0.3     # the `wait` verb asks for a calmer window
DOM_CAP_S = 1.0        # changes with nothing loading behind them (an animation) stop blocking after this
CAP_S = 6.0            # requests without a navigation
NAV_CAP_S = 15.0       # a navigation or a visible loading indicator
POLL_S = 0.025
AMBIENT_QUIET_S = 0.3  # a window that was changing right before the action (a carousel, a clock) changes on its own
# How long an action may take to start something before "nothing changed" is concluded. A request shows only when
# its result reaches the screen, so actions that usually start one get a longer window; anything later is caught by
# stale().
QUICK_S = {"type_text": 0.5, "hover": 0.25, "key_chord": 0.3, "press_key": 0.3}
QUICK_DEFAULT_S = 0.15
ENTER_S = 0.6
LINK_S = 0.6
WAIT_FOR_CHANGE_S = 3.0  # the `wait` verb: Jev expects something UI Automation cannot see until it lands
SPARSE_CONTROLS = 30   # fewer controls than this: also watch the window's pixels

PAGE_VERBS = {"left_click", "right_click", "double_click", "hover", "type_text", "select_text", "press_key", "key_chord",
              "scroll_up", "scroll_down", "scroll_left", "scroll_right", "zoom_in", "zoom_out", "zoom_reset", "wait",
              "set_slider"}
TAB_CHORDS = {"next_tab", "previous_tab", "new_tab", "close_tab"}  # these leave the page; the window-level settle fits
CLICKS = {"left_click", "double_click"}


@dataclass
class Armed:
    page: Any
    requests: int
    navigations: int
    count: int
    ambient: bool


def arm_page(page: Any, now: float) -> Armed | None:
    """The source's counters just before an action. An observer that started moments ago cannot yet tell whether the
    window changes on its own, so it is only called ambient when it has watched a while and saw a change just now."""
    state = page.poll()
    if not state or not state["visible"]:
        return None
    seen = page.counters(now)
    ambient = state["ambient"] if "ambient" in state else         state["installedMs"] > 1000 * AMBIENT_QUIET_S and state["quietMs"] < 1000 * AMBIENT_QUIET_S
    return Armed(page, seen["requests"], seen["navigations"], state["count"], ambient)


def settle_page(page: Any, armed: Armed, verb: str, idle_only: bool = False, quick: float | None = None,
                min_idle: float = 0.0, until_change: float | None = None, clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep) -> tuple[str, dict]:
    """Wait until what the action started has finished. Returns (reason, facts).

    With `idle_only` (the `wait` verb) it waits for a calm window instead. `until_change` makes that wait first for
    something to change, up to that many seconds: a request is invisible until its result lands, so waiting only for
    calm would return at once and Jev would choose `wait` again and again.

    `page` is a signal source: poll() -> {count, quietMs, installedMs, ready, visible} or None while its document is
    being replaced; counters(now) -> {requests, navigations, loading, inflight, network_quiet}."""
    start = clock()
    quick = QUICK_S.get(verb, QUICK_DEFAULT_S) if quick is None else quick
    need_quiet = IDLE_QUIET_S if idle_only else QUIET_S
    was_loading = False
    last_busy = start  # the animation cap counts from when requests or loading last held the window
    while True:
        now = clock()
        elapsed = now - start
        state = page.poll()
        seen = page.counters(now)
        navigated = seen["navigations"] > armed.navigations
        requested = seen["requests"] > armed.requests
        was_loading |= bool(seen["loading"])
        if state is not None and not state["visible"]:
            return "left the window", {"navigated": navigated, "requested": requested}
        # A document replaced since arming has a fresh observer, so its count is not comparable; navigation covers it.
        mutated = state is not None and not navigated and state["count"] != armed.count and not armed.ambient
        busy = navigated or requested or bool(seen["loading"])
        if seen["inflight"] or seen["loading"] or state is None:
            last_busy = now
        facts = {"navigated": navigated, "requested": requested, "mutated": mutated, "inflight": seen["inflight"],
                 "loading": was_loading}
        if not idle_only and not (busy or mutated) and elapsed >= quick:
            return "nothing changed", facts
        loading = seen["loading"] or state is None or (state["ready"] != "complete" and (navigated or idle_only))
        dom_quiet = float("inf") if armed.ambient or state is None else state["quietMs"] / 1000
        quiet = min(dom_quiet, seen["network_quiet"])
        if idle_only and until_change is not None and not (busy or mutated):
            if elapsed >= until_change:
                return "nothing changed", facts
            sleep(POLL_S)
            continue
        started = elapsed >= min_idle if idle_only else (elapsed >= quick or requested or navigated)
        if not loading and not seen["inflight"] and quiet >= need_quiet and started:
            return ("page loaded" if navigated else "requests finished" if requested else
                    "loading finished" if was_loading else "window quiet" if idle_only else "window changed"), facts
        if not (navigated or requested) and not idle_only and now - last_busy >= DOM_CAP_S:
            return "still animating", facts
        if elapsed >= (NAV_CAP_S if navigated or seen["loading"] else CAP_S):
            return "timed out", facts
        sleep(POLL_S)


class Settler:
    """Acts and settles for the goal loop, and tells it when a decision was made on a screen that has since changed."""

    def __init__(self, screen: Any = None) -> None:
        from . import uia_watch
        self.screen = screen if screen is not None else uia_watch.shared()
        self.last_mark = None

    # ------------------------------------------------------------ reading the screen
    def before_capture(self, hwnd: int) -> None:
        """The window is about to be read. Any change from now on (even during the read, which takes a while) makes
        decisions based on that reading stale."""
        self.last_mark = self.screen.mark(hwnd)

    def stale(self) -> bool:
        return self.screen.stale(self.last_mark)

    # ------------------------------------------------------------ acting
    @staticmethod
    def _quick(verb: str, target: dict[str, Any], controls: list) -> float:
        if verb == "press_key" and target.get("key") == "Enter":
            return ENTER_S
        if verb in CLICKS and any(c.id == target.get("id") and c.role == "Hyperlink" for c in controls):
            return LINK_S
        return QUICK_S.get(verb, QUICK_DEFAULT_S)

    @staticmethod
    def _settle(armed: Armed, verb: str, quick: float, idle_only: bool = False) -> tuple[int, str] | None:
        started = time.monotonic()
        try:
            reason, _ = settle_page(armed.page, armed, verb, idle_only, quick, until_change=WAIT_FOR_CHANGE_S)
        except Exception:
            return None
        if reason == "left the window":
            return None
        return round((time.monotonic() - started) * 1000), reason

    def act(self, verb: str, target: dict[str, Any], state: dict[str, Any], controls: list, apps: list,
            utterance: str, app_hwnd: int | None = None, text: str | None = None) -> str:
        """windows.execute, then the settle that fits: signal-driven inside a window, window-level for window verbs."""
        from .windows import _top_level_picture, execute, settle
        hwnd = state["activeWindow"]["hwnd"]
        before = _top_level_picture()
        armed = None
        if verb in PAGE_VERBS and not (verb == "key_chord" and target.get("key") in TAB_CHORDS):
            signals = self.screen.signals(hwnd, sparse=len(controls) < SPARSE_CONTROLS)
            armed = arm_page(signals, time.monotonic()) if signals is not None else None
        quick = self._quick(verb, target, controls)
        if verb == "wait" and armed is not None:
            settled = self._settle(armed, verb, quick, idle_only=True)
            if settled is not None:
                return f"waited until the window was idle (settled in {settled[0]} ms: {settled[1]})"
        result = execute(verb, target, state, controls, apps, utterance, app_hwnd, text)
        settled = self._settle(armed, verb, quick) if armed is not None else None
        if settled is not None:
            return f"{result} (settled in {settled[0]} ms: {settled[1]})"
        return f"{result} (settled in {settle(verb, before)} ms)"
