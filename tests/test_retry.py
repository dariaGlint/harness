from __future__ import annotations

import unittest

from production_harness.retry import RetryPolicy


class RetryPolicyTests(unittest.TestCase):
    def test_shrinks_by_half_and_respects_minimum(self) -> None:
        policy = RetryPolicy(max_attempts=4, shrink_factor=0.5, minimum_chunk_size=1)
        self.assertEqual(policy.next_chunk_size(16), 8)
        self.assertEqual(policy.next_chunk_size(2), 1)
        self.assertEqual(policy.next_chunk_size(1), 1)

    def test_retry_bound(self) -> None:
        policy = RetryPolicy(max_attempts=2)
        self.assertTrue(policy.can_retry(0))
        self.assertTrue(policy.can_retry(1))
        self.assertFalse(policy.can_retry(2))


if __name__ == "__main__":
    unittest.main()
