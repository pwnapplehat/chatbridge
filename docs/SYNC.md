# Sync semantics

## Events and identity

Every conversation (Cursor chat or Claude session) is read into a list of events:

| Event | Cursor source | Claude source |
|---|---|---|
| `UserText` | user bubble (`type 1`) | `user` entry text block |
| `AssistantText` | assistant bubble with `text` | `assistant` entry `text` block |
| `Reasoning` | bubble with `thinking` | `thinking` block, or text marked `[Cursor reasoning]` |
| `ToolCall` (+ output, error) | `toolFormerData` | `tool_use` + matching `tool_result` |

An event's **identity key** is a hash of its meaning, independent of the tool it came from:

- user text: the text with harness injections (`<system-reminder>…`) removed and whitespace trimmed;
- assistant text and reasoning: trimmed text;
- tool call: canonical tool name (Claude-origin tools are stored in Cursor as `mcp-claude-<name>`; the prefix is stripped) plus the canonical JSON of its input. Outputs are deliberately not part of identity.

Placeholder text written by ChatBridge (opening note for subagent chats, attachment notes, "no output stored") is never an event, so it can never be re-synced.

## Merge

`missing(source, destination)` is a multiset difference by identity key: if `source` has a message three times and `destination` once, the last two are missing. Merging a pair appends `missing(cursor, claude)` to Claude and `missing(claude, cursor)` to Cursor.

Properties (all covered by tests):

1. **Idempotent**: syncing twice changes nothing the second time.
2. **Convergent**: after a sync both sides contain the same multiset of events.
3. **Non-destructive**: nothing is rewritten or removed; deletions never propagate.
4. **Baseline-free**: no stored "last synced" snapshot is needed for correctness.

## Ordering and time

- Only one side changed → its new events arrive in order after the existing ones. "Claude until 6 pm, then Cursor" works because each tool receives the other's newer messages when you sync.
- Both sides changed → each side receives the other's new events appended after its own. Cursor records the original timestamps (`createdAt`); Claude logs keep a monotonic chain, so a foreign message older than the log's tail takes the tail's timestamp.
- Existing history is never reordered, because a Claude log is a parent-linked chain and Cursor orders by its header list.

## Change detection

Each link stores a cheap fingerprint of both sides after the last sync (Cursor: stored record count plus last update time; Claude: log size plus mtime). A side whose fingerprint differs has changed since the last sync. A side with undelivered messages (for example Cursor was open) is stored as `dirty` so it keeps showing as changed until delivered. The exact comparison always happens at sync time.

## Pairing without a database

Ids are deterministic: a Cursor chat `C` is represented in Claude as session `local_<uuid5(C)>` / log `<uuid5(C)>`; a Claude session `S` becomes Cursor chat `uuid5(S)`. So pairs are rediscovered even if `links.db` is deleted. The Claude app re-keys a continued session (new `cliSessionId`, history copied); ChatBridge follows the desktop record's current `cliSessionId`.

## Auto-sync

Polls fingerprints of linked conversations (one indexed query and one `stat` each). Only changed, linked conversations are synced. Cursor writes wait while Cursor runs on that profile; a Claude log modified within the last 3 seconds is skipped (a turn may be in progress). Nothing new is ever imported automatically.
