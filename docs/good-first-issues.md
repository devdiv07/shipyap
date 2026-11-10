# Good first issues

Small, real tasks found in the current code. Each one is offline and needs no API key. Comment on
(or open) an issue before starting so two people don't pick the same one, then follow
[CONTRIBUTING.md](../CONTRIBUTING.md).

## 1. `shipyap review --port 70000` crashes with a traceback

- **What:** `--port` is a plain `int`. An out-of-range port reaches `socket.bind`, which raises
  `OverflowError`. `automation/review.py` only catches `OSError`, so the user sees a Python
  traceback instead of a fixed message.
- **Where:** `src/shipyap/automation/cli.py` (the `review` parser's `--port`), or the `except` in
  `serve` in `src/shipyap/automation/review.py`.
- **Fix idea:** validate 1-65535 in the argument type, so the existing redacting parser exits 2.
- **Test:** in `tests/`, call `cli.main(["review", "--config", "x.toml", "--port", "70000"])` and
  expect exit code 2 and no traceback; keep `--port 8765` working.

## 2. `shipyap run` prints a review command that breaks on paths with spaces

- **What:** `init` prints `shipyap review --config "<path>"` with quotes, but `run` prints the same
  hint without them, so copying it fails when the workspace path has a space.
- **Where:** the last `print` in `main` in `src/shipyap/automation/cli.py` (compare with the
  `init` branch).
- **Test:** `tests/test_automation.py` already asserts the unquoted line (search for
  `Review: shipyap review --config`, which uses a fake GitHub through
  `monkeypatch.setattr(cli, "HttpsTransport", ...)`). Update that assertion to the quoted form and
  add a case with a space in the workspace path.

## 3. The CLI module docstring lists four commands; there are seven

- **What:** the docstring of `src/shipyap/automation/cli.py` says "`run`, `review`, `eval` and
  `repo`", but the parser also has `init`, `demo` and `publication migrate`.
- **Where:** `src/shipyap/automation/cli.py`, lines 1-2.
- **Test:** `uv run --no-sync ruff check .` and the full test suite still pass.

## 4. Confirm LinkedIn's 3,000-character limit from an official source

- **What:** `src/shipyap/editorial/models.py` notes the LinkedIn post limit is "as reported by
  third-party sources; UNVERIFIED against LinkedIn's own docs".
- **Where:** that comment, and `src/shipyap/publication/linkedin.py`, which documents the Buffer
  side.
- **Do:** find LinkedIn's own documentation (or say clearly that none exists), cite it with the
  date you read it, and update the comment. Don't change the number unless the source says so.
- **Test:** the full test suite still passes; the pull request links the source.

## 5. Pin what POSIX `rename` does to an empty target directory

- **What:** `src/shipyap/ingestion/writer.py` says that on POSIX `rename(2)` replaces an EMPTY
  directory, marked "UNVERIFIED here, not exercised on Linux".
- **Where:** a new Linux-only test next to the existing writer tests in
  `tests/test_ingestion_writer.py`, then the docstring in `writer.py`.
- **Do:** write a test (skipped on Windows) that renames a staged directory onto an existing
  empty one and records the result; update the docstring to say what was observed. Adding a test
  changes the test inventory; say so in the pull request.
- **Test:** `uv run --no-sync python -m pytest -q tests/test_ingestion_writer.py` on Linux.

## 6. Try Shipyap on macOS and report what happens

- **What:** the README says macOS is untested. `scripts/check_expected_skips.py` only knows the
  platforms `win32` and `linux`, so the CI inventory check cannot run there as written.
- **Do:** on a Mac, run the offline commands from CONTRIBUTING.md and
  `uv run --locked --no-dev shipyap demo`, and open an issue with the exact output: which tests
  fail or skip, whether the demo page works. A report is a complete contribution; a fix is a
  bonus.
- **Test:** the commands themselves; paste their output (it contains no keys).
