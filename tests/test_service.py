"""Service-layer tests against the synthetic Cursor/Claude environment."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from chatbridge.catalog import ChatFilter, ChatRow, StatusFilter, apply_filter
from chatbridge.model import Counts
from chatbridge.service import ImportRequest, ImportService
from chatbridge.writer import local_session_name, session_uuid
from tests.fixtures import CHAT_BACKUP_ONLY, CHAT_EMPTY, CHAT_MAIN, CHAT_SUB, CHAT_TRANSCRIPT, World


def rows_by_id(service: ImportService) -> dict[str, ChatRow]:
    rows, warnings = service.load_catalog()
    assert warnings == []
    return {r.ref.chat_id: r for r in rows}


def read_log(world: World, chat_id: str) -> list[dict[str, object]]:
    logs = list((world.paths.claude_dir / "projects").glob(f"*/{session_uuid(chat_id)}.jsonl"))
    assert len(logs) == 1
    return [json.loads(line) for line in logs[0].read_text(encoding="utf-8").splitlines()]


def test_catalog_finds_every_source_and_dedupes(service: ImportService) -> None:
    rows = rows_by_id(service)
    assert set(rows) == {CHAT_MAIN, CHAT_SUB, CHAT_EMPTY, CHAT_BACKUP_ONLY, CHAT_TRANSCRIPT}
    assert rows[CHAT_MAIN].ref.record_count == 8, "richer live copy must win over the shorter backup copy"
    assert rows[CHAT_MAIN].ref.source_label == "live"
    assert rows[CHAT_BACKUP_ONLY].ref.source_label == "backup"
    assert rows[CHAT_TRANSCRIPT].ref.kind == "transcript"
    assert rows[CHAT_SUB].ref.is_subagent and rows[CHAT_EMPTY].is_empty


def test_windows_path_maps_to_existing_linux_folder(service: ImportService, world: World) -> None:
    assert rows_by_id(service)[CHAT_BACKUP_ONLY].target_cwd == str(world.project_dir)


def test_default_filter_hides_drafts_and_subagents(service: ImportService) -> None:
    rows, _ = service.load_catalog()
    visible = {r.ref.chat_id for r in apply_filter(rows, ChatFilter())}
    assert visible == {CHAT_MAIN, CHAT_BACKUP_ONLY, CHAT_TRANSCRIPT}
    everything = {r.ref.chat_id for r in apply_filter(rows, ChatFilter(include_subagents=True, include_empty=True))}
    assert len(everything) == 5


def test_search_matches_title_preview_and_project(service: ImportService) -> None:
    rows, _ = service.load_catalog()
    assert [r.ref.chat_id for r in apply_filter(rows, ChatFilter(query="parser bug"))] == [CHAT_MAIN]
    assert [r.ref.chat_id for r in apply_filter(rows, ChatFilter(query="LEGACY"))] == [CHAT_BACKUP_ONLY]
    # The transcript chat also matches: its source label ("transcript:<project slug>") contains the project name.
    assert {r.ref.chat_id for r in apply_filter(rows, ChatFilter(query="parser-app"))} == {CHAT_MAIN, CHAT_BACKUP_ONLY, CHAT_TRANSCRIPT}
    assert {r.ref.chat_id for r in apply_filter(rows, ChatFilter(project="parser-app"))} == {CHAT_MAIN, CHAT_BACKUP_ONLY}


def test_import_is_complete_including_bubbles_missing_from_headers(service: ImportService, world: World) -> None:
    row = rows_by_id(service)[CHAT_MAIN]
    assert row.ref.expected_visible == 4 and row.ref.record_count == 8
    (report,) = service.import_chats([ImportRequest(row.ref)], apply=True)
    assert report.status == "written" and report.verified
    expected = Counts(user=2, assistant_text=2, reasoning=1, tool_calls=2, tool_results=2)
    assert report.source_counts.content_tuple()[:5] == expected.content_tuple()[:5]
    assert report.source_counts.empty_records == 1
    blocks = [b for e in read_log(world, CHAT_MAIN) if isinstance(e.get("message"), dict) for b in e["message"]["content"]]  # type: ignore[index]
    results = [b for b in blocks if b["type"] == "tool_result"]
    assert results[0]["content"] == json.dumps({"contents": "print('hi')"})
    assert results[1].get("is_error") is True
    assert any("[Cursor reasoning]" in str(b.get("text", "")) for b in blocks)
    assert any("image(s) were attached" in str(b.get("text", "")) for b in blocks)


def test_import_writes_sidebar_metadata_and_uses_project_folder(service: ImportService, world: World) -> None:
    row = rows_by_id(service)[CHAT_MAIN]
    service.import_chats([ImportRequest(row.ref)], apply=True)
    meta = json.loads(next(world.paths.desktop_dir.glob(f"*/*/{local_session_name(CHAT_MAIN)}.json")).read_text(encoding="utf-8"))
    assert meta["cliSessionId"] == session_uuid(CHAT_MAIN) and meta["cwd"] == str(world.project_dir)
    assert meta["title"] == "[Cursor] Fix the parser"
    assert (world.paths.claude_dir / "projects").glob("*")  # project slug folder created


def test_dry_run_writes_nothing(service: ImportService, world: World) -> None:
    row = rows_by_id(service)[CHAT_MAIN]
    (report,) = service.import_chats([ImportRequest(row.ref)], apply=False)
    assert report.status == "dry-run" and report.source_counts.user == 2
    assert list((world.paths.claude_dir / "projects").glob("*/*.jsonl")) == []


def test_import_is_idempotent(service: ImportService) -> None:
    row = rows_by_id(service)[CHAT_MAIN]
    first = service.import_chats([ImportRequest(row.ref)], apply=True)
    second = service.import_chats([ImportRequest(row.ref)], apply=True)
    assert (first[0].status, second[0].status) == ("written", "exists")
    assert rows_by_id(service)[CHAT_MAIN].imported is True


def test_empty_chat_is_not_written(service: ImportService, world: World) -> None:
    (report,) = service.import_chats([ImportRequest(rows_by_id(service)[CHAT_EMPTY].ref)], apply=True)
    assert report.status == "empty"
    assert list((world.paths.claude_dir / "projects").glob("*/*.jsonl")) == []


def test_folder_override_is_respected(service: ImportService, world: World, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    service.import_chats([ImportRequest(rows_by_id(service)[CHAT_BACKUP_ONLY].ref, str(target))], apply=True)
    meta = json.loads(next(world.paths.desktop_dir.glob(f"*/*/{local_session_name(CHAT_BACKUP_ONLY)}.json")).read_text(encoding="utf-8"))
    assert meta["cwd"] == str(target)


def test_transcript_chat_imports_with_placeholder_outputs(service: ImportService, world: World) -> None:
    (report,) = service.import_chats([ImportRequest(rows_by_id(service)[CHAT_TRANSCRIPT].ref)], apply=True)
    assert report.status == "written" and report.verified and report.source_counts.tool_calls == 1
    text = json.dumps(read_log(world, CHAT_TRANSCRIPT))
    assert "did not store an output" in text and "transcript question" in text


def test_one_failing_chat_does_not_stop_the_batch(service: ImportService, world: World) -> None:
    rows = rows_by_id(service)
    conn = sqlite3.connect(world.live_user / "globalStorage" / "state.vscdb")
    conn.execute("UPDATE cursorDiskKV SET value = 'not json' WHERE key LIKE ?", (f"bubbleId:{CHAT_SUB}:%",))
    conn.commit()
    conn.close()
    reports = service.import_chats([ImportRequest(rows[CHAT_SUB].ref), ImportRequest(rows[CHAT_MAIN].ref)], apply=True)
    assert [r.status for r in reports] == ["failed", "written"]
    assert "corrupt JSON" in reports[0].detail


def test_cancel_stops_between_chats(service: ImportService) -> None:
    rows = rows_by_id(service)
    seen: list[str] = []
    reports = service.import_chats(
        [ImportRequest(rows[CHAT_MAIN].ref), ImportRequest(rows[CHAT_BACKUP_ONLY].ref)],
        apply=True,
        on_progress=lambda p: seen.append(p.report.chat_id),
        should_cancel=lambda: len(seen) >= 1,
    )
    assert len(reports) == 1 and seen == [CHAT_MAIN]


def test_progress_reports_every_chat(service: ImportService) -> None:
    rows = rows_by_id(service)
    done: list[tuple[int, int]] = []
    service.import_chats(
        [ImportRequest(rows[CHAT_MAIN].ref), ImportRequest(rows[CHAT_BACKUP_ONLY].ref)],
        apply=True,
        on_progress=lambda p: done.append((p.done, p.total)),
    )
    assert done == [(1, 2), (2, 2)]


def test_verify_accepts_exact_and_grown_logs(service: ImportService, world: World) -> None:
    ref = rows_by_id(service)[CHAT_MAIN].ref
    service.import_chats([ImportRequest(ref)], apply=True)
    (result,) = service.verify([ref])
    assert result.exact
    log = next((world.paths.claude_dir / "projects").glob("*/*.jsonl"))
    last = json.loads(log.read_text(encoding="utf-8").splitlines()[-4])
    extra = {
        **last,
        "uuid": "new-1",
        "parentUuid": last["uuid"],
        "message": {"role": "assistant", "content": [{"type": "text", "text": "later reply"}]},
    }
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(extra) + "\n")
    (grown,) = service.verify([ref])
    assert not grown.exact and grown.grown


def test_verify_detects_tampered_log(service: ImportService, world: World) -> None:
    ref = rows_by_id(service)[CHAT_MAIN].ref
    service.import_chats([ImportRequest(ref)], apply=True)
    log = next((world.paths.claude_dir / "projects").glob("*/*.jsonl"))
    kept = [ln for ln in log.read_text(encoding="utf-8").splitlines() if "found the issue" not in ln]
    log.write_text("\n".join(kept) + "\n", encoding="utf-8")
    (result,) = service.verify([ref])
    assert not result.exact and not result.grown


def test_undo_moves_files_and_never_deletes(service: ImportService, world: World) -> None:
    ref = rows_by_id(service)[CHAT_MAIN].ref
    service.import_chats([ImportRequest(ref)], apply=True)
    (undone,) = service.undo([CHAT_MAIN])
    assert len(undone.moved) == 2
    assert list((world.paths.claude_dir / "projects").glob("*/*.jsonl")) == []
    assert not list(world.paths.desktop_dir.glob(f"*/*/{local_session_name(CHAT_MAIN)}.json"))
    assert (world.paths.desktop_dir / "org" / "acct" / "local_existing.json").exists(), "foreign sessions must be untouched"
    assert len(list(Path(undone.destination).rglob("*.jsonl"))) == 1 and (Path(undone.destination) / "manifest.json").exists()
    assert rows_by_id(service)[CHAT_MAIN].imported is False
    assert service.undo([CHAT_MAIN]) == [], "undoing twice is a no-op"


def test_undo_also_moves_forked_session_log(service: ImportService, world: World) -> None:
    """When the app continues an imported chat it re-keys cliSessionId; undo must follow the new id."""
    ref = rows_by_id(service)[CHAT_MAIN].ref
    service.import_chats([ImportRequest(ref)], apply=True)
    meta_path = next(world.paths.desktop_dir.glob(f"*/*/{local_session_name(CHAT_MAIN)}.json"))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    forked = world.paths.claude_dir / "projects" / "slug" / "forked-id.jsonl"
    forked.parent.mkdir(parents=True)
    forked.write_text("{}\n", encoding="utf-8")
    meta["cliSessionId"] = "forked-id"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    (undone,) = service.undo([CHAT_MAIN])
    assert len(undone.moved) == 3 and not forked.exists()


def test_preview_is_fast_path_and_formatted(service: ImportService) -> None:
    lines = service.preview(rows_by_id(service)[CHAT_MAIN].ref, limit=10)
    assert [(ln.role, ln.text) for ln in lines][:2] == [
        ("You", "Please fix the parser bug"),
        ("Assistant", "I found the issue and fixed it."),
    ]
    transcript = service.preview(rows_by_id(service)[CHAT_TRANSCRIPT].ref, limit=10)
    assert [ln.role for ln in transcript] == ["You", "Assistant", "Tool"]


def test_status_filter(service: ImportService) -> None:
    ref = rows_by_id(service)[CHAT_MAIN].ref
    service.import_chats([ImportRequest(ref)], apply=True)
    rows, _ = service.load_catalog()
    imported = {r.ref.chat_id for r in apply_filter(rows, ChatFilter(status=StatusFilter.IMPORTED))}
    pending = {r.ref.chat_id for r in apply_filter(rows, ChatFilter(status=StatusFilter.NOT_IMPORTED))}
    assert imported == {CHAT_MAIN} and CHAT_MAIN not in pending
