"""Small, secret-free TOML configuration. Preferences remain the repository allowlist."""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from shipyap.automation.plan_route import PROVIDERS, valid_window
from shipyap.drafting.policy import MODELS, parse_budget
from shipyap.drafting.prompt import MAX_STYLE_CHARS
from shipyap.drafting.story_draft import AuthorProfile
from shipyap.editorial.closing import DEFAULT_BUILT_BY_ME, split_style
from shipyap.editorial.models import Relationship
from shipyap.ingestion.request import IngestionRequest


class AutomationError(Exception):
    """Only fixed, non-sensitive codes cross the console boundary."""


@dataclass(frozen=True)
class Config:
    workspace_root: Path
    workspace_id: str
    preferences: Path
    backlog: Path
    drafts: Path
    snapshots: Path
    state: Path
    model: str = "claude-sonnet-5-5"
    max_cost_usd: str = "1.00"
    max_new_drafts: int = 1
    # Repository id -> the owner's own statements about it (the optional [author] table).
    authors: dict[str, AuthorProfile] = field(default_factory=dict)
    # Publishing defaults for `pros review`: the approval form is pre-filled with this channel
    # and the next free slot. The slots also cap posts: at most one per slot.
    buffer_channel_id: str = ""
    # Optional X channel (Step 6): approved posts also go to X, same slot, after LinkedIn, with
    # the X text `review.x_post` derives. "": X is off.
    x_channel_id: str = ""
    posting_slots: tuple[str, ...] = ("Mon 09:30", "Wed 09:30", "Fri 09:30")
    utc_offset: str = "+05:30"  # the owner's clock for the slots (India by default)
    weekly_draft_cap: int = 4  # paid draft requests per ISO week, across repositories
    # The post voice. "plain": drafting prompt p4, unchanged. "dry-nerdy" (C2b): prompt p5, dry
    # humour in FRAMING only, never in facts; it needs author_style_file.
    voice: str = "plain"
    # A text file under workspace_root with the owner's style rules and 1-2 example posts; p5
    # appends it to its prompt. Required for voice = "dry-nerdy", unused for "plain".
    author_style_file: str = ""
    author_style: str = ""  # the file's text, read by load_config (not a pros.toml key)
    # C2c: end every post with the repository URL, added by the tool (`editorial.closing`).
    # None (unset): on for "dry-nerdy", off for "plain", so p4 output stays byte for byte.
    link_repo: bool | None = None
    # The closing wording for a repository marked built_by_me, from the style file's
    # "Built-by-me closing:" line (not a pros.toml key); that line never reaches the prompt.
    built_by_me_line: str = DEFAULT_BUILT_BY_ME
    # C3: where model calls go. "anthropic-api" (default): the Anthropic API with
    # PROS_ANTHROPIC_API_KEY, only with --allow-paid, exactly as before. "claude-plan": Claude
    # Code in print mode through the owner's Claude subscription (`automation.plan_route`); it
    # costs no API credits, so it needs no --allow-paid and is bounded by plan_calls_per_day and
    # plan_window instead. "chatgpt-plan": reserved; refused in this version.
    provider: str = "anthropic-api"
    plan_calls_per_day: int = 8  # CLI calls per day on the owner's clock (utc_offset)
    plan_window: str = ""  # "HH:MM-HH:MM" on the owner's clock, e.g. "01:00-06:00"; "": any time
    plan_timeout_seconds: int = 300  # one CLI call; longer is killed and asked again later

    @property
    def plan_route(self) -> bool:
        return self.provider == "claude-plan"

    @property
    def links_repo(self) -> bool:
        return self.voice == "dry-nerdy" if self.link_repo is None else self.link_repo


VOICES = ("plain", "dry-nerdy")
_CHANNEL_ID = re.compile(r"[0-9a-f]{24}")
_SLOT = re.compile(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([01]\d|2[0-3]):([0-5]\d)")
_OFFSET = re.compile(r"[+-](0\d|1[0-4]):(00|30|45)")
_REPO_ID = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
_AUTHOR_KEYS = {"name", "repos"}
_REPO_KEYS = {
    "repo",
    "relationship",
    "statements",
    "learned",
    "do_not_claim",
    "hashtags",
    "built_by_me",
}


def _strings(value: object, low: int, high: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise ValueError
    if not all(isinstance(item, str) and 1 <= len(item.strip()) <= 600 for item in value):
        raise ValueError
    return tuple(item.strip() for item in value)


def _flag(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError
    return value


def _authors(value: object) -> dict[str, AuthorProfile]:
    """The optional [author] table: a name and one [[author.repos]] entry per repository.

    [author]
    name = "Your Name"
    [[author.repos]]
    repo = "owner/repo"
    relationship = "primary_author"        # or "contributor"
    statements = ["I built this repository to test ..."]   # 1 to 6, your own words
    learned = []                           # optional, up to 6
    do_not_claim = []                      # optional, up to 6
    hashtags = []                          # optional, up to 3, like "#AIAgents"
    built_by_me = true                     # optional (C2c): the closing line credits you
    """
    if not isinstance(value, dict) or set(value) - _AUTHOR_KEYS or "name" not in value:
        raise ValueError
    name = value["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError
    profiles: dict[str, AuthorProfile] = {}
    for entry in value.get("repos", []):
        if not isinstance(entry, dict) or set(entry) - _REPO_KEYS:
            raise ValueError
        repo = entry["repo"]
        if not isinstance(repo, str) or not _REPO_ID.fullmatch(repo) or repo in profiles:
            raise ValueError
        relationship = Relationship(entry["relationship"])
        if relationship not in (Relationship.PRIMARY_AUTHOR, Relationship.CONTRIBUTOR):
            raise ValueError  # first person is only for work the owner did
        profiles[repo] = AuthorProfile(
            name=name.strip(),
            relationship=relationship,
            statements=_strings(entry["statements"], 1, 6),
            learned=_strings(entry.get("learned", []), 0, 6),
            do_not_claim=_strings(entry.get("do_not_claim", []), 0, 6),
            hashtags=_strings(entry.get("hashtags", []), 0, 3),
            built_by_me=_flag(entry.get("built_by_me", False)),
        )
    return profiles


def _style(root: Path, name: object) -> str:
    """The author style file's text: a UTF-8 file under `root`, 1 to MAX_STYLE_CHARS characters.
    "" when none is configured."""
    if not isinstance(name, str):
        raise ValueError
    if not name:
        return ""
    path = (root / name).resolve(strict=True)
    if path == root or not path.is_relative_to(root) or not path.is_file():
        raise ValueError
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    if not 1 <= len(text) <= MAX_STYLE_CHARS:
        raise ValueError
    return text


def load_config(path: Path) -> Config:
    try:
        values = tomllib.loads(path.read_text(encoding="utf-8"))
        if (
            set(values) - set(Config.__dataclass_fields__) - {"author"}
            or "authors" in values
            or "author_style" in values
            or "built_by_me_line" in values
        ):
            raise ValueError
        authors = _authors(values.pop("author")) if "author" in values else {}
        if "max_cost_usd" not in values:
            raise ValueError  # paid opt-in must use an explicitly configured cap
        root = Path(values.pop("workspace_root")).resolve(strict=True)
        if not root.is_dir():
            raise ValueError
        for name in ("preferences", "backlog", "drafts", "snapshots", "state"):
            value = Path(values[name])
            resolved = (root / value).resolve()
            if resolved == root or not resolved.is_relative_to(root):
                raise ValueError
            values[name] = resolved
        if "posting_slots" in values:
            slots = values["posting_slots"]
            if not isinstance(slots, list) or not 1 <= len(slots) <= 14:
                raise ValueError
            values["posting_slots"] = tuple(slots)
        values["author_style"], values["built_by_me_line"] = split_style(
            _style(root, values.get("author_style_file", ""))
        )
        if "link_repo" in values:
            values["link_repo"] = _flag(values["link_repo"])
        config = Config(workspace_root=root, **values, authors=authors)
        if (
            not isinstance(config.buffer_channel_id, str)
            or (config.buffer_channel_id and not _CHANNEL_ID.fullmatch(config.buffer_channel_id))
            or not isinstance(config.x_channel_id, str)
            or (config.x_channel_id and not _CHANNEL_ID.fullmatch(config.x_channel_id))
            or (config.x_channel_id and config.x_channel_id == config.buffer_channel_id)
            or not all(isinstance(x, str) and _SLOT.fullmatch(x) for x in config.posting_slots)
            or len(set(config.posting_slots)) != len(config.posting_slots)
            or not isinstance(config.utc_offset, str)
            or not _OFFSET.fullmatch(config.utc_offset)
            or type(config.weekly_draft_cap) is not int
            or not 0 <= config.weekly_draft_cap <= 14
            or config.voice not in VOICES
            or (config.voice == "dry-nerdy" and not config.author_style)
            or config.provider not in PROVIDERS
            or type(config.plan_calls_per_day) is not int
            or not 1 <= config.plan_calls_per_day <= 50
            or not valid_window(config.plan_window)
            or type(config.plan_timeout_seconds) is not int
            or not 60 <= config.plan_timeout_seconds <= 1800
        ):
            raise ValueError
        # Reuse the existing workspace identifier contract without writing a request file.
        IngestionRequest(
            request_version="1",
            workspace_id=config.workspace_id,
            owner="config",
            repo="validation",
            revision_sha="0" * 40,
            paths=("README.md",),
        )
        if (
            config.model not in MODELS
            or not isinstance(config.max_cost_usd, str)
            or parse_budget(config.max_cost_usd) is None
            or type(config.max_new_drafts) is not int
            or config.max_new_drafts not in (0, 1)
            or len(
                {config.preferences, config.backlog, config.drafts, config.snapshots, config.state}
            )
            != 5
        ):
            raise ValueError
        directories = (config.drafts, config.snapshots, config.state)
        if any(a != b and a.is_relative_to(b) for a in directories for b in directories):
            raise ValueError
        if any(
            p.is_relative_to(d) for p in (config.preferences, config.backlog) for d in directories
        ):
            raise ValueError
        return config
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        raise AutomationError("CONFIG_INVALID") from None
