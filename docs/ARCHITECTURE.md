# Architecture

```
chatbridge/
  model.py          shared dataclasses (events, ChatRef, reports) and typed JSON helpers
  events.py         event identity keys, normalisation, multiset diff (pure)
  cursor_source.py  read Cursor databases / transcripts -> events (read-only)
  claude_source.py  read Claude logs + desktop records -> events
  converter.py      events -> Claude log entries (new session or continuation)
  writer.py         Claude-side writes (new session, atomic; append), verification
  validate.py       structural validation of Claude logs
  cursor_writer.py  events -> Cursor records (transaction, journal, undo, running-Cursor guard)
  links.py          SQLite link store (pairs + fingerprints)
  sync.py           unified catalog, planning, applying, undo of Cursor writes
  autosync.py       polling auto-sync
  conversations.py  list filtering (pure)
  service.py        legacy one-way import facade used by the importer CLI commands
  cli.py            command line
  doctor.py         environment diagnostics
  gui/              GTK4 / libadwaita front end (thin: all logic is in the layers above)
```

Layering rule: nothing below `gui/` imports GTK, so every behaviour is testable without a display. The GUI tests drive the real window under Xvfb.

Data flow of a sync: `SyncService.load_conversations` pairs both catalogs → `plan` reads both sides to events and diffs them → `sync` applies: Claude by appending converted entries (`writer.append_events_to_claude`), Cursor by `cursor_writer.upsert_events` (one transaction plus journal) → fingerprints are recorded in `links.db`.

Safety invariants (tested): append-only writes; Cursor writes refused while Cursor runs; journal before commit; quarantine of unverified Claude logs; read-only profiles cannot be written.
