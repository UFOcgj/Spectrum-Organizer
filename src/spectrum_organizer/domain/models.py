from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json


class SampleValidationError(ValueError):
    pass


OXYGEN_ENVIRONMENTS = ("Air", "DeO2")


class SpectrumClass(Enum):
    STEADY_EMISSION = "steady_emission"
    STEADY_EXCITATION = "steady_excitation"
    STEADY_2D = "steady_2d"
    DELAYED_EMISSION = "delayed_emission"
    DELAYED_EXCITATION = "delayed_excitation"
    DELAYED_2D = "delayed_2d"
    DELAY_TIME_SERIES = "delay_time_series"


@dataclass(frozen=True)
class LiquidSample:
    sample: str
    solvent: str
    concentration: str
    temperature: str

    def __post_init__(self) -> None:
        _trim_fields(self, "sample", "solvent", "concentration", "temperature")
        _require_fields(self.sample, self.solvent, self.concentration, self.temperature)

    @property
    def sample_type(self) -> str:
        return "liquid"

    @property
    def system_label(self) -> str:
        return f"{self.sample}-{self.solvent}-{self.concentration}"

    @property
    def canonical_label(self) -> str:
        return f"{self.system_label}-{self.temperature}"

    def identity_json(self) -> str:
        return _identity_json(
            self.sample_type,
            sample=self.sample,
            solvent=self.solvent,
            concentration=self.concentration,
            temperature=self.temperature,
        )

    def system_identity_json(self) -> str:
        return _identity_json(
            self.sample_type,
            sample=self.sample,
            solvent=self.solvent,
            concentration=self.concentration,
        )


@dataclass(frozen=True)
class NeatSample:
    sample: str
    state: str
    temperature: str
    oxygen_environment: str = ""

    def __post_init__(self) -> None:
        _trim_fields(self, "sample", "state", "temperature", "oxygen_environment")
        _require_fields(self.sample, self.state, self.temperature)
        object.__setattr__(
            self,
            "oxygen_environment",
            canonicalize_oxygen_environment(self.oxygen_environment, allow_blank=True),
        )

    @property
    def sample_type(self) -> str:
        return "neat"

    @property
    def system_label(self) -> str:
        label = f"{self.sample}-{self.state}"
        return f"{label}-{self.oxygen_environment}" if self.oxygen_environment else label

    @property
    def canonical_label(self) -> str:
        return f"{self.system_label}-{self.temperature}"

    def identity_json(self) -> str:
        return _identity_json(
            self.sample_type,
            sample=self.sample,
            state=self.state,
            temperature=self.temperature,
            oxygen_environment=self.oxygen_environment,
        )

    def system_identity_json(self) -> str:
        return _identity_json(
            self.sample_type,
            sample=self.sample,
            state=self.state,
            oxygen_environment=self.oxygen_environment,
        )


@dataclass(frozen=True)
class DopedSample:
    guest: str
    host: str
    concentration: str
    state: str
    temperature: str
    oxygen_environment: str = ""

    def __post_init__(self) -> None:
        _trim_fields(
            self,
            "guest",
            "host",
            "concentration",
            "state",
            "temperature",
            "oxygen_environment",
        )
        _require_fields(self.guest, self.host, self.concentration, self.state, self.temperature)
        object.__setattr__(
            self,
            "oxygen_environment",
            canonicalize_oxygen_environment(self.oxygen_environment, allow_blank=True),
        )

    @property
    def sample_type(self) -> str:
        return "doped"

    @property
    def system_label(self) -> str:
        label = f"{self.guest}-in-{self.host}-{self.concentration}-{self.state}"
        return f"{label}-{self.oxygen_environment}" if self.oxygen_environment else label

    @property
    def canonical_label(self) -> str:
        return f"{self.system_label}-{self.temperature}"

    def identity_json(self) -> str:
        return _identity_json(
            self.sample_type,
            guest=self.guest,
            host=self.host,
            concentration=self.concentration,
            state=self.state,
            temperature=self.temperature,
            oxygen_environment=self.oxygen_environment,
        )

    def system_identity_json(self) -> str:
        return _identity_json(
            self.sample_type,
            guest=self.guest,
            host=self.host,
            concentration=self.concentration,
            state=self.state,
            oxygen_environment=self.oxygen_environment,
        )


def canonicalize_oxygen_environment(value: str, *, allow_blank: bool = False) -> str:
    text = "" if value is None else str(value).strip()
    if not text and allow_blank:
        return ""
    canonical = {item.casefold(): item for item in OXYGEN_ENVIRONMENTS}.get(text.casefold())
    if canonical is None:
        raise SampleValidationError("oxygen_environment must be Air or DeO2")
    return canonical


def _trim_fields(instance, *names: str) -> None:
    for name in names:
        value = getattr(instance, name)
        if value is not None:
            object.__setattr__(instance, name, str(value).strip())


def _require_fields(*values: str) -> None:
    if any(value is None or str(value).strip() == "" for value in values):
        raise SampleValidationError("All sample fields are required")


def _identity_json(sample_type: str, **fields: str) -> str:
    payload = {"type": sample_type, **fields}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
