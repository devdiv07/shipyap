"""C3: model calls through the owner's Claude plan (Claude Code in print mode), not API credits.

Python never starts a process (tests enforce it). A run on the plan route is two-phase:

1. `pros run` plans every request exactly as the API route does. A request whose reply is not in
   `<workspace>/model-inbox/<request hash>.json` is written to `<workspace>/model-outbox/` (the
   request record, plus its system prompt and user text as exact UTF-8 files), its hash is listed
   in `model-outbox/needed.json`, and the run pauses with NEEDS_MODEL (exit 3). Nothing about the
   request differs from what the API route would send; only the transport does.
2. `scripts/model-plan.ps1` runs `claude -p` once per needed request and writes the inbox file
   (raw output, exit code, duration, CLI version). `pros run --resume` then plans the same
   requests again: each answered one is replayed from its inbox file by request hash and goes
   through the same normaliser, grounding, C1 rules, fact check, repair and review page as an API
   reply. Repair and fact check may need further rounds; the wrapper loops until no request is
   needed or the daily call cap is reached.

The pause happens in `count_input_tokens`, which every caller runs before it records a
generation attempt, so a paused request never leaves an attempt marker behind.

Inbox files are never deleted: they are the stored replies. A reply that says nothing was
generated (not signed in, a subscription limit, a timeout, a CLI failure) is moved to
`model-inbox/archive/` and the request may be sent again on a later run (it counts against
`plan_calls_per_day`). Any other reply is final, exactly like an API reply: a malformed one is
refused by the usual checks and never sent again.
"""

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from shipyap.drafting.errors import DraftingError, DraftingErrorCode, Phase
from shipyap.drafting.models import ModelReply, ModelRequest, Usage
from shipyap.drafting.policy import OFFLINE_BYTES_PER_TOKEN
from shipyap.drafting.prompt import safe_model_id
from shipyap.evidence.report import canonical_json

ROUTE = "claude-plan"
PROVIDERS = ("anthropic-api", "claude-plan", "chatgpt-plan")
PLAN_PROVIDERS = ("claude-plan",)  # chatgpt-plan is a stub: refused before anything is read
OUTBOX = "model-outbox"
INBOX = "model-inbox"
ARCHIVE = "archive"
NEEDED = "needed.json"
AUTH_STATUS = "auth-status.json"
FOLLOW_UP = "plan-follow-up.json"
SERIES = "plan-run.json"  # the summary of a run's rounds, for last-run.txt
OUTBOX_VERSION = "1"
INBOX_VERSION = "1"
MAX_MESSAGE_CHARS = 2_000  # a longer CLI message is not inspected for sign-in or limit words
PLAN_LIMIT_HOLD = timedelta(hours=5)  # when the CLI gives no reset time
SIGN_IN_HOLD = timedelta(days=30)  # released early once the wrapper sees a good sign-in

_HASH = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")  # one directory name, no separators
_WINDOW = re.compile(r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)")
_OFFSET = re.compile(r"([+-])(\d\d):(\d\d)")
_SIGN_IN = re.compile(
    r"not logged in|please run /login|run /login|invalid api key|oauth token (?:has )?expired|"
    r"token (?:has )?expired|authentication_error|sign in again|log in again|login required",
    re.IGNORECASE,
)
_LIMIT = re.compile(
    r"usage limit|limit reached|hit your (?:usage )?limit|out of (?:extra )?usage", re.IGNORECASE
)
_RESET_EPOCH = re.compile(r"limit reached\|(\d{10})\b")
_FENCE = re.compile(r"\A```[a-zA-Z]*\n(.*)\n```\Z", re.DOTALL)
_STOP_REASONS = frozenset({"end_turn", "max_tokens", "refusal", "stop_sequence"})
TRANSIENT = frozenset(
    {
        DraftingErrorCode.PLAN_SIGNIN_REQUIRED,
        DraftingErrorCode.PLAN_NOT_SUBSCRIPTION,
        DraftingErrorCode.PLAN_USAGE_LIMIT,
        DraftingErrorCode.PROVIDER_TIMEOUT,
        DraftingErrorCode.PROVIDER_UNAVAILABLE,
    }
)


class PlanPaused(Exception):
    """The run stops asking the model, without failing: see the subclasses."""


class NeedsModel(PlanPaused):
    """This request is in the outbox; the wrapper answers it and resumes the run."""

    def __init__(self, request_hash: str) -> None:
        super().__init__(request_hash)
        self.request_hash = request_hash


class PlanCapReached(PlanPaused):
    """`plan_calls_per_day` would be passed: the rest waits for tomorrow (owner's clock)."""


def pause_message(error: DraftingError) -> str:
    """What the owner reads when the plan route stops for `error` (one line, one toast)."""
    code = error.code
    if code == DraftingErrorCode.PLAN_SIGNIN_REQUIRED:
        return (
            "Sign in to Claude Code (run `claude` once). Shipyap sends nothing until you have; "
            "the next run after that continues by itself"
        )
    if code == DraftingErrorCode.PLAN_NOT_SUBSCRIPTION:
        return (
            f"{error}; sign in with your Claude account (claude auth login) and remove the "
            "API key or apiKeyHelper from Claude Code"
        )
    if code == DraftingErrorCode.PLAN_USAGE_LIMIT:
        return error.owner_message()
    if code == DraftingErrorCode.PROVIDER_TIMEOUT:
        return (
            "Claude Code did not answer within plan_timeout_seconds; the request is asked again "
            "on the next run"
        )
    if code == DraftingErrorCode.PROVIDER_UNAVAILABLE:
        return "Claude Code failed without an answer; the request is asked again on the next run"
    return str(error)


class PlanBlocked(PlanPaused):
    """A reply said nothing was generated (sign-in, limit, timeout). No call is made after it
    this run; `error` says why."""

    def __init__(self, error: DraftingError) -> None:
        super().__init__(str(error))
        self.error = error


def request_hash(request: ModelRequest) -> str:
    """The same identity as `engine.request_hash` (a test pins the two together)."""
    return hashlib.sha256(canonical_json(asdict(request)).encode()).hexdigest()


def owner_clock(now: datetime, utc_offset: str) -> datetime:
    match = _OFFSET.fullmatch(utc_offset)
    if match is None:
        raise ValueError(utc_offset)
    sign = 1 if match[1] == "+" else -1
    delta = timedelta(hours=int(match[2]), minutes=int(match[3]))
    return now.astimezone(timezone(sign * delta))


def valid_window(window: object) -> bool:
    """ "" (always open) or "HH:MM-HH:MM" with different ends; it may wrap past midnight."""
    if not isinstance(window, str):
        return False
    if not window:
        return True
    match = _WINDOW.fullmatch(window)
    return match is not None and match.groups()[0:2] != match.groups()[2:4]


def in_window(window: str, now: datetime, utc_offset: str) -> bool:
    if not window:
        return True
    start, end = (int(h) * 60 + int(m) for h, m in (p.split(":") for p in window.split("-")))
    local = owner_clock(now, utc_offset)
    minute = local.hour * 60 + local.minute
    return start <= minute < end if start < end else minute >= start or minute < end


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8-sig"))


# --- the follow-up of a draft (fact check and repair) across rounds ---------------------------


def save_follow_up(state_dir: Path, story_id: str, snapshot_name: str) -> None:
    record = {"follow_up_version": "1", "story_id": story_id, "snapshot": snapshot_name}
    _write_atomic(state_dir / FOLLOW_UP, _json_bytes(record))


def load_follow_up(state_dir: Path) -> tuple[str, str] | None:
    path = state_dir / FOLLOW_UP
    if not path.is_file():
        return None
    value = _read_json(path)
    if (
        not isinstance(value, dict)
        or value.get("follow_up_version") != "1"
        or not isinstance(value.get("story_id"), str)
        or not _HASH.fullmatch(value["story_id"])
        or not isinstance(value.get("snapshot"), str)
        or not _NAME.fullmatch(value["snapshot"])
        or value["snapshot"] in (".", "..")
    ):
        raise ValueError("plan follow-up record is invalid")
    return value["story_id"], value["snapshot"]


def clear_follow_up(state_dir: Path) -> None:
    (state_dir / FOLLOW_UP).unlink(missing_ok=True)


# --- the whole run, across rounds --------------------------------------------------------------


def load_series(state_dir: Path) -> dict | None:
    """The summary of the plan run in progress (from its first NEEDS_MODEL), or None. An
    unreadable record starts a new summary rather than stopping the run."""
    try:
        value = _read_json(state_dir / SERIES)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("series_version") == "1" else None


def save_series(state_dir: Path, series: dict) -> None:
    _write_atomic(state_dir / SERIES, _json_bytes(series))


def clear_series(state_dir: Path) -> None:
    (state_dir / SERIES).unlink(missing_ok=True)


def calls_made(root: Path, hashes: list[str]) -> int:
    """CLI calls made for these requests: their inbox records, archived ones included."""
    wanted = set(hashes)
    total = sum((root / INBOX / f"{h}.json").is_file() for h in wanted)
    archive = root / INBOX / ARCHIVE
    if archive.is_dir():
        total += sum(p.name[:64] in wanted for p in archive.glob("*.json"))
    return total


# --- the sign-in the wrapper last saw ---------------------------------------------------------


def signed_in_since(root: Path) -> datetime | None:
    """When the wrapper last saw a good Claude subscription sign-in (`auth-status.json`), or
    None when it did not, or the record is unreadable."""
    try:
        value = _read_json(root / INBOX / AUTH_STATUS)
        if not isinstance(value, dict) or value.get("ok") is not True:
            return None
        checked = datetime.fromisoformat(str(value["checked_at"]).replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return checked if checked.tzinfo is not None else None


# --- reading one inbox record ------------------------------------------------------------------


def _error(code: DraftingErrorCode, resets_at: datetime | None = None) -> DraftingError:
    return DraftingError(code, phase=Phase.GENERATE, resets_at=resets_at)


def _message_error(text: str) -> DraftingError | None:
    """A sign-in or plan-limit refusal named in a CLI message; None for anything else."""
    if len(text) > MAX_MESSAGE_CHARS:
        return None
    if _SIGN_IN.search(text):
        return _error(DraftingErrorCode.PLAN_SIGNIN_REQUIRED)
    if _LIMIT.search(text):
        match = _RESET_EPOCH.search(text)
        resets = datetime.fromtimestamp(int(match[1]), UTC) if match else None
        return _error(DraftingErrorCode.PLAN_USAGE_LIMIT, resets)
    return None


def json_text(result: str) -> str:
    """The JSON answer in a text result: the whole text, or the inside of one ``` fence, or the
    one {...} span when the text around it is prose. Returned as found; the strict parser and
    every check after it judge it exactly like an API reply's text."""
    text = result.strip()
    fenced = _FENCE.match(text)
    if fenced:
        return fenced[1].strip()
    start, end = text.find("{"), text.rfind("}")
    if start > 0 or (start == 0 and end != len(text) - 1):
        candidate = text[start : end + 1] if end > start else text
        try:
            if isinstance(json.loads(candidate), dict):
                return candidate
        except ValueError:
            pass
    return text


def _count(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _usage(result: dict) -> Usage:
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    return Usage(
        input_tokens=_count(usage.get("input_tokens"))
        + _count(usage.get("cache_creation_input_tokens"))
        + _count(usage.get("cache_read_input_tokens")),
        output_tokens=_count(usage.get("output_tokens")),
    )


def reported_model(result: dict, asked: str) -> str | None:
    """The model the CLI reports (`modelUsage`): the one asked for when it is listed, else the
    one that wrote the most output. None when it reports none."""
    models = result.get("modelUsage")
    if not isinstance(models, dict) or not models:
        return None
    if asked in models:
        return asked

    def output(name: str) -> int:
        entry = models[name]
        return _count(entry.get("outputTokens")) if isinstance(entry, dict) else 0

    return max(sorted(models), key=output)


@dataclass(frozen=True)
class Answer:
    """One inbox record read: the reply, or the error that stands for it, and the output shape
    (`structured_output` or `result`) the text was taken from."""

    reply: ModelReply | None
    error: DraftingError | None
    shape: str | None

    @property
    def transient(self) -> bool:
        return self.error is not None and self.error.code in TRANSIENT


def read_answer(record: object, request: ModelRequest, digest: str) -> Answer:
    """What one inbox record means for `request`. Content is never put into an error."""
    if (
        not isinstance(record, dict)
        or record.get("inbox_version") != INBOX_VERSION
        or record.get("request_hash") != digest
        or record.get("route") != ROUTE
    ):
        return Answer(None, _error(DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE), None)
    skipped = record.get("skipped")
    if skipped == "not_signed_in":
        return Answer(None, _error(DraftingErrorCode.PLAN_SIGNIN_REQUIRED), None)
    if skipped == "not_subscription":
        return Answer(None, _error(DraftingErrorCode.PLAN_NOT_SUBSCRIPTION), None)
    if skipped is not None:
        return Answer(None, _error(DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE), None)
    if record.get("timed_out") is True:
        return Answer(None, _error(DraftingErrorCode.PROVIDER_TIMEOUT), None)
    stdout = record.get("stdout")
    stderr = record.get("stderr_tail")
    stdout = stdout if isinstance(stdout, str) else ""
    stderr = stderr if isinstance(stderr, str) else ""
    exit_code = record.get("exit_code")
    try:
        result = json.loads(stdout)
    except ValueError:
        result = None
    if not isinstance(result, dict) or result.get("type") != "result":
        named = _message_error(stdout.strip()) or _message_error(stderr.strip())
        if named is not None:
            return Answer(None, named, None)
        # A CLI that failed without a result generated nothing: it may be asked again later.
        code = (
            DraftingErrorCode.PROVIDER_UNAVAILABLE
            if exit_code != 0
            else DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE
        )
        return Answer(None, _error(code), None)
    text = result.get("result")
    if result.get("is_error") is not False or result.get("subtype") != "success":
        named = _message_error(text.strip()) if isinstance(text, str) else None
        if named is not None:
            return Answer(None, named, None)
        if result.get("subtype") == "error_max_structured_output_retries":
            return Answer(None, _error(DraftingErrorCode.MODEL_REPLY_MALFORMED), None)
        return Answer(None, _error(DraftingErrorCode.PROVIDER_UNAVAILABLE), None)
    structured = result.get("structured_output")
    if isinstance(structured, dict):
        texts, shape = (json.dumps(structured, ensure_ascii=False),), "structured_output"
    elif isinstance(text, str):
        texts, shape = (json_text(text),), "result"
    else:
        texts, shape = (), None  # no text block: refused as malformed, like an API reply
    reported = reported_model(result, request.model)
    stop = result.get("stop_reason")
    reply = ModelReply(
        model=reported if safe_model_id(reported) else request.model,
        request_id=None,  # the CLI's session id is not a provider request id
        stop_reason=stop if stop in _STOP_REASONS else "end_turn",
        texts=texts,
        usage=_usage(result),
    )
    return Answer(reply, None, shape)


# --- the Drafter -------------------------------------------------------------------------------


@dataclass
class PlanDrafter:
    """`Drafter` over the outbox/inbox. `kind` names the call (discovery, draft, fact_check,
    repair) for the outbox record only."""

    root: Path
    now: datetime
    utc_offset: str
    calls_per_day: int
    timeout_seconds: int
    kind: str = "discovery"
    needed: list[str] = field(default_factory=list)
    _answers: dict[str, Answer] = field(default_factory=dict)

    @property
    def outbox(self) -> Path:
        return self.root / OUTBOX

    @property
    def inbox(self) -> Path:
        return self.root / INBOX

    def start(self) -> None:
        """A new round: nothing is needed yet."""
        self.needed.clear()
        self._write_needed()

    def calls_today(self) -> int:
        """CLI calls started today on the owner's clock: every inbox record, archived ones too.
        A record whose start cannot be read counts (the cap errs on the owner's side)."""
        today = owner_clock(self.now, self.utc_offset).date()
        total = 0
        for directory in (self.inbox, self.inbox / ARCHIVE):
            if not directory.is_dir():
                continue
            for path in directory.glob("*.json"):
                if not _HASH.fullmatch(path.name[:64]):
                    continue
                try:
                    value = _read_json(path)
                    started = datetime.fromisoformat(
                        str(value["started_at"]).replace("Z", "+00:00")
                    )
                    counted = owner_clock(started, self.utc_offset).date() == today
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    counted = True
                total += counted
        return total

    def unanswered(self) -> list[str]:
        """Needed requests that have no inbox record yet."""
        return [h for h in self.needed if not (self.inbox / f"{h}.json").is_file()]

    def count_input_tokens(self, request: ModelRequest) -> int:
        digest = request_hash(request)
        path = self.inbox / f"{digest}.json"
        if digest not in self._answers:
            if not path.is_file():
                self._ask(digest, request)  # raises
            try:
                record = _read_json(path)
            except (OSError, ValueError):
                record = None
            answer = read_answer(record, request, digest)
            if answer.transient:
                self._archive(path)
                raise PlanBlocked(answer.error)
            self._answers[digest] = answer
        size = sum(
            len(part.encode("utf-8"))
            for part in (
                request.system_prompt,
                request.user_text,
                json.dumps(request.output_schema),
            )
        )
        return math.ceil(size / OFFLINE_BYTES_PER_TOKEN)

    def generate(self, request: ModelRequest) -> ModelReply:
        answer = self._answers.get(request_hash(request))
        if answer is None:  # count_input_tokens always runs first
            raise DraftingError(DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE, phase=Phase.COUNT)
        if answer.error is not None:
            raise answer.error
        return answer.reply

    def _ask(self, digest: str, request: ModelRequest) -> None:
        if digest not in self.needed:
            if self.calls_today() + len(self.unanswered()) >= self.calls_per_day:
                raise PlanCapReached()
            self._write_request(digest, request)
            self.needed.append(digest)
            self._write_needed()
        raise NeedsModel(digest)

    def _write_request(self, digest: str, request: ModelRequest) -> None:
        stem = self.outbox / digest
        _write_atomic(stem.with_name(f"{digest}.system.txt"), request.system_prompt.encode())
        _write_atomic(stem.with_name(f"{digest}.user.txt"), request.user_text.encode())
        record = {
            "outbox_version": OUTBOX_VERSION,
            "request_hash": digest,
            "route": ROUTE,
            "kind": self.kind,
            "model": request.model,
            "effort": request.effort,
            "max_tokens": request.max_tokens,
            "output_schema_json": json.dumps(request.output_schema, separators=(",", ":")),
            "system_prompt_file": f"{digest}.system.txt",
            "user_text_file": f"{digest}.user.txt",
            "created_at": self.now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "request": asdict(request),
        }
        _write_atomic(self.outbox / f"{digest}.json", _json_bytes(record))

    def _write_needed(self) -> None:
        record = {
            "needed_version": "1",
            "route": ROUTE,
            "needed": list(self.needed),
            "timeout_seconds": self.timeout_seconds,
        }
        _write_atomic(self.outbox / NEEDED, _json_bytes(record))

    def _archive(self, path: Path) -> None:
        """Keep a reply that generated nothing, out of the way of the next attempt."""
        archive = self.inbox / ARCHIVE
        archive.mkdir(parents=True, exist_ok=True)
        stamp = self.now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = archive / f"{path.stem}-{stamp}.json"
        n = 2
        while target.exists():
            target = archive / f"{path.stem}-{stamp}-{n}.json"
            n += 1
        path.replace(target)


def answered_all(root: Path) -> bool:
    """`--resume`: every request the last round needed has an inbox record."""
    try:
        value = _read_json(root / OUTBOX / NEEDED)
    except (OSError, ValueError):
        return False
    needed = value.get("needed") if isinstance(value, dict) else None
    if not isinstance(needed, list) or not all(
        isinstance(h, str) and _HASH.fullmatch(h) for h in needed
    ):
        return False
    return all((root / INBOX / f"{h}.json").is_file() for h in needed)
