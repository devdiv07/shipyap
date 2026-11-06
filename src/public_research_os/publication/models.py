"""Immutable-on-paper content contracts; actual human authorization is not implemented."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Channel(StrEnum):
    LINKEDIN = "linkedin"
    X = "x"
    INSTAGRAM = "instagram"


class PostVariant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: Channel
    text: str = Field(min_length=1)
    claim_ids: tuple[str, ...] = Field(min_length=1)
    media_sha256: tuple[str, ...] = ()

    @field_validator("media_sha256")
    @classmethod
    def check_media_hashes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        import re

        for value in values:
            if re.fullmatch(r"[a-f0-9]{64}", value) is None:
                raise ValueError("media digest must be lowercase sha256 hex")
        return values


class ContentPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    package_id: str = Field(min_length=3)
    workspace_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")
    posts: tuple[PostVariant, ...] = Field(min_length=1)
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def check_times(self) -> "ContentPackage":
        if self.created_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("package timestamps must include UTC offset")
        if self.expires_at <= self.created_at:
            raise ValueError("package must expire after creation")
        return self
