"""Probe Jev's next choice on frozen, synthetic UI states without desktop actions.

These cases test a prompt distinction in apps outside the live VLC fixtures.
They do not measure task completion or real UI behavior.

    python -m voice_control.goal_probe --runs 3
    python -m voice_control.goal_probe --prompts candidate.json --runs 3
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from . import core, goal

ROOT = Path(__file__).resolve().parents[1]
CASES = Path(__file__).with_name("goal_probe_cases.json")


def state_for(case: dict[str, Any]):
    active = {"hwnd": 10, "title": case["window"], "process": case["process"]}
    controls = [core.Control(identifier, name, "MenuItem", (20, 60 + i * 28, 300, 84 + i * 28),
                             f"Open menu > {case['window']}")
                for i, (identifier, name) in enumerate(case["controls"])]
    state = {"activeWindow": active, "openWindows": [active], "texts": [], "controlCount": len(controls),
             "controls": [{"id": c.id, "name": c.name, "role": c.role, "rect": c.rect, "path": c.path,
                           "enabled": c.enabled} for c in controls]}
    return state, controls


def probe(key: str, case: dict[str, Any], prompts: dict[str, Any] | None = None) -> dict[str, Any]:
    state, controls = state_for(case)
    step: dict[str, Any] = {}
    decision = goal.plan_step(key, case["goal"], [], state, state, controls, [], step, prompts)
    verb = (step.get("verb") or {}).get("choice")
    target = (step.get("target") or {}).get("id")
    return {"decision": decision, "verb": verb, "target": target,
            "matched": decision == "act" and target == case["expected_target"] and verb in case["accepted_verbs"],
            "jev_ms": step.get("timings_ms", {}).get("jev")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--prompts", type=Path, help="JSON object overriding keys of GOAL_PROMPTS")
    parser.add_argument("--ablate-existing-preference", action="store_true",
                        help="remove the existing-choice preference sentence for an A/B check")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    prompts = copy.deepcopy(goal.GOAL_PROMPTS)
    if args.prompts:
        prompts.update(json.loads(args.prompts.read_text(encoding="utf-8")))
    if args.ablate_existing_preference:
        preference = ("To use, turn on, or switch to something the app may already offer "
                      "(a track, device, mode, or recent item), prefer the entry that lists existing choices over "
                      "one that adds, imports, or browses for a new file, unless the goal asks to add, import, or browse. ")
        if preference not in prompts["target"]:
            parser.error("existing-choice preference sentence is absent from the target prompt")
        prompts["target"] = prompts["target"].replace(preference, "")
    key = core.read_key(ROOT / ".env.openrouter")
    passed = total = 0
    for case in cases:
        results = [probe(key, case, prompts) for _ in range(args.runs)]
        matched = sum(result["matched"] for result in results)
        passed += matched
        total += len(results)
        print(f"{case['id']}: {matched}/{len(results)} expected next choices; "
              + ", ".join(f"{r['verb']}->{r['target']}" for r in results))
    print(f"{passed}/{total} expected next choices on synthetic screens; no desktop actions or task-success score")


if __name__ == "__main__":
    main()
