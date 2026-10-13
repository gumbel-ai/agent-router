import unittest

from router.pricing import DEFAULT_PRICES, estimate


class PricingTests(unittest.TestCase):
    def test_cache_tiers_and_per_request_context_pricing(self):
        short = {"model": "claude-haiku-5-5", "input_tokens": 1000, "output_tokens": 100,
                 "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 3000,
                 "cache_write_5m": 1000, "cache_write_1h": 2000, "speed": "standard", "inference_geo": "global"}
        long = {**short, "cache_read_input_tokens": 100001}
        cost, reason = estimate({"first": short, "second": long}, DEFAULT_PRICES)
        # Each request selects its tier independently; cache writes are not
        # charged again at the uncached input rate.
        expected_short = (1000 * .1 + 100 * .5 + 2000 * .01 + 1000 * .125 + 2000 * .2) / 1_000_000
        expected_long = (1000 * .5 + 100 * 2.5 + 100001 * .05 + 1000 * .625 + 2000 * 1) / 1_000_000
        self.assertAlmostEqual(cost, expected_short + expected_long)
        self.assertIsNone(reason)

    def test_unknown_rates_or_cache_duration_do_not_mean_free(self):
        for message in ({"model": "unknown", "input_tokens": 1, "output_tokens": 1},
                        {"model": "claude-haiku-5-5", "input_tokens": 1, "output_tokens": 1,
                         "cache_creation_input_tokens": 10}):
            with self.subTest(message=message):
                cost, reason = estimate({"id": message}, DEFAULT_PRICES)
                self.assertIsNone(cost)
                self.assertTrue(reason)

    def test_fast_and_region_modifiers_apply_to_all_token_categories(self):
        message = {"model": "claude-opus-5-5", "input_tokens": 1000, "output_tokens": 10,
                   "cache_creation_input_tokens": 1000, "cache_write_5m": 1000, "speed": "fast", "inference_geo": "us"}
        cost, reason = estimate({"id": message}, DEFAULT_PRICES)
        self.assertAlmostEqual(cost, .02024)
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
