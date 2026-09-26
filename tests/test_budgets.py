"""Monthly budgets use the same eligible expense records as the summary."""

import os
import tempfile
import unittest
from unittest.mock import patch

import app as bill_app


class BudgetApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            bill_app, "DB_PATH", os.path.join(self.temp_dir.name, "test.db")
        )
        self.db_patch.start()
        bill_app.init_db()
        self.client = bill_app.app.test_client()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_budget_tracks_only_counted_expenses(self):
        conn = bill_app.get_db()
        try:
            records = [
                (100, "支出", "生活", 0, 1),
                (30, "支出", "生活", 0, 0),
                (25, "支出", "生活", 1, 1),
                (1000, "收入", "生活", 0, 1),
                (20, "支出", "副账本", 0, 1),
            ]
            conn.executemany(
                "INSERT INTO transactions "
                "(source, month, amount, direction, branch, is_duplicate, count_in_expense) "
                "VALUES ('manual', '2026-09', ?, ?, ?, ?, ?)",
                records,
            )
            conn.commit()
        finally:
            conn.close()

        response = self.client.put("/api/budgets/2026-09", json={"amount": "120.50"})
        self.assertEqual(response.status_code, 200)
        summary = self.client.get("/api/summary?month=2026-09").get_json()
        self.assertEqual(summary["budget"], {
            "amount": 120.5, "spent": 100.0, "remaining": 20.5, "percent": 83.0
        })
        self.assertEqual([item["amount"] for item in summary["top_expenses"]], [100.0])
        self.assertIn("2026-09", summary["available_months"])

        response = self.client.put("/api/budgets/2026-09", json={"amount": 80})
        self.assertEqual(response.status_code, 200)
        summary = self.client.get("/api/summary?month=2026-09").get_json()
        self.assertEqual(summary["budget"]["remaining"], -20.0)
        self.assertEqual(summary["budget"]["percent"], 125.0)

        response = self.client.delete("/api/budgets/2026-09")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.client.get("/api/summary?month=2026-09").get_json()["budget"])

    def test_invalid_budget_never_changes_saved_amount(self):
        self.client.put("/api/budgets/2026-09", json={"amount": 50})
        for value in ("0", "-1", "1.001", "NaN", "Infinity", "abc", "1000000000.01"):
            with self.subTest(value=value):
                response = self.client.put("/api/budgets/2026-09", json={"amount": value})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self.client.put("/api/budgets/2026-13", json={"amount": 50}).status_code,
            400,
        )
        summary = self.client.get("/api/summary?month=2026-09").get_json()
        self.assertEqual(summary["budget"]["amount"], 50.0)
        self.assertIn("2026-09", summary["available_months"])


if __name__ == "__main__":
    unittest.main()
