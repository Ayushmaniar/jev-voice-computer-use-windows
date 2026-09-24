"""Sliders: reading them for Jev, and moving them by a named amount in any app.

A slider is anything UI Automation calls a Slider: a web page's range input or ARIA slider, the volume and
brightness sliders in Windows Settings, a video player's volume or seek bar. Jev never picks a raw number; it picks a
change from a small categorical set (a little, some, a lot up or down; all the way; the middle; or a number the user
said), and the change is turned into a value here, relative to the slider's own range.

Apps expose sliders very differently, so moving one tries, in order, and reads the value back after each:
1. the RangeValue pattern (Chrome, Windows Settings, most toolkits): set the value directly;
2. the keyboard, when the slider takes focus (ARIA sliders that ignore SetValue): Home/End, arrows, Page Up/Down;
3. the mouse, for sliders that expose nothing but a value (VLC): click the track where the value should be, then
   correct from the value read back, since tracks have padding at their ends. Without even a readable value, a
   relative change becomes mouse-wheel notches over the slider.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

STEPS = (5, 10, 25)  # percent of the slider's range for a small, medium and large change
CHANGE_CRITERIA = {
    **{f"increase_{p}": f"Increase it by about {p}% of its range ({label})"
       for p, label in zip(STEPS, ("a little; a notch", "the default for 'up', 'louder', 'brighter', 'more'", "a lot"))},
    **{f"decrease_{p}": f"Decrease it by about {p}% of its range ({label})"
       for p, label in zip(STEPS, ("a little; a notch", "the default for 'down', 'quieter', 'dimmer', 'less'", "a lot"))},
    "max": "All the way up, to its maximum ('full', 'max', 'all the way up')",
    "min": "All the way down, to its minimum ('mute' for a volume slider, 'all the way down', 'lowest')",
    "middle": "To the middle of its range ('half', 'halfway')",
}
NUMBER = re.compile(r"(?<![\w.])(\d{1,5}(?:\.\d+)?)\s*(%|percent\b)?", re.I)
MAX_NUMBERS = 8
MAX_KEYS = 200          # arrow presses at most, for a slider with tiny steps
MOUSE_TRIES = 4         # clicks on the track while homing in on the value
SETTLE_S = 0.25         # an app updates its slider shortly after the input
DEFAULT_RANGE = (0.0, 100.0)  # for a slider that reports a value but no range (VLC's volume reads 0-125, near enough)


def change_criteria(goal: str) -> dict[str, str]:
    """The changes Jev may choose for this goal: the fixed steps plus "set to N" for each number the user said."""
    criteria = dict(CHANGE_CRITERIA)
    for match in list(NUMBER.finditer(goal))[:MAX_NUMBERS]:
        number = match.group(1)
        percent = bool(match.group(2))
        key = f"set_{number}{'pct' if percent else ''}"
        criteria[key] = f"Set it to {number}{'%' if percent else ''}, the number the user said"
    criteria["none"] = "No change fits the request."
    return criteria


@dataclass
class Reading:
    value: float | None
    low: float | None = None
    high: float | None = None
    small: float | None = None
    large: float | None = None
    writable: bool = False  # RangeValue.SetValue is offered

    @property
    def span(self) -> tuple[float, float]:
        if self.low is not None and self.high is not None and self.high > self.low:
            return self.low, self.high
        return DEFAULT_RANGE

    def label(self) -> str:
        """For Jev and the logs: '40 (0 to 100)'."""
        if self.value is None:
            return ""
        value = _number(self.value)
        if self.low is not None and self.high is not None and self.high > self.low:
            return f"{value} ({_number(self.low)} to {_number(self.high)})"
        return value


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def read(item: Any) -> Reading:
    """What the slider reports: RangeValue if it has it, else a number from its Value pattern."""
    try:
        pattern = item.iface_range_value
        return Reading(float(pattern.CurrentValue), float(pattern.CurrentMinimum), float(pattern.CurrentMaximum),
                       float(pattern.CurrentSmallChange or 0) or None, float(pattern.CurrentLargeChange or 0) or None,
                       not pattern.CurrentIsReadOnly)
    except Exception:
        pass
    try:
        match = re.search(r"-?\d+(?:\.\d+)?", str(item.iface_value.CurrentValue or ""))
        return Reading(float(match.group(0)) if match else None)
    except Exception:
        return Reading(None)


def target_value(change: str, reading: Reading) -> float | None:
    """The value a change asks for, clamped to the slider's range; None when it cannot be known."""
    low, high = reading.span
    if change == "max":
        return high
    if change == "min":
        return low
    if change == "middle":
        return low + (high - low) / 2
    match = re.fullmatch(r"(increase|decrease)_(\d+)", change)
    if match:
        if reading.value is None:
            return None
        delta = (high - low) * int(match.group(2)) / 100
        return _clamp(reading.value + (delta if match.group(1) == "increase" else -delta), low, high)
    match = re.fullmatch(r"set_(\d+(?:\.\d+)?)(pct)?", change)
    if match:
        number = float(match.group(1))
        if match.group(2) or not low <= number <= high:
            # "50%" on any range, or "set brightness to 70" on a 0-1 slider: a percentage of the range.
            number = low + (high - low) * min(number, 100) / 100
        return _clamp(number, low, high)
    return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _close(value: float | None, target: float, reading: Reading) -> bool:
    low, high = reading.span
    tolerance = max((high - low) * 0.02, (reading.small or 0) / 2, 1e-9)
    return value is not None and abs(value - target) <= tolerance


def _vertical(item: Any, rect: tuple[int, int, int, int]) -> bool:
    try:
        orientation = item.element_info.element.CurrentOrientation  # 1 horizontal, 2 vertical, 0 not reported
        if orientation in (1, 2):
            return orientation == 2
    except Exception:
        pass
    return (rect[3] - rect[1]) > (rect[2] - rect[0])


def _point(rect: tuple[int, int, int, int], fraction: float, vertical: bool) -> tuple[int, int]:
    left, top, right, bottom = rect
    fraction = _clamp(fraction, 0.0, 1.0)
    if vertical:  # the maximum is at the top
        return (left + right) // 2, round(bottom - 1 - fraction * (bottom - top - 2))
    return round(left + 1 + fraction * (right - left - 2)), (top + bottom) // 2


def _by_pattern(item: Any, target: float, before: Reading) -> bool:
    if not before.writable:
        return False
    try:
        item.iface_range_value.SetValue(float(target))
    except Exception:
        return False
    time.sleep(SETTLE_S)
    return _close(read(item).value, target, before)


def _by_keyboard(item: Any, change: str, target: float, before: Reading) -> bool:
    from pywinauto import keyboard
    try:
        if not item.element_info.element.CurrentIsKeyboardFocusable:
            return False
        item.set_focus()
    except Exception:
        return False
    if change in {"max", "min"}:
        keyboard.send_keys("{END}" if change == "max" else "{HOME}")
        time.sleep(SETTLE_S)
        return _close(read(item).value, target, before)
    current = before.value
    if current is None:
        return False
    for _ in range(3):  # a first estimate from the step size, then corrections from what the slider says
        step = before.small or (before.span[1] - before.span[0]) / 100
        presses = min(MAX_KEYS, round(abs(target - current) / step))
        if presses == 0:
            break
        keyboard.send_keys(("{RIGHT %d}" if target > current else "{LEFT %d}") % presses)
        time.sleep(SETTLE_S)
        current = read(item).value
        if current is None or _close(current, target, before):
            break
    return _close(current, target, before)


def _by_mouse(item: Any, rect: tuple[int, int, int, int], change: str, target: float | None, before: Reading) -> bool:
    from pywinauto import mouse
    vertical = _vertical(item, rect)
    low, high = before.span
    if target is None:  # a relative change on a slider with no readable value: wheel notches over it
        notches = max(1, int(re.search(r"\d+", change).group(0)) // 5)
        mouse.scroll(coords=_point(rect, 0.5, vertical), wheel_dist=notches if change.startswith("increase") else -notches)
        return True
    if change in {"max", "min"}:
        mouse.click(coords=_point(rect, 1.0 if change == "max" else 0.0, vertical))
        time.sleep(SETTLE_S)
        after = read(item).value
        return after is None or after != before.value or _close(after, target, before)
    # Home in on the value: tracks have padding at their ends, so the first click is only an estimate.
    guesses: list[tuple[float, float]] = []  # (fraction clicked, value read after)
    fraction = (target - low) / (high - low)
    for _ in range(MOUSE_TRIES):
        mouse.click(coords=_point(rect, fraction, vertical))
        time.sleep(SETTLE_S)
        value = read(item).value
        if value is None:
            return True  # nothing to check against; one click at the estimate is all that can be done
        if _close(value, target, before):
            return True
        guesses.append((fraction, value))
        if len(guesses) >= 2 and guesses[-1][1] != guesses[-2][1]:
            (f1, v1), (f2, v2) = guesses[-2], guesses[-1]
            fraction = f2 + (target - v2) * (f2 - f1) / (v2 - v1)
        else:
            fraction += (target - value) / (high - low)
    return _close(read(item).value, target, before)


def move(item: Any, rect: tuple[int, int, int, int], change: str) -> str:
    """Move the slider as `change` asks and say what happened. Raises when no method moved it."""
    before = read(item)
    target = target_value(change, before)
    if target is not None and before.value is not None and _close(before.value, target, before):
        return f"already at {before.label() or _number(target)}"
    for method, attempt in (("value", lambda: target is not None and _by_pattern(item, target, before)),
                            ("keyboard", lambda: target is not None and _by_keyboard(item, change, target, before)),
                            ("mouse", lambda: _by_mouse(item, rect, change, target, before))):
        if attempt():
            after = read(item)
            was = before.label() or "unknown"
            return f"from {was} to {after.label() or 'a new value'} ({method})"
    raise RuntimeError(f"The slider did not move to {_number(target) if target is not None else change}")


def valid_change(change: Any) -> bool:
    return isinstance(change, str) and (change in CHANGE_CRITERIA or re.fullmatch(r"set_\d{1,5}(?:\.\d+)?(?:pct)?", change) is not None)


def describe_amount(change: str) -> str:
    if change.startswith(("increase_", "decrease_")):
        verb, percent = change.split("_")
        return f"{verb} by {percent}%"
    if change.startswith("set_"):
        return "set to " + change[4:].replace("pct", "%")
    return {"max": "to the maximum", "min": "to the minimum", "middle": "to the middle"}.get(change, change)


def unnamed_label(rect: tuple[int, int, int, int], frame: tuple[int, int, int, int]) -> str:
    """Where an unnamed slider sits, so 'the volume' can still be told from a seek bar: its length and place."""
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    length = max(width, height)
    span = max(1, (frame[2] - frame[0]) if width >= height else (frame[3] - frame[1]))
    size = "long" if length >= span * 0.4 else "short"
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    fx = (cx - frame[0]) / max(1, frame[2] - frame[0])
    fy = (cy - frame[1]) / max(1, frame[3] - frame[1])
    across = "left" if fx < 0.33 else "right" if fx > 0.67 else "center"
    down = "top" if fy < 0.33 else "bottom" if fy > 0.67 else "middle"
    return f"unnamed {size} {'vertical' if height > width else 'horizontal'} slider, {down} {across} of the window"
