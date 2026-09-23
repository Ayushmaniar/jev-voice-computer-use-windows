"""Print Jev's per-step decisions for goal-eval runs: python -m voice_control.goal_inspect [task ...] [--batch B]."""

import argparse
import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[1] / "logs" / "goal-eval.jsonl"


def top(probabilities: dict, n: int = 3) -> str:
    return ", ".join(f"{k}={v:.2f}" for k, v in sorted(probabilities.items(), key=lambda kv: -kv[1])[:n] if v)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tasks", nargs="*")
    parser.add_argument("--batch")
    parser.add_argument("--controls", type=int, default=0, help="also print the first N exposed controls per step")
    args = parser.parse_args()
    rows = [json.loads(line) for line in RESULTS.read_text(encoding="utf-8").splitlines() if line.strip()]
    batch = args.batch or rows[-1]["batch"]
    for row in rows:
        if row["batch"] != batch or (args.tasks and row["task"] not in args.tasks):
            continue
        print(f"== {row['task']} ({'PASS' if row['passed'] else 'FAIL'}, {row['outcome']}): {row['goal']}")
        for step in row.get("steps", []):
            print(f"  step {step['index']} in {step['window']['title'][:60]!r}: done_p={step.get('done_p')}")
            if "verb" in step:
                print(f"    verb: {top(step['verb'].get('probabilities', {}))}")
            for call in step.get("jev_calls", []):
                answer = call.get("response", {}).get("answers", {})
                criteria = call["request"]["questions"]
                for name, a in answer.items():
                    if name in {"next_action", "goal_done"}:
                        continue
                    probs = a.get("probabilities", {})
                    best = sorted(probs.items(), key=lambda kv: -kv[1])[:3]
                    print(f"    {name}: " + "; ".join(f"{p:.2f} {criteria[name]['criteria'].get(k, k)[:70]}" for k, p in best))
            if args.controls:
                controls = step["inputs"]["controls"][:args.controls]
                print("    controls: " + " | ".join(f"{c['role']} {c['name'][:30]!r}" for c in controls))
            for tried in step.get("tried", []):
                print(f"    tried {tried['verb']}: {tried.get('reason')}")
            if step.get("reason") or step.get("error"):
                print(f"    -> {step.get('reason') or step.get('error')}")


if __name__ == "__main__":
    main()
