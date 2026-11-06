"""Orchestration of `story-discover` and `story-list`. Offline: no GitHub request, no model, no
credential, no publisher state.

Discovery order matters. Everything is read and verified before the backlog is opened: the
repository preferences, every snapshot (the existing completion rule, through
`editorial.sources.load_verified_snapshot`) and every findings file. A structural problem raises
and writes nothing. Then, in ONE backlog transaction, each finding of an eligible repository is
checked and either recorded or refused.

Preferences are read ONCE per run, by a single `list()` query, and that snapshot decides both
eligibility and the reported mode, so a run never describes one repository as both excluded and
eligible. A mode change made after that read applies to the next run and to `story-list`, not
to the run in progress; a repository already `off` in the snapshot is excluded. The preferences
and the backlog are separate SQLite files, so this is a consistent read, not an atomic
cancellation of a run in progress.

Eligibility is the repository's mode in that snapshot only: `focus` or `occasional`. A
repository's age, its latest commit and whether a snapshot is new play no part. A snapshot
without findings yields no candidates, so a new commit is never by itself a story. Findings of an
`off` or never-selected repository are not checked and not recorded; stories recorded earlier stay
in the backlog.

Ranking (`rank`) uses the CURRENT mode: focus before occasional, then first sighting, repository
and key. Rank is an order of attention, not a quota or a promise of a post.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from shipyap.editorial.sources import VerifiedSnapshot, load_verified_snapshot
from shipyap.evidence.snapshot import read_input
from shipyap.ingestion.request import GITHUB_BASE_URL
from shipyap.preferences.errors import PreferencesError
from shipyap.preferences.repositories import (
    ELIGIBLE_MODES,
    Mode,
    RepositoryPreferences,
    normalize_repository,
)
from shipyap.stories.backlog import (
    Observation,
    RecordOutcome,
    Story,
    StoryBacklog,
    StoryState,
)
from shipyap.stories.errors import StoryError, StoryErrorCode
from shipyap.stories.findings import (
    FINDINGS_CAP_BYTES,
    FindingCheck,
    FindingsDocument,
    Refusal,
    check_finding,
    parse_findings,
)

_MODE_ORDER = {Mode.FOCUS: 0, Mode.OCCASIONAL: 1, Mode.OFF: 2}
_UNSELECTED_ORDER = 3


@dataclass(frozen=True)
class FindingResult:
    story_key: str
    outcome: RecordOutcome | None  # None when refused
    refusal: Refusal | None
    detail: str | None
    story_id: str | None


@dataclass(frozen=True)
class SnapshotResult:
    repo_id: str
    revision_sha: str
    mode: Mode | None  # None: the repository was never selected
    findings_supplied: int
    results: tuple[FindingResult, ...]  # empty for an ineligible repository


@dataclass(frozen=True)
class DiscoveryResult:
    eligible_repositories: tuple[str, ...]
    snapshots: tuple[SnapshotResult, ...]

    def count(self, outcome: RecordOutcome) -> int:
        return sum(r.outcome == outcome for s in self.snapshots for r in s.results)

    @property
    def refused(self) -> int:
        return sum(r.refusal is not None for s in self.snapshots for r in s.results)


@dataclass(frozen=True)
class RankedStory:
    story: Story
    mode: Mode | None  # the repository's current mode; None when never selected


def repository_of(snapshot: VerifiedSnapshot) -> str:
    """The preferences identifier for a snapshot's canonical `https://github.com/OWNER/REPO`."""
    prefix = f"{GITHUB_BASE_URL}/"
    if not snapshot.source_url.startswith(prefix):
        raise StoryError(StoryErrorCode.SNAPSHOT_REPOSITORY_INVALID)
    try:
        return normalize_repository(snapshot.source_url[len(prefix) :])
    except PreferencesError:
        raise StoryError(StoryErrorCode.SNAPSHOT_REPOSITORY_INVALID) from None


def run_discovery(
    preferences_path: Path,
    backlog_path: Path,
    snapshot_dirs: list[Path],
    findings_paths: list[Path],
    now: datetime,
) -> DiscoveryResult:
    with RepositoryPreferences.open(preferences_path, create=False) as preferences:
        saved = preferences.list()  # the run's one preference snapshot, including `off`
    modes = {chosen.repo_id: chosen.mode for chosen in saved}
    eligible = {repo_id: mode for repo_id, mode in modes.items() if mode in ELIGIBLE_MODES}

    snapshots: dict[str, tuple[VerifiedSnapshot, str]] = {}  # by manifest digest, first wins
    for directory in snapshot_dirs:
        snapshot = load_verified_snapshot(directory)
        snapshots.setdefault(snapshot.manifest_sha256, (snapshot, repository_of(snapshot)))

    documents: dict[str, list[FindingsDocument]] = {digest: [] for digest in snapshots}
    for path in findings_paths:
        document = parse_findings(read_input(path, FINDINGS_CAP_BYTES))
        if document.snapshot_manifest_sha256 not in snapshots:
            raise StoryError(StoryErrorCode.FINDINGS_SNAPSHOT_UNKNOWN)
        snapshot, _ = snapshots[document.snapshot_manifest_sha256]
        if document.workspace_id != snapshot.workspace_id:
            raise StoryError(StoryErrorCode.FINDINGS_WORKSPACE_MISMATCH)
        documents[document.snapshot_manifest_sha256].append(document)

    planned: list[tuple[VerifiedSnapshot, str, Mode | None, int, list[FindingCheck]]] = []
    for digest, (snapshot, repo_id) in snapshots.items():
        findings = [finding for document in documents[digest] for finding in document.findings]
        checks = [check_finding(f, snapshot) for f in findings] if repo_id in eligible else []
        planned.append((snapshot, repo_id, modes.get(repo_id), len(findings), checks))

    results: list[SnapshotResult] = []
    with StoryBacklog.open(backlog_path, create=True) as backlog, backlog.transaction():
        for snapshot, repo_id, mode, supplied, checks in planned:
            outcomes = tuple(_record(backlog, snapshot, repo_id, check, now) for check in checks)
            results.append(SnapshotResult(repo_id, snapshot.revision_sha, mode, supplied, outcomes))
    return DiscoveryResult(tuple(eligible), tuple(results))


def _record(
    backlog: StoryBacklog,
    snapshot: VerifiedSnapshot,
    repo_id: str,
    check: FindingCheck,
    now: datetime,
) -> FindingResult:
    key = check.finding.story_key
    if check.refusal is not None:
        return FindingResult(key, None, check.refusal, check.detail, None)
    observation = Observation(
        source_url=snapshot.source_url,
        revision_sha=snapshot.revision_sha,
        manifest_sha256=snapshot.manifest_sha256,
        angle=check.finding.angle,
        support=check.finding.support,
        refs=check.refs,
    )
    outcome, sid = backlog.record(repo_id, key, observation, now)
    if outcome == RecordOutcome.DUPLICATE_ANGLE:
        return FindingResult(key, None, Refusal.DUPLICATE_ANGLE, None, None)
    return FindingResult(key, outcome, None, None, sid)


def rank(
    stories: tuple[Story, ...], modes: dict[str, Mode], include_all: bool = False
) -> tuple[RankedStory, ...]:
    """Without `include_all`: available stories of currently eligible repositories only."""
    selected = [
        RankedStory(story, modes.get(story.repo_id))
        for story in stories
        if include_all
        or (story.state == StoryState.AVAILABLE and modes.get(story.repo_id) in ELIGIBLE_MODES)
    ]
    return tuple(
        sorted(
            selected,
            key=lambda item: (
                _MODE_ORDER.get(item.mode, _UNSELECTED_ORDER) if item.mode else _UNSELECTED_ORDER,
                item.story.first_seen_at,
                item.story.repo_id,
                item.story.story_key,
            ),
        )
    )


def list_backlog(
    preferences_path: Path, backlog_path: Path, include_all: bool = False
) -> tuple[RankedStory, ...]:
    with RepositoryPreferences.open(preferences_path, create=False) as preferences:
        modes = {saved.repo_id: saved.mode for saved in preferences.list()}
    with StoryBacklog.open(backlog_path, create=False) as backlog:
        stories = backlog.stories()
    return rank(stories, modes, include_all)
