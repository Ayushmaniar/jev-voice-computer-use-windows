"""Goal mode: reach a multi-step goal with repeated Jev decisions, no generative model.

Each step reads the screen, then asks Jev, in one request, how likely it is
that the goal is already reached (a noul probability) and which action
primitive comes next, along with target choices (and, for typing, which words
of the goal to type). Large screens may need a grouped target request. The
action runs, the screen settles,
and the loop repeats with every earlier step described in the state, until the
done probability clears a threshold, Jev finds no useful action, the same
action stops changing anything, or the step budget runs out.

Screen reading and execution are passed in, so the app, the evaluation
harness, and offline tests share this loop.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from .core import (PROMPTS, SOUND_MATCH_RULE, SPEECH_NOTE, Control, ask_questions, choose_target, describe_control, eligible_targets,
                   reference_hint, runner_up, snapshot_inputs, surface_target, target_accepted)

MAX_STEPS = 12
DONE_THRESHOLD = 0.75  # goal counts as reached at or above this noul probability
VERB_FLOOR = 0.15      # a verb below this is never tried; several verbs can each be a valid next step
VERB_TRIES = 3         # when the likeliest verb has no confident target, the next ones are tried (no extra request)
MENU_PATH = "Open menu"
# A wrong step in goal mode is followed by a fresh screen read, so the target margin is a bit looser than the
# single-command gate (2.5x); the floor and the absolute bar are the same.
GOAL_MARGIN_RATIO = 2.0
MENU_BAR_FLOOR = 0.4  # opening a menu bar menu is reversible, so it needs less certainty than an action
SUGGESTION_FLOOR = 0.4  # likewise picking between an open field's suggestions ("Los Angeles" or "LAX"): retyping undoes it
REPEAT_LIMIT = 6      # the same action this many times in a row, with the goal no closer, stops the run
PLAYER_CLOCK = re.compile(r"\d{1,2}(?::\d{2}){1,2}(?:\s*/\s*\d{1,2}(?::\d{2}){1,2})?")
WAIT_LIMIT = 3       # this many waits in a row that leave the screen unchanged stop the run: nothing is loading
IMPORT_MENU = re.compile(r"\b(?:add|import|browse|select|open)\b.*\bfile\b", re.I)
EXPLICIT_FILE = re.compile(r"\b(?:file|browse|import|from disk|from computer)\b", re.I)
# Verbs that act on the current window as a whole: there is no target to choose.
WHOLE_WINDOW = {"scroll_up", "scroll_down", "scroll_left", "scroll_right", "zoom_in", "zoom_out", "zoom_reset", "wait", "alt_tab"}
RISKY_CONTROL = re.compile(r"\b(send|submit|post|publish|delete|remove|erase|discard|confirm|purchase|buy|pay|transfer|save|overwrite)\b", re.I)
CREDENTIAL_SECRET = re.compile(r"\b(password|passcode|passphrase|security code|verification code|one.time code|otp|pin)\b", re.I)
CREDENTIAL_CONTACT = re.compile(r"^(?:e.?mail(?: address)?|phone(?: number)?|e.?mail or phone|phone or e.?mail)$", re.I)

GOAL_PROMPTS: dict[str, Any] = {
    "verb_criteria": {**PROMPTS["verb_criteria"],
                      "wait": "Wait about a second because the app or page is still opening or loading.",
                      "select_text": "Select (highlight) words already written in a field, as a person would drag across "
                                     "them, before formatting them (bold, italic) or copying them.",
                      "no_action": "Nothing available can make progress toward the goal."},
    "done": SPEECH_NOTE + "Decide whether the user's entire goal is already accomplished in the current state, "
            "judging only from the active window, its visible controls, the open windows, and the steps already taken. "
            "An app, page, folder, or item that the goal asks to open must be the one now in front. A setting, mode, "
            "or selection the goal asks for must already be in effect: shown on screen, or done by a step already taken "
            "whose effect cannot be seen (playback speed, volume, sound, a keyboard shortcut). If any part of the goal "
            "still needs an action, it is not accomplished. If a prior step successfully typed the exact requested "
            "text into the intended field and the current screen offers no evidence that it failed, count that text "
            "entry as complete even when the field's contents are not exposed by UI Automation. For an open or show "
            "request, count it as complete when "
            "the requested item is the active window and the steps show it was opened. Do not require an editable "
            "control or another click once that item is in front.",
    "verb": SPEECH_NOTE + "The goal may need several actions. Choose only the NEXT single action primitive from the "
            "CURRENT state; after it runs, the screen is read again and the following action is chosen then. Use the "
            "steps already taken and do not repeat one that already succeeded. If the goal concerns an app that is "
            "not the active window, switch_window when it is in openWindows, otherwise launch_app. Inside the active "
            "app, act on the exposed controls: items marked (in open menu) belong to a menu that is open right now and "
            "are chosen with left_click, and choosing the menu's own title again would close it; a menu bar item or a "
            "button with a dropdown opens with left_click; right_click opens a context menu. Menus often hold commands "
            "that have no visible button. To enter words into a field, use type_text; a field shown with = \"...\" "
            "already holds that text, and type_text replaces it. After typing a search or "
            "address, press_key Enter submits it. Before submitting text already in an address or search bar, "
            "check the current visible controls: if a hyperlink names the requested destination itself, clicking "
            "that link is a more direct next step than submitting a search for its name. Use wait only when the "
            "last step opened something that is still loading.",
    "target": SPEECH_NOTE + SOUND_MATCH_RULE + " The goal may need several actions; this choice is only the target of "
              "the next action, which has already been decided. Choose the one observed target that makes the most "
              "progress toward the goal from the current state, given the steps already taken. Targets described as "
              "under 'Open menu' are items of a menu that is open now; prefer them over the item that opened the menu. "
              "When the goal needs several targets in order (digits of a number, parts of a path, a menu and then its "
              "submenu), choose the earliest one not yet done. To use, turn on, or switch to something the app may "
              "already offer (a track, device, mode, or recent item), prefer the entry that lists existing choices over "
              "one that adds, imports, or browses for a new file, unless the goal asks to add, import, or browse. "
              "Choose none if nothing fits.",
    "text": SPEECH_NOTE + "The chosen next action types into targetField. Choose the exact words from the user's goal that "
            "belong in that field now: only the content to enter (a search query, name, address, or message), "
            "without command words such as 'search for', 'type', 'look up', or the name of the app, and without words "
            "that only say where or how to enter it ('at the end', 'on a new line', 'in the subject'). A value already "
            "entered into a different field (see stepsTaken and the fields' current values) usually belongs only there, "
            "so choose the words the goal gives for this field; repeat a value only when the goal says it goes in both "
            "fields. Do not use a site or app name as an account credential.",
}


def text_candidates(goal: str, limit: int = 254) -> dict[str, str]:
    """Literal spans of the goal, with explicit long text kept ahead of short spans.

    Jev cannot generate text. Quoted content and text after an explicit
    dictation cue remain available even when a long goal fills the choice
    budget; every candidate is still copied from the user's request.
    """
    words = goal.split()
    spans: dict[str, str] = {}
    seen: set[str] = set()

    def add(value: str) -> None:
        value = value.strip()
        folded = value.casefold()
        if value and len(value) <= 1200 and folded not in seen and len(spans) < limit:
            seen.add(folded)
            spans[f"t{len(spans)}"] = value

    for opening, closing in (("\"", "\""), ("“", "”"), ("‘", "’")):
        for match in re.finditer(re.escape(opening) + r"(.+?)" + re.escape(closing), goal, re.S):
            add(match.group(1))
    for match in re.finditer(r"(?<!\w)'(.+?)'(?!\w)", goal, re.S):
        add(match.group(1))
    introduced = re.search(r":\s+(.+)$", goal, re.S)
    if introduced:
        add(introduced.group(1))
    for match in re.finditer(r"\b(?:type|write|enter text|dictate|search for|look up)\b\s+", goal, re.I):
        add(goal[match.end():])
    for length in range(1, min(len(words), 14) + 1):
        for start in range(len(words) - length + 1):
            text = " ".join(words[start:start + length]).strip(" \t\"'“”‘’,.;:!?")
            add(text)
            if len(spans) >= limit:
                return {key: f'Type "{value}"' for key, value in spans.items()}
    return {key: f'Type "{value}"' for key, value in spans.items()}


def control_label(control: Control) -> str:
    menu = " (in open menu)" if control.path.startswith(MENU_PATH) else ""
    return describe_control(control) + menu


def goal_state(steps: list[dict[str, Any]], initial: dict[str, Any], state: dict[str, Any], controls: list[Control],
               apps: list[dict[str, str]]) -> dict[str, Any]:
    return {"startedIn": initial["activeWindow"]["title"],
            "stepsTaken": [step["summary"] for step in steps] or ["None yet; this is the first step."],
            "activeWindow": state["activeWindow"], "controlCount": len(controls),
            "exposedControls": [control_label(c) for c in controls[:180]],
            "visibleText": state.get("texts", [])[:60],
            "openWindows": [w["title"] for w in state["openWindows"]], "installedApps": [app["name"] for app in apps]}


# Primitives that choose from the same candidates share one target head, which keeps a
# busy page (a mail inbox, a long web page) inside Jev's input limit.
HEADS = {"control": {"left_click", "right_click", "double_click", "hover"}, "field": {"type_text", "select_text"},
         "window": {"switch_window", "minimize_window", "maximize_window", "close_window"},
         "app": {"launch_app"}, "key": {"press_key"}, "chord": {"key_chord"}}
HEAD_OF = {verb: head for head, verbs in HEADS.items() for verb in verbs}
HEAD_MEANING = {"control": "the control the next click, double-click, right-click, or hover acts on",
                "field": "the editable field the next typing or text selection acts on",
                "window": "the window the next switch, minimize, maximize, or close acts on",
                "app": "the installed app to launch next", "key": "the key to press next",
                "chord": "the keyboard shortcut to send next"}


def _head_targets(head: str, controls: list[Control], state: dict[str, Any], apps: list[dict[str, str]]) -> list[dict[str, Any]]:
    hwnd = state["activeWindow"]["hwnd"]
    if head == "control":  # every enabled control; each click verb's role limits are applied after the choice
        targets = [{"id": "none", "kind": "none", "description": "No matching target; do nothing."}]
        targets += [{"id": c.id, "kind": "control", "description": f"{control_label(c)} at {c.rect}, under {c.path[-60:]}"}
                    for c in controls if c.enabled]
        surface = surface_target(controls)
        return targets + [surface] if surface else targets
    if head == "window":
        return eligible_targets("close_window", controls, state["openWindows"], apps, hwnd)
    verb = next(iter(HEADS[head]))
    return eligible_targets(verb, controls, state["openWindows"], apps, hwnd)


def _allowed(verb: str, target: dict[str, Any], controls: list[Control], state: dict[str, Any], apps: list[dict[str, str]]) -> bool:
    """Whether a shared head's choice is valid for this particular primitive (right-click roles, current window...)."""
    eligible = eligible_targets(verb, controls, state["openWindows"], apps, state["activeWindow"]["hwnd"])
    return any(t["id"] == target["id"] for t in eligible)


def plan_step(key: str, goal: str, steps: list[dict[str, Any]], initial: dict[str, Any], state: dict[str, Any],
              controls: list[Control], apps: list[dict[str, str]], step: dict[str, Any],
              prompts: dict[str, Any] | None = None) -> str:
    """Fill `step` with the decision for the current screen. Returns "done", "act", or "blocked".

    One Jev request answers everything: the done probability, the next action
    primitive, and a target head for each kind of target (each head sees the
    same full state). The chosen primitive's head is used; if its target is not
    confident, the runner-up primitive is tried without another request. If the
    screen is too big for one request, the heads go in a second request."""
    prompts = prompts or GOAL_PROMPTS
    trace = step.setdefault("jev_calls", [])
    timings = step.setdefault("timings_ms", {})
    view = goal_state(steps, initial, state, controls, apps)
    view["relativeReferenceHint"] = reference_hint(goal, controls)
    base = {"goal_done": {"type": "noul", "instructions": prompts["done"]},
            "next_action": {"type": "choice", "instructions": prompts["verb"], "criteria": prompts["verb_criteria"]}}
    heads = {head: _head_targets(head, controls, state, apps) for head in HEADS}
    heads = {head: targets for head, targets in heads.items() if len(targets) > 1}
    small_heads = {head: targets for head, targets in heads.items() if len(targets) <= 255}

    def head_questions(names) -> dict[str, dict[str, Any]]:
        # The text to type is asked once the field is known (below): asked alongside the field, it was chosen
        # without knowing which field it goes into ("Dubai" for "Where to?").
        return {f"{head}_target": {"type": "choice", "instructions": f"Assume the next action needs {HEAD_MEANING[head]}. "
                                   + prompts["target"], "criteria": {t["id"]: t["description"] for t in heads[head]}}
                for head in names}

    try:
        answers, elapsed = ask_questions(key, goal, view, {**base, **head_questions(small_heads)}, trace)
        timings["jev"] = round(elapsed)
    except Exception as error:
        if "max_tokens" not in str(getattr(getattr(error, "response", None), "text", "")):
            raise
        answers, elapsed = ask_questions(key, goal, view, base, trace)
        ranked = sorted(answers["next_action"].get("probabilities", {}).items(), key=lambda kv: -float(kv[1]))
        wanted: list[str] = []
        for verb, _ in ranked[:VERB_TRIES]:
            if HEAD_OF.get(verb) in small_heads and HEAD_OF[verb] not in wanted:
                wanted.append(HEAD_OF[verb])
        more, more_elapsed = ask_questions(key, goal, view, head_questions(wanted), trace) if wanted else ({}, 0.0)
        answers = {**answers, **more}
        timings["jev"] = round(elapsed + more_elapsed)
        heads = {head: targets for head, targets in heads.items() if head not in small_heads or head in wanted}
        small_heads = {head: small_heads[head] for head in wanted}
    done = float(answers["goal_done"]["noul"])
    verb_answer = answers["next_action"]
    step["done_p"] = done
    step["verb"] = verb_answer
    ranked = sorted(verb_answer.get("probabilities", {}).items(), key=lambda kv: -float(kv[1]))
    if done >= DONE_THRESHOLD or (ranked and ranked[0][0] == "no_action" and done >= 0.5):
        return "done"  # a confident done, or "nothing left to do" backed by an even-odds done
    if not ranked or ranked[0][0] == "no_action":
        step["reason"] = "Jev found no action that makes progress"
        return "blocked"
    candidates = [verb for verb, p in ranked if verb != "no_action" and float(p) >= VERB_FLOOR][:VERB_TRIES]
    texts: dict[str, dict[str, Any]] = {}  # text answer per field, so type_text and select_text on one field ask once
    if not candidates:
        step["reason"] = f"next action unclear (best {ranked[0][0]} at {ranked[0][1]})"
        return "blocked"
    for verb in candidates:
        step["verb"] = {**verb_answer, "choice": verb}
        if verb in WHOLE_WINDOW:
            step["target"] = {"id": "current", "kind": "current", "description": "Current active window", "probability": 1.0}
            return "act"
        head = HEAD_OF.get(verb)
        if head not in heads:
            step.setdefault("tried", []).append({"verb": verb, "reason": "no eligible target on screen"})
            continue
        if head in small_heads:
            answer = answers[f"{head}_target"]
            target = next(t for t in heads[head] if t["id"] == answer["choice"])
            alternative = _existing_menu_choice(goal, verb, target, answer, heads[head], controls)
            if alternative:
                target = alternative
            step["target"] = {**target, "probability": answer.get("probabilities", {}).get(answer["choice"]),
                              "runner_up": runner_up(answer)}
            if alternative:
                step["target"]["probability"] = answer["probabilities"][target["id"]]
                step["target"]["runner_up"] = None
                step["target"]["selection_rule"] = "prefer existing choices to file import"
            elif verb == "left_click" and step["target"]["runner_up"] \
                    and _nested_twin(target["id"], step["target"]["runner_up"]["id"], controls):
                # Both answers are one control exposed twice; their split is not doubt about what to click.
                twin = step["target"]["runner_up"]["id"]
                step["target"]["probability"] = float(step["target"]["probability"] or 0) + step["target"]["runner_up"]["probability"]
                step["target"]["runner_up"] = runner_up({"choice": target["id"], "probabilities": {
                    k: v for k, v in answer.get("probabilities", {}).items() if k != twin}})
                step["target"]["merged_with"] = twin  # still judged by the usual margin, now against the next other target
        else:
            # Reuse the core planner's group-choice path for large screens.
            # Every candidate remains selectable rather than dropping #255+.
            target, extra_ms = choose_target(key, goal, verb, heads[head], view,
                                             view["relativeReferenceHint"], trace,
                                             {"group": PROMPTS["group"], "target": prompts["target"]})
            timings["jev"] += round(extra_ms)
            step["target"] = target
            answer = None
        menu_tie = step["target"].get("selection_rule") and float(step["target"].get("probability") or 0) >= 0.3
        confident = _confident(step["target"].get("probability"), step["target"].get("runner_up"))
        opens_menu = not confident and verb == "left_click" and _menu_bar_item(target["id"], controls) \
            and float(step["target"].get("probability") or 0) >= MENU_BAR_FLOOR
        picks_suggestion = not confident and not opens_menu and verb == "left_click" \
            and _open_suggestion(target["id"], (step["target"].get("runner_up") or {}).get("id"), controls) \
            and float(step["target"].get("probability") or 0) >= SUGGESTION_FLOOR
        if target["id"] == "none" or not (menu_tie or opens_menu or picks_suggestion or confident):
            step.setdefault("tried", []).append({"verb": verb, "target": step["target"], "reason": "no confident target"})
            tab = _tab_instead_of_window(verb, answers, controls)
            if tab is None:
                continue
            # A browser or editor tab is not a top-level window: switching to it is a click on the tab.
            verb, head, target = "left_click", "control", tab
            step["verb"] = {**verb_answer, "choice": verb}
            step["target"] = {**tab, "selection_rule": "tab instead of window"}
        if opens_menu:
            step["target"]["selection_rule"] = "open a menu bar menu to see its items"
        if picks_suggestion:
            step["target"]["selection_rule"] = "pick the likelier of an open field's suggestions"
        elif step["target"].get("merged_with"):
            step["target"]["selection_rule"] = "same control nested in its own container"
        if not _allowed(verb, target, controls, state, apps):
            step.setdefault("tried", []).append({"verb": verb, "target": step["target"], "reason": f"not a {verb} target"})
            continue
        if verb in {"type_text", "select_text"}:
            field = next((c for c in controls if c.id == target["id"]), None)
            text_answer = texts.get(target["id"])
            if text_answer is None:
                more, extra_ms = ask_questions(key, goal, {**view, "targetField": control_label(field) if field else target["description"]},
                                               {"text_to_type": {"type": "choice", "instructions": prompts["text"],
                                                                 "criteria": text_candidates(goal)}}, trace)
                timings["jev"] += round(extra_ms)
                text_answer = texts[target["id"]] = more["text_to_type"]
            step["text_p"] = text_answer.get("probabilities", {}).get(text_answer["choice"])
            if not _confident(step["text_p"], runner_up(text_answer)):
                # Like an unclear target: the next likeliest action gets its turn (opening a date picker instead).
                step.setdefault("tried", []).append({"verb": verb, "target": step["target"], "reason": "text to type is unclear"})
                step.pop("text_p")
                continue
            step["text"] = text_candidates(goal)[text_answer["choice"]][len('Type "'):-1]
            if verb == "select_text":
                if field and field.value and step["text"].casefold() not in field.value.casefold():
                    step.setdefault("tried", []).append({"verb": verb, "target": step["target"],
                                                         "reason": f'"{step.pop("text")}" is not in that field'})
                    continue
                return "act"
            if field and CREDENTIAL_SECRET.search(field.name):
                step["reason"] = "Enter passwords and verification codes yourself; goal mode will not type them"
                step.pop("text", None)
                return "blocked"
            if field and CREDENTIAL_CONTACT.search(field.name):
                value = step["text"].strip()
                email = re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value)
                phone = len(re.sub(r"\D", "", value)) >= 7 and not re.search(r"[A-Za-z]", value)
                if not email and not phone:
                    step["reason"] = "An email address or phone number was not supplied for this sign-in field"
                    step.pop("text", None)
                    return "blocked"
        return "act"
    unclear_text = step.get("tried") and all(t.get("reason") == "text to type is unclear" for t in step["tried"][-1:])
    step["reason"] = "text to type is unclear" if unclear_text else "no sufficiently confident target"
    return "blocked"


def _menu_bar_item(control_id: str, controls: list[Control]) -> bool:
    """A menu title in the window's menu bar. Opening it only shows its items (another click or Escape closes it),
    so a looser choice between two plausible menus costs one reversible step and shows the real options."""
    control = next((c for c in controls if c.id == control_id), None)
    return bool(control and control.role == "MenuItem" and not control.path.startswith(MENU_PATH)
                and not RISKY_CONTROL.search(control.name))


def _nested_twin(first_id: str, second_id: str, controls: list[Control]) -> bool:
    """One control inside another of exactly the same name: a thumbnail's play button inside its "Play <song>" link.
    Clicking either does the same thing. A differently named control nested in a row (a Delete button in an email
    row) never qualifies, and neither do same-named controls side by side."""
    first = next((c for c in controls if c.id == first_id), None)
    second = next((c for c in controls if c.id == second_id), None)
    if not first or not second or not first.name or first.name.casefold() != second.name.casefold():
        return False

    def inside(inner: Control, outer: Control) -> bool:
        return (outer.rect[0] <= inner.rect[0] and outer.rect[1] <= inner.rect[1] and inner.rect[2] <= outer.rect[2]
                and inner.rect[3] <= outer.rect[3] and inner.path.endswith(outer.name))
    return inside(first, second) or inside(second, first)


def _open_suggestion(control_id: str, runner_up_id: str | None, controls: list[Control]) -> bool:
    """A close call between two items of an expanded field's suggestion list: both came from what was typed, so
    either is a reasonable pick. Items listed before the field, or a runner-up outside the list, do not qualify."""
    field = next((i for i, c in enumerate(controls) if c.role in {"ComboBox", "Edit"} and "expanded" in c.state), None)
    if field is None:
        return False
    suggestions = {c.id for c in controls[field + 1:] if c.role == "ListItem" and not RISKY_CONTROL.search(c.name)}
    return control_id in suggestions and (runner_up_id in suggestions or runner_up_id in {None, "none"})


def _tab_instead_of_window(verb: str, answers: dict[str, Any], controls: list[Control]) -> dict[str, Any] | None:
    """When switch_window finds no matching window but the control head confidently picks a tab, use that tab."""
    if verb != "switch_window" or "control_target" not in answers:
        return None
    answer = answers["control_target"]
    control = next((c for c in controls if c.id == answer["choice"]), None)
    probability = answer.get("probabilities", {}).get(answer["choice"])
    if not control or control.role != "TabItem" or not _confident(probability, runner_up(answer)):
        return None
    return {"id": control.id, "kind": "control", "description": f"{control_label(control)} at {control.rect}, under {control.path[-60:]}",
            "probability": probability, "runner_up": runner_up(answer)}


def _confident(probability: Any, second: dict[str, Any] | None) -> bool:
    try:
        top = float(probability)
    except (TypeError, ValueError):
        return False
    if top >= 0.5:
        return True
    return top >= 0.35 and top >= GOAL_MARGIN_RATIO * (second["probability"] if second else 0.0)


def _existing_menu_choice(goal: str, verb: str, target: dict[str, Any], answer: dict[str, Any],
                          candidates: list[dict[str, Any]], controls: list[Control]) -> dict[str, Any] | None:
    """Use an existing-choice menu before a file picker when the request names no file.

    This only resolves a close Jev tie between items in the same open menu;
    it does not choose a target that Jev assigned little probability to.
    """
    if verb != "left_click" or EXPLICIT_FILE.search(goal):
        return None
    second = runner_up(answer)
    if not second or second["id"] == "none":
        return None
    first_p = float(answer.get("probabilities", {}).get(target["id"], 0))
    second_p = float(second["probability"])
    if second_p < 0.3 or second_p < first_p * 0.45:
        return None
    by_id = {c.id: c for c in controls}
    first, other = by_id.get(target["id"]), by_id.get(second["id"])
    if not first or not other or not all(c.role == "MenuItem" and c.path.startswith(MENU_PATH) for c in (first, other)):
        return None
    if first.path != other.path:
        return None
    if IMPORT_MENU.search(first.name) and not IMPORT_MENU.search(other.name):
        return next((t for t in candidates if t["id"] == other.id), None)
    if IMPORT_MENU.search(other.name) and not IMPORT_MENU.search(first.name):
        return target
    return None


def describe(index: int, step: dict[str, Any], before: dict[str, Any], after: dict[str, Any] | None) -> str:
    verb = step["verb"]["choice"]
    target = step.get("target", {})
    what = target.get("description", "") if target.get("kind") not in {None, "none", "current"} else ""
    what = re.sub(r" at \(-?\d+, -?\d+, -?\d+, -?\d+\), under .*$", "", what)  # geometry and tree path are noise here
    typed = f' text "{step["text"]}"' if step.get("text") else ""
    outcome = step.get("error") or step.get("result") or ""
    change = ""
    if after is not None:
        if after["activeWindow"]["title"] != before["activeWindow"]["title"]:
            change = f'; active window is now "{after["activeWindow"]["title"]}"'
        elif step.get("screen_changed") is False:
            change = "; the screen did not visibly change"
    return f"{index}. {verb} {what}{typed}".rstrip() + (f" -> {outcome}" if outcome else "") + change


Observe = Callable[[], tuple[dict[str, Any], list[Control], list[dict[str, str]]]]
Act = Callable[[str, dict[str, Any], dict[str, Any], list[Control], list[dict[str, str]], str | None], str]


def signature(state: dict[str, Any], controls: list[Control], ambient: bool = True) -> tuple:
    """What a person would notice changing: the window in front, its title, and the named controls and field text it shows.

    Without `ambient`, what changes on its own is left out: a playing track's clock ("2:34 / 2:55") and a background
    tab's title ("(1) Inbox", memory use); a tab opening or closing still counts. That view only judges whether
    waiting is getting anywhere; an action's effect is judged on everything, since seeking a video moves only the clock."""
    def seen(c: Control) -> tuple:
        background = not ambient and c.role == "TabItem" and "selected" not in c.state
        return (c.role, "" if background else c.name, c.state, c.value)
    return (state["activeWindow"]["hwnd"], state["activeWindow"]["title"], tuple(seen(c) for c in controls),
            tuple(t for t in state.get("texts", []) if ambient or not PLAYER_CLOCK.fullmatch(t.strip())))


def action_requires_review(step: dict[str, Any], state: dict[str, Any], controls: list[Control]) -> bool:
    """Conservative gate for irreversible steps in an unattended goal run."""
    verb = step["verb"]["choice"]
    target = step.get("target", {})
    if verb == "close_window":
        return True
    if verb == "key_chord" and target.get("key") in {"close_tab", "paste", "save"}:
        return True
    if verb == "press_key" and target.get("key") == "Delete":
        return True
    if verb == "press_key" and target.get("key") == "Enter":
        # An address bar or search box also uses Enter. A terminal, Run dialog,
        # or message field can execute or send without an explicit button.
        active = state["activeWindow"]
        if active.get("process", "").casefold() in {"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe"}:
            return True
        if active.get("title", "").casefold() == "run":
            return True
        return any((c.role in {"Button", "MenuItem"} and RISKY_CONTROL.search(c.name)) or
                   (c.role == "Edit" and re.search(r"\b(message|chat|compose|comment|reply)\b", c.name, re.I))
                   for c in controls)
    if verb in {"left_click", "double_click"}:
        control = next((c for c in controls if c.id == target.get("id")), None)
        return bool(control and RISKY_CONTROL.search(control.name))
    return False


def run_goal(key: str, goal: str, observe: Observe, act: Act, record: dict[str, Any], *,
             max_steps: int = MAX_STEPS, prompts: dict[str, Any] | None = None,
             progress: Callable[[str, dict[str, Any]], None] | None = None,
             should_stop: Callable[[], bool] | None = None,
             allow_action: Callable[[dict[str, Any], dict[str, Any], list[Control]], bool] | None = None,
             initial_observation: tuple[dict[str, Any], list[Control], list[dict[str, str]]] | None = None,
             current_observation: tuple[dict[str, Any], list[Control], list[dict[str, str]]] | None = None,
             refresh_first: bool = False) -> str:
    """Drive the observe → decide → act loop. Fills record["steps"] and returns the outcome.

    act(verb, target, state, controls, apps, text) executes one action, waits for the
    screen to settle, and returns a short result text."""
    started = time.perf_counter()
    steps: list[dict[str, Any]] = record.setdefault("steps", [])
    state, controls, apps = initial_observation if initial_observation is not None else observe()
    initial = state
    record["initial_window"] = state["activeWindow"]
    if current_observation is not None:
        state, controls, apps = current_observation
    if refresh_first:
        state, controls, apps = observe()
    outcome = "step_limit"
    for index in range(1, max_steps + 2):
        if should_stop and should_stop():
            outcome = "cancelled"
            break
        step: dict[str, Any] = {"index": index, "window": state["activeWindow"],
                                "inputs": snapshot_inputs(goal, state, apps)}
        steps.append(step)
        if progress:
            progress("deciding", step)
        try:
            decision = plan_step(key, goal, steps[:-1], initial, state, controls, apps, step, prompts)
        except Exception as error:
            step["error"] = f"{type(error).__name__}: {error}"
            outcome = "error"
            break
        if should_stop and should_stop():
            outcome = "cancelled"
            break
        if decision != "act":
            outcome = decision
            break
        if index > max_steps:
            step["reason"] = f"stopped after {max_steps} actions without reaching the goal"
            break
        if allow_action and not allow_action(step, state, controls):
            step["reason"] = "The proposed step was not approved"
            outcome = "cancelled" if should_stop and should_stop() else "review_required"
            break
        if should_stop and should_stop():
            outcome = "cancelled"
            break
        if progress:
            progress("acting", step)
        if _repeat_of_silent_click(steps):
            # UI Automation's Invoke can report success and do nothing (a switch inside a closed shadow root).
            step["target"]["physical"] = True
        elif _waiting_on_a_silent_click(steps, controls):
            # Jev expects the last click to be loading something, but nothing moved: Google Flights' Search ignores
            # Invoke. A person would click again, so the same control gets a real mouse click instead of a wait.
            click = next(s for s in reversed(steps[:-1]) if s["verb"]["choice"] != "wait")
            control = _still_there(click["target"], controls)
            step["verb"] = {**step["verb"], "choice": "left_click"}
            step["target"] = {**click["target"], "id": control.id, "physical": True, "selection_rule": "click again with the mouse"}
        start = time.perf_counter()
        try:
            step["result"] = act(step["verb"]["choice"], step["target"], state, controls, apps, step.get("text"))
        except Exception as error:
            step["error"] = f"{type(error).__name__}: {error}"
            outcome = "error"
            break
        step["timings_ms"]["execute"] = round((time.perf_counter() - start) * 1000)
        before, before_signature = state, signature(state, controls)
        before_settled = signature(state, controls, ambient=False)
        try:
            state, controls, apps = observe()
            step["screen_changed"] = signature(state, controls) != before_signature
            step["content_changed"] = signature(state, controls, ambient=False) != before_settled
        except Exception as error:
            step["summary"] = describe(index, step, before, None)
            step["observe_error"] = f"{type(error).__name__}: {error}"
            outcome = "error"
            break
        step["summary"] = describe(index, step, before, state)
        if progress:
            progress("acted", step)
        if _stuck(steps):
            step["reason"] = "the same action twice in a row changed nothing"
            outcome = "stuck"
            break
        if _idle(steps):
            step["reason"] = f"waited {WAIT_LIMIT} times in a row and the screen did not change"
            outcome = "stuck"
            break
        if _repeating(steps):
            step["reason"] = f"the same action {REPEAT_LIMIT} times in a row did not bring the goal closer"
            outcome = "stuck"
            break
    record["outcome"] = outcome
    record["actions"] = [s["summary"] for s in steps if "summary" in s]
    record["step_count"] = len(record["actions"])
    record["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    return outcome


def _stuck(steps: list[dict[str, Any]]) -> bool:
    if len(steps) < 2:
        return False
    a, b = steps[-2], steps[-1]
    same = (a["verb"]["choice"], a.get("target", {}).get("id"), a.get("text")) == (b["verb"]["choice"], b.get("target", {}).get("id"), b.get("text"))
    return same and a.get("screen_changed") is False and b.get("screen_changed") is False and b["verb"]["choice"] != "wait"


def _repeat_of_silent_click(steps: list[dict[str, Any]]) -> bool:
    """The step about to run is the same left click as the previous step, which changed nothing on screen."""
    if len(steps) < 2:
        return False
    previous, current = steps[-2], steps[-1]
    return (current["verb"]["choice"] == previous["verb"]["choice"] == "left_click" and previous.get("screen_changed") is False
            and current.get("target", {}).get("id") == previous.get("target", {}).get("id"))


def _waiting_on_a_silent_click(steps: list[dict[str, Any]], controls: list[Control]) -> bool:
    """The step about to run waits again, and the waits since the last click brought nothing new: the click
    probably never registered. Only a plain button or link (no checked/expanded/selected state, so a second click
    cannot flip anything back; no risky word such as Send) that is still on screen qualifies."""
    if len(steps) < 3 or steps[-1]["verb"]["choice"] != "wait":
        return False
    if steps[-2]["verb"]["choice"] != "wait" or steps[-2].get("content_changed") is not False:
        return False  # the latest wait must have brought nothing new; earlier ones may show the page settling
    i = len(steps) - 2
    while i >= 0 and steps[i]["verb"]["choice"] == "wait":
        i -= 1
    if i < 0:
        return False
    click = steps[i]
    if click["verb"]["choice"] != "left_click" or click.get("target", {}).get("physical"):
        return False
    control = _still_there(click.get("target", {}), controls)
    return bool(control and control.role in {"Button", "Hyperlink"} and not control.state and not RISKY_CONTROL.search(control.name))


def _still_there(target: dict[str, Any], controls: list[Control]) -> Control | None:
    """The one control on the current screen with the earlier target's role and name (ids are positional per capture)."""
    described = re.match(r'(\w+) "([^"]*)"', target.get("description", ""))
    if not described:
        return None
    same = [c for c in controls if c.role == described.group(1) and c.name == described.group(2)]
    return same[0] if len(same) == 1 else None


def _idle(steps: list[dict[str, Any]]) -> bool:
    """Waiting while nothing loads: a dialog the goal needs answered (a date picker) is left open until the step limit."""
    recent = steps[-WAIT_LIMIT:]
    return len(recent) == WAIT_LIMIT and all(s["verb"]["choice"] == "wait" and s.get("content_changed") is False for s in recent)


def _repeating(steps: list[dict[str, Any]]) -> bool:
    """An action repeated REPEAT_LIMIT times (scrolling a long page, pressing Next) while the done estimate stays flat."""
    if len(steps) < REPEAT_LIMIT:
        return False
    recent = steps[-REPEAT_LIMIT:]
    keys = {(s["verb"]["choice"], s.get("target", {}).get("id"), s.get("text")) for s in recent}
    done = [float(s.get("done_p") or 0) for s in recent]
    return len(keys) == 1 and max(done) < done[0] + 0.1
