"""Run the real Jev goal loop against small, deterministic synthetic UIs.

This measures full decision chains and the stopping decision, but it cannot
measure Windows UIA capture, mouse execution, or real application success.

    python -m voice_control.goal_sim --runs 3
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from . import core, goal
from .windows import ROLES

CASES = Path(__file__).with_name("goal_sim_cases.json")
ROOT = Path(__file__).resolve().parents[1]


def run_case(key: str, case: dict[str, Any], prompts: dict[str, Any] | None = None) -> dict[str, Any]:
    current = case["start"]
    states = case["states"]
    apps = case.get("apps", [])
    record: dict[str, Any] = {}

    def observe():
        scene = states[current]
        active = scene["active"]
        windows = scene.get("windows", [active])
        if any(item[2] not in ROLES for item in scene["controls"]):
            raise ValueError(f"{current} includes a role the UIA collector treats as read-only text")
        controls = [core.Control(item[0], item[1], item[2], (20, 60 + i * 28, 300, 84 + i * 28),
                                 active["title"], state=item[3] if len(item) > 3 else "",
                                 value=item[4] if len(item) > 4 else "")
                    for i, item in enumerate(scene["controls"])]
        state = {"activeWindow": active, "openWindows": windows, "texts": scene.get("texts", []),
                 "controlCount": len(controls), "controls": []}
        return state, controls, apps

    def act(verb, target, _state, _controls, _apps, typed_text):
        nonlocal current
        transition = f"{verb}:{target['id']}"
        rule = states[current]["transitions"].get(transition)
        if rule is None:
            raise RuntimeError(f"No simulated transition from {current} for {transition}")
        if isinstance(rule, dict):
            if typed_text != rule.get("text"):
                raise RuntimeError(f"Selected text did not match the requested literal for {transition}")
            destination = rule["next"]
        else:
            destination = rule
        current = destination
        return f"simulated {transition}"

    outcome = goal.run_goal(key, case["goal"], observe, act, record, max_steps=8, prompts=prompts)
    return {"id": case["id"], "passed": outcome == "done" and current == case["success"],
            "outcome": outcome, "final_state": current, "steps": [s.get("summary") for s in record["steps"]],
            "done_probabilities": [s.get("done_p") for s in record["steps"]],
            "final_verb": record["steps"][-1].get("verb", {}).get("choice"),
            "final_target": record["steps"][-1].get("target", {}).get("id"),
            "reason": next((s.get("reason") or s.get("error") for s in reversed(record["steps"])
                            if s.get("reason") or s.get("error")), None)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--prompts", type=Path, help="JSON object overriding keys of GOAL_PROMPTS")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    key = core.read_key(ROOT / ".env.openrouter")
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    prompts = copy.deepcopy(goal.GOAL_PROMPTS)
    if args.prompts:
        prompts.update(json.loads(args.prompts.read_text(encoding="utf-8")))
    passed = total = 0
    for case in cases:
        results = [run_case(key, case, prompts) for _ in range(args.runs)]
        count = sum(r["passed"] for r in results)
        passed += count
        total += len(results)
        print(f"{case['id']}: {count}/{len(results)} completed simulated goals")
        for result in results:
            if not result["passed"]:
                print(f"  {result['outcome']} at {result['final_state']}: {result['reason']}; "
                      f"done={result['done_probabilities']}; "
                      f"next={result['final_verb']}:{result['final_target']}")
                print("  " + " | ".join(s or "(no action)" for s in result["steps"]))
    print(f"{passed}/{total} simulated chains; no desktop actions or real-application task-success score")


if __name__ == "__main__":
    main()
