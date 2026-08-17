"""Independent version identifiers used to make every run traceable."""

from __future__ import annotations

import re
from dataclasses import dataclass

_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_version(value: str, *, field_name: str) -> None:
    if not _VERSION_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} is not a valid version identifier: {value!r}")


@dataclass(frozen=True)
class VersionBundle:
    """Versions that uniquely identify model inputs and implementation."""

    software_version: str
    model_version: str
    parameter_set_version: str
    config_version: str
    case_version: str
    scenario_version: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in self.as_dict().items():
            if value is not None:
                _validate_version(value, field_name=field_name)

    def as_dict(self) -> dict[str, str | None]:
        return {
            "software_version": self.software_version,
            "model_version": self.model_version,
            "parameter_set_version": self.parameter_set_version,
            "config_version": self.config_version,
            "case_version": self.case_version,
            "scenario_version": self.scenario_version,
        }
