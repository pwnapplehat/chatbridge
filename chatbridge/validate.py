"""Structural validator for written Claude session logs.

Usage: python -m chatbridge.validate <claude-projects-dir>
Checks per log: valid JSON, unbroken parentUuid chain, unique uuids, every tool_use answered by tool_results in the very next
entry (and no orphan results), no empty text blocks, non-decreasing timestamps, first message is from the user.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .model import JsonObj, as_list, as_obj, as_str


def validate(log: Path) -> list[str]:
    problems: list[str] = []
    entries: list[JsonObj] = []
    with log.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            try:
                entries.append(as_obj(json.loads(line)))
            except json.JSONDecodeError as exc:
                problems.append(f"line {number}: invalid JSON ({exc})")
    convo = [e for e in entries if as_str(e.get("type")) in ("user", "assistant")]
    if not convo:
        return ["no conversation entries"]
    if as_str(convo[0].get("type")) != "user":
        problems.append("first message is not from the user")
    seen: set[str] = set()
    parent: object = None
    last_ts = ""
    for index, entry in enumerate(convo):
        uid = as_str(entry.get("uuid"))
        if uid in seen:
            problems.append(f"duplicate uuid {uid}")
        seen.add(uid)
        if entry.get("parentUuid") != parent:
            problems.append(f"entry {index}: broken parent chain")
        parent = uid
        stamp = as_str(entry.get("timestamp"))
        if stamp < last_ts:
            problems.append(f"entry {index}: timestamp goes backwards")
        last_ts = max(last_ts, stamp)
        blocks = [as_obj(b) for b in as_list(as_obj(entry.get("message")).get("content"))]
        if not blocks:
            problems.append(f"entry {index}: empty content")
        for block in blocks:
            if as_str(block.get("type")) == "text" and not as_str(block.get("text")).strip():
                problems.append(f"entry {index}: empty text block")
        uses = [as_str(b.get("id")) for b in blocks if as_str(b.get("type")) == "tool_use"]
        results = [as_str(b.get("tool_use_id")) for b in blocks if as_str(b.get("type")) == "tool_result"]
        if uses:
            following = convo[index + 1] if index + 1 < len(convo) else None
            answered = (
                [as_str(as_obj(b).get("tool_use_id")) for b in as_list(as_obj(as_obj(following).get("message")).get("content"))]
                if following is not None
                else []
            )
            if answered != uses:
                problems.append(f"entry {index}: tool_use ids not answered by the next entry")
        if results and not (index > 0 and as_str(convo[index - 1].get("type")) == "assistant"):
            problems.append(f"entry {index}: tool_result without preceding assistant tool_use")
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[0])
    logs = sorted(root.glob("*/*.jsonl"))
    bad = 0
    for log in logs:
        problems = validate(log)
        if problems:
            bad += 1
            print(f"{log.name}: {len(problems)} problem(s); first: {problems[0]}")
    print(f"validated {len(logs)} logs, {bad} with problems")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
