"""Locations the importer reads from / writes to, and persisted user settings."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .cursor_source import CursorProfile
from .model import ImporterError, as_int, as_list, as_obj, as_str


class SettingsError(ImporterError):
    """The settings file exists but cannot be used."""


def discover_account_profiles(root: Path) -> tuple[CursorProfile, ...]:
    """Cursor 'account' profiles started with --user-data-dir=<root>/<name> (each has a User/ folder)."""
    if not root.is_dir():
        return ()
    found = [
        CursorProfile(d / "User", d.name, writable=True)
        for d in sorted(root.iterdir())
        if (d / "User" / "globalStorage" / "state.vscdb").is_file()
    ]
    return tuple(found)


@dataclass(frozen=True)
class AppPaths:
    """Every filesystem location the application touches (injectable for tests)."""

    claude_dir: Path
    desktop_dir: Path
    data_dir: Path
    config_dir: Path
    transcripts_dir: Path | None
    live_profile: CursorProfile | None
    account_profiles: tuple[CursorProfile, ...] = ()

    @staticmethod
    def default() -> "AppPaths":
        """Standard Linux locations for Claude, Cursor and this app."""
        home = Path.home()
        xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        xdg_data = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
        return AppPaths(
            claude_dir=home / ".claude",
            desktop_dir=xdg_config / "Claude" / "claude-code-sessions",
            data_dir=xdg_data / "chatbridge",
            config_dir=xdg_config / "chatbridge",
            transcripts_dir=home / ".cursor" / "projects",
            live_profile=CursorProfile(xdg_config / "Cursor" / "User", "live", writable=True),
            account_profiles=discover_account_profiles(home / ".cursor-accounts"),
        )

    @property
    def links_db(self) -> Path:
        return self.data_dir / "links.db"

    @property
    def journal_dir(self) -> Path:
        """Undo journals for every write into a Cursor database."""
        return self.data_dir / "journal"

    @property
    def removed_dir(self) -> Path:
        """Where undone imports are moved (never deleted)."""
        return self.data_dir / "removed"


@dataclass
class Settings:
    """User-editable settings persisted as JSON."""

    extra_profiles: list[tuple[str, str]] = field(default_factory=list)
    show_subagents: bool = False
    show_empty: bool = False
    dry_run: bool = True
    auto_sync: bool = False
    auto_interval: int = 20
    carry_context: bool = False

    def profiles(self) -> list[CursorProfile]:
        """Extra (backup) Cursor profiles configured by the user."""
        return [CursorProfile(Path(path), label) for label, path in self.extra_profiles]


def settings_file(paths: AppPaths) -> Path:
    return paths.config_dir / "settings.json"


def load_settings(paths: AppPaths) -> Settings:
    """Load settings; a missing file yields defaults, a corrupt one raises SettingsError."""
    target = settings_file(paths)
    if not target.exists():
        return Settings()
    try:
        raw = as_obj(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsError(f"cannot read {target}: {exc}") from exc
    profiles = [
        (as_str(as_obj(item).get("label")), as_str(as_obj(item).get("path")))
        for item in as_list(raw.get("extra_profiles"))
        if as_str(as_obj(item).get("path"))
    ]
    return Settings(
        extra_profiles=profiles,
        show_subagents=raw.get("show_subagents") is True,
        show_empty=raw.get("show_empty") is True,
        dry_run=raw.get("dry_run") is not False,
        auto_sync=raw.get("auto_sync") is True,
        auto_interval=max(5, as_int(raw.get("auto_interval"), 20)),
        carry_context=raw.get("carry_context") is True,
    )


def save_settings(paths: AppPaths, settings: Settings) -> None:
    """Persist settings atomically."""
    target = settings_file(paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "extra_profiles": [{"label": label, "path": path} for label, path in settings.extra_profiles],
        "show_subagents": settings.show_subagents,
        "show_empty": settings.show_empty,
        "dry_run": settings.dry_run,
        "auto_sync": settings.auto_sync,
        "auto_interval": settings.auto_interval,
        "carry_context": settings.carry_context,
    }
    partial = target.with_name(target.name + ".partial")
    partial.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(partial, target)
