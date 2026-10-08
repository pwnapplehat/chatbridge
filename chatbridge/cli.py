"""Command-line interface: inventory, import, verify, undo. Dry run unless --apply is given."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path

from .autosync import AutoSyncEvent
from .catalog import ChatFilter, ChatRow, apply_filter
from .config import AppPaths, Settings, load_settings
from .cursor_source import CursorProfile
from .discovery import parse_profile_arg
from .doctor import run_doctor
from .model import ConversionReport, ImporterError
from .service import ImportRequest, ImportService
from .sync import Conversation, SyncReport, SyncService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chatbridge", description="Import Cursor chats into Claude (native sessions).")
    parser.add_argument("command", choices=["inventory", "list", "import", "verify", "undo", "sync", "watch", "cursor-undo", "doctor"])
    parser.add_argument("--profile", action="append", default=[], help="extra Cursor user dir, '<label>=<path-to-User>' (repeatable)")
    parser.add_argument("--no-live", action="store_true", help="do not read the live ~/.config/Cursor profile")
    parser.add_argument("--cursor-user-dir", type=Path, default=None, help="live Cursor user-data folder (default ~/.config/Cursor/User)")
    parser.add_argument("--config-dir", type=Path, default=None, help="where settings.json lives (default ~/.config/chatbridge)")
    parser.add_argument("--transcripts", type=Path, default=None, help="agent-transcripts root (default ~/.cursor/projects)")
    parser.add_argument("--no-transcripts", action="store_true")
    parser.add_argument("--claude-dir", type=Path, default=None)
    parser.add_argument("--desktop-dir", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=None, help="where undone imports are moved (default ~/.local/share/chatbridge)")
    parser.add_argument("--chat", action="append", default=[], help="chat id or unique id prefix (repeatable)")
    parser.add_argument("--all", action="store_true", help="select every non-empty chat (must be explicit)")
    parser.add_argument("--subagents", action="store_true", help="include subagent chats")
    parser.add_argument("--cwd", default=None, help="import into this folder instead of the chat's own project folder")
    parser.add_argument("--apply", action="store_true", help="write files (default: dry run)")
    parser.add_argument("--claude", action="append", default=[], help="Claude session key / id prefix (sync, repeatable)")
    parser.add_argument("--linked", action="store_true", help="sync: every conversation that exists in both tools")
    parser.add_argument("--direction", choices=["both", "to-claude", "to-cursor"], default="both", help="sync direction (default both)")
    parser.add_argument("--cursor-profile", default=None, help="sync: Cursor profile label that receives new chats from Claude")
    parser.add_argument("--interval", type=float, default=20.0, help="watch: seconds between checks")
    parser.add_argument("--once", action="store_true", help="watch: run a single pass and exit")
    parser.add_argument("--journal", type=Path, default=None, help="cursor-undo: journal file written by a Cursor write")
    return parser


def make_paths(args: argparse.Namespace) -> AppPaths:
    base = AppPaths.default()
    return replace(
        base,
        claude_dir=args.claude_dir or base.claude_dir,
        desktop_dir=args.desktop_dir or base.desktop_dir,
        data_dir=args.data_dir or base.data_dir,
        transcripts_dir=None if args.no_transcripts else (args.transcripts or base.transcripts_dir),
        config_dir=args.config_dir or base.config_dir,
        live_profile=None
        if args.no_live
        else (CursorProfile(args.cursor_user_dir, "live", writable=True) if args.cursor_user_dir else base.live_profile),
    )


def select_rows(rows: list[ChatRow], args: argparse.Namespace) -> list[ChatRow]:
    """Pick chats named with --chat, or all with --all. Nothing is selected implicitly."""
    visible = apply_filter(rows, ChatFilter(include_subagents=args.subagents))
    if args.all:
        return visible
    picked = [
        r
        for r in apply_filter(rows, ChatFilter(include_subagents=True, include_empty=True))
        if any(r.ref.chat_id.startswith(w) for w in args.chat)
    ]
    missing = [w for w in args.chat if not any(r.ref.chat_id.startswith(w) for r in rows)]
    if missing:
        raise ImporterError(f"no chat matches: {', '.join(missing)}")
    if not picked:
        raise ImporterError("nothing selected: pass --chat <id> (repeatable) or --all")
    return picked


def format_report(report: ConversionReport) -> str:
    s = report.source_counts
    line = (
        f"[{report.status:8}] {report.chat_id[:8]} {report.title[:44]:44} "
        f"user={s.user} text={s.assistant_text} reason={s.reasoning} tools={s.tool_calls} chars={s.text_chars}"
    )
    if report.status in ("written", "failed"):
        line += f"  -> {report.log_bytes / 1e6:.1f}MB verified={report.verified}"
    return line + (f"  ({report.detail})" if report.detail else "")


def print_inventory(rows: list[ChatRow]) -> None:
    shown = [r for r in apply_filter(rows, ChatFilter(include_subagents=True, include_empty=True)) if not r.is_empty]
    print(f"{len(rows)} chats found, {len(shown)} with content, {len(rows) - len(shown)} empty drafts")
    for row in shown:
        ref = row.ref
        flags = ("S" if ref.is_subagent else "-") + ("I" if row.imported else "-")
        print(
            f"{ref.chat_id[:8]}  {flags}  {ref.kind[:4]:4} {ref.record_count:>7} recs  "
            f"{ref.source_label[:14]:14}  {row.project[:22]:22}  {row.title[:60]}"
        )


def select_conversations(args: argparse.Namespace, conversations: list[Conversation]) -> list[Conversation]:
    """Pick conversations for sync. Nothing is selected implicitly; --all must be explicit."""
    visible = [c for c in conversations if not c.is_empty and (args.subagents or not c.is_subagent)]
    if args.all:
        return visible
    if args.linked:
        return [c for c in visible if c.cursor and c.claude]
    picked: list[Conversation] = []
    for conv in conversations:
        by_cursor = conv.cursor and any(conv.cursor.ref.chat_id.startswith(w) for w in args.chat)
        by_claude = conv.claude and any(conv.claude.key.startswith(w) or conv.claude.cli_id.startswith(w) for w in args.claude)
        if by_cursor or by_claude:
            picked.append(conv)
    if not picked:
        raise ImporterError("nothing selected: pass --chat <cursor id>, --claude <session key>, --linked or --all")
    return picked


def format_sync_report(report: SyncReport) -> str:
    line = f"[{report.status:8}] {report.title[:46]:46} -> claude +{report.to_claude}  cursor +{report.to_cursor}"
    return line + (f"  ({report.detail})" if report.detail else "")


def print_conversations(conversations: list[Conversation]) -> None:
    shown = [c for c in conversations if not c.is_empty and not c.is_subagent]
    print(f"{len(shown)} conversations ({len(conversations) - len(shown)} hidden: empty drafts / subagent runs)")
    for conv in shown:
        ids = (conv.cursor.ref.chat_id[:8] if conv.cursor else "--------") + "/" + (conv.claude.key[-8:] if conv.claude else "--------")
        print(f"{conv.state.value:26} {ids}  {conv.project[:20]:20}  {conv.title[:60]}")


def run_sync_commands(args: argparse.Namespace, paths: AppPaths, settings: Settings) -> int:
    sync = SyncService(paths, settings)
    if args.command == "cursor-undo":
        if args.journal is None:
            raise ImporterError("cursor-undo needs --journal <file> (printed by sync when it wrote into Cursor)")
        removed = sync.undo_cursor_write(args.journal)
        print(f"removed {removed} message record(s) written by the journaled sync")
        return 0
    if args.command == "watch":
        return run_watch(args, sync)
    conversations, warnings = sync.load_conversations()
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.command == "list":
        print_conversations(conversations)
        return 0
    picked = select_conversations(args, conversations)
    target: CursorProfile | None = None
    if args.cursor_profile:
        target = next((p for p in sync.writable_profiles() if p.label == args.cursor_profile), None)
        if target is None:
            raise ImporterError(f"no writable Cursor profile named '{args.cursor_profile}'")
    reports = sync.sync_many(
        picked, args.direction, args.apply, lambda p: print(format_sync_report(p.report), flush=True), target_profile=target
    )
    if not args.apply:
        print("dry run - nothing written (use --apply)")
    for report in reports:
        if report.journal:
            print(f"  undo journal: {report.journal}")
    return 1 if any(r.status == "failed" for r in reports) else 0


def run_watch(args: argparse.Namespace, sync: SyncService) -> int:
    from .autosync import AutoSyncer

    def show(event: AutoSyncEvent) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {event.kind}: {event.message}", flush=True)

    syncer = AutoSyncer(sync, args.interval, show)
    if args.once:
        reports = syncer.run_once()
        syncer._emit(reports)
        return 1 if any(r.status == "failed" for r in reports) else 0
    print(f"watching linked conversations every {args.interval:.0f}s (Ctrl+C to stop)", flush=True)
    syncer.start()
    try:
        while syncer.running:
            time.sleep(1)
    except KeyboardInterrupt:
        syncer.stop()
    return 0


def run(args: argparse.Namespace, service: ImportService) -> int:
    rows, warnings = service.load_catalog()
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.command == "inventory":
        print_inventory(rows)
        return 0
    picked = select_rows(rows, args)
    if args.command == "verify":
        results = service.verify([r.ref for r in picked])
        for result in results:
            label = "OK" if result.exact else ("OK+NEW" if result.grown else "MISMATCH")
            print(f"[{label:8}] {result.chat_id[:8]} source={result.source.content_tuple()} written={result.written.content_tuple()}")
        return 0 if all(r.exact or r.grown for r in results) else 1
    if args.command == "undo":
        if not args.apply:
            print(f"dry run: would move imported files of {len(picked)} chat(s) to {service.paths.removed_dir} (use --apply)")
            return 0
        for undone in service.undo([r.ref.chat_id for r in picked]):
            print(f"moved {len(undone.moved)} file(s) of {undone.chat_id[:8]} -> {undone.destination}")
        return 0
    requests = [ImportRequest(r.ref, args.cwd) for r in picked]
    reports = service.import_chats(requests, args.apply, lambda p: print(format_report(p.report), flush=True))
    if not args.apply:
        print("dry run - nothing written (use --apply)")
    return 1 if any(r.status == "failed" for r in reports) else 0


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.WARNING)
    args = build_parser().parse_args(argv)
    try:
        paths = make_paths(args)
        settings: Settings = load_settings(paths)
        settings.extra_profiles += [(p.label, str(p.user_dir)) for p in map(parse_profile_arg, args.profile)]
        if args.command == "doctor":
            checks = run_doctor(paths, settings)
            for check in checks:
                print(f"[{check.level:5}] {check.text}")
            return 1 if any(c.level == "error" for c in checks) else 0
        if args.command in ("list", "sync", "watch", "cursor-undo"):
            return run_sync_commands(args, paths, settings)
        return run(args, ImportService(paths, settings))
    except (ImporterError, OSError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def main_entry() -> None:
    """Console-script entry point."""
    sys.exit(main(sys.argv[1:]))
