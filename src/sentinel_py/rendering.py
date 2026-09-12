"""Exact, locale-independent rendering for nonnegative fractions."""

from __future__ import annotations


DECIMAL_PLACES = 12
DECIMAL_SCALE = 10**DECIMAL_PLACES


class FractionRenderError(ValueError):
    """Raised when an exact fraction cannot be rendered safely."""


def render_canonical_decimal(numerator: int, denominator: int) -> str:
    _validate_fraction(numerator, denominator)
    scaled, remainder = divmod(numerator * DECIMAL_SCALE, denominator)
    comparison = remainder * 2 - denominator
    if comparison > 0 or (comparison == 0 and scaled % 2 == 1):
        scaled += 1
    integer, fractional = divmod(scaled, DECIMAL_SCALE)
    if fractional == 0:
        return str(integer)
    width = DECIMAL_PLACES
    for _ in range(DECIMAL_PLACES):
        quotient, trailing_digit = divmod(fractional, 10)
        if trailing_digit != 0:
            break
        fractional = quotient
        width -= 1
    return f"{integer}.{fractional:0{width}d}"


def _validate_fraction(numerator: int, denominator: int) -> None:
    invalid_numerator = not isinstance(numerator, int) or isinstance(numerator, bool)
    invalid_denominator = not isinstance(denominator, int) or isinstance(denominator, bool)
    if invalid_numerator or numerator < 0:
        raise FractionRenderError("numeratorInvalid")
    if invalid_denominator or denominator <= 0:
        raise FractionRenderError("denominatorInvalid")
