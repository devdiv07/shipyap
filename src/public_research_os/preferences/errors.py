"""Fixed error codes for repository preferences. Nothing taken from an argument, a path or an OS
error is put into a message, so a pasted value cannot leak through an error."""

from enum import StrEnum


class PreferencesErrorCode(StrEnum):
    REPOSITORY_INVALID = "REPOSITORY_INVALID"
    MODE_INVALID = "MODE_INVALID"
    STORE_UNUSABLE = "STORE_UNUSABLE"
    STORE_WRITE_FAILED = "STORE_WRITE_FAILED"


MESSAGES: dict[PreferencesErrorCode, str] = {
    PreferencesErrorCode.REPOSITORY_INVALID: (
        "the repository must be OWNER/REPOSITORY with a GitHub-style owner and name"
    ),
    PreferencesErrorCode.MODE_INVALID: "the mode must be focus, occasional or off",
    PreferencesErrorCode.STORE_UNUSABLE: (
        "the preferences database is missing, not a preferences database or unreadable"
    ),
    PreferencesErrorCode.STORE_WRITE_FAILED: "the preference could not be saved",
}


class PreferencesError(Exception):
    def __init__(self, code: PreferencesErrorCode) -> None:
        super().__init__(MESSAGES[code])
        self.code = code
