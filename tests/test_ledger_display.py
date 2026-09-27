"""Ledger-only amounts and display groups must leave imported rows intact."""

import io
import os
import tempfile
import unittest
from unittest.mock import patch

import app as bill_app


class LedgerDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            bill_app, "DB_PATH", os.path.join(self.temp_dir.name, "test.db")
        )
        self.db_patch.start()
        bill_app.init_db()
        self.client = bill_app.app.test_client()
        ledger_response = self.client.post(
            "/api/ledgers", json={"name": "旅行账本", "include_in_summary": True}
        )
        self.ledger_id = ledger_response.get_json()["item"]["id"]
        self.statement = (
            "Date,Amount,Description\n"
            "2026-09-03 12:00:00,-100.00,Hotel\n"
            "2026-09-04 12:00:00,30.00,Refund\n"
        ).encode()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def import_statement(self):
        response = self.client.post(
            "/api/import",
            data={
                "files": (io.BytesIO(self.statement), "travel.csv"),
                "branch": "旅行账本",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["results"][0]

    def source_rows(self):
        conn = bill_app.get_db()
        try:
            return [dict(row) for row in conn.execute(
                "SELECT id, amount, direction, unique_key FROM transactions ORDER BY id"
            )]
        finally:
            conn.close()

    def display(self):
        response = self.client.get(f"/api/ledgers/{self.ledger_id}/display")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_override_group_and_reimport_keep_original_transactions(self):
        self.assertEqual(self.import_statement()["inserted"], 2)
        original = self.source_rows()
        expense_id = next(row["id"] for row in original if row["direction"] == "支出")
        income_id = next(row["id"] for row in original if row["direction"] == "收入")

        override = self.client.post(
            f"/api/transactions/{expense_id}/ledger-amount", json={"amount": "80.00"}
        )
        self.assertEqual(override.status_code, 200)
        self.assertEqual(self.display()["expense"], 80)
        self.assertEqual(self.client.get("/api/summary?month=2026-09").get_json()["total_expense"], 100)
        self.assertEqual(self.source_rows(), original)

        group_response = self.client.post(
            f"/api/ledgers/{self.ledger_id}/display-groups",
            json={"tx_ids": [expense_id, income_id], "title": "酒店退款后"},
        )
        self.assertEqual(group_response.status_code, 200)
        grouped = self.display()
        self.assertEqual(len(grouped["items"]), 1)
        self.assertEqual(grouped["expense"], 50)
        self.assertEqual(grouped["income"], 0)
        self.assertEqual(grouped["items"][0]["amount"], 50)
        self.assertEqual(len(grouped["items"][0]["members"]), 2)
        self.assertEqual(self.source_rows(), original)
        self.assertEqual(self.import_statement()["skipped"], 2)
        self.assertEqual(self.source_rows(), original)
        self.assertEqual(len(self.display()["items"]), 1)
        raw_api_rows = self.client.get(
            "/api/transactions?branch=%E6%97%85%E8%A1%8C%E8%B4%A6%E6%9C%AC"
        ).get_json()["items"]
        self.assertEqual(sorted(row["amount"] for row in raw_api_rows), [30, 100])

        group_id = group_response.get_json()["group_id"]
        self.assertEqual(self.client.delete(f"/api/ledger-display-groups/{group_id}").status_code, 200)
        self.assertEqual(len(self.display()["items"]), 2)
        self.assertEqual(self.client.delete(f"/api/transactions/{expense_id}/ledger-amount").status_code, 200)
        self.assertEqual(self.display()["expense"], 100)

    def test_invalid_ledger_amount_and_group_membership_do_not_mutate_sources(self):
        self.import_statement()
        original = self.source_rows()
        expense_id = next(row["id"] for row in original if row["direction"] == "支出")
        income_id = next(row["id"] for row in original if row["direction"] == "收入")
        for value in ("-1", "1.001", "NaN", "abc"):
            with self.subTest(value=value):
                response = self.client.post(
                    f"/api/transactions/{expense_id}/ledger-amount", json={"amount": value}
                )
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post(
            f"/api/ledgers/{self.ledger_id}/display-groups",
            json={"tx_ids": [expense_id, expense_id]},
        ).status_code, 400)
        self.assertEqual(self.source_rows(), original)

        self.assertEqual(self.client.post(
            f"/api/ledgers/{self.ledger_id}/display-groups",
            json={"tx_ids": [expense_id, income_id]},
        ).status_code, 200)
        self.assertEqual(self.client.post(
            f"/api/transactions/{income_id}/branch", json={"branch": "生活"}
        ).status_code, 200)
        self.assertEqual(len(self.display()["items"]), 1)
        self.assertEqual(self.display()["items"][0]["type"], "transaction")

    def test_three_mixed_transactions_net_and_reimport(self):
        self.statement = (
            "Date,Amount,Description\n"
            "2026-09-03 12:00:00,-100.00,Hotel\n"
            "2026-09-04 12:00:00,-20.00,Fee\n"
            "2026-09-05 12:00:00,30.00,Refund\n"
        ).encode()
        self.assertEqual(self.import_statement()["inserted"], 3)
        original = self.source_rows()
        ids = [row["id"] for row in original]
        self.assertEqual(self.client.post(
            f"/api/ledgers/{self.ledger_id}/display-groups",
            json={"tx_ids": ids[:1]},
        ).status_code, 400)
        self.assertEqual(self.client.post(
            f"/api/ledgers/{self.ledger_id}/display-groups",
            json={"tx_ids": [ids[0], ids[1], ids[0]]},
        ).status_code, 400)
        response = self.client.post(
            f"/api/ledgers/{self.ledger_id}/display-groups",
            json={"tx_ids": ids, "title": "酒店及退款"},
        )
        self.assertEqual(response.status_code, 200)
        grouped = self.display()
        self.assertEqual(len(grouped["items"]), 1)
        self.assertEqual(grouped["expense"], 90)
        self.assertEqual(grouped["income"], 0)
        self.assertEqual(grouped["items"][0]["direction"], "支出")
        self.assertEqual(len(grouped["items"][0]["members"]), 3)
        self.assertEqual(self.source_rows(), original)
        self.assertEqual(self.import_statement()["skipped"], 3)
        self.assertEqual(self.source_rows(), original)
        group_id = response.get_json()["group_id"]
        self.assertEqual(self.client.delete(f"/api/ledger-display-groups/{group_id}").status_code, 200)
        self.assertEqual(len(self.display()["items"]), 3)
        income_id = next(row["id"] for row in original if row["direction"] == "收入")
        self.assertEqual(self.client.post(
            f"/api/transactions/{income_id}/ledger-amount", json={"amount": "150.00"}
        ).status_code, 200)
        self.assertEqual(self.client.post(
            f"/api/ledgers/{self.ledger_id}/display-groups",
            json={"tx_ids": ids},
        ).status_code, 200)
        income_group = self.display()
        self.assertEqual(income_group["expense"], 0)
        self.assertEqual(income_group["income"], 30)
        self.assertEqual(income_group["items"][0]["direction"], "收入")
        self.assertEqual(self.source_rows(), original)


if __name__ == "__main__":
    unittest.main()
