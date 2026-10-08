# Storage formats (as observed)

These are the formats ChatBridge reads and writes. They are not documented by Cursor or Anthropic; this file records what was observed (Cursor 3.23.12, Claude desktop app 2.1.x) and what the code relies on.

## Cursor

Per profile (`~/.config/Cursor/User`, or `<--user-data-dir>/User`):

- `globalStorage/state.vscdb` (SQLite), table `cursorDiskKV(key TEXT UNIQUE, value BLOB)`:
  - `composerData:<chat>`: the chat record (name, timestamps, `fullConversationHeadersOnly`, model/context settings, …).
  - `bubbleId:<chat>:<bubble>`: one record per message: user text (`type 1`), assistant text, reasoning (`capabilityType 30`, `thinking.text`), tool call (`capabilityType 15`, `toolFormerData{name, params, rawArgs, result, status, error}`).
- table `composerHeaders(composerId, workspaceId, createdAt, lastUpdatedAt, isArchived, isSubagent, recency, checkpointAt, subagentTypeName, value)`: the chat list; `workspaceId` is the hash of the folder it belongs to (`workspaceStorage/<hash>/workspace.json`).
- A chat belongs to the workspace named by `composerHeaders.workspaceId`, which Cursor/VS Code derives on Linux as `md5(folder path + folder inode)` (verified against every workspace in a real profile). Chats with `workspaceId = empty-window` only show in windows with no folder open.
- Important: for large chats `fullConversationHeadersOnly` lists only a subset of the bubbles. The reader therefore reads **every** `bubbleId:<chat>:` row and orders by `createdAt`.
- Key ranges (`key >= 'bubbleId:<chat>:' AND key < 'bubbleId:<chat>;'`) use the unique index; `LIKE` would scan the whole (often multi-GB) table.
- `~/.cursor/projects/*/agent-transcripts/**/*.jsonl`: older/auxiliary transcripts (`{"role", "message": {"content": […]}}`), no tool outputs; read-only here.

Writing a chat creates the three record kinds above, shaped from `chatbridge/data/cursor_templates.json` (generated from a real chat by `scripts/gen_cursor_templates.py`). Claude-origin tool calls are written MCP-style (`name = mcp-claude-<tool>`, server `claude`).

## Claude

- `~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl`: the conversation log, one JSON object per line. Conversation entries have `type` `user`/`assistant`, `uuid`, `parentUuid`, `sessionId`, `timestamp`, `message`. Other line types (`queue-operation`, `attachment`, `system`, `custom-title`, `last-prompt`, …) are not conversation.
- `~/.config/Claude/claude-code-sessions/<org>/<account>/local_<uuid>.json`: the desktop app's sidebar record (`sessionId`, `cliSessionId`, `cwd`, `title`, timestamps, …). The sidebar needs it; Claude Code CLI works with the log alone (`claude --resume`).
- Noise skipped when reading: sidechain (subagent) entries, `isMeta`, compaction summaries, API-error stubs, `<system-reminder>` blocks, slash-command wrappers.
