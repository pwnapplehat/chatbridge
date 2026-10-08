"""Build a self-contained demo environment (fake Cursor + Claude data) that exercises every sync state.

Used for README screenshots and as a safe sandbox:  python scripts/make_demo.py /tmp/chatbridge-demo
then:  chatbridge-gui --demo /tmp/chatbridge-demo   (or see scripts/screenshot.py --demo)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from chatbridge.config import AppPaths, Settings
from chatbridge.cursor_source import CursorProfile
from chatbridge.sync import SyncService
from tests.fixtures import ClaudeLog, World, _new_db, add_chat, bubble, claude_follow_up, cursor_follow_up, write_claude_session

NOW = int(time.time() * 1000)
HOUR = 3_600_000

CURSOR_CHATS = [
    (
        "Fix flaky checkout test",
        "shop-web",
        [
            "The checkout e2e test fails randomly on CI. Can you find why?",
            "It waits for a network idle that never settles when the analytics script retries. I'll wait on the order confirmation element instead.",
            "Done: replaced the idle wait with an explicit locator wait and added a retry-free assertion.",
        ],
        26,
    ),
    (
        "Add dark mode toggle",
        "shop-web",
        [
            "Add a dark mode toggle to the settings page.",
            "I'll add a theme context, persist the choice in localStorage and respect prefers-color-scheme.",
            "Added ThemeProvider, the toggle component and tests.",
        ],
        50,
    ),
    (
        "Migrate users table to uuid",
        "billing-api",
        [
            "We need to move users.id from bigint to uuid without downtime.",
            "Plan: expand-contract in five steps with a dual write and a batched backfill.",
            "Migration written and reviewed; backfill runs in batches of 5000.",
        ],
        74,
    ),
    (
        "Profile the slow dashboard query",
        "analytics",
        [
            "The dashboard query takes 9s. Why?",
            "A sequential scan on events because the composite index is in the wrong column order.",
            "Reordered the index and the query now takes 140ms.",
        ],
        120,
    ),
    (
        "Write release notes for v2.3",
        "shop-web",
        ["Draft release notes for v2.3 from the merged PRs.", "Here is a draft grouped into Features, Fixes and Internal."],
        200,
    ),
]
CLAUDE_SESSIONS = [
    (
        "Refactor the invoice PDF generator",
        "billing-api",
        "Please split the PDF generator into layout and rendering.",
        "I'll extract a Layout class and keep the renderer pure.",
        30,
    ),
    (
        "Explain the retry policy",
        "analytics",
        "Why do we retry 5 times with jitter?",
        "Exponential backoff with jitter avoids thundering herds when the upstream recovers.",
        90,
    ),
]


def build_demo(root: Path) -> tuple[AppPaths, Settings]:
    """Create the demo files under root and return paths/settings that point at them."""
    root.mkdir(parents=True, exist_ok=True)
    folders = {name: root / "projects" / name for name in ("shop-web", "billing-api", "analytics")}
    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)
    live_user = root / "cursor" / "User"
    (live_user / "workspaceStorage").mkdir(parents=True, exist_ok=True)
    for index, folder in enumerate(folders.values()):
        ws = live_user / "workspaceStorage" / f"ws{index}"
        ws.mkdir(exist_ok=True)
        (ws / "workspace.json").write_text(json.dumps({"folder": f"file://{folder}"}), encoding="utf-8")
    conn = _new_db(live_user / "globalStorage" / "state.vscdb")
    for index, (title, project, messages, hours_ago) in enumerate(CURSOR_CHATS):
        chat_id = f"d0d0d0d0-0000-4000-8000-{index:012d}"
        bubbles = [bubble(1 if n % 2 == 0 else 2, n, text=text) for n, text in enumerate(messages)]
        ws = f"ws{list(folders).index(project)}"
        add_chat(conn, chat_id, title, bubbles, list(range(len(bubbles))), ws, folders[project])
        base = NOW - hours_ago * HOUR
        data = json.loads(conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{chat_id}",)).fetchone()[0])
        data["createdAt"], data["lastUpdatedAt"] = base, base + 600_000
        conn.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(data), f"composerData:{chat_id}"))
        conn.execute("UPDATE composerHeaders SET createdAt = ?, lastUpdatedAt = ? WHERE composerId = ?", (base, base + 600_000, chat_id))
        for n, item in enumerate(bubbles):
            stamp = base + n * 40_000
            item["createdAt"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(stamp / 1000))
            conn.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(item), f"bubbleId:{chat_id}:{item['bubbleId']}"))
    conn.commit()
    conn.close()

    desktop = root / "claude-config" / "claude-code-sessions" / "org" / "acct"
    desktop.mkdir(parents=True, exist_ok=True)
    (desktop / "local_seed.json").write_text(
        json.dumps({"sessionId": "local_seed", "cliSessionId": "seed", "title": "seed"}), encoding="utf-8"
    )
    paths = AppPaths(
        claude_dir=root / "claude", desktop_dir=root / "claude-config" / "claude-code-sessions", data_dir=root / "data", config_dir=root / "config",
        transcripts_dir=None, live_profile=CursorProfile(live_user, "work-laptop", writable=True),
    )  # fmt: skip
    (root / "claude" / "projects").mkdir(parents=True, exist_ok=True)
    world = World(root, folders["shop-web"], live_user, live_user, paths)
    for index, (title, project, question, answer, hours_ago) in enumerate(CLAUDE_SESSIONS):
        log = ClaudeLog(f"cccccccc-0000-4000-8000-{index:012d}", str(folders[project]), ts=NOW - hours_ago * HOUR)
        log.user(question)
        log.assistant(
            answer,
            thinking="Consider the existing structure first.",
            tool=("Read", {"file_path": f"{folders[project]}/src/main.py"}, "# source", False),
        )
        log.assistant("Let me know if you want me to continue.")
        write_claude_session(
            world,
            f"local_demo{index:02d}-0000-4000-8000-000000000000",
            f"cccccccc-0000-4000-8000-{index:012d}",
            title,
            log,
            str(folders[project]),
        )
    settings = Settings()
    sync = SyncService(paths, settings)
    # Pair three Cursor chats with Claude, then create the interesting states.
    conversations, _ = sync.load_conversations()
    by_title = {c.title: c for c in conversations}
    for title in ("Fix flaky checkout test", "Add dark mode toggle", "Migrate users table to uuid", "Profile the slow dashboard query"):
        sync.sync(by_title[title], apply=True)
    _make_states(sync, world)
    return paths, settings


def _make_states(sync: SyncService, world: World) -> None:
    """In sync (checkout), Claude ahead (dark mode), Cursor ahead (migrate), both changed (checkout is left in sync)."""
    from tests.fixtures import age

    conversations, _ = sync.load_conversations()
    by_title = {c.title: c for c in conversations}
    for conv in conversations:
        if conv.claude and conv.cursor:
            age(conv.claude.log_path)
            sync.sync(conv, apply=True)  # re-record fingerprints after ageing the file times
    conversations, _ = sync.load_conversations()
    by_title = {c.title: c for c in conversations}
    dark = by_title["Add dark mode toggle"]
    assert dark.claude and dark.cursor
    claude_follow_up(
        world, dark.claude.cli_id, [("u", "Also add a keyboard shortcut for the toggle."), ("a", "Added Ctrl+Shift+L and a tooltip hint.")]
    )
    mig = by_title["Migrate users table to uuid"]
    assert mig.cursor and mig.claude
    cursor_follow_up(
        mig.cursor.ref.source_path,
        mig.cursor.ref.chat_id,
        [(1, "Can you also add a rollback script?"), (2, "Rollback script added with the reverse dual-write.")],
        NOW - 30 * 60_000,
    )
    both = by_title["Fix flaky checkout test"]
    assert both.claude and both.cursor
    claude_follow_up(world, both.claude.cli_id, [("u", "One more flaky spec in the cart page.")])
    cursor_follow_up(
        both.cursor.ref.source_path, both.cursor.ref.chat_id, [(1, "Please also run the full suite locally.")], NOW - 10 * 60_000
    )


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "chatbridge-demo")
    build_demo(target.resolve())
    print("demo environment written to", target.resolve())
