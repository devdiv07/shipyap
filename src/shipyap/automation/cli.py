"""The `pros` console entry point: `run`, `review`, `eval` and `repo`. Paid execution is an
explicit per-run flag; `eval` and `repo` are always offline."""

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from shipyap.automation import repair
from shipyap.automation import review as review_page
from shipyap.automation.config import load_config
from shipyap.automation.engine import _ERRORS, _code, run
from shipyap.automation.posted import check_posted
from shipyap.automation.review import REVIEW_LEDGER
from shipyap.ingestion.github import DEFAULT_LIMITS
from shipyap.ingestion.transport import HttpsTransport
from shipyap.preferences.errors import PreferencesError
from shipyap.preferences.repositories import Mode, RepositoryPreferences


class RedactingArgumentParser(argparse.ArgumentParser):
    """argparse errors can echo raw argument values (e.g. a pasted token); never print them."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid or missing arguments (values not shown)\n")


REVIEW_LINK = "shipyap://review/"  # + a 64-hex story id; scripts\open-review.ps1 opens it


def _tell(config, result, posted: list[str]) -> None:
    """Toasts for what the owner must act on, plus last-run.txt (always written). Shelved drafts
    are listed in both: the review page hides them, so this is where the owner learns of them."""
    try:
        waiting = repair.shelved(config.drafts)
    except OSError:
        waiting = []
    lines = [
        f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC: {' / '.join(dict.fromkeys(result.statuses))}",
        f"drafts created {result.drafts_created}, estimated spend ${result.estimated_usd:.4f} "
        f"reserved (a ceiling), ${result.charged_usd:.4f} at list price for the usage reported",
        *(f"needs you: {failure}" for failure in result.failures),
        *(f"needs you: draft shelved {line}" for line in result.shelved),
        *(f"repair: {line}" for line in result.repairs),
        *(f"shelved (hidden from review): {sid[:12]}: {'; '.join(why)}" for sid, why in waiting),
        *(f"note: {note}" for note in result.notes),
        *(f"post: {message}" for message in posted),
    ]
    try:
        (config.workspace_root / "last-run.txt").write_text("\n".join(lines) + "\n", "utf-8")
    except OSError:
        pass
    # For the timer's PowerShell wrapper (scripts\\run-and-notify.ps1), which shows them as
    # Windows notifications: "Title|Text", or "Title|Text|shipyap://review/<story id>" for a
    # toast whose Review button opens that draft. Python itself never starts a process.
    toasts = []
    if result.drafts_created and not result.shelved and result.draft_story:
        count = len({*review_page.waiting(config), result.draft_story})
        toasts.append(
            f"Shipyap: {count} draft{'' if count == 1 else 's'} ready to review"
            "|Click Review to open it, then approve or reject."
            f"|{REVIEW_LINK}{result.draft_story}"
        )
    toasts += [f"Draft shelved (not shown for review)|{line[:200]}" for line in result.shelved]
    toasts += [f"Shipyap|{message}" for message in posted]
    if result.failures:
        toasts.append(f"Shipyap needs you|{result.failures[0][:200]}")
    try:
        (config.workspace_root / "notify.txt").write_text(
            "".join(line.replace("\n", " ") + "\n" for line in toasts), "utf-8"
        )
    except OSError:
        pass


def _eval(parser, args) -> int:
    from shipyap.automation import evaluation

    if args.cases is None and args.workspace is None:
        parser.error("eval needs --cases, --workspace or both")
    if args.check and args.cases is None:
        parser.error("--check needs --cases (its expected.json)")
    if args.profile == "public":
        if args.cases is None or args.workspace is not None:
            parser.error("public eval needs --cases and refuses --workspace")
        from shipyap.automation.eval_profiles import public_cases

        try:
            args.cases = public_cases(args.cases)
        except ValueError:
            print("EVAL_CASES_INVALID: PUBLIC_CASES_INVALID")
            return 2
    try:
        stories, drafts = evaluation.load_cases(args.cases) if args.cases else ([], [])
        if args.workspace is not None:
            stories += evaluation.load_workspace(args.workspace)
        decisions = evaluation.load_decisions(args.workspace) if args.workspace else []
        report = evaluation.evaluate(stories, drafts, decisions)
        expected = (
            json.loads((args.cases / "expected.json").read_text(encoding="utf-8"))
            if args.check
            else {}
        )
    except (evaluation.CaseError, sqlite3.Error, OSError, ValueError, KeyError) as exc:
        detail = str(exc) if isinstance(exc, evaluation.CaseError) else type(exc).__name__
        print(f"EVAL_CASES_INVALID: {detail}")
        return 2
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")  # model text on a legacy console
    for line in evaluation.render(report):
        print(line)
    live = None
    if args.workspace is not None:
        from shipyap.automation import live_drafts
        from shipyap.automation.config import AutomationError
        from shipyap.publication.errors import PublicationError
        from shipyap.review.service import ReviewError
        from shipyap.stories.errors import StoryError

        if (args.workspace / live_drafts.CONFIG_FILE).is_file():
            try:
                live = live_drafts.replay_live(args.workspace)
            except (AutomationError, ReviewError, StoryError, PublicationError, sqlite3.Error):
                print("EVAL_CASES_INVALID: the workspace's drafts could not be read")
                return 2
            for line in live_drafts.render(live):
                print(line)
        else:
            print(f"LIVE skipped: no {live_drafts.CONFIG_FILE} in the workspace")
    if not args.check:
        return 0
    failures = evaluation.gate_failures(report, expected)
    if args.workspace is not None:
        pins = expected.get("workspace_drafts") or {}
        if live is not None or pins:
            failures += live_drafts.pin_failures(live or [], pins)
    for failure in failures:
        print(f"GATE FAIL {failure}")
    print("GATE PASS" if not failures else f"GATE FAIL ({len(failures)})")
    return 1 if failures else 0


def _write_stdout(prog: str, text: str) -> bool:
    stdout = sys.stdout
    try:
        if stdout is None:  # started with the standard output descriptor closed
            raise OSError
        stdout.write(text)
        stdout.flush()
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError) and not getattr(stdout, "closed", False):
            raise
        print(
            f"{prog}: error: STDOUT_WRITE_FAILED: report could not be written to standard output",
            file=sys.stderr,
        )
        return False
    return True


def _repo(args) -> int:
    """`pros repo set` creates or updates one repository's mode; `pros repo list` lists every
    saved repository. Offline: no GitHub request, no credential, no publisher state touched.
    Exit 0 on success; 2 for every refusal, as a fixed code on stderr."""
    prog = f"repo-{args.action}"
    try:
        if args.action == "set":
            with RepositoryPreferences.open(args.db, create=True) as store:
                saved = store.set_mode(args.repo, args.mode, datetime.now(UTC))
            text = f"{prog}: {saved.repo_id} {saved.mode.value}\n"
        else:
            with RepositoryPreferences.open(args.db, create=False) as store:
                listed = store.list()
            text = f"{prog}: {len(listed)} repositor{'y' if len(listed) == 1 else 'ies'}\n"
            text += "".join(f"  {item.repo_id} {item.mode.value}\n" for item in listed)
    except PreferencesError as exc:
        line = f"{prog}: error: {exc.code.value}: {exc}\n"
        print(line.encode("ascii", "backslashreplace").decode("ascii"), end="", file=sys.stderr)
        return 2
    return 0 if _write_stdout(prog, text) else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = RedactingArgumentParser(prog="shipyap")
    subcommands = parser.add_subparsers(dest="command", required=True)
    init = subcommands.add_parser(
        "init", help="create a workspace without overwriting existing data"
    )
    init.add_argument("--workspace", type=Path, required=True)
    init.add_argument("--repo", required=True, metavar="OWNER/REPO")
    init.add_argument(
        "--utc-offset",
        help="fixed posting offset, e.g. +05:30 or --utc-offset=-04:00; defaults to this machine's current offset",
    )
    subcommands.add_parser(
        "demo", help="start a fresh synthetic offline demo; scheduling is impossible"
    )
    command = subcommands.add_parser(
        "run", help="inspect public evidence and prepare a review draft"
    )
    command.add_argument("--config", type=Path, required=True)
    command.add_argument(
        "--allow-paid",
        action="store_true",
        help="authorize this manual run's bounded model requests",
    )
    command.add_argument(
        "--reexamine",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="read this repository again with the current evidence selection (needs --allow-paid)",
    )
    review = subcommands.add_parser(
        "review", help="open the review page with your saved channel and the next free slot"
    )
    review.add_argument("--config", type=Path, required=True)
    review.add_argument("--port", type=int, default=8765)
    evaluate = subcommands.add_parser(
        "eval", help="replay every stored model reply through the current checks (offline)"
    )
    evaluate.add_argument("--cases", type=Path, help="a committed case directory, e.g. evals")
    evaluate.add_argument(
        "--profile",
        choices=("private", "public"),
        default="private",
        help="private: existing real cases; public: only CASES/public synthetic cases",
    )
    evaluate.add_argument(
        "--workspace", type=Path, help="also replay automation.sqlite here (opened read-only)"
    )
    evaluate.add_argument(
        "--check", action="store_true", help="exit 1 unless --cases/expected.json holds"
    )
    repo = subcommands.add_parser(
        "repo", help="set or list which repositories get posts (focus, occasional or off)"
    )
    actions = repo.add_subparsers(dest="action", required=True)
    repo_set = actions.add_parser(
        "set",
        help="set one repository's mode",
        description=(
            "Offline: set one repository's publishing mode. focus is preferred, occasional is "
            "eligible with lower priority, off excludes it from automatic story selection (the "
            "record is kept). A mode is not a promise of a post and does not check that the "
            "repository exists."
        ),
    )
    repo_set.add_argument("--repo", required=True, help="explicit GitHub OWNER/REPOSITORY")
    repo_set.add_argument("--mode", required=True, choices=tuple(Mode), help="publishing mode")
    repo_set.add_argument("--db", type=Path, required=True, help="local SQLite preferences file")
    repo_list = actions.add_parser(
        "list",
        help="list saved repositories and modes",
        description="Offline: list saved repositories and modes, including those set to off.",
    )
    repo_list.add_argument("--db", type=Path, required=True, help="local SQLite preferences file")
    args = parser.parse_args(argv)
    if args.command == "init":
        from shipyap.automation.initialise import InitError, initialise

        try:
            config = initialise(args.workspace, args.repo, args.utc_offset)
        except InitError as exc:
            print(f"INIT_FAILED: {exc}")
            return 2
        print(f"Workspace ready. Config: {config.workspace_root / 'pros.toml'}")
        print(f'Review: shipyap review --config "{config.workspace_root / "pros.toml"}"')
        print(
            "No credentials, network calls or approvals were used. Existing files were preserved."
        )
        return 0
    if args.command == "demo":
        from shipyap.automation.demo import serve

        return serve()
    if args.command == "repo":
        return _repo(args)
    if args.command == "eval":
        return _eval(parser, args)
    if args.command == "review":
        from shipyap.automation.review import serve

        return serve(args.config, args.port)
    try:
        config = load_config(args.config)
        result = run(
            config,
            HttpsTransport(DEFAULT_LIMITS),
            allow_paid=args.allow_paid,
            reexamine=tuple(args.reexamine),
        )
        if result.statuses == ["STOPPED"]:
            print("STOPPED: remove the STOP file in your workspace to run again. Nothing was done.")
            return 0
        if result.statuses == ["NO_REPOSITORIES"]:
            _tell(config, result, [])
            print(result.failures[0])
            return 2
        posted = check_posted(config.state / REVIEW_LEDGER)
        _tell(config, result, posted)
        print(" / ".join(dict.fromkeys(result.statuses)))
        print(f"Repositories inspected: {result.repositories_inspected}")
        print(f"New evidence examined: {result.evidence_examined}")
        print(f"New story candidates: {result.new_candidates}")
        print(f"Existing available candidates: {result.existing_available}")
        print(f"Drafts created: {result.drafts_created}")
        print(f"Estimated model spending (reserved maximum): ${result.estimated_usd:.4f}")
        print(f"List price of the usage reported: ${result.charged_usd:.4f}")
        for line in result.repairs:
            print(f"  REPAIR {line}")
        for line in result.shelved:
            print(f"  SHELVED {line}")
        print(f"Failures requiring intervention: {len(result.failures)}")
        for failure in result.failures:
            print(f"  {failure}")
        for note in result.notes:
            print(f"  NOTE {note}")
        for message in posted:
            print(f"  POST {message}")
        for draft in result.draft_paths:
            print(f"Review bundle: {draft}")
        print("Drafts are UNAPPROVED; semantic support remains UNKNOWN.")
        print(f"Review: shipyap review --config {args.config}")
        return 2 if result.failures else 0
    except _ERRORS as exc:
        print(f"NEEDS_ATTENTION: {_code(exc)}")
        return 2
    except (ValueError, KeyError, TypeError):
        # Unusable local state must not echo persisted model/source content.
        print("NEEDS_ATTENTION: STATE_INVALID")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
