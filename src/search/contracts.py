"""Typed, solver-neutral contracts for strategy parameter search.

The optimizer represents values in their encoded form (usually an integer
level).  Decoding into strategy semantics remains the strategy's job.  This
keeps solvers portable without letting them inspect signal or execution code.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


def stable_hash(value: object) -> str:
    """Return a stable SHA-256 hash for JSON-compatible contract metadata."""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ParameterKind(str, Enum):
    CATEGORICAL = "categorical"
    BOOLEAN = "boolean"
    ORDINAL = "ordinal"
    CONTINUOUS = "continuous"
    WEIGHT = "weight"


@dataclass(frozen=True)
class ParameterSpec:
    """One typed search dimension and its legal local moves."""

    name: str
    kind: ParameterKind = ParameterKind.ORDINAL
    values: tuple[object, ...] = ()
    low: float | None = None
    high: float | None = None
    step: float | None = None
    mutation_step: float | None = None
    group: str = "default"
    transfer_key: str | None = None
    active_if: tuple[tuple[str, tuple[object, ...]], ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("parameter name cannot be empty")
        if (
            self.kind
            in {
                ParameterKind.CATEGORICAL,
                ParameterKind.BOOLEAN,
                ParameterKind.ORDINAL,
            }
            and not self.values
        ):
            raise ValueError(f"{self.name}: discrete parameters require values")
        if self.kind in {ParameterKind.CONTINUOUS, ParameterKind.WEIGHT}:
            if self.low is None or self.high is None or self.high < self.low:
                raise ValueError(f"{self.name}: invalid continuous bounds")
            if self.step is not None and self.step <= 0:
                raise ValueError(f"{self.name}: step must be positive")

    def is_active(self, parameters: Mapping[str, object]) -> bool:
        return all(
            parameters.get(parent) in allowed for parent, allowed in self.active_if
        )

    def sample(self, rng: random.Random) -> object:
        if self.kind in {
            ParameterKind.CATEGORICAL,
            ParameterKind.BOOLEAN,
            ParameterKind.ORDINAL,
        }:
            return self.values[rng.randrange(len(self.values))]
        value = rng.uniform(float(self.low), float(self.high))
        return self.coerce(value)

    def coerce(self, value: object) -> object:
        if self.kind in {
            ParameterKind.CATEGORICAL,
            ParameterKind.BOOLEAN,
            ParameterKind.ORDINAL,
        }:
            if value not in self.values:
                raise ValueError(f"{self.name}: illegal value {value!r}")
            return value
        number = min(max(float(value), float(self.low)), float(self.high))
        if self.step:
            offset = round((number - float(self.low)) / self.step)
            number = float(self.low) + offset * self.step
            number = min(max(number, float(self.low)), float(self.high))
        return float(number)

    def neighbor(self, current: object, rng: random.Random) -> object:
        """Return a legal one-dimensional local move."""
        if self.kind == ParameterKind.ORDINAL:
            index = self.values.index(current)
            choices = []
            if index > 0:
                choices.append(self.values[index - 1])
            if index + 1 < len(self.values):
                choices.append(self.values[index + 1])
            return rng.choice(choices) if choices else current
        if self.kind in {ParameterKind.CATEGORICAL, ParameterKind.BOOLEAN}:
            choices = [value for value in self.values if value != current]
            return rng.choice(choices) if choices else current
        move = self.mutation_step or self.step
        if move is None:
            move = max((float(self.high) - float(self.low)) * 0.05, 1e-12)
        choices = [
            self.coerce(float(current) - move),
            self.coerce(float(current) + move),
        ]
        choices = [value for value in choices if value != current]
        return rng.choice(choices) if choices else current

    def local_values(self, current: object, max_levels: int) -> tuple[object, ...]:
        """Return every distinct legal value within the local step limit.

        Ordered and numeric dimensions preserve their natural direction.
        Categorical and boolean dimensions have no ordered distance, so every
        other legal value is considered a one-step local alternative.
        """
        levels = int(max_levels)
        if levels < 1:
            raise ValueError("max_levels must be at least 1")
        existing = self.coerce(current)
        if self.kind == ParameterKind.ORDINAL:
            index = self.values.index(existing)
            alternatives = [
                self.values[target]
                for target in range(
                    max(0, index - levels),
                    min(len(self.values), index + levels + 1),
                )
                if target != index
            ]
        elif self.kind in {ParameterKind.CATEGORICAL, ParameterKind.BOOLEAN}:
            alternatives = [value for value in self.values if value != existing]
        else:
            move = self.mutation_step or self.step
            if move is None:
                move = max(
                    (float(self.high) - float(self.low)) * 0.05,
                    1e-12,
                )
            alternatives = []
            for offset in range(-levels, levels + 1):
                if offset == 0:
                    continue
                value = self.coerce(float(existing) + offset * move)
                if value != existing:
                    alternatives.append(value)

        unique = []
        seen = set()
        for value in alternatives:
            key = repr(value)
            if key in seen:
                continue
            seen.add(key)
            unique.append(value)
        return tuple(unique)

    def to_contract(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "values": list(self.values),
            "low": self.low,
            "high": self.high,
            "step": self.step,
            "mutation_step": self.mutation_step,
            "group": self.group,
            "transfer_key": self.transfer_key,
            "active_if": [
                {"parameter": parent, "values": list(values)}
                for parent, values in self.active_if
            ],
        }


@dataclass(frozen=True)
class ParameterSchema:
    """A typed parameter space shared by strategies and explicit evaluation."""

    parameters: tuple[ParameterSpec, ...]
    schema_id: str = "parameter-schema/1"

    def __post_init__(self) -> None:
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("parameter names must be unique")
        known: set[str] = set()
        for parameter in self.parameters:
            for parent, _values in parameter.active_if:
                if parent not in known:
                    raise ValueError(
                        f"{parameter.name}: conditional parent {parent!r} "
                        "must precede it"
                    )
            known.add(parameter.name)

    @classmethod
    def from_param_space(cls, param_space: object) -> ParameterSchema:
        """Adapt the historical ``ParamDim`` contract without strategy branches."""
        parameters = []
        for dim in getattr(param_space, "dims", ()):  # pragma: no branch
            levels = max(1, int(dim.levels))
            parameters.append(
                ParameterSpec(
                    name=str(dim.name),
                    kind=ParameterKind.ORDINAL,
                    values=tuple(range(levels)),
                    low=float(getattr(dim, "lo", 0.0)),
                    high=float(getattr(dim, "hi", levels - 1)),
                    mutation_step=1.0,
                    transfer_key=str(dim.name),
                )
            )
        return cls(tuple(parameters), schema_id="adapted-param-space/1")

    @property
    def hash(self) -> str:
        return stable_hash(self.to_contract())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(parameter.name for parameter in self.parameters)

    def to_contract(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "parameters": [parameter.to_contract() for parameter in self.parameters],
        }

    def validate(self, values: Mapping[str, object]) -> dict[str, object]:
        unknown = set(values) - set(self.names)
        if unknown:
            raise ValueError(f"unknown parameters: {sorted(unknown)}")
        normalized: dict[str, object] = {}
        for parameter in self.parameters:
            if parameter.name not in values:
                raise ValueError(f"missing parameter: {parameter.name}")
            value = values[parameter.name]
            if parameter.is_active({**values, **normalized}):
                normalized[parameter.name] = parameter.coerce(value)
            else:
                # Inactive values are canonicalized so caches and archives do
                # not treat semantically identical candidates as different.
                normalized[parameter.name] = (
                    parameter.values[0] if parameter.values else parameter.low
                )
        return normalized

    def sample(self, rng: random.Random) -> dict[str, object]:
        values: dict[str, object] = {}
        for parameter in self.parameters:
            if parameter.is_active(values):
                values[parameter.name] = parameter.sample(rng)
            else:
                values[parameter.name] = (
                    parameter.values[0] if parameter.values else parameter.low
                )
        return values

    def neighbor(
        self, values: Mapping[str, object], rng: random.Random
    ) -> dict[str, object]:
        current = self.validate(values)
        movable = [
            parameter
            for parameter in self.parameters
            if parameter.is_active(current)
            and (
                len(parameter.values) > 1
                or (
                    parameter.kind in {ParameterKind.CONTINUOUS, ParameterKind.WEIGHT}
                    and float(parameter.high) > float(parameter.low)
                )
            )
        ]
        if not movable:
            return current
        parameter = rng.choice(movable)
        current[parameter.name] = parameter.neighbor(current[parameter.name], rng)
        return self.validate(current)

    def local_perturbations(
        self, values: Mapping[str, object]
    ) -> list[dict[str, object]]:
        """Return deterministic one-parameter local moves for robustness tests."""
        current = self.validate(values)
        result = []
        seen = set()
        for parameter in self.parameters:
            if not parameter.is_active(current):
                continue
            existing = current[parameter.name]
            if parameter.kind == ParameterKind.ORDINAL:
                index = parameter.values.index(existing)
                alternatives = []
                if index > 0:
                    alternatives.append(parameter.values[index - 1])
                if index + 1 < len(parameter.values):
                    alternatives.append(parameter.values[index + 1])
            elif parameter.kind in {
                ParameterKind.CATEGORICAL,
                ParameterKind.BOOLEAN,
            }:
                alternatives = [
                    value for value in parameter.values if value != existing
                ]
            else:
                move = parameter.mutation_step or parameter.step
                if move is None:
                    move = max(
                        (float(parameter.high) - float(parameter.low)) * 0.05,
                        1e-12,
                    )
                alternatives = [
                    parameter.coerce(float(existing) - move),
                    parameter.coerce(float(existing) + move),
                ]
            for alternative in alternatives:
                changed = dict(current)
                changed[parameter.name] = alternative
                normalized = self.validate(changed)
                key = tuple(normalized[name] for name in self.names)
                if normalized != current and key not in seen:
                    seen.add(key)
                    result.append(normalized)
        return result
