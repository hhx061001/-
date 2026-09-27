"""Multiple ledger views must share source rows without double counting them."""

import os
import tempfile
import unittest
from unittest.mock import patch

import app as bill_app


class MultiLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            bill_app, "DB_PATH", os.path.join(self.temp_dir.name, "test.db")
        )
        self.db_patch.start()
        bill_app.init_db()
        self.client = bill_app.app.test_client()
        self.dy = self.client.post("/api/ledgers", json={"name": "dy"}).get_json()["item"]["id"]
        self.work = self.client.post("/api/ledgers", json={"name": "长期投入"}).get_json()["item"]["id"]

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def add(self, branch="生活", amount="100", direction="支出", month="09", category="吃饭"):
        response = self.client.post("/api/manual/add", json={
            "branch": branch, "amount": amount, "direction": direction,
            "category": category, "transaction_time": f"2026-{month}-10 12:00:00",
            "counterparty": "验证交易",
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        conn = bill_app.get_db()
        try:
            return conn.execute("SELECT MAX(id) FROM transactions").fetchone()[0]
        finally:
            conn.close()

    def row(self, tx_id):
        conn = bill_app.get_db()
        try:
            return dict(conn.execute(
                "SELECT id, amount, unique_key, category, branch, tags FROM transactions WHERE id=?",
                (tx_id,),
            ).fetchone())
        finally:
            conn.close()

    def combined(self, ledger_ids, month="2026-09"):
        response = self.client.post("/api/reports/combined", json={
            "month": month, "ledger_ids": ledger_ids,
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_auto_and_manual_membership_single_tag_and_amount_priority(self):
        tx_id = self.add()
        original = self.row(tx_id)
        response = self.client.put("/api/category-ledger-rules", json={
            "category": "吃饭", "ledger_ids": [self.dy, self.work],
        })
        self.assertEqual(response.status_code, 200)
        detail = self.client.get("/api/transactions?branch=%E7%94%9F%E6%B4%BB").get_json()["items"]
        self.assertEqual(detail[0]["ledger_ids"], [0, self.dy, self.work])

        self.assertEqual(self.client.post(f"/api/transactions/{tx_id}/ledgers", json={
            "ledger_ids": [0, self.dy],
        }).status_code, 200)
        self.assertEqual(self.client.post(f"/api/transactions/{tx_id}/category", json={
            "category": "玩乐", "remember": False,
        }).status_code, 200)
        self.assertEqual(self.client.post(f"/api/transactions/{tx_id}/category", json={
            "category": "吃饭", "remember": False,
        }).status_code, 200)
        self.assertEqual(self.client.get(f"/api/ledgers/{self.work}/display").get_json()["items"], [])
        self.assertEqual(self.client.post(f"/api/transactions/{tx_id}/tags", json={
            "tags": ["个人", "长期"],
        }).status_code, 400)
        self.assertEqual(self.client.post(f"/api/transactions/{tx_id}/tags", json={
            "tags": ["个人"],
        }).status_code, 200)

        self.assertEqual(self.client.post(f"/api/transactions/{tx_id}/ledgers", json={
            "ledger_ids": [0, self.dy, self.work],
        }).status_code, 200)
        for ledger_id, amount in ((self.dy, "80"), (self.work, "50")):
            self.assertEqual(self.client.post(f"/api/transactions/{tx_id}/ledger-amount", json={
                "ledger_id": ledger_id, "amount": amount,
            }).status_code, 200)
        self.assertEqual(self.combined([0, self.dy])["expense"], 80)
        report = self.combined([0, self.dy, self.work])
        self.assertEqual((report["expense"], report["count"]), (50, 1))
        self.assertEqual(report["items"][0]["amount_source"], "长期投入")
        self.assertEqual(self.combined([self.work, 0])["expense"], 100)
        month = self.client.get("/api/monthly-ledgers?month=2026-09").get_json()["items"]
        self.assertEqual({item["ledger_id"]: item["expense"] for item in month},
                         {0: 100, self.dy: 80, self.work: 50})
        self.assertEqual(self.row(tx_id)["amount"], original["amount"])
        self.assertEqual(self.row(tx_id)["unique_key"], original["unique_key"])

    def test_monthly_and_total_views_keep_cross_month_sources_separate(self):
        self.add(branch="dy", amount="100", month="09")
        self.add(branch="dy", amount="30", direction="收入", month="10")
        total = self.client.get(f"/api/ledgers/{self.dy}/display").get_json()
        september = self.client.get(f"/api/ledgers/{self.dy}/display?month=2026-09").get_json()
        october = self.client.get(f"/api/ledgers/{self.dy}/display?month=2026-10").get_json()
        self.assertEqual((total["expense"], total["income"]), (100, 30))
        self.assertEqual((september["expense"], september["income"]), (100, 0))
        self.assertEqual((october["expense"], october["income"]), (0, 30))
        self.assertEqual(self.combined([0, self.dy], "2026-09")["net_expense"], 100)
        self.assertEqual(self.combined([0, self.dy], "2026-10")["net_expense"], -30)

    def test_detail_views_include_all_editable_ledger_memberships(self):
        first = self.add(branch="dy", amount="100")
        second = self.add(branch="dy", amount="30")
        for tx_id in (first, second):
            response = self.client.post(f"/api/transactions/{tx_id}/ledgers", json={
                "ledger_ids": [0, self.dy, self.work],
            })
            self.assertEqual(response.status_code, 200, response.get_json())
        display = self.client.get(f"/api/ledgers/{self.dy}/display").get_json()["items"]
        self.assertEqual(display[0]["ledger_ids"], [0, self.dy, self.work])
        group = self.client.post(f"/api/ledgers/{self.dy}/display-groups", json={
            "tx_ids": [first, second],
        })
        self.assertEqual(group.status_code, 200, group.get_json())
        grouped = self.client.get(f"/api/ledgers/{self.dy}/display").get_json()["items"][0]
        self.assertEqual([item["ledger_ids"] for item in grouped["members"]],
                         [[0, self.dy, self.work], [0, self.dy, self.work]])
        combined = self.combined([0])
        self.assertEqual([item["ledger_ids"] for item in combined["items"]],
                         [[0, self.dy, self.work], [0, self.dy, self.work]])

    def test_category_rule_applies_to_future_transactions(self):
        self.assertEqual(self.client.put("/api/category-ledger-rules", json={
            "category": "吃饭", "ledger_ids": [self.dy, self.work],
        }).status_code, 200)
        tx_id = self.add(amount="23")
        detail = self.client.get("/api/transactions?branch=%E7%94%9F%E6%B4%BB").get_json()["items"]
        self.assertEqual(detail[0]["ledger_ids"], [0, self.dy, self.work])
        self.assertEqual(self.combined([0, self.dy, self.work])["count"], 1)
        self.assertEqual(self.row(tx_id)["amount"], 23)

    def test_category_can_leave_main_and_enter_two_independent_ledgers(self):
        existing = self.add(category="吃饭")
        source = self.row(existing)
        response = self.client.put("/api/category-ledger-rules", json={
            "category": "吃饭", "main_policy": "exclude",
            "ledger_ids": [self.dy, self.work],
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        future = self.add(amount="25", category="吃饭")
        self.assertEqual(self.client.get("/api/transactions?branch=%E7%94%9F%E6%B4%BB").get_json()["items"], [])
        for ledger_id in (self.dy, self.work):
            items = self.client.get(f"/api/ledgers/{ledger_id}/display").get_json()["items"]
            self.assertEqual({item["id"] for item in items}, {existing, future})
        self.assertEqual(self.combined([0, self.dy, self.work])["count"], 2)
        self.assertEqual(self.row(existing)["branch"], source["branch"])
        self.assertEqual(self.row(existing)["unique_key"], source["unique_key"])
        self.assertEqual(self.client.post(f"/api/transactions/{existing}/ledgers", json={
            "ledger_ids": [0, self.dy, self.work],
        }).status_code, 200)
        self.assertEqual([item["id"] for item in self.client.get(
            "/api/transactions?branch=%E7%94%9F%E6%B4%BB").get_json()["items"]], [existing])

    def test_category_can_keep_main_and_two_independent_ledgers(self):
        tx_id = self.add(branch="dy", category="购物")
        response = self.client.put("/api/category-ledger-rules", json={
            "category": "吃饭", "main_policy": "include",
            "ledger_ids": [self.dy, self.work],
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.client.post(f"/api/transactions/{tx_id}/category", json={
            "category": "吃饭", "remember": False,
        }).status_code, 200)
        detail = self.client.get("/api/transactions?branch=%E7%94%9F%E6%B4%BB").get_json()["items"]
        self.assertEqual(detail[0]["ledger_ids"], [0, self.dy, self.work])
        self.assertEqual(self.combined([0, self.dy, self.work])["count"], 1)

    def test_ledger_bound_categories_preserve_other_ledgers_and_main_policy(self):
        existing = self.add(category="吃饭")
        source = self.row(existing)
        self.assertEqual(self.client.put("/api/category-ledger-rules", json={
            "category": "吃饭", "main_policy": "exclude", "ledger_ids": [self.work],
        }).status_code, 200)
        url = f"/api/ledgers/{self.dy}/categories"
        response = self.client.put(url, json={"categories": ["吃饭", "玩乐"]})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.client.get(url).get_json()["categories"], ["吃饭", "玩乐"])
        rules = self.client.get("/api/category-ledger-rules").get_json()
        self.assertEqual(rules["rules"]["吃饭"], [self.dy, self.work])
        self.assertFalse(rules["main_rules"]["吃饭"])
        future = self.add(category="吃饭")
        self.assertEqual({item["id"] for item in self.client.get(
            f"/api/ledgers/{self.dy}/display").get_json()["items"]}, {existing, future})

        self.assertEqual(self.client.put(url, json={"categories": ["玩乐"]}).status_code, 200)
        self.assertEqual(self.client.get(url).get_json()["categories"], ["玩乐"])
        rules = self.client.get("/api/category-ledger-rules").get_json()
        self.assertEqual(rules["rules"]["吃饭"], [self.work])
        self.assertFalse(rules["main_rules"]["吃饭"])
        self.assertEqual(self.client.get(f"/api/ledgers/{self.dy}/display").get_json()["items"], [])
        self.assertEqual({item["id"] for item in self.client.get(
            f"/api/ledgers/{self.work}/display").get_json()["items"]}, {existing, future})
        self.assertEqual(self.row(existing)["unique_key"], source["unique_key"])
        self.assertEqual(self.client.put(url, json={"categories": ["玩乐", "玩乐"]}).status_code, 400)
        self.assertEqual(self.client.get(url).get_json()["categories"], ["玩乐"])

    def test_new_category_can_choose_related_ledgers_and_apply_to_transaction(self):
        tx_id = self.add(category="吃饭")
        original = self.row(tx_id)
        response = self.client.post("/api/categories", json={
            "name": "聚餐", "ledger_ids": [self.dy, self.work],
            "main_policy": "exclude",
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.client.post(f"/api/transactions/{tx_id}/category", json={
            "category": "聚餐", "remember": False,
        }).status_code, 200)
        rules = self.client.get("/api/category-ledger-rules").get_json()
        self.assertEqual(rules["rules"]["聚餐"], [self.dy, self.work])
        self.assertFalse(rules["main_rules"]["聚餐"])
        self.assertEqual(self.client.get("/api/transactions?branch=%E7%94%9F%E6%B4%BB").get_json()["items"], [])
        for ledger_id in (self.dy, self.work):
            self.assertEqual([item["id"] for item in self.client.get(
                f"/api/ledgers/{ledger_id}/display").get_json()["items"]], [tx_id])
        future = self.add(category="聚餐")
        self.assertEqual({item["id"] for item in self.client.get(
            f"/api/ledgers/{self.dy}/display").get_json()["items"]}, {tx_id, future})
        self.assertEqual(self.row(tx_id)["unique_key"], original["unique_key"])
        self.assertEqual(self.client.post("/api/categories", json={
            "name": "聚餐", "ledger_ids": [self.dy], "main_policy": "include",
        }).status_code, 409)
        self.assertEqual(self.client.post("/api/categories", json={
            "name": "无效绑定", "ledger_ids": [999999], "main_policy": "include",
        }).status_code, 400)
        names = [item["name"] for item in self.client.get("/api/categories").get_json()["custom"]]
        self.assertNotIn("无效绑定", names)

    def test_unassigned_category_remains_findable_and_can_be_reassigned(self):
        tx_id = self.add(category="吃饭")
        self.assertEqual(self.client.put("/api/category-ledger-rules", json={
            "category": "吃饭", "main_policy": "exclude", "ledger_ids": [],
        }).status_code, 200)
        unassigned = self.client.get("/api/transactions?branch=__unassigned__").get_json()["items"]
        self.assertEqual([item["id"] for item in unassigned], [tx_id])
        self.assertEqual(self.client.get("/api/transactions?branch=__all__").get_json()["items"][0]["id"], tx_id)
        self.assertEqual(self.client.post(f"/api/transactions/{tx_id}/ledgers", json={
            "ledger_ids": [self.dy],
        }).status_code, 200)
        self.assertEqual(self.client.get("/api/transactions?branch=__unassigned__").get_json()["items"], [])
        self.assertEqual(self.client.get(f"/api/ledgers/{self.dy}/display").get_json()["items"][0]["id"], tx_id)

    def test_display_groups_are_independent_in_each_ledger(self):
        first = self.add(amount="100")
        second = self.add(amount="30", direction="收入")
        self.assertEqual(self.client.put("/api/category-ledger-rules", json={
            "category": "吃饭", "ledger_ids": [self.dy, self.work],
        }).status_code, 200)
        for ledger_id in (self.dy, self.work):
            response = self.client.post(f"/api/ledgers/{ledger_id}/display-groups", json={
                "tx_ids": [first, second],
            })
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(len(self.client.get(
                f"/api/ledgers/{ledger_id}/display").get_json()["items"]), 1)
        self.assertEqual(self.client.post(f"/api/transactions/{first}/category", json={
            "category": "玩乐", "remember": False,
        }).status_code, 200)
        for ledger_id in (self.dy, self.work):
            items = self.client.get(f"/api/ledgers/{ledger_id}/display").get_json()["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["type"], "transaction")

    def test_existing_group_can_be_combined_again_and_fully_split(self):
        first = self.add(branch="dy", amount="100")
        second = self.add(branch="dy", amount="30", direction="收入")
        third = self.add(branch="dy", amount="20")
        fourth = self.add(branch="dy", amount="10", direction="收入")
        base = f"/api/ledgers/{self.dy}/display-groups"
        initial = self.client.post(base, json={"tx_ids": [first, second]}).get_json()["group_id"]
        response = self.client.post(base, json={"item_ids": [f"g-{initial}", str(third)]})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["combined_group_ids"], [initial])
        next_group = response.get_json()["group_id"]
        display = self.client.get(f"/api/ledgers/{self.dy}/display").get_json()
        self.assertEqual((display["expense"], display["income"]), (90, 10))
        self.assertEqual(len(display["items"][0]["members"]), 3)
        final = self.client.post(base, json={"item_ids": [f"g-{next_group}", fourth]})
        self.assertEqual(final.status_code, 200, final.get_json())
        display = self.client.get(f"/api/ledgers/{self.dy}/display").get_json()
        self.assertEqual(len(display["items"]), 1)
        self.assertEqual(len(display["items"][0]["members"]), 4)
        self.assertEqual((display["expense"], display["income"]), (80, 0))
        self.assertEqual(self.client.delete(
            f"/api/ledger-display-groups/{final.get_json()['group_id']}"
        ).status_code, 200)
        self.assertEqual(len(self.client.get(f"/api/ledgers/{self.dy}/display").get_json()["items"]), 4)
        self.assertEqual(self.row(first)["amount"], 100)

    def test_group_shows_three_most_frequent_source_categories(self):
        categories = ["吃饭", "玩乐", "吃饭", "学习", "购物", "玩乐"]
        ids = [self.add(branch="dy", category=category) for category in categories]
        response = self.client.post(f"/api/ledgers/{self.dy}/display-groups", json={"tx_ids": ids})
        self.assertEqual(response.status_code, 200, response.get_json())

        def top_categories():
            items = self.client.get(f"/api/ledgers/{self.dy}/display").get_json()["items"]
            self.assertEqual(len(items), 1)
            return items[0]["top_categories"]

        self.assertEqual(top_categories(), ["吃饭", "玩乐", "学习"])
        self.assertEqual(self.client.post(f"/api/transactions/{ids[0]}/category", json={
            "category": "玩乐", "remember": False,
        }).status_code, 200)
        self.assertEqual(top_categories(), ["玩乐", "吃饭", "学习"])

    def test_combined_report_supplies_main_dashboard_values(self):
        self.add(branch="dy", amount="100", month="08", category="玩乐")
        self.add(branch="dy", amount="80", month="09", category="吃饭")
        self.add(branch="dy", amount="20", month="09", direction="收入", category="吃饭")
        report = self.combined([self.dy])
        self.assertEqual((report["expense"], report["income"], report["net_expense"]), (80, 20, 60))
        self.assertEqual(report["categories"][0]["amount"], 60)
        self.assertEqual(report["prev_total"], 100)
        self.assertEqual(report["prev_pct"], -40)
        self.assertEqual(report["hist_avg"], 100)
        self.assertEqual(report["top_expenses"][0]["amount"], 80)


if __name__ == "__main__":
    unittest.main()
