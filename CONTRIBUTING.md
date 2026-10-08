# Contributing

1. `python3 -m venv .venv --system-site-packages && .venv/bin/pip install -e . mypy pytest ruff` (system GTK bindings are needed for the GUI tests).
2. Make a change with tests. Anything that touches how data is written into Cursor or Claude needs a test for the safety rules (append-only, journaled, refuses while Cursor runs).
3. `./scripts/check.sh` must pass: `ruff check`, `ruff format --check`, `mypy --strict`, unit + CLI tests, and GUI end-to-end tests under `xvfb-run`.

Guidelines: keep logic out of `gui/`; strict typing, no bare `except`; never read or write real user data in tests (use `tests/fixtures.py`); update `docs/` when a format assumption changes.

Cursor format changes: capture a small complete agent chat, run `python scripts/gen_cursor_templates.py <state.vscdb> <chat-id>`, run the tests, and note the Cursor version in the PR.
