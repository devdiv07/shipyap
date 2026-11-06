"""Strict JSON decoding for security-sensitive inputs. Pure; standard library only.

Python's json module (like many parsers) silently keeps the LAST value of a repeated key,
so `{"status": "candidate", "status": "approved"}` would read as approved. This decoder
rejects, at every nesting level:
- duplicate object keys (compared after escape decoding, so "st\\u0061tus" == "status");
- NaN, Infinity, -Infinity, and numbers that overflow to a non-finite float;
- non-UTF-8 bytes (RFC 8259 interchange encoding) and excessive nesting;
- lone surrogate code points (U+D800..U+DFFF) in any key or string. Raw UTF-8 cannot encode
  them, but escapes such as "\\ud800" decode to them; a valid escaped pair such as
  "\\ud83d\\ude00" decodes to one supplementary-plane character and is allowed.
Decoded values must still be validated by the strict Pydantic models.
"""

import json
import math
import re
from enum import StrEnum
from typing import Any


class JsonViolation(StrEnum):
    DUPLICATE_KEY = "DUPLICATE_KEY"
    NON_STANDARD_NUMBER = "NON_STANDARD_NUMBER"
    MALFORMED = "MALFORMED"
    TOO_DEEP = "TOO_DEEP"
    INVALID_UNICODE = "INVALID_UNICODE"


class StrictJsonError(ValueError):
    """Carries a violation and, for duplicates, the key. Callers must not echo unknown keys."""

    def __init__(self, violation: JsonViolation, key: str | None = None) -> None:
        super().__init__(violation.value)
        self.violation = violation
        self.key = key


_SURROGATE = re.compile(r"[\ud800-\udfff]")


def loads_strict(data: bytes) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise StrictJsonError(JsonViolation.MALFORMED) from None
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except StrictJsonError:
        raise
    except RecursionError:
        raise StrictJsonError(JsonViolation.TOO_DEEP) from None
    except ValueError:  # JSONDecodeError, or int() digit-limit errors for huge integers
        raise StrictJsonError(JsonViolation.MALFORMED) from None
    _reject_surrogates(value)
    return value


def _reject_surrogates(value: Any) -> None:
    """Visit every key and string. Iterative, so any depth json.loads accepted is safe here,
    and the work is bounded by the already size-capped input."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if _SURROGATE.search(item):
                raise StrictJsonError(JsonViolation.INVALID_UNICODE)
        elif isinstance(item, dict):
            pending.extend(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError(JsonViolation.DUPLICATE_KEY, key)
        result[key] = value
    return result


def _reject_constant(_name: str) -> Any:
    raise StrictJsonError(JsonViolation.NON_STANDARD_NUMBER)


def _finite_float(literal: str) -> float:
    value = float(literal)
    if not math.isfinite(value):  # e.g. 1e400 would otherwise become inf
        raise StrictJsonError(JsonViolation.NON_STANDARD_NUMBER)
    return value
