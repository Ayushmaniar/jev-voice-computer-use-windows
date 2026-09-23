"""Pure planning, reference resolution, Jev transport, and structured logging."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

# Jev is reachable through OpenRouter or directly from TypeSafe AI; both take the
# same {model, state, questions} body and Bearer key and return the same answers.
# OpenRouter keys always start with "sk-or-"; any other key is a TypeSafe key.
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "~typesafe/jev-latest"
TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_MODEL = "jev-latest"
KEY_NAMES = ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY")
MAX_CHOICES = 255
VERBS = (
    "left_click", "right_click", "double_click", "hover", "scroll_up", "scroll_down", "scroll_left", "scroll_right",
    "type_text", "press_key", "key_chord", "zoom_in", "zoom_out", "zoom_reset",
    "minimize_window", "maximize_window", "close_window", "switch_window",
    "alt_tab", "launch_app", "wait", "no_action",
)
CONTROL_VERBS = {"left_click", "right_click", "double_click", "hover"}
RIGHT_ROLES = {"ListItem", "TreeItem", "TabItem", "Edit", "Hyperlink", "Button", "MenuItem"}
DOUBLE_ROLES = {"ListItem", "TreeItem", "Hyperlink"}
# Roles that accept typed text: a plain edit, a search/autocomplete box (a combo box on most web pages), a spin box.
FIELD_ROLES = {"Edit", "ComboBox", "Spinner", "Document"}  # Document only when editable (windows._editable)
# A video, canvas, or game window exposes almost no controls, yet a person right-clicks or double-clicks its content.
SURFACE_ID = "surface"
SPARSE_CONTROLS = 30
# Voice-agent practice: tell the model its input is an ASR transcript so it
# reasons about how the words sound rather than their literal spelling.
SPEECH_NOTE = (
    "The task is an automatic speech-recognition transcript of a spoken command, not typed text. "
    "It may contain recognition errors: misheard or misspelled names, homophones, words split or merged "
    "('and jolly' for 'Anjali'), near-sounding words ('scrawled' for 'scrolled', "
    "'Jon Smyth' for 'John Smith'), and filler such as 'okay', 'can you please', or 'let's'. "
    "Infer what the user most likely said from how the words sound and from the available options. "
)
SOUND_MATCH_RULE = (
    "Match spoken names to option labels by sound, not exact spelling; labels may also include extra text "
    "such as dates, message previews, or '(pinned)'. Use a sound-alike match only when exactly one option is "
    "clearly the closest; if two or more options sound similarly close, choose none."
)


def should_auto_run(enabled: bool, verb: str) -> bool:
    """Auto-run every valid plan when enabled; no_action is never executed."""
    return enabled and verb in VERBS and verb != "no_action"


@dataclass(frozen=True)
class Control:
    id: str
    name: str
    role: str
    rect: tuple[int, int, int, int]
    path: str
    enabled: bool = True
    hwnd: int = 0  # top-level window holding the control when it is not the active window (an open popup menu)
    state: str = ""  # UIA state worth knowing before acting: "checked", "selected", "expanded", "has submenu"...
    detail: str = ""  # text shown inside the control but missing from its name (a card's title under a link)
    value: str = ""  # text already in a field ("Ahmedabad" in a prefilled origin box); never read from password fields
    context: str = ""  # for a repeated name only: the text read just before it ("draft.txt" for its row's "Rename")


MAX_VALUE = 80


def describe_control(control: Control) -> str:
    state = f" [{control.state}]" if control.state else ""
    detail = f' (shows "{control.detail}")' if control.detail else ""
    value = control.value if len(control.value) <= MAX_VALUE else control.value[:MAX_VALUE - 3] + "..."
    value = f' = "{value}"' if value else ""
    context = f' near "{control.context}"' if control.context else ""
    return f'{control.role} "{control.name}"{value}{state}{detail}{context}'


def surface_target(controls: list[Control]) -> dict[str, Any] | None:
    """The window's content area as a target, offered only when the window exposes few controls and no menu is open."""
    if len(controls) >= SPARSE_CONTROLS or any(c.path.startswith("Open menu") for c in controls):
        return None
    return {"id": SURFACE_ID, "kind": "surface",
            "description": "The window's content area (its center): the video, picture, or canvas filling the window, "
                           "which exposes no controls of its own. Right-clicking it opens the app's context menu of "
                           "commands; double-clicking a video usually toggles full screen."}


def read_key(path: Path) -> str:
    """An OpenRouter or TypeSafe key: environment first, then `path` (.env.openrouter) and a sibling .env.typesafe."""
    import os
    for name in KEY_NAMES:
        key = os.environ.get(name, "").strip()
        if key:
            return key
    for file in (path, path.with_name(".env.typesafe")):
        if file.exists():
            for line in file.read_text(encoding="utf-8").splitlines():
                name, _, value = line.partition("=")
                value = value.strip().strip('"\'')
                if name.strip() in KEY_NAMES and value:
                    return value
    raise RuntimeError("Set OPENROUTER_API_KEY or TYPESAFE_API_KEY, or add it to .env.openrouter or .env.typesafe")


def jev_route(key: str) -> tuple[str, str]:
    """(endpoint, model) for a key: OpenRouter for its "sk-or-" keys, TypeSafe's own API otherwise."""
    return (ENDPOINT, MODEL) if key.startswith("sk-or-") else (TYPESAFE_ENDPOINT, TYPESAFE_MODEL)


# Everything tunable about how Jev is asked lives here. Each log record stores
# a copy, and replay.py A/B-tests this dict against the logged one while
# keeping the captured screen state fixed.
PROMPTS: dict[str, Any] = {
    "verb_criteria": {
        "left_click": "Click a visible control once, including links and buttons.",
        "right_click": "Open a control's context menu.",
        "double_click": "Open a file, folder, or content item with two clicks.",
        "hover": "Move the pointer over a visible control without clicking.",
        "scroll_up": "Scroll the current window upward.",
        "scroll_down": "Scroll the current window downward.",
        "scroll_left": "Scroll the current window horizontally left.",
        "scroll_right": "Scroll the current window horizontally right.",
        "type_text": "Dictate literal text into a focused editable field; command starts with type or enter text.",
        "press_key": "Press a named keyboard key such as Enter, Escape, Tab, or Backspace.",
        "key_chord": "Send a permitted keyboard shortcut such as Back, Forward, Find, Next tab, Copy, Undo, or Full screen.",
        "zoom_in": "Increase view zoom one step.",
        "zoom_out": "Decrease view zoom one step.",
        "zoom_reset": "Reset view zoom to default.",
        "minimize_window": "Minimize the current app window or a named open window.",
        "maximize_window": "Maximize the current app window or a named open window; its title bar stays visible, unlike full screen.",
        "close_window": "Close the current app window or a named open window, e.g. 'close Slack'.",
        "switch_window": "Activate a specifically named, already open window.",
        "alt_tab": "Switch to the previously active window; destination is not named or guaranteed.",
        "launch_app": "Open an installed app that is not currently running.",
        "no_action": "No safe action fits this request.",
    },
    "verb": SPEECH_NOTE + "Ignore filler and politeness; if a word sounds like an action word, treat it as that action. "
            "Choose the single best action primitive, not its target. 'Open the first link' is a left click. Compare spoken names to exposedControls and openWindows by sound: 'go to' or 'open' a person, chat, channel, folder, or item that sounds like an exposed control is a left click, not switch_window. Use switch_window only for an open window or app named in openWindows; alt_tab only for the previous window. Use scroll for moving content and zoom for changing scale. Do not invent unsupported actions.",
    "group": SPEECH_NOTE + SOUND_MATCH_RULE + " Choose the group containing the requested target. Every group's description lists its eligible controls. Choose none if none fit.",
    "target": SPEECH_NOTE + SOUND_MATCH_RULE + " Choose exactly one observed eligible target. Match ordinals like 'first link' to accessibility order and 'below' to bounding boxes. The relativeReferenceHint is a deterministic geometry/ordinal calculation; use it only if it makes sense. Choose none if ambiguous.",
}


def ask_jev(key: str, question: str, task: str, criteria: dict[str, str], state: dict[str, Any], instructions: str,
            trace: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], float]:
    answers, elapsed = ask_questions(key, task, state, {question: {"type": "choice", "instructions": instructions, "criteria": criteria}}, trace)
    return answers[question], elapsed


def ask_questions(key: str, task: str, state: dict[str, Any], questions: dict[str, dict[str, Any]],
                  trace: list[dict[str, Any]] | None = None) -> tuple[dict[str, dict[str, Any]], float]:
    """One Jev request answering several questions about the same state: choice questions and noul (a probability)."""
    for name, question in questions.items():
        if question["type"] == "choice" and len(question["criteria"]) > MAX_CHOICES:
            raise ValueError(f"{len(question['criteria'])} choices for {name} exceed Jev's {MAX_CHOICES}-choice limit")
    endpoint, model = jev_route(key)
    payload = {"model": model, "state": {"task": task, **state}, "questions": questions}
    # The exact request body (the key travels only in the header) is recorded
    # before sending, so failed calls are replayable too.
    call: dict[str, Any] = {"question": "+".join(questions), "request": payload}
    if trace is not None:
        trace.append(call)
    start = time.perf_counter()
    response = requests.post(endpoint, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=payload, timeout=30)
    elapsed = (time.perf_counter() - start) * 1000
    call["elapsed_ms"] = round(elapsed)
    call["http_status"] = response.status_code
    if response.status_code >= 400:
        call["error_body"] = str(getattr(response, "text", ""))[:2000]  # e.g. max_tokens_exceeded; goal mode retries in two requests
    response.raise_for_status()
    result = response.json()
    call["response"] = result
    answers = result.get("answers", {})
    for name, question in questions.items():
        answer = answers.get(name, {})
        if question["type"] == "choice" and answer.get("choice") not in question["criteria"]:
            raise RuntimeError(f"Jev returned an invalid {name}: {answer.get('choice')!r}")
        if question["type"] == "noul" and not isinstance(answer.get("noul"), (int, float)):
            raise RuntimeError(f"Jev returned no probability for {name}")
    return answers, elapsed


def build_verb_state(state: dict[str, Any], controls: list[Control], apps: list[dict[str, str]]) -> dict[str, Any]:
    return {"activeWindow": state["activeWindow"], "controlCount": len(controls),
            "exposedControls": [f'{c.role} "{c.name}"' for c in controls[:180]],
            "openWindows": [w["title"] for w in state["openWindows"]], "installedApps": [app["name"] for app in apps]}


def choose_verb(key: str, task: str, state: dict[str, Any], trace: list[dict[str, Any]] | None = None,
                prompts: dict[str, Any] | None = None) -> tuple[dict[str, Any], float]:
    prompts = prompts or PROMPTS
    return ask_jev(key, "verb", task, prompts["verb_criteria"], state, prompts["verb"], trace)


WINDOW_VERBS = {"minimize_window", "maximize_window", "close_window"}


def eligible_targets(verb: str, controls: list[Control], windows: list[dict[str, Any]], apps: list[dict[str, str]], current_hwnd: int) -> list[dict[str, Any]]:
    targets = [{"id": "none", "kind": "none", "description": "No matching target; do nothing."}]
    if verb in CONTROL_VERBS or verb in {"type_text", "select_text"}:
        for control in controls:
            if not control.enabled:
                continue
            if verb == "right_click" and control.role not in RIGHT_ROLES:
                continue
            if verb == "double_click" and control.role not in DOUBLE_ROLES:
                continue
            if verb in {"type_text", "select_text"} and control.role not in FIELD_ROLES:
                continue
            targets.append({"id": control.id, "kind": "control", "description": f'{describe_control(control)} at {control.rect}, under {control.path}'})
        surface = surface_target(controls) if verb in {"right_click", "double_click"} else None
        if surface:
            targets.append(surface)
    elif verb in WINDOW_VERBS:
        # "Close Valorant" names a window that is often not the one in front.
        targets.append({"id": "current", "kind": "current", "description": "Current active window"})
        targets += _window_targets(windows, current_hwnd)
    elif verb == "switch_window":
        targets += _window_targets(windows, current_hwnd)
    elif verb == "launch_app":
        for index, app in enumerate(apps):
            targets.append({"id": f"a{index}", "kind": "app", "description": f'Installed app "{app["name"]}"'})
    elif verb == "press_key":
        for key in PRESS_KEYS:
            targets.append({"id": f'k{key.replace("+", "_")}', "kind": "key", "key": key, "description": f"Press {key} in the current window"})
    elif verb == "key_chord":
        for name, chord in CHORDS.items():
            targets.append({"id": f"ch_{name}", "kind": "chord", "key": name, "description": f"{name.replace('_', ' ')} ({chord[0]}) in the current window; behavior is app-dependent"})
    elif verb == "no_action":
        return targets
    else:
        targets.append({"id": "current", "kind": "current", "description": "Current active window"})
    return targets


PRESS_KEYS = ("Enter", "Escape", "Tab", "Shift+Tab", "Backspace", "Delete", "Home", "End", "PageUp", "PageDown", "Space")
def _window_targets(windows: list[dict[str, Any]], current_hwnd: int) -> list[dict[str, Any]]:
    """Other open windows. Windows with the same title and process cannot be told apart by name, so only the most
    recently used one (the first in z-order) is offered; otherwise a choice between identical labels is a coin flip."""
    seen: set[tuple[str, str]] = set()
    targets = []
    for window in windows:
        label = (window["title"], window["process"])
        if window["hwnd"] == current_hwnd or label in seen:
            continue
        seen.add(label)
        targets.append({"id": f'w{window["hwnd"]}', "kind": "window", "description": f'Open window "{window["title"]}" ({window["process"]})'})
    return targets


CHORDS = {
    "back": ("Alt+Left", "%{LEFT}"),
    "forward": ("Alt+Right", "%{RIGHT}"),
    "refresh": ("Ctrl+R", "^r"),
    "find": ("Ctrl+F", "^f"),
    "next_tab": ("Ctrl+Tab", "^{TAB}"),
    "previous_tab": ("Ctrl+Shift+Tab", "^+{TAB}"),
    "new_tab": ("Ctrl+T", "^t"),
    "close_tab": ("Ctrl+W", "^w"),
    "copy": ("Ctrl+C", "^c"),
    "paste": ("Ctrl+V", "^v"),
    "select_all": ("Ctrl+A", "^a"),
    "save": ("Ctrl+S", "^s"),
    "undo": ("Ctrl+Z", "^z"),
    "redo": ("Ctrl+Shift+Z", "^+z"),
    "full_screen": ("F11", "{F11}"),
}


def reference_hint(task: str, controls: list[Control]) -> str | None:
    """Conservative generic ordinal/spatial hint; final choice still belongs to Jev."""
    words = task.casefold()
    match = re.search(r"\b(first|second|third|fourth|last)\s+(link|button)\b", words)
    if match:
        role = "Hyperlink" if match.group(2) == "link" else "Button"
        matching = [c for c in controls if c.role == role]
        position = {"first": 0, "second": 1, "third": 2, "fourth": 3, "last": -1}[match.group(1)]
        if matching and (position == -1 or position < len(matching)):
            return matching[position].id
    match = re.search(r"\b(?:button|link|item)\s+(below|above)\s+(?:the\s+)?(.+?)(?:\s+(?:button|link|item))?\s*$", words)
    if match:
        direction, anchor_name = match.groups()
        anchor_name = re.sub(r"\s+(button|link|item)$", "", anchor_name).strip()
        anchors = [c for c in controls if c.name.casefold() == anchor_name]
        if len(anchors) != 1:
            return None
        anchor = anchors[0]
        ax = (anchor.rect[0] + anchor.rect[2]) / 2
        ay = (anchor.rect[1] + anchor.rect[3]) / 2
        options = []
        for c in controls:
            if c.id == anchor.id or c.rect[2] <= c.rect[0] or c.rect[3] <= c.rect[1]:
                continue
            cx = (c.rect[0] + c.rect[2]) / 2
            cy = (c.rect[1] + c.rect[3]) / 2
            dy = cy - ay
            if (direction == "below" and dy > 0) or (direction == "above" and dy < 0):
                if abs(cx - ax) <= max(100, (anchor.rect[2] - anchor.rect[0]) * 1.5):
                    options.append((abs(dy) + abs(cx - ax) * 0.4, c.id))
        options.sort()
        if options and (len(options) == 1 or options[1][0] > options[0][0] * 1.3):
            return options[0][1]
    return None


def choose_target(key: str, task: str, verb: str, targets: list[dict[str, Any]], state: dict[str, Any], hint: str | None,
                  trace: list[dict[str, Any]] | None = None, prompts: dict[str, Any] | None = None) -> tuple[dict[str, Any], float]:
    prompts = prompts or PROMPTS
    context = {"chosenVerb": verb, "activeWindow": state.get("activeWindow")}
    # Goal mode supplies these fields; the original single-step request stays
    # byte-for-byte the same for replay fidelity.
    context.update({name: state[name] for name in ("startedIn", "stepsTaken") if name in state})
    group_ms = 0.0
    if len(targets) > MAX_CHOICES:
        groups = [targets[1 + i:1 + i + MAX_CHOICES - 1] for i in range(0, len(targets) - 1, MAX_CHOICES - 1)]
        hinted = next((group for group in groups if any(t["id"] == hint for t in group)), None)
        if hinted is not None:
            targets = [targets[0], *hinted]
        else:
            group_criteria = {f"g{i}": "; ".join(t["description"] for t in group) for i, group in enumerate(groups)}
            group_criteria["none"] = "No target in any group matches the task."
            if len(group_criteria) > MAX_CHOICES:
                raise ValueError("Too many target groups for one Jev choice; narrow the window")
            group_answer, group_ms = ask_jev(key, "group", task, group_criteria,
                                              context,
                                              prompts["group"], trace)
            if group_answer["choice"] == "none" or not minimum_confidence(group_answer):
                return {"id": "none", "kind": "none", "description": "No confidently matching group"}, group_ms
            targets = [targets[0], *groups[int(group_answer["choice"][1:])]]
    criteria = {t["id"]: t["description"] for t in targets}
    answer, elapsed = ask_jev(key, "target", task, criteria,
                              {**context, "relativeReferenceHint": hint},
                              prompts["target"], trace)
    selected = next(t for t in targets if t["id"] == answer["choice"])
    return {**selected, "probability": answer.get("probabilities", {}).get(answer["choice"]), "confidence": answer.get("confidence"),
            "runner_up": runner_up(answer), "group_ms": round(group_ms)}, elapsed + group_ms


def plan_command(key: str, utterance: str, state: dict[str, Any], controls: list[Control], apps: list[dict[str, str]],
                 record: dict[str, Any], prompts: dict[str, Any] | None = None,
                 progress: Callable[[str], None] | None = None) -> None:
    """Verb choice, eligible targets, target choice, and every safety gate.

    Shared by the app and replay.py so a replay exercises identical logic.
    Fills record progressively (verb, relative_hint, target, timings_ms,
    jev_calls) and raises RuntimeError when the plan must not execute.
    progress, if given, receives the chosen verb before the target call.
    """
    prompts = prompts or PROMPTS
    trace = record.setdefault("jev_calls", [])
    timings = record.setdefault("timings_ms", {})
    verb_answer, verb_ms = choose_verb(key, utterance, build_verb_state(state, controls, apps), trace, prompts)
    verb = verb_answer["choice"]
    timings["jev_verb"] = round(verb_ms)
    record["verb"] = verb_answer
    if not minimum_confidence(verb_answer):
        raise RuntimeError("Jev verb confidence below 0.5")
    if progress:
        progress(verb)
    targets = eligible_targets(verb, controls, state["openWindows"], apps, state["activeWindow"]["hwnd"])
    hint = reference_hint(utterance, controls)
    record["relative_hint"] = hint
    target, target_ms = choose_target(key, utterance, verb, targets, state, hint, trace, prompts)
    timings["jev_group"] = target.get("group_ms", 0)
    timings["jev_target"] = round(target_ms - target.get("group_ms", 0))
    timings["jev_total"] = round(verb_ms + target_ms)
    record["target"] = target
    if target["id"] == "none" or verb == "no_action" or not target_accepted(target.get("probability"), target.get("runner_up")):
        raise RuntimeError("No sufficiently confident target")
    if hint and verb in {"left_click", "right_click", "double_click"} and target["id"] != hint:
        raise RuntimeError(f"Jev target {target['id']} disagreed with spatial/ordinal hint {hint}; action cancelled")
    if verb == "type_text" and not dictation_text(utterance):
        raise RuntimeError("For literal typing, say 'type <text>'")


def snapshot_inputs(utterance: str, state: dict[str, Any], apps: list[dict[str, str]]) -> dict[str, Any]:
    """Everything plan_command reads, in JSON form. Installed-app shortcut paths are omitted; only names reach Jev."""
    return {"utterance": utterance, "activeWindow": state["activeWindow"], "openWindows": state["openWindows"],
            "controls": state["controls"], "installedApps": [{"name": app["name"]} for app in apps],
            **({"texts": state["texts"]} if state.get("texts") else {})}


def restore_inputs(inputs: dict[str, Any]) -> tuple[str, dict[str, Any], list[Control], list[dict[str, str]]]:
    controls = [Control(c["id"], c["name"], c["role"], tuple(c["rect"]), c["path"], c["enabled"], c.get("hwnd", 0), c.get("state", ""),
                        c.get("detail", ""), c.get("value", ""), c.get("context", "")) for c in inputs["controls"]]
    state = {"activeWindow": inputs["activeWindow"], "openWindows": inputs["openWindows"], "controls": inputs["controls"],
             "texts": inputs.get("texts", [])}
    return inputs["utterance"], state, controls, inputs["installedApps"]


def minimum_confidence(answer: dict[str, Any], threshold: float = 0.5) -> bool:
    value = answer.get("probabilities", {}).get(answer.get("choice"))
    try:
        return float(value) >= threshold
    except (TypeError, ValueError):
        return False


def runner_up(answer: dict[str, Any]) -> dict[str, Any] | None:
    """Second most probable choice (including "none"), so the target gate can judge the margin."""
    others = [(choice, float(p)) for choice, p in answer.get("probabilities", {}).items()
              if choice != answer.get("choice") and isinstance(p, (int, float))]
    if not others:
        return None
    choice, probability = max(others, key=lambda item: item[1])
    return {"id": choice, "probability": probability}


TARGET_ABSOLUTE = 0.5      # always accept at or above this
TARGET_FLOOR = 0.35        # below this, never accept
TARGET_MARGIN_RATIO = 2.5  # between floor and absolute, top pick must be this many times the runner-up


def target_accepted(probability: Any, second: dict[str, Any] | None) -> bool:
    """Accept a clear winner even when probability mass is spread over near-duplicate controls or search boxes."""
    try:
        top = float(probability)
    except (TypeError, ValueError):
        return False
    if top >= TARGET_ABSOLUTE:
        return True
    if top < TARGET_FLOOR:
        return False
    return top >= TARGET_MARGIN_RATIO * (second["probability"] if second else 0.0)


def dictation_text(task: str) -> str | None:
    match = re.match(r"\s*(?:type|enter text|dictate)\s+(.+?)\s*$", task, re.I | re.S)
    return match.group(1) if match else None


def append_log(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), **record}
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# Whisper prompt hints: command vocabulary plus names the user can see, so
# "Marisol Grey" or "scroll" win over sound-alikes. Whisper allows about 220
# prompt tokens; ~700 characters stays well inside that.
ASR_COMMAND_WORDS = ("Click, double-click, right-click, scroll up, scroll down, open, go to, switch to, "
                     "minimize, maximize, zoom in, zoom out, type, press Enter.")
HOTWORD_ROLES = ("ListItem", "TreeItem", "TabItem", "Hyperlink", "MenuItem", "Button")
# Window chrome and scrollbar parts every app exposes; they only use up prompt room.
HOTWORD_SKIP = {"system", "line up", "line down", "page up", "page down", "page left", "page right", "column left",
                "column right", "minimize", "maximize", "restore", "close", "back", "forward", "more options",
                "filter dropdown", "application", "system menu bar", "horizontal", "vertical"}


def asr_hotwords(state: dict[str, Any], controls: list[Control], limit: int = 700) -> str:
    names: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        text = re.sub(r"\s*\((?:pinned|unread|selected|active|new)\)", "", " ".join(text.split()), flags=re.I).strip(" ,.;:")
        if text.lower() in HOTWORD_SKIP or not 2 <= len(text) <= 40 or len(text.split()) > 5 or not re.search(r"[A-Za-z]{2}", text):
            return
        if text.lower() not in seen:
            seen.add(text.lower())
            names.append(text)

    # Open windows first: they are few, and a busy foreground window's controls
    # would otherwise fill the limit and drop "switch to <app>" targets.
    for window in state.get("openWindows", []):
        parts = re.split(r" [-–—|] ", window.get("title", ""))
        add(parts[0])
        add(parts[-1])
    for role in HOTWORD_ROLES:  # then navigable items (people, chats, folders, tabs)
        for control in controls:
            if control.role == role:
                add(control.name)
                if control.detail:
                    add(control.detail)
    text = ASR_COMMAND_WORDS
    for name in names:
        if len(text) + len(name) + 2 > limit:
            break
        text += f" {name},"
    return text.rstrip(",") + ("." if names else "")


def collapse_repeats(text: str) -> str:
    """Drop immediately repeated sentences, Whisper's usual failure on cut-off audio."""
    sentences = re.findall(r"[^.?!]+[.?!]*", text or "")
    kept: list[str] = []
    for sentence in sentences:
        if not kept or sentence.strip().lower() != kept[-1].strip().lower():
            kept.append(sentence)
    return "".join(kept).strip()
