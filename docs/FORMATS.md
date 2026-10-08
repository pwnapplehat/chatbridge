# Storage formats (as observed)

These are the formats ChatBridge reads and writes. They are not documented by Cursor or Anthropic; this file records what was observed (Cursor 3.23.12, Claude desktop app 2.1.x) and what the code relies on.

## Cursor

Per profile (`~/.config/Cursor/User`, or `<--user-data-dir>/User`):

- `globalStorage/state.vscdb` (SQLite), table `cursorDiskKV(key TEXT UNIQUE, value BLOB)`:
  - `composerData:<chat>`: the chat record (name, timestamps, `fullConversationHeadersOnly`, model/context settings, …).
  - `bubbleId:<chat>:<bubble>`: one record per message: user text (`type 1`), assistant text, reasoning (`capabilityType 30`, `thinking.text`), tool call (`capabilityType 15`, `toolFormerData{name, params, rawArgs, result, status, error}`).
- table `composerHeaders(composerId, workspaceId, createdAt, lastUpdatedAt, isArchived, isSubagent, recency, checkpointAt, subagentTypeName, value)`: the chat list; `workspaceId` is the hash of the folder it belongs to (`workspaceStorage/<hash>/workspace.json`).
- A chat belongs to the workspace named by `composerHeaders.workspaceId`, which Cursor/VS Code derives on Linux as `md5(folder path + folder inode)` (verified against every workspace in a real profile). Chats with `workspaceId = empty-window` only show in windows with no folder open.
- Recent projects: `ItemTable` key `history.recentlyOpenedPathsList` = `{"entries": [{"folderUri": "file://<path>"}, …]}` (most recent first). ChatBridge adds a project folder at the front only when it is absent.
- Context meter: `composerData.contextTokensUsed`, `contextTokenLimit`, `contextUsagePercent`, `promptTokenBreakdown` (and `contextUsagePercent` in the header). ChatBridge writes an estimate (about 4 characters per token); Cursor overwrites it with real figures after the next message.
- Important: for large chats `fullConversationHeadersOnly` lists only a subset of the bubbles. The reader therefore reads **every** `bubbleId:<chat>:` row and orders by `createdAt`.
- Key ranges (`key >= 'bubbleId:<chat>:' AND key < 'bubbleId:<chat>;'`) use the unique index; `LIKE` would scan the whole (often multi-GB) table.
- `~/.cursor/projects/*/agent-transcripts/**/*.jsonl`: older/auxiliary transcripts (`{"role", "message": {"content": […]}}`), no tool outputs; read-only here.

Writing a chat creates the three record kinds above, shaped from `chatbridge/data/cursor_templates.json` (generated from a real chat by `scripts/gen_cursor_templates.py`). Claude-origin tool calls are written MCP-style (`name = mcp-claude-<tool>`, server `claude`).

## Cursor agent state (experimental)

What the model is sent when you continue a chat is *not* the bubbles. Cursor keeps:

- `agentKv:blob:<sha256>` rows: content-addressed, plain compact JSON messages in an AI-SDK style: `{"role":"system"|"user"|"assistant"|"tool","content":…}`. Assistant content parts are `text`, `reasoning` (provider-signed, not forgeable), `tool-call {toolCallId, toolName, args}`; tool messages hold `tool-result {toolCallId, toolName, result, experimental_content}` with `providerOptions.cursor.highLevelToolCallResult`. The key is the sha256 of the stored bytes.
- `composerData.conversationState` = `"~" + base64(protobuf)`: field 1 repeats the 32-byte hashes of the messages in order (system prompt, a large environment/rules message, then user/assistant/tool messages); further fields hold per-turn metadata (token usage in 5, format version 10, timestamp 26, todo/file-state blobs…).

ChatBridge writes field 1 (+ 5, 10, 26) for chats it creates, with the first two messages borrowed from the profile's newest native chat. This mapping was reverse-engineered from a real profile and is unverified against future Cursor versions; the option is off by default.

## Claude

- `~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl`: the conversation log, one JSON object per line. Conversation entries have `type` `user`/`assistant`, `uuid`, `parentUuid`, `sessionId`, `timestamp`, `message`. Other line types (`queue-operation`, `attachment`, `system`, `custom-title`, `last-prompt`, …) are not conversation.
- `~/.config/Claude/claude-code-sessions/<org>/<account>/local_<uuid>.json`: the desktop app's sidebar record (`sessionId`, `cliSessionId`, `cwd`, `title`, timestamps, …). The sidebar needs it; Claude Code CLI works with the log alone (`claude --resume`).
- Noise skipped when reading: sidechain (subagent) entries, `isMeta`, compaction summaries, API-error stubs, `<system-reminder>` blocks, slash-command wrappers.
