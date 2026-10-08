# Changelog

## 1.1.2

- Fix: after deleting one side of a pair (for example the Claude session), the surviving chat showed "Counterpart missing" and "Stop syncing" did nothing (it looked for the missing side to find the link). A link whose other side is gone is now ignored: the survivor is listed as "Only in Cursor" / "Only in Claude" and can be imported again (the stale link is replaced). "Stop syncing" removes the link by its own key.

## 1.1.1

- Fix: long Cursor chats imported into Claude made the session far larger than the model's window (a 17,000-record chat is about 18.8 million tokens against 1 million), so it could not be continued and `/compact` failed. Big imports are now compacted the way Claude Code itself does it: the full history stays in the log, followed by a `compact_boundary`, a summary of the older part (extractive, no model call) and the latest turns (tool outputs capped at 20,000 characters), so the active context is about 55,000 tokens. Copies are flagged and never synced twice.
- New repair: a sync appends the same compaction to oversized sessions that ChatBridge imported earlier (append-only; Compare lists it; quit the Claude app or close the session first).
- The log validator understands compaction boundaries.

## 1.1.0

- **Experimental, opt-in: carry the conversation into Cursor as model context** (`chatbridge sync --carry-context`, or the app menu "Carry model context into Cursor (experimental)"). Cursor's agent builds its prompt from an internal conversation state (content-addressed JSON messages in `agentKv:blob:*` plus `composerData.conversationState`), not from the displayed messages, so an imported chat looked complete but the model started blank. ChatBridge can now rebuild that state for chats it creates: the system prompt and environment message are borrowed from the profile's most recent native chat, the conversation follows as AI-SDK style messages, and Claude follow-ups extend it. Reasoning is not included (it is provider-signed and cannot be forged). Long conversations keep the most recent turns that fit half of Cursor's context window, with a note about what was left out; tool outputs are capped at 20,000 characters. Everything is journaled and removed by Undo; native Cursor chats are never given synthetic state.

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
