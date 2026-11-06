"""Default-deny publishing port. `DisabledPublisher` makes no external call.

`SchedulingProvider` is the provider-neutral shape the owner-approved scheduling flow
(`publication.service`) needs. It is separate from `Publisher` on purpose: `Publisher.submit` takes a
`ContentPackage`, which carries no scheduling time and no human approval, so it cannot express a
safe scheduled send. `DisabledPublisher` and `Publisher` are unchanged.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from public_research_os.publication.models import ContentPackage


class Publisher(Protocol):
    def submit(self, package: ContentPackage) -> str: ...


class DisabledPublisher:
    def submit(self, package: ContentPackage) -> str:
        """Fail closed until real authorization, ledger and provider code exist."""
        raise PermissionError("Live publishing is not implemented or authorized")


class CreateKind(StrEnum):
    ACCEPTED = "ACCEPTED"  # the provider returned a post id; NOT proof of scheduling
    REJECTED = "REJECTED"  # a typed, definitive refusal: the provider did not create the post
    AMBIGUOUS = "AMBIGUOUS"  # the request may or may not have been processed


@dataclass(frozen=True)
class CreateOutcome:
    kind: CreateKind
    code: str  # a fixed, allow-listed code; never provider text
    post_id: str | None = None


@dataclass(frozen=True)
class ProviderPost:
    post_id: str
    status: str
    due_at: str | None  # canonical UTC `YYYY-MM-DDTHH:MM:SSZ`, or None when absent/unparseable
    channel_id: str
    text: str
    share_mode: str | None = None


@dataclass(frozen=True)
class PostListing:
    posts: tuple[ProviderPost, ...]
    complete: bool  # False when the page cap was hit: absence from `posts` proves nothing


class SchedulingProvider(Protocol):
    def check_destination(self, channel_id: str, service: str = "linkedin") -> None:
        """Raise `PublicationError` unless the channel is a healthy channel of `service`
        (Buffer's `linkedin` or `twitter`)."""

    def create_scheduled_post(self, text: str, channel_id: str, due_at: str) -> CreateOutcome:
        """Exactly one provider request. Never retried. Never raises for a provider outcome."""

    def read_post(self, post_id: str) -> ProviderPost | None: ...

    def list_posts(self, organization_id: str, channel_id: str) -> PostListing: ...
