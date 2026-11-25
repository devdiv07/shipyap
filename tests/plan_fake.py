"""Test helpers for the plan route (C3): inbox fixtures and a Python stand-in for the wrapper.

The fixture records in fixtures/plan_inbox are what scripts/model-plan.ps1 writes. Their `stdout`
is Claude Code's `--output-format json` result object as documented for print mode (type,
subtype, is_error, result, usage, modelUsage, and structured_output when --json-schema is
honoured); the doctor (`model-plan.ps1 -Doctor`) shows the real shape on the owner's machine.
Placeholders: <HASH>, <STARTED>, and the model's answer as "<ANSWER_OBJECT>" (structured output),
"<ANSWER_TEXT>" (the whole result text) or <ANSWER_RAW> (inside a longer result text).
"""

import json
from pathlib import Path

from shipyap.automation import plan_route
from shipyap.drafting.models import ModelRequest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "plan_inbox"


def fixture(name: str, digest: str, started: str, answer: str | None = None) -> dict:
    record = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    record["request_hash"] = digest
    record["started_at"] = started
    if answer is not None:
        record["stdout"] = (
            record["stdout"]
            .replace('"<ANSWER_OBJECT>"', answer)
            .replace('"<ANSWER_TEXT>"', json.dumps(answer))
            .replace("<ANSWER_RAW>", json.dumps(answer)[1:-1])
        )
    return record


def needed(root: Path) -> list[str]:
    path = root / plan_route.OUTBOX / plan_route.NEEDED
    return json.loads(path.read_text(encoding="utf-8"))["needed"] if path.is_file() else []


def outbox_record(root: Path, digest: str) -> dict:
    return json.loads((root / plan_route.OUTBOX / f"{digest}.json").read_text(encoding="utf-8"))


def request_of(root: Path, digest: str) -> ModelRequest:
    return ModelRequest(**outbox_record(root, digest)["request"])


def write_inbox(root: Path, digest: str, record: dict) -> None:
    inbox = root / plan_route.INBOX
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{digest}.json").write_text(json.dumps(record), encoding="utf-8")


def answer_all(root: Path, model, started: str, shape="good_structured", by_kind=None) -> list:
    """What the wrapper does for every needed request, with `model` (a test fake) as Claude.
    `by_kind` maps an outbox kind to another fixture (e.g. {"draft": "malformed"})."""
    answered = []
    for digest in needed(root):
        if (root / plan_route.INBOX / f"{digest}.json").is_file():
            continue
        kind = outbox_record(root, digest)["kind"]
        name = (by_kind or {}).get(kind, shape)
        answer = model.generate(request_of(root, digest)).texts[0]
        write_inbox(root, digest, fixture(name, digest, started, answer))
        answered.append((kind, digest))
    return answered


QUOTES, QUOTE_IDS = ["'", '"'], ["single", "double"]


def toml_workspace(
    tmp_path: Path, quote: str, provider: str | None = "claude-plan"
) -> tuple[Path, Path]:
    """A workspace whose path has a space, and a config naming it in `quote` style: a literal
    'single-quoted' string, or a "double-quoted" one with escaped backslashes."""
    root = tmp_path / "my posts" / "ws"
    root.mkdir(parents=True)
    text = str(root) if quote == "'" else str(root).replace("\\", "\\\\")
    lines = [
        f"workspace_root = {quote}{text}{quote}",
        "workspace_id = 'demo_workspace'",
        'preferences = "preferences.sqlite"',
        "backlog = 'backlog.sqlite'",
        'drafts = "drafts"',
        "snapshots = 'snapshots'",
        'state = "automation"',
        "max_cost_usd = '1.00'",
    ]
    if provider:
        lines.append(f"provider = {quote}{provider}{quote}")
    config = tmp_path / "pros.toml"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root, config
