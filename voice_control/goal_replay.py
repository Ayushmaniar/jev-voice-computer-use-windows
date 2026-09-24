"""Re-plan a saved goal-eval step with Jev, without moving the desktop.

This checks a decision on a frozen screen. It does not run an action or score
task completion. Repeat a step to observe Jev's variation before editing a
prompt or an action-selection rule.

    python -m voice_control.goal_replay vlc_subs_en --step 2 --runs 3
    python -m voice_control.goal_replay vlc_subs_fr --step 2 --prompts candidate.json
    python -m voice_control.goal_replay 8d9b9713 --log logs/voice-actions.jsonl --step 3

In a voice session log (logs/voice-actions.jsonl) the task is the start of an utterance ID.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from . import core, goal

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "logs" / "goal-eval.jsonl"


def saved_step(path: Path, task: str, number: int, batch: str | None = None) -> dict[str, Any]:
    if number < 1:
        raise ValueError("step must be at least 1")
    rows = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    matching = [row for row in rows if (row.get("task") == task or (row.get("utterance_id") or "").startswith(task)) and (batch is None or row.get("batch") == batch)
                and len(row.get("steps", [])) >= number and row["steps"][number - 1].get("inputs")
                and row.get("initial_window")]
    if not matching:
        raise ValueError(f"no saved step {number} for {task!r}")
    return matching[-1]


def replan(key: str, record: dict[str, Any], number: int, prompts: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    saved = record["steps"][number - 1]
    _, state, controls, apps = core.restore_inputs(saved["inputs"])
    initial = {"activeWindow": record["initial_window"]}
    step: dict[str, Any] = {}
    decision = goal.plan_step(key, record.get("goal") or record["transcript"], record["steps"][:number - 1], initial,
                              state, controls, apps, step, prompts)
    return decision, step


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task", help="task ID in goal-eval.jsonl, or the start of an utterance ID in a voice log")
    parser.add_argument("--step", type=int, default=2, help="one-based saved step to re-plan")
    parser.add_argument("--runs", type=int, default=3, help="number of independent Jev decisions")
    parser.add_argument("--batch", help="use a specific evaluation batch instead of the latest usable one")
    parser.add_argument("--log", type=Path, default=RESULTS)
    parser.add_argument("--prompts", type=Path, help="JSON object overriding keys of current GOAL_PROMPTS")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    record = saved_step(args.log, args.task, args.step, args.batch)
    prompts = copy.deepcopy(goal.GOAL_PROMPTS)
    if args.prompts:
        prompts.update(json.loads(args.prompts.read_text(encoding="utf-8")))
    key = core.read_key(ROOT / ".env.openrouter")
    source = record["steps"][args.step - 1]
    print(f"{args.task} step {args.step} from batch {record.get('batch')} in {source['window']['title']!r}")
    print(f"Original run: {record.get('outcome')}; task passed: {record.get('passed')}. Replays do not execute or score the goal.")
    for index in range(1, args.runs + 1):
        decision, step = replan(key, record, args.step, prompts)
        verb = (step.get("verb") or {}).get("choice")
        target = (step.get("target") or {}).get("description", "")
        typed = f'  text="{step["text"]}" p={step.get("text_p"):.2f}' if step.get("text") else ""
        rule = f' ({step["text_rule"]})' if step.get("text_rule") else ""
        reason = f"  [{step['reason']}]" if step.get("reason") else ""
        print(f"  {index}. {decision}: {verb or '-'} {target[:70]}{typed}{rule}{reason}  done={step.get('done_p')}  "
              f"Jev={step.get('timings_ms', {}).get('jev')} ms")


if __name__ == "__main__":
    main()
