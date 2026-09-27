"""Unrecorded transactions stay recoverable without entering the normal bill."""

import io
import os
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

import app as bill_app


class ExcludedTransactionsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            bill_app, "DB_PATH", os.path.join(self.temp_dir.name, "test.db")
        )
        self.db_patch.start()
        bill_app.init_db()
        self.client = bill_app.app.test_client()

        conn = bill_app.get_db()
        try:
            conn.executemany(
                "INSERT INTO transactions "
                "(source, month, transaction_time, amount, direction, branch, "
                "counterparty, count_in_expense, exclude_reason) "
                "VALUES ('manual', ?, ?, ?, '支出', ?, ?, ?, ?)",
                [
                    ("2026-09", "2026-09-01 12:00:00", 100, "生活", "正常消费", 1, ""),
                    ("2026-09", "2026-09-02 12:00:00", 30, "生活", "手动不记录", 0, "手工不计入"),
                    ("2026-08", "2026-08-01 12:00:00", 20, "副账本", "自动排除", 0, "花呗还款"),
                ],
            )
            conn.commit()
            self.ids = {
                row["counterparty"]: row["id"]
                for row in conn.execute("SELECT id, counterparty FROM transactions")
            }
        finally:
            conn.close()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_regular_bill_export_and_summary_exclude_unrecorded(self):
        normal = self.client.get("/api/transactions").get_json()["items"]
        excluded = self.client.get("/api/transactions?count_in_expense=0").get_json()["items"]
        self.assertEqual([row["counterparty"] for row in normal], ["正常消费"])
        self.assertEqual({row["counterparty"] for row in excluded}, {"手动不记录", "自动排除"})

        summary = self.client.get("/api/summary?month=2026-09").get_json()
        self.assertEqual(summary["total_expense"], 100)
        self.assertEqual(summary["available_months"], ["2026-09"])

        response = self.client.get("/api/export?columns=counterparty,amount")
        self.assertEqual(response.status_code, 200)
        sheet = load_workbook(io.BytesIO(response.data), read_only=True)["明细"]
        self.assertEqual(list(sheet.values), [("金额", "交易对方"), (100, "正常消费")])

    def test_restore_returns_item_to_normal_bill(self):
        tx_id = self.ids["手动不记录"]
        response = self.client.post(
            f"/api/transactions/{tx_id}/count", json={"count_in_expense": True}
        )
        self.assertEqual(response.status_code, 200)
        normal_ids = {row["id"] for row in self.client.get("/api/transactions").get_json()["items"]}
        excluded_ids = {
            row["id"] for row in self.client.get("/api/transactions?count_in_expense=0").get_json()["items"]
        }
        self.assertIn(tx_id, normal_ids)
        self.assertNotIn(tx_id, excluded_ids)
        self.assertEqual(self.client.get("/api/summary?month=2026-09").get_json()["total_expense"], 130)


if __name__ == "__main__":
    unittest.main()
