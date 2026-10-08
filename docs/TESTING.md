# Manual test plan (what automated tests cannot cover)

The automated suite (`./scripts/check.sh`, CI) covers the engine, CLI and GUI on synthetic data, and the engine has been run against real chats in sandboxes. What it **cannot** do is open your real Cursor and Claude apps. This plan covers exactly that. Steps marked **★** are the highest-value ones.

Safety notes before you start:

- ChatBridge only appends, journals every write into Cursor, and refuses to write while Cursor is running. Undo is on the Activity page.
- For the first Claude → Cursor tests use a **small** conversation and, if you have one, the smaller Cursor profile (for example `~/.cursor-accounts/<name>`, a few hundred MB) instead of your 8 GB default profile.
- Optional but wise before step 5 (Cursor closed): `cp <profile>/User/globalStorage/state.vscdb <profile>/User/globalStorage/state.vscdb.bak`.
- Run the GUI from a terminal (`chatbridge-gui`) so its log output is visible if something goes wrong.

## 1. Install

```bash
sudo apt install ./chatbridge_1.0.0_all.deb       # from dist/ or the CI artifact
chatbridge doctor
man chatbridge | head
```

- [ ] `apt` installs without errors and pulls in missing dependencies.
- [ ] `chatbridge doctor` lists your Cursor profile(s) (default and `~/.cursor-accounts/*`), the Claude folders, and says whether Cursor is running.
- [ ] "ChatBridge" appears in the app menu with its icon.

## 2. Read-only look (nothing is written)

Open ChatBridge.

- [ ] The list shows your Cursor chats and Claude sessions with sensible titles, projects and dates.
- [ ] Search, the *All conversations / Needs sync / …* filter, project filter and the *Subagent chats* / *Empty drafts* toggles work.
- [ ] Selecting a conversation shows its details and a preview of the first messages.
- [ ] `chatbridge list` shows the same conversations in the terminal.

## 3. Cursor → Claude, one chat

Pick a small **Only in Cursor** chat → **Compare** → **Import into Claude** → confirm.

- [ ] Compare reports the number of messages that will be added to Claude before anything is written.
- [ ] After the sync the row becomes **In sync**.
- [ ] Reopen the Claude app (or restart it). The session appears in the sidebar under the right project.
- [ ] Open it: your messages, replies, `[Cursor reasoning]` blocks and tool calls with outputs are all there.
- [ ] ★ Send a follow-up message in Claude and get a sensible answer using the earlier context.

## 4. ★ Follow-up in Claude → back into Cursor (appending to an existing Cursor chat)

With the chat from step 3: after your follow-up in Claude, click refresh in ChatBridge. **Close Cursor completely** (all windows), then **Sync now**.

- [ ] The row first shows **Claude is ahead**; Compare says N messages will be added to Cursor.
- [ ] The sync succeeds (Activity page lists it and an undo entry).
- [ ] Open Cursor, open that chat: the follow-up messages are at the end, in order, and readable.
- [ ] Tool calls from Claude look acceptable (they appear as generic MCP-style tool calls).
- [ ] Continue the chat in Cursor and check whether the agent still has the earlier context.

## 5. ★★ Claude → Cursor, a brand-new chat (the part that has not been seen in a live Cursor yet)

Pick a small **Only in Claude** session. Choose the target Cursor profile in the detail pane. Close Cursor. **Send to Cursor**.

- [ ] A chat is created and the row becomes **In sync**.
- [ ] Open Cursor on that profile and that project folder: the chat is listed (if the folder was never opened in this profile it is under "no folder").
- [ ] It opens without errors; messages, reasoning and tool calls display.
- [ ] You can send a new message in it.
- [ ] If anything looks wrong or Cursor complains: close Cursor, use **Undo** on the Activity page (or `chatbridge cursor-undo --journal <file>`), and report it with a screenshot and `chatbridge doctor`.

## 6. Undo

Activity → **Undo** next to a Cursor write (Cursor closed).

- [ ] The messages (or the whole chat, if ChatBridge created it) disappear from Cursor.
- [ ] The Claude session is untouched, and the conversation is back to **Only in Claude** / **Claude is ahead**.

## 7. Both tools continued

Take a paired chat, add a message in Claude **and** a different one in Cursor, then sync (Cursor closed).

- [ ] State shows **Both changed**; Compare shows messages for both directions.
- [ ] After syncing both tools contain both new messages exactly once. A second sync reports nothing to do.

## 8. Cursor-open guard

With Cursor open on the profile, change a paired chat in Claude and click **Sync now**.

- [ ] A banner says Cursor is open; the Cursor part is deferred (not lost), the row stays **Claude is ahead**.
- [ ] Closing Cursor and syncing again completes it.

## 9. Auto-sync

Turn on **Auto-sync** (header switch).

- [ ] Continue a *paired* conversation in Claude, wait up to ~20 s, with Cursor closed: it syncs and the Activity page logs it.
- [ ] Unpaired conversations are never imported automatically.
- [ ] Optional background service: `systemctl --user enable --now chatbridge-sync`, check `systemctl --user status chatbridge-sync` and `journalctl --user -u chatbridge-sync`, then `systemctl --user disable --now chatbridge-sync`.

## 10. Several Cursor profiles / backups

- [ ] If you use `~/.cursor-accounts/*`, each profile appears in the target-profile chooser and `chatbridge doctor`.
- [ ] Menu → *Cursor backup profiles…* accepts a backup `User` folder (rejects a wrong one with a clear message). Backups are read-only.

## 11. A big conversation

- [ ] Sync a large chat (thousands of messages). Note the time; it should finish without freezing the UI, and open normally in Claude.

## 12. Lifecycle

```bash
sudo apt remove chatbridge     # data in ~/.local/share/chatbridge and ~/.config/chatbridge stays
sudo apt install ./chatbridge_1.0.0_all.deb
```

- [ ] After reinstalling, links and Activity history are still there (conversations still show **In sync**).
- [ ] `sudo apt purge chatbridge` removes the program but never your chats.

## What to send me when something fails

- Output of `chatbridge doctor`.
- The terminal output of `chatbridge-gui` (or `journalctl --user -u chatbridge-sync`).
- For Cursor writes: the journal file named in the Activity page / sync output (`~/.local/share/chatbridge/journal/*.json`). It stores the previous chat record, which includes short previews of earlier messages, so review it and share it privately, not in a public issue.
- A screenshot of how Cursor/Claude displays the problem.
- Never attach real chat content you would not want public.
