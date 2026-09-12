import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from sentinel_py.rendering import (  # noqa: E402
    FractionRenderError,
    render_canonical_decimal,
)


class CanonicalDecimalTests(unittest.TestCase):
    def test_renders_exact_half_even_vectors(self):
        cases = (
            (17, 4, "4.25"),
            (123, 10_000, "0.0123"),
            (1, 3, "0.333333333333"),
            (2, 3, "0.666666666667"),
            (246_913_578_025, 2_000_000_000_000, "0.123456789012"),
            (246_913_578_027, 2_000_000_000_000, "0.123456789014"),
            (1_999_999_999_999, 2_000_000_000_000, "1"),
            (0, 7, "0"),
        )

        for numerator, denominator, expected in cases:
            with self.subTest(numerator=numerator, denominator=denominator):
                self.assertEqual(
                    expected,
                    render_canonical_decimal(numerator, denominator),
                )

    def test_rejects_non_exact_or_negative_inputs(self):
        invalid = (
            (True, 1, "numeratorInvalid"),
            (-1, 1, "numeratorInvalid"),
            (1.0, 1, "numeratorInvalid"),
            (1, False, "denominatorInvalid"),
            (1, 0, "denominatorInvalid"),
            (1, None, "denominatorInvalid"),
            (1, 1.0, "denominatorInvalid"),
        )

        for numerator, denominator, expected in invalid:
            with self.subTest(numerator=numerator, denominator=denominator):
                with self.assertRaises(FractionRenderError) as raised:
                    render_canonical_decimal(numerator, denominator)
                self.assertEqual(expected, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
