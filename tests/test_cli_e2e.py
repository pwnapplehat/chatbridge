"""End-to-end tests of the command line, run as a real subprocess against the synthetic environment."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from chatbridge import __version__
from chatbridge.writer import cwd_slug, local_session_name, session_uuid
from tests.fixtures import CHAT_BACKUP_ONLY, CHAT_MAIN, CHAT_TRANSCRIPT, World, age

ROOT = Path(__file__).resolve().parent.parent


def cli(world: World, *args: str) -> subprocess.CompletedProcess[str]:
    base = [
        sys.executable, "-m", "chatbridge", args[0],
        "--cursor-user-dir", str(world.live_user),
        "--profile", f"backup={world.backup_user}",
        "--transcripts", str(world.paths.transcripts_dir),
        "--claude-dir", str(world.paths.claude_dir),
        "--desktop-dir", str(world.paths.desktop_dir),
        "--data-dir", str(world.paths.data_dir),
        "--config-dir", str(world.paths.config_dir),
        *args[1:],
    ]  # fmt: skip
    env = {**os.environ, "PYTHONPATH": str(ROOT), "HOME": str(world.root / "home")}
    return subprocess.run(base, capture_output=True, text=True, check=False, env=env, cwd=ROOT)


def logs(world: World) -> list[Path]:
    return list((world.paths.claude_dir / "projects").glob("*/*.jsonl"))


def test_inventory_lists_chats_and_flags(world: World) -> None:
    result = cli(world, "inventory")
    assert result.returncode == 0, result.stderr
    assert "5 chats found, 4 with content, 1 empty drafts" in result.stdout
    assert CHAT_MAIN[:8] in result.stdout and "Old windows chat" in result.stdout


def test_import_needs_explicit_selection(world: World) -> None:
    result = cli(world, "import", "--apply")
    assert result.returncode == 2 and "nothing selected" in result.stderr
    assert logs(world) == []


def test_unknown_chat_is_an_error(world: World) -> None:
    result = cli(world, "import", "--chat", "deadbeef", "--apply")
    assert result.returncode == 2 and "no chat matches" in result.stderr


def test_full_cycle_dry_run_import_verify_reimport_undo(world: World) -> None:
    dry = cli(world, "import", "--chat", CHAT_MAIN[:8])
    assert dry.returncode == 0 and "dry run" in dry.stdout and logs(world) == []

    done = cli(world, "import", "--chat", CHAT_MAIN[:8], "--apply")
    assert done.returncode == 0 and "[written ]" in done.stdout and "verified=True" in done.stdout, done.stdout + done.stderr
    assert [p.stem for p in logs(world)] == [session_uuid(CHAT_MAIN)]
    assert (world.paths.desktop_dir / "org" / "acct" / f"{local_session_name(CHAT_MAIN)}.json").exists()

    again = cli(world, "import", "--chat", CHAT_MAIN[:8], "--apply")
    assert "[exists  ]" in again.stdout and len(logs(world)) == 1

    verify = cli(world, "verify", "--chat", CHAT_MAIN[:8])
    assert verify.returncode == 0 and "[OK" in verify.stdout

    preview = cli(world, "undo", "--chat", CHAT_MAIN[:8])
    assert "dry run" in preview.stdout and len(logs(world)) == 1
    undo = cli(world, "undo", "--chat", CHAT_MAIN[:8], "--apply")
    assert undo.returncode == 0 and "moved 2 file(s)" in undo.stdout
    assert logs(world) == [] and list(world.paths.removed_dir.rglob("*.jsonl"))


def test_all_imports_every_visible_chat_but_not_subagents_or_drafts(world: World) -> None:
    result = cli(world, "import", "--all", "--apply")
    assert result.returncode == 0, result.stderr
    assert {p.stem for p in logs(world)} == {session_uuid(c) for c in (CHAT_MAIN, CHAT_BACKUP_ONLY, CHAT_TRANSCRIPT)}


def test_cwd_flag_overrides_project_folder(world: World, tmp_path: Path) -> None:
    target = tmp_path / "target-folder"
    target.mkdir()
    result = cli(world, "import", "--chat", CHAT_BACKUP_ONLY[:8], "--cwd", str(target), "--apply")
    assert result.returncode == 0
    assert (world.paths.claude_dir / "projects" / cwd_slug(str(target))).is_dir()


def test_missing_claude_sessions_directory_is_a_clear_error(world: World) -> None:
    for meta in world.paths.desktop_dir.glob("*/*/local_*.json"):
        meta.unlink()
    result = cli(world, "import", "--chat", CHAT_MAIN[:8], "--apply")
    assert result.returncode == 2 and "no existing local_*.json" in result.stderr


# --------------------------------------------------------------------------- sync commands
def test_list_shows_states(world: World) -> None:
    result = cli(world, "list")
    assert result.returncode == 0, result.stderr
    assert "Cursor only" in result.stdout and "Fix the parser" in result.stdout


def test_sync_requires_explicit_selection(world: World) -> None:
    result = cli(world, "sync", "--apply")
    assert result.returncode == 2 and "nothing selected" in result.stderr


def test_sync_dry_run_then_apply_then_linked_noop(world: World) -> None:
    dry = cli(world, "sync", "--chat", CHAT_MAIN[:8])
    assert dry.returncode == 0 and "dry run" in dry.stdout and logs(world) == []
    done = cli(world, "sync", "--chat", CHAT_MAIN[:8], "--apply")
    assert done.returncode == 0 and "[synced  ]" in done.stdout, done.stdout + done.stderr
    assert [p.stem for p in logs(world)] == [session_uuid(CHAT_MAIN)]
    again = cli(world, "sync", "--linked", "--apply")
    assert again.returncode == 0 and "[noop    ]" in again.stdout
    listing = cli(world, "list")
    assert "In sync" in listing.stdout


def test_cursor_undo_reverts_a_claude_to_cursor_sync(world: World) -> None:
    from tests.fixtures import CLAUDE_CLI, CLAUDE_KEY, ClaudeLog, write_claude_session

    log = ClaudeLog(CLAUDE_CLI, str(world.project_dir))
    log.user("hello from claude")
    log.assistant("hi")
    write_claude_session(world, CLAUDE_KEY, CLAUDE_CLI, "From Claude", log, str(world.project_dir))
    done = cli(world, "sync", "--claude", CLAUDE_KEY[:14], "--apply")
    assert done.returncode == 0 and "undo journal:" in done.stdout, done.stdout + done.stderr
    journal = next(line.split("undo journal:")[1].strip() for line in done.stdout.splitlines() if "undo journal:" in line)
    undone = cli(world, "cursor-undo", "--journal", journal)
    assert undone.returncode == 0 and "removed 2 message record(s)" in undone.stdout, undone.stdout + undone.stderr
    assert "Claude only" in cli(world, "list").stdout


def test_watch_once_syncs_changes_in_linked_conversations(world: World) -> None:
    from tests.fixtures import T0, cursor_follow_up

    cli(world, "sync", "--chat", CHAT_MAIN[:8], "--apply")
    assert "nothing" not in cli(world, "watch", "--once").stdout
    cursor_follow_up(world.live_user / "globalStorage" / "state.vscdb", CHAT_MAIN, [(1, "newer cursor question")], T0 + 99_000_000)
    early = cli(world, "watch", "--once")
    assert "mid-turn" in early.stdout, "a Claude log written moments ago must not be appended to"
    age(logs(world)[0])
    result = cli(world, "watch", "--once")
    assert result.returncode == 0 and "synced" in result.stdout, result.stdout + result.stderr


def test_doctor_reports_environment(world: World) -> None:
    result = cli(world, "doctor")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"ChatBridge {__version__}" in result.stdout and "Cursor profile 'live' [read/write]" in result.stdout
    assert "Claude desktop app sessions folder" in result.stdout and "Cursor is closed" in result.stdout
