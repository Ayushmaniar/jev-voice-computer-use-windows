"""A/B-test Jev prompts against logged commands with the captured state held fixed.

Each plan/error record in logs/voice-actions.jsonl stores the exact planner
inputs, the prompts in force, and every Jev request/response. This script
first proves that rebuilding the requests from the stored inputs reproduces
the logged requests byte for byte, then re-plans each command with the logged
prompts (A) and the current PROMPTS in core.py (B). Only prompts differ.

    python -m voice_control.replay --write-labels     # create/extend labels file
    python -m voice_control.replay --runs 3           # A/B test (makes Jev calls)
    python -m voice_control.replay --check-only       # fidelity check, no calls
"""

from __future__ import annotations

import argparse
import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import patch

from . import core

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "logs" / "voice-actions.jsonl"
DEFAULT_LABELS = ROOT / "logs" / "replay-labels.json"


def load_commands(log: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if "inputs" in r and "prompts" in r and r.get("event") in {"plan", "error"}]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class _Logged:
    """Stand-in HTTP response that returns a logged Jev response."""

    def __init__(self, call: dict[str, Any]):
        self.status_code = call.get("http_status", 200)
        self._body = call.get("response")

    def raise_for_status(self) -> None:
        if self.status_code >= 400 or self._body is None:
            raise RuntimeError(f"logged call failed (HTTP {self.status_code})")

    def json(self) -> dict[str, Any]:
        return self._body


def fidelity_check(record: dict[str, Any]) -> tuple[bool, str]:
    """Re-run the planner offline, feeding back logged responses; requests must match exactly."""
    logged = record.get("jev_calls", [])
    if not logged:
        return False, "no Jev calls logged"
    sent: list[dict[str, Any]] = []
    responses = iter(logged)

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        return _Logged(next(responses))

    utterance, state, controls, apps = core.restore_inputs(record["inputs"])
    # A stand-in key of the logged provider, so the rebuilt request names the same model.
    key = "sk-or-offline" if logged[0].get("request", {}).get("model") == core.MODEL else "offline"
    with patch.object(core.requests, "post", fake_post):
        try:
            core.plan_command(key, utterance, state, controls, apps, {}, record["prompts"])
        except (RuntimeError, StopIteration):
            pass
    if len(sent) != len(logged):
        return False, f"{len(sent)} rebuilt vs {len(logged)} logged requests"
    for i, (rebuilt, call) in enumerate(zip(sent, logged)):
        if canonical(rebuilt) != canonical(call["request"]):
            return False, f"request {i} ({call['question']}) differs"
    return True, f"{len(sent)} request(s) identical"


def replan(key: str, record: dict[str, Any], prompts: dict[str, Any]) -> dict[str, Any]:
    utterance, state, controls, apps = core.restore_inputs(record["inputs"])
    out: dict[str, Any] = {}
    try:
        core.plan_command(key, utterance, state, controls, apps, out, prompts)
        acted = True
        reason = ""
    except RuntimeError as error:
        acted, reason = False, str(error)
    except Exception as error:  # network/HTTP: not a planning verdict
        return {"error": f"{type(error).__name__}: {error}"}
    verb = out.get("verb", {})
    target = out.get("target", {})
    return {"acted": acted, "verb": verb.get("choice"), "verb_p": verb.get("probabilities", {}).get(verb.get("choice")),
            "target": target.get("id"), "target_p": target.get("probability"), "reason": reason}


def summarize(results: list[dict[str, Any]]) -> str:
    errors = sum(1 for r in results if "error" in r)
    outcomes = Counter(f'{r["verb"]}→{r["target"]}' if r["acted"] else f'stop({r["verb"]}→{r["target"]})'
                       for r in results if "error" not in r)
    tp = [r["target_p"] for r in results if "error" not in r and r["target_p"] is not None]
    text = ", ".join(f"{k} ×{v}" for k, v in outcomes.most_common())
    if tp:
        text += f" | target p {min(tp):.2f}–{max(tp):.2f}"
    return text + (f" | {errors} call error(s)" if errors else "")


def passed(result: dict[str, Any], label: dict[str, Any]) -> bool:
    if "error" in result:
        return False
    if label["target"] == "none":
        return not result["acted"]
    return result["acted"] and result["verb"] == label["verb"] and result["target"] == label["target"]


def write_labels(commands: list[dict[str, Any]], path: Path) -> None:
    labels = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    added = 0
    for r in commands:
        if r["utterance_id"] in labels:
            continue
        planned = r["outcome"] == "planned"
        labels[r["utterance_id"]] = {
            "transcript": r["transcript"],
            "window": r["inputs"]["activeWindow"]["title"],
            "verb": r.get("verb", {}).get("choice"),
            "target": r.get("target", {}).get("id") if planned else "none",
            "logged_target_description": r.get("target", {}).get("description"),
            "logged_outcome": r["outcome"] if planned else r.get("error"),
            "reviewed": False,
        }
        added += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(labels, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{added} label(s) added to {path}. Each is prefilled with what originally happened; correct the verb and "
          f"target (a control id from the record, or \"none\" when nothing should happen) and set reviewed to true.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--last", type=int, help="only the N most recent commands")
    parser.add_argument("--ids", nargs="*", help="only these utterance_ids")
    parser.add_argument("--runs", type=int, default=3, help="repeats per variant; Jev is not fully deterministic")
    parser.add_argument("--candidate", type=Path, help="JSON file overriding keys of the current PROMPTS for variant B")
    parser.add_argument("--check-only", action="store_true", help="only verify state fidelity; no Jev calls")
    parser.add_argument("--write-labels", action="store_true", help="add prefilled labels for unlabeled commands and exit")
    args = parser.parse_args()

    commands = load_commands(args.log)
    if args.ids:
        commands = [r for r in commands if r["utterance_id"] in set(args.ids)]
    if args.last:
        commands = commands[-args.last:]
    if not commands:
        raise SystemExit("No replayable commands. Records need the 'inputs' field, which the app logs from this version on.")
    if args.write_labels:
        write_labels(commands, args.labels)
        return

    print(f"{len(commands)} replayable command(s). Fidelity check (offline):")
    faithful = []
    for r in commands:
        ok, detail = fidelity_check(r)
        print(f"  {'OK  ' if ok else 'FAIL'} {r['transcript']!r}: {detail}")
        if ok:
            faithful.append(r)
    if args.check_only:
        return
    if len(faithful) < len(commands):
        print(f"Skipping {len(commands) - len(faithful)} command(s) whose state cannot be reproduced exactly.")

    candidate = copy.deepcopy(core.PROMPTS)
    if args.candidate:
        candidate.update(json.loads(args.candidate.read_text(encoding="utf-8")))
    labels = json.loads(args.labels.read_text(encoding="utf-8")) if args.labels.exists() else {}
    key = core.read_key(ROOT / ".env.openrouter")
    print(f"\nA = prompts logged with each command, B = {'current PROMPTS + ' + str(args.candidate) if args.candidate else 'current PROMPTS in core.py'}")
    print(f"About {len(faithful) * args.runs * 2 * 2} Jev calls.\n")

    score = {"A": 0, "B": 0}
    scored = 0
    for r in faithful:
        results = {name: [replan(key, r, prompts) for _ in range(args.runs)]
                   for name, prompts in (("A", r["prompts"]), ("B", candidate))}
        label = labels.get(r["utterance_id"])
        head = f"{r['transcript']!r}  [{r['inputs']['activeWindow']['title'][:40]}]"
        if label and label.get("reviewed"):
            scored += 1
            head += f"  expect {label['verb']}→{label['target']}"
            for name in score:
                score[name] += sum(passed(x, label) for x in results[name])
        print(head)
        for name in ("A", "B"):
            print(f"   {name}: {summarize(results[name])}")
    if scored:
        total = scored * args.runs
        print(f"\nReviewed labels: A passed {score['A']}/{total}, B passed {score['B']}/{total}")
    else:
        print("\nNo reviewed labels yet; run with --write-labels, review the file, then re-run for pass rates.")


if __name__ == "__main__":
    main()
