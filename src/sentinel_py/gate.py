"""Gate thresholds: the CRAP upper bound and the minimum mutation kill rate.

The text contract is shared with SENTINEL_SPEC golden/gate/threshold-v1.json:
a decimal string with at most two fractional places, read as an exact Fraction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction


THRESHOLD_PATTERN = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$")
DEFAULT_CRAP_MAX = "8"
DEFAULT_MUTATION_MIN = "90"


class GateInputError(ValueError):
    """A threshold string violates the shared gate contract."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _parse(text: object, field_name: str) -> Fraction:
    if not isinstance(text, str) or not THRESHOLD_PATTERN.fullmatch(text):
        raise GateInputError(f"{field_name}Invalid")
    return Fraction(text)


def parse_crap_max(text: object) -> Fraction:
    value = _parse(text, "crapMax")
    if value <= 0:
        raise GateInputError("crapMaxOutOfRange")
    return value


def parse_mutation_min(text: object) -> Fraction:
    value = _parse(text, "mutationMin")
    if value > 100:
        raise GateInputError("mutationMinOutOfRange")
    return value


@dataclass(frozen=True)
class GateThresholds:
    crap_max: Fraction
    mutation_min: Fraction

    def as_json(self) -> dict:
        return {"crapMax": _text(self.crap_max), "mutationMin": _text(self.mutation_min)}


def _text(value: Fraction) -> str:
    """Render the threshold back as the shortest exact decimal (at most two places)."""

    scaled = value * 100
    integer, remainder = divmod(scaled.numerator, scaled.denominator)
    if remainder:
        raise GateInputError("thresholdNotExact")
    whole, cents = divmod(integer, 100)
    if cents == 0:
        return str(whole)
    return f"{whole}.{cents:02d}".rstrip("0")


DEFAULT_GATE = GateThresholds(parse_crap_max(DEFAULT_CRAP_MAX), parse_mutation_min(DEFAULT_MUTATION_MIN))


def load_gate(crap_max: str | None, mutation_min: str | None) -> GateThresholds:
    """Build thresholds from optional command-line text; None keeps the default."""

    return GateThresholds(
        DEFAULT_GATE.crap_max if crap_max is None else parse_crap_max(crap_max),
        DEFAULT_GATE.mutation_min if mutation_min is None else parse_mutation_min(mutation_min),
    )


def crap_passes(numerator: int, denominator: int, crap_max: Fraction) -> bool:
    return numerator * crap_max.denominator <= crap_max.numerator * denominator


def kill_rate_passes(killed: int, in_scope: int, mutation_min: Fraction) -> bool:
    return killed * 100 * mutation_min.denominator >= mutation_min.numerator * in_scope
