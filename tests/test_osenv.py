"""Platform helpers: URIs, SQLite URIs, locations, atomic file replacement and newline handling."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from chatbridge import osenv
from chatbridge.config import AppPaths


# --------------------------------------------------------------------------- URIs
@pytest.mark.parametrize(
    ("path", "uri"),
    [
        ("C:\\Users\\me\\proj", "file:///c%3A/Users/me/proj"),
        ("e:\\Work\\X-API", "file:///e%3A/Work/X-API"),
        ("c:/Users/me/My Docs/a#b", "file:///c%3A/Users/me/My%20Docs/a%23b"),
        ("/home/me/proj", "file:///home/me/proj"),
        ("/home/me/with space", "file:///home/me/with%20space"),
    ],
)
def test_path_to_uri_matches_what_vscode_stores(path: str, uri: str) -> None:
    assert osenv.path_to_uri(path) == uri


@pytest.mark.parametrize(
    ("uri", "windows_path", "posix_path"),
    [
        ("file:///c%3A/Users/me/proj", "c:\\Users\\me\\proj", "c:/Users/me/proj"),
        ("file:///e%3A/Work/TwitterRE", "e:\\Work\\TwitterRE", "e:/Work/TwitterRE"),
        ("file:///home/me/with%20space", "\\home\\me\\with space", "/home/me/with space"),
    ],
)
def test_uri_to_path(uri: str, windows_path: str, posix_path: str) -> None:
    got = osenv.uri_to_path(uri)
    if uri.startswith("file:///home"):
        assert got.replace("\\", "/") == "/home/me/with space"
    else:
        assert got == (windows_path if osenv.IS_WINDOWS else posix_path)


def test_plain_paths_pass_through_uri_to_path() -> None:
    assert osenv.uri_to_path("C:\\x\\y") == "C:\\x\\y" and osenv.uri_to_path("/a/b") == "/a/b"


def test_uri_roundtrip_for_windows_style_paths() -> None:
    for folder in ("c:\\Users\\me\\proj", "d:\\a b\\c#d\\e%f"):
        assert osenv.norm_path(osenv.uri_to_path(osenv.path_to_uri(folder))) == osenv.norm_path(folder.replace("\\", os.sep))


def test_cursor_workspace_identifier_shape_for_a_windows_folder() -> None:
    assert osenv.uri_dict("C:\\Users\\me\\proj") == {
        "$mid": 1,
        "fsPath": "c:\\Users\\me\\proj",
        "external": "file:///c%3A/Users/me/proj",
        "path": "/c:/Users/me/proj",
        "scheme": "file",
    }
    assert osenv.uri_dict("/home/me/proj")["fsPath"] == "/home/me/proj"


def test_drive_letters_are_shown_the_way_each_tool_writes_them() -> None:
    assert osenv.vscode_fs_path("C:/Users/me") == "c:\\Users\\me"
    assert osenv.vscode_fs_path("/home/me") == "/home/me"
    if osenv.IS_WINDOWS:
        assert osenv.display_path("c:/Users/me") == "C:\\Users\\me"  # Claude writes upper-case drive letters
    else:
        assert osenv.display_path("/home/me") == "/home/me"


@pytest.mark.skipif(not osenv.IS_WINDOWS, reason="Windows paths are case-insensitive")
def test_windows_paths_compare_case_insensitively() -> None:
    assert osenv.same_path("C:\\Users\\Me\\", "c:/users/me")
    assert not osenv.same_path("C:\\Users\\Me", "C:\\Users\\You")


# --------------------------------------------------------------------------- SQLite
def test_sqlite_uri_opens_databases_in_awkward_folders(tmp_path: Path) -> None:
    folder = tmp_path / "my data #1 100% (copy)"
    folder.mkdir()
    db = folder / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.execute("INSERT INTO t VALUES (7)")
    conn.commit()
    conn.close()
    ro = sqlite3.connect(osenv.sqlite_uri(db), uri=True)
    assert ro.execute("SELECT v FROM t").fetchone() == (7,)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO t VALUES (8)")
    ro.close()


# --------------------------------------------------------------------------- files
def test_text_helpers_never_write_windows_newlines(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    osenv.write_text(target, "a\nb\n")
    osenv.append_text(target, "c\n")
    assert target.read_bytes() == b"a\nb\nc\n"


def test_replace_file_overwrites_an_existing_target(tmp_path: Path) -> None:
    source, target = tmp_path / "a.partial", tmp_path / "a"
    target.write_text("old", encoding="utf-8")
    source.write_text("new", encoding="utf-8")
    osenv.replace_file(source, target)
    assert target.read_text(encoding="utf-8") == "new" and not source.exists()


def test_local_moment_survives_dates_windows_cannot_convert() -> None:
    assert osenv.local_moment(0).year in (1969, 1970)
    assert osenv.local_moment(-86_400_000 * 400).year == 1968
    assert osenv.local_moment(1_788_000_000_000).year == 2026


# --------------------------------------------------------------------------- locations
def test_default_paths_follow_the_platform_conventions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("CHATBRIDGE_CURSOR_ACCOUNTS", str(tmp_path / "accounts"))
    paths = AppPaths.default()
    assert paths.claude_dir == Path.home() / ".claude"
    assert paths.transcripts_dir == Path.home() / ".cursor" / "projects"
    if osenv.IS_WINDOWS:
        assert paths.live_profile is not None and paths.live_profile.user_dir == tmp_path / "roaming" / "Cursor" / "User"
        assert paths.desktop_dir == tmp_path / "roaming" / "Claude" / "claude-code-sessions"
        assert paths.config_dir == tmp_path / "roaming" / "ChatBridge" and paths.data_dir == tmp_path / "local" / "ChatBridge"
    elif osenv.IS_LINUX:
        assert paths.live_profile is not None and paths.live_profile.user_dir == tmp_path / "xdg-config" / "Cursor" / "User"
        assert paths.config_dir == tmp_path / "xdg-config" / "chatbridge" and paths.data_dir == tmp_path / "xdg-data" / "chatbridge"


def test_account_profiles_are_discovered_under_the_override_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "accounts" / "work" / "User" / "globalStorage" / "state.vscdb"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")
    monkeypatch.setenv("CHATBRIDGE_CURSOR_ACCOUNTS", str(tmp_path / "accounts"))
    profiles = AppPaths.default().account_profiles
    assert [p.label for p in profiles] == ["work"] and profiles[0].writable


@pytest.mark.skipif(not osenv.IS_WINDOWS, reason="Microsoft Store layout is Windows only")
def test_store_build_of_the_claude_app_is_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    store = tmp_path / "local" / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude" / "claude-code-sessions"
    store.mkdir(parents=True)
    assert osenv.claude_desktop_sessions_dir() == store
    (tmp_path / "roaming" / "Claude" / "claude-code-sessions").mkdir(parents=True)
    assert osenv.claude_desktop_sessions_dir() == tmp_path / "roaming" / "Claude" / "claude-code-sessions", "the classic install wins"


# --------------------------------------------------------------------------- workspace id
@pytest.mark.skipif(not osenv.IS_WINDOWS, reason="Windows uses the folder's creation time")
def test_windows_workspace_id_is_stable_and_case_insensitive_on_the_drive_letter(tmp_path: Path) -> None:
    folder = tmp_path / "proj"
    folder.mkdir()
    upper = osenv.workspace_hash(str(folder).replace(str(folder)[0], str(folder)[0].upper(), 1))
    lower = osenv.workspace_hash(str(folder).replace(str(folder)[0], str(folder)[0].lower(), 1))
    assert upper == lower and upper is not None and len(upper) == 32
    assert osenv.workspace_hash(str(tmp_path / "missing")) is None
