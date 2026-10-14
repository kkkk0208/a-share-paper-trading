"""Make sure real-sample validation reports skipped comparisons honestly."""

from __future__ import annotations

import unittest

import tests.test_real_quote_samples as validation
from tests.test_quote_layer import STOCK, at, sina_body, tencent_body


def pair(tencent_time, sina_time, sina_last="10.000", label="continuous") -> list[dict]:
    shared = {"symbols": [STOCK], "captured": at(16, 40), "phase": label, "label": label}
    return [
        shared | {"file": f"sample_{label}_tencent.txt", "source": "tencent",
                  "text": f'v_sz002195="{tencent_body("002195", tencent_time)}";'},
        shared | {"file": f"sample_{label}_sina.txt", "source": "sina",
                  "text": f'var hq_str_sz002195="{sina_body(sina_time, last=sina_last)}";'},
    ]


def validate(samples) -> unittest.TestResult:
    case = validation.RealQuoteSampleTests("test_paired_captures_agree_between_sources")
    case.setUp = lambda: setattr(case, "samples", samples)
    result = unittest.TestResult()
    case.run(result)
    return result


class PairValidationTests(unittest.TestCase):
    def test_after_close_timestamp_mismatch_is_a_skip_not_a_pass(self):
        result = validate(pair(at(16, 15), at(15, 35), label="after_close"))
        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("timestamps", result.skipped[0][1])
        self.assertIn("10", result.skipped[0][1])

    def test_continuous_pair_with_near_timestamps_is_compared(self):
        result = validate(pair(at(10, 0, 3), at(10, 0, 4)))
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])
        # A large mismatch in the same eligible pair must really fail.
        mismatch = validate(pair(at(10, 0, 3), at(10, 0, 4), sina_last="11.000"))
        self.assertEqual(mismatch.skipped, [])
        self.assertEqual(mismatch.errors, [])
        self.assertEqual(len(mismatch.failures), 1)

    def test_old_pairs_do_not_prevent_comparison_of_a_usable_intraday_pair(self):
        result = validate(pair(at(16, 15), at(15, 35), label="after_close") +
                          pair(at(10, 0, 3), at(10, 0, 4)))
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])


if __name__ == "__main__":
    unittest.main()
