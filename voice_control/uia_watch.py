"""General settle signals for any Windows app, with nothing installed: UI Automation events, loading indicators,
the busy cursor, and (for windows that expose almost no controls) the window's pixels.

One background thread in the COM multithreaded apartment owns the subscriptions. It follows one window at a time
(the one the goal is working in): structure changes and a few property changes anywhere in that window, plus menus
and windows that the same app opens. The handlers only count and timestamp events, so a busy page costs little.

The same counters serve two purposes:
- settling: after an action, wait until what it started has stopped (settle.settle_page reads WindowSignals);
- staleness: if the window changed while Jev was deciding, the decision was made on an old screen (ScreenWatch.stale).
If UI Automation cannot be subscribed, the Settler falls back to the window-level settle.
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

# A control whose name says the app is working: "Loading…", "Loading results", Chrome's "Stop loading this page".
LOADING_NAME = re.compile(r"\bloading\b|\bplease wait\b", re.I)
# A window changes "on its own" (a clock, a carousel, a playing video) when events arrived in every one of the last
# AMBIENT_BUCKETS slices of AMBIENT_SLICE_S. A burst from the last action is not that: it is over within a slice or two.
AMBIENT_SLICE_S = 0.25
AMBIENT_BUCKETS = 4
PROPERTIES = {         # property changes that show something happened (not bounds, which animations change constantly)
    30005: "name", 30045: "value", 30086: "toggle", 30070: "expand", 30079: "selected", 30010: "enabled",
    30022: "offscreen",
}
NAME_PROPERTY = 30005
TREE_SUBTREE, TREE_CHILDREN = 7, 2
MENU_OPENED, WINDOW_OPENED, ASYNC_CONTENT_LOADED, LAYOUT_INVALIDATED, LIVE_REGION = 20003, 20016, 20006, 20008, 20024
WINDOW_EVENTS = (ASYNC_CONTENT_LOADED, LAYOUT_INVALIDATED, LIVE_REGION)
ROOT_EVENTS = (MENU_OPENED, WINDOW_OPENED)
PIXEL_STEP = 8          # sample every 8th pixel in each direction
PIXEL_CHANGE = 0.002    # a change to more than 0.2% of sampled pixels counts (not a blinking caret)


@dataclass(frozen=True)
class Mark:
    generation: int
    count: int
    at: float
    ambient: bool


class ScreenWatch:
    """Counts UI Automation events in the watched window. Thread-safe; never raises to callers."""

    def __init__(self, subscribe: bool = True) -> None:
        self.enabled = subscribe  # turned off if UI Automation cannot be subscribed
        self.lock = threading.Lock()
        self.requests: queue.Queue = queue.Queue()
        self.hwnd = 0
        self.pid = 0
        self.generation = 0
        self.count = 0
        self.last = 0.0
        self.since = 0.0
        self.loading: set[tuple] = set()
        self.recent: deque[float] = deque(maxlen=256)  # times of the latest events, for continuous()
        self.ready = threading.Event()
        self.error = ""
        self.handlers: list[tuple] = []
        if self.enabled:
            threading.Thread(target=self._run, name="jev-uia-events", daemon=True).start()

    # ------------------------------------------------------------ the COM thread
    def _run(self) -> None:
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            self.uia_module = comtypes.client.GetModule("UIAutomationCore.dll")
            self.uia = comtypes.client.CreateObject(self.uia_module.CUIAutomation, interface=self.uia_module.IUIAutomation)
            self._handlers_classes()
            root = self.uia.GetRootElement()
            window_events = self.RootEvents(self)
            for event in ROOT_EVENTS:
                self.uia.AddAutomationEventHandler(event, root, TREE_SUBTREE if event == MENU_OPENED else TREE_CHILDREN,
                                                   None, window_events)
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            self.enabled = False
            self.ready.set()
            return
        self.ready.set()
        while True:
            hwnd, done = self.requests.get()
            if hwnd != self.hwnd:
                self._subscribe(hwnd)
            done.set()

    def _handlers_classes(self) -> None:
        from comtypes import COMObject
        module, watch = self.uia_module, self

        class Structure(COMObject):
            _com_interfaces_ = [module.IUIAutomationStructureChangedEventHandler]

            def __init__(self, generation: int) -> None:
                super().__init__()
                self.generation = generation

            def HandleStructureChangedEvent(self, sender, change_type, runtime_id):
                watch._event(self.generation)

        class Property(COMObject):
            _com_interfaces_ = [module.IUIAutomationPropertyChangedEventHandler]

            def __init__(self, generation: int) -> None:
                super().__init__()
                self.generation = generation

            def HandlePropertyChangedEvent(self, sender, property_id, new_value):
                watch._event(self.generation)
                if property_id == NAME_PROPERTY:
                    watch._name_changed(self.generation, sender, new_value)

        class Events(COMObject):
            _com_interfaces_ = [module.IUIAutomationEventHandler]

            def __init__(self, generation: int) -> None:
                super().__init__()
                self.generation = generation

            def HandleAutomationEvent(self, sender, event_id):
                watch._event(self.generation)

        class RootEvents(COMObject):
            """Menus and windows opened anywhere; only those of the watched app count."""
            _com_interfaces_ = [module.IUIAutomationEventHandler]

            def __init__(self, owner: Any) -> None:
                super().__init__()

            def HandleAutomationEvent(self, sender, event_id):
                try:
                    pid = sender.CurrentProcessId
                except Exception:
                    return
                if pid and pid == watch.pid and pid != os.getpid():
                    watch._event(watch.generation)

        self.Structure, self.Property, self.Events, self.RootEvents = Structure, Property, Events, RootEvents

    def _subscribe(self, hwnd: int) -> None:
        import win32process
        for remove in self.handlers:
            try:
                remove[0](*remove[1:])
            except Exception:
                pass
        self.handlers = []
        with self.lock:
            self.generation += 1
            generation = self.generation
            self.hwnd, self.count, self.loading = hwnd, 0, set()
            self.recent.clear()
            self.since = self.last = time.monotonic()
            try:
                self.pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            except Exception:
                self.pid = 0
        try:
            element = self.uia.ElementFromHandle(hwnd)
            structure = self.Structure(generation)
            self.uia.AddStructureChangedEventHandler(element, TREE_SUBTREE, None, structure)
            self.handlers.append((self.uia.RemoveStructureChangedEventHandler, element, structure))
            prop = self.Property(generation)
            self.uia.AddPropertyChangedEventHandlerNativeArray(element, TREE_SUBTREE, None, prop,
                                                               _int_array(list(PROPERTIES)), len(PROPERTIES))
            self.handlers.append((self.uia.RemovePropertyChangedEventHandler, element, prop))
            events = self.Events(generation)
            for event in WINDOW_EVENTS:
                self.uia.AddAutomationEventHandler(event, element, TREE_SUBTREE, None, events)
                self.handlers.append((self.uia.RemoveAutomationEventHandler, event, element, events))
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
        with self.lock:
            self.since = self.last = time.monotonic()  # subscribing itself is not a change in the window

    def _event(self, generation: int) -> None:
        with self.lock:
            if generation == self.generation:
                self.count += 1
                self.last = time.monotonic()
                self.recent.append(self.last)

    def _name_changed(self, generation: int, sender: Any, name: Any) -> None:
        """Track controls whose name says the app is loading, e.g. Chrome's Reload becoming "Stop loading this page"."""
        loading = isinstance(name, str) and bool(LOADING_NAME.search(name))
        if not loading and not self.loading:
            return
        try:
            key = tuple(sender.GetRuntimeId())
        except Exception:
            return
        with self.lock:
            if generation == self.generation:
                (self.loading.add if loading else self.loading.discard)(key)

    # ------------------------------------------------------------ callers
    def watch(self, hwnd: int, wait: float = 0.0) -> bool:
        """Follow `hwnd` from now on. With `wait`, block until the subscription is live (or the wait runs out)."""
        if not self.enabled or not hwnd:
            return False
        if hwnd == self.hwnd:
            return True
        done = threading.Event()
        self.requests.put((hwnd, done))
        return done.wait(wait) if wait else False

    def mark(self, hwnd: int) -> Mark | None:
        """The window's event position now, for a later stale() check."""
        if not self.enabled or not self.watch(hwnd, wait=1.0):
            return None
        with self.lock:
            now = time.monotonic()
            return Mark(self.generation, self.count, now, self.continuous(now))

    def continuous(self, now: float) -> bool:
        """Events in every recent slice: the window has been changing on its own. Call with the lock held."""
        if now - self.since < AMBIENT_SLICE_S * AMBIENT_BUCKETS:
            return False
        slices = {int((now - at) / AMBIENT_SLICE_S) for at in self.recent if now - at < AMBIENT_SLICE_S * AMBIENT_BUCKETS}
        return len(slices) == AMBIENT_BUCKETS

    def stale(self, mark: Mark | None) -> bool:
        """The watched window changed after `mark`. A window that was changing on its own is never called stale."""
        if mark is None or mark.ambient:
            return False
        with self.lock:
            return mark.generation == self.generation and self.count > mark.count

    def signals(self, hwnd: int, sparse: bool = False) -> "WindowSignals | None":
        if not self.enabled or not self.watch(hwnd, wait=1.0):
            return None
        return WindowSignals(self, hwnd, sparse)


class WindowSignals:
    """The watched window in the shape settle.settle_page reads."""

    def __init__(self, watch: ScreenWatch, hwnd: int, sparse: bool) -> None:
        self.watch = watch
        self.hwnd = hwnd
        self.pixels = WindowPixels(hwnd) if sparse else None
        self.pixel_count = 0
        self.pixel_last = 0.0

    def poll(self) -> dict | None:
        now = time.monotonic()
        if self.pixels is not None and self.pixels.changed():
            self.pixel_count += 1
            self.pixel_last = now
        with self.watch.lock:
            if self.watch.hwnd != self.hwnd:  # the watcher moved on to another window: this one can't be followed
                return {"count": 0, "quietMs": 0, "installedMs": 0, "ready": "complete", "visible": False}
            count = self.watch.count + self.pixel_count
            last = max(self.watch.last, self.pixel_last)
            since = self.watch.since
            ambient = self.watch.continuous(now)
        return {"count": count, "quietMs": (now - last) * 1000, "installedMs": (now - since) * 1000,
                "ready": "complete", "visible": True, "ambient": ambient}

    def counters(self, now: float) -> dict:
        with self.watch.lock:
            loading = bool(self.watch.loading)
        return {"requests": 0, "navigations": 0, "loading": loading or busy_cursor(self.hwnd), "inflight": 0,
                "network_quiet": float("inf")}

    def frames(self) -> None:
        pass


def busy_cursor(hwnd: int) -> bool:
    """The app under the pointer shows the wait or working-in-background cursor (browsers do while a page loads)."""
    import win32con
    import win32gui
    import win32process
    try:
        _, cursor, point = win32gui.GetCursorInfo()
        if cursor not in {win32gui.LoadCursor(0, win32con.IDC_APPSTARTING), win32gui.LoadCursor(0, win32con.IDC_WAIT)}:
            return False
        under = win32gui.GetAncestor(win32gui.WindowFromPoint(point), 2)
        return under == hwnd or win32process.GetWindowThreadProcessId(under)[1] == win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:
        return False


class WindowPixels:
    """A coarse sample of the window's pixels, for windows whose content UI Automation cannot see (video, canvas)."""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.previous = self._grab()

    def _grab(self):
        import numpy as np
        import win32con
        import win32gui
        import win32ui
        try:
            left, top, right, bottom = win32gui.GetWindowRect(self.hwnd)
            width, height = right - left, bottom - top
            if width <= 0 or height <= 0:
                return None
            screen = win32gui.GetDC(0)
            source = win32ui.CreateDCFromHandle(screen)
            memory = source.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            try:
                bitmap.CreateCompatibleBitmap(source, width, height)
                memory.SelectObject(bitmap)
                memory.BitBlt((0, 0), (width, height), source, (left, top), win32con.SRCCOPY)
                data = np.frombuffer(bitmap.GetBitmapBits(True), dtype=np.uint8).reshape(height, width, 4)
                return data[::PIXEL_STEP, ::PIXEL_STEP, :3].copy()
            finally:
                memory.DeleteDC()
                source.DeleteDC()
                win32gui.ReleaseDC(0, screen)
                win32gui.DeleteObject(bitmap.GetHandle())
        except Exception:
            return None

    def changed(self) -> bool:
        current = self._grab()
        previous, self.previous = self.previous, current
        if current is None or previous is None or current.shape != previous.shape:
            return False
        differing = (abs(current.astype("int16") - previous.astype("int16")).max(axis=2) > 24).mean()
        return float(differing) > PIXEL_CHANGE


def _int_array(values: list[int]) -> Any:
    import ctypes
    return (ctypes.c_int * len(values))(*values)


_shared: ScreenWatch | None = None
_shared_lock = threading.Lock()


def shared() -> ScreenWatch:
    """One watcher per process: UI Automation subscriptions are per client, and one window is followed at a time."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = ScreenWatch()
        return _shared
