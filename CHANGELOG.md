# Changelog

## 1.0.2

- New: a chat created in Cursor from a Claude session now adds its project folder to Cursor's "Recent projects" (only when the folder is not already there; the previous list is journaled and restored by Undo).
- New: Cursor's context-usage meter is filled with an estimate (about 4 characters per token) instead of 0, for new chats, for messages appended later, and for chats created by 1.0.0/1.0.1 (repaired on the next sync). Cursor replaces it with the real figure after the next message.
- The repair step is now one journaled transaction covering workspace, recent projects and context estimate; Compare and the confirmation dialog list each fix.

## 1.0.1

- Fix: chats sent from Claude to Cursor are now filed under the workspace Cursor will assign to the project folder (`md5(path + inode)` on Linux), even if that folder was never opened in Cursor. Previously they landed under "no folder" and were invisible when the project folder was opened.
- New: a sync now detects ChatBridge-created Cursor chats that are filed under "no folder" although their folder is known, and moves them into the project (journaled, undoable, deferred while Cursor runs).

## 1.0.0

- Two-way sync between Cursor and Claude (messages, reasoning, tool calls with outputs), append-only and idempotent.
- Unified GTK4 / libadwaita app: conversation list with sync states, detail pane (Compare / Sync / Stop syncing), Activity page with Undo for Cursor writes, auto-sync switch, banner while Cursor is open.
- CLI: `list`, `sync`, `watch`, `cursor-undo`, `doctor` (plus the original one-way `import` / `verify` / `undo`).
- Multiple Cursor profiles (live, `~/.cursor-accounts/*`, read-only backups) and Cursor transcript files.
- Claude Code CLI-only setups (no desktop sessions folder) are supported.
- systemd user service for background auto-sync (`./install.sh --service`).
- Packaging: reproducible `.deb` (man pages, AppStream, desktop entry, systemd user unit), generic tarball and installer with per-distro dependency hints, CI job that installs the package on Ubuntu 24.04.
