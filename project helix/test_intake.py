import unittest
from intake import build_intake


class IntakeTests(unittest.TestCase):
    def payload(self, **updates):
        return {"city": "Houston, TX", "total_budget_usd": "899999.99", "financing": "cash",
                "business_preferences": "art studio", "open_to_ideas": False, **updates}

    def test_houston_below_threshold_still_includes_sale_and_preserves_budget(self):
        value = build_intake(self.payload())
        self.assertEqual(value["transaction_types"], ["sale", "lease"])
        self.assertEqual(value["total_budget_usd"], 899999.99)
        self.assertIsNone(value["acquisition_allocation"])
        self.assertIsNone(value["lease_horizon_years"])

    def test_threshold_includes_sale_and_lease(self):
        self.assertEqual(build_intake(self.payload(total_budget_usd="900000"))["transaction_types"], ["sale", "lease"])

    def test_open_to_ideas_allows_blank_preferences(self):
        value = build_intake(self.payload(business_preferences="", open_to_ideas=True))
        self.assertEqual(value["business_preferences"], "")
        self.assertTrue(value["open_to_ideas"])

    def test_invalid_inputs_are_rejected(self):
        for update in ({"city": "New York, NY"}, {"total_budget_usd": "0"},
                       {"total_budget_usd": "nope"}, {"financing": "loan"},
                       {"business_preferences": 123}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                build_intake(self.payload(**update))


if __name__ == "__main__":
    unittest.main()
