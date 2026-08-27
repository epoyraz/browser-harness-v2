"""Typed applicant facts and choices with explicit provenance.

The browser harness must distinguish an unknown value from a known absence and from a
user-approved choice.  Plain dictionaries collapse all three and caused a completed
``required.txt`` questionnaire to be reported as 69 missing required controls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self


@dataclass(frozen=True, slots=True)
class ProfileValue:
    value: Any
    source: str
    known_absent: bool = False
    candidates: tuple[str, ...] = ()


@dataclass(slots=True)
class ApplicantProfile:
    values: dict[str, ProfileValue] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, values: dict[str, Any], *, source: str) -> Self:
        return cls({key: ProfileValue(value, source) for key, value in values.items()})

    def merged(self, other: ApplicantProfile) -> Self:
        return type(self)({**self.values, **other.values})

    def get(self, key: str) -> ProfileValue | None:
        return self.values.get(key)

    def answer(self, key: str, default: Any = None) -> Any:
        item = self.get(key)
        return default if item is None or item.known_absent else item.value


def load_answer_file(path: str | Path) -> ApplicantProfile:
    """Read the deliberately tiny ``key=value`` questionnaire format.

    Comma-separated priorities remain ordered candidates.  ``none`` is a known absence,
    not missing data.  Comments and blank lines are ignored.
    """
    values: dict[str, ProfileValue] = {}
    source = str(Path(path))
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = (part.strip() for part in line.split("=", 1))
        lower = raw_value.lower()
        if lower == "none":
            values[key] = ProfileValue(None, source, known_absent=True)
        elif key.endswith("_priority"):
            candidates = tuple(part.strip() for part in raw_value.split(",") if part.strip())
            values[key] = ProfileValue(candidates[0] if candidates else None, source,
                                       candidates=candidates)
        elif lower in {"yes", "true"}:
            values[key] = ProfileValue(True, source)
        elif lower in {"no", "false"}:
            values[key] = ProfileValue(False, source)
        else:
            values[key] = ProfileValue(raw_value, source)
    return ApplicantProfile(values)
