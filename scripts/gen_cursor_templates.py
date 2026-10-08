"""Regenerate chatbridge/data/cursor_templates.json from a real, complete Cursor chat.

Usage: python scripts/gen_cursor_templates.py <state.vscdb> <composer-id>
The chat should be a small agent chat containing user, reasoning, tool and text messages. Identifiers, secrets and
conversation content are stripped; only the *shape* (keys and neutral defaults) is kept so the writer can emit records
that look exactly like Cursor's own.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

BUBBLE_KEYS_TO_CLEAR = {
    "bubbleId",
    "createdAt",
    "text",
    "richText",
    "requestId",
    "conversationState",
    "checkpointId",
    "thinking",
    "toolFormerData",
}


def load(conn: sqlite3.Connection, key: str) -> dict[str, object]:
    row = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0])  # type: ignore[no-any-return]


def main(db: str, composer_id: str) -> None:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    composer = load(conn, f"composerData:{composer_id}")
    kinds: dict[str, dict[str, object]] = {}
    low, high = f"bubbleId:{composer_id}:", f"bubbleId:{composer_id};"
    for _key, raw in conn.execute("SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ?", (low, high)):
        bubble = json.loads(raw)
        kind = "user" if bubble["type"] == 1 else "tool" if bubble.get("toolFormerData") else "think" if bubble.get("thinking") else "text"
        kinds.setdefault(kind, bubble)
        if len(kinds) == 4:
            break
    base = {k: v for k, v in kinds["text"].items() if k not in BUBBLE_KEYS_TO_CLEAR | {"type", "unifiedMode"}}
    user_extra = {k: v for k, v in kinds["user"].items() if k not in base and k not in BUBBLE_KEYS_TO_CLEAR | {"type", "unifiedMode"}}
    composer_tpl = {
        k: v
        for k, v in composer.items()
        if k
        not in {
            "composerId", "name", "text", "richText", "fullConversationHeadersOnly", "conversationState", "originalFileStates",
            "newlyCreatedFiles", "todos", "subtitle", "filesChangedCount", "totalLinesAdded", "totalLinesRemoved",
            "latestChatGenerationUUID", "blobEncryptionKey", "speculativeSummarizationEncryptionKey", "createdAt",
            "lastUpdatedAt", "conversationCheckpointLastUpdatedAt", "trackedGitRepos", "contextUsagePercent",
            "contextTokensUsed", "promptTokenBreakdown", "modelConfig", "workspaceIdentifier",
        }
    }  # fmt: skip
    templates = {"bubble_base": base, "user_extra": user_extra, "composer": composer_tpl}
    out = Path(__file__).resolve().parent.parent / "chatbridge" / "data" / "cursor_templates.json"
    out.write_text(json.dumps(templates, indent=1, sort_keys=True), encoding="utf-8")
    print("wrote", out, {k: len(v) for k, v in templates.items()})


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
