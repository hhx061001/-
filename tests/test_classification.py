"""Personal category learning only follows confirmed user choices."""

import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app as bill_app
import ai_category


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            bill_app, "DB_PATH", os.path.join(self.temp_dir.name, "test.db")
        )
        self.db_patch.start()
        self.settings_patch = patch.object(
            bill_app, "AI_SETTINGS_PATH", os.path.join(self.temp_dir.name, "ai_settings.json")
        )
        self.settings_patch.start()
        bill_app.init_db()
        self.client = bill_app.app.test_client()

    def tearDown(self):
        self.settings_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def add(self, merchant, description, category="其他", source="自动"):
        conn = bill_app.get_db()
        try:
            cursor = conn.execute(
                """INSERT INTO transactions
                   (source, transaction_time, month, amount, direction,
                    counterparty, description, category, category_source)
                   VALUES ('other', '2026-09-01', '2026-09', 20, '支出', ?, ?, ?, ?)""",
                (merchant, description, category, source),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def row(self, row_id):
        conn = bill_app.get_db()
        try:
            return dict(conn.execute(
                "SELECT * FROM transactions WHERE id=?", (row_id,)
            ).fetchone())
        finally:
            conn.close()

    def test_confirmed_local_merchant_fills_existing_and_future_records(self):
        first = self.add("楼下早餐店", "豆浆")
        waiting = self.add("楼下早餐店", "包子")
        response = self.client.post(
            f"/api/transactions/{first}/category",
            json={"category": "吃饭", "remember": True},
        )
        self.assertEqual(response.get_json()["learned"], 1)
        self.assertEqual(self.row(waiting)["category"], "吃饭")
        conn = bill_app.get_db()
        try:
            rule = conn.execute(
                "SELECT match_text, category FROM merchant_rule_items WHERE counterparty=?",
                ("楼下早餐店",),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(rule), ("", "吃饭"))
        category, origin = bill_app.classify_transaction(
            {"counterparty": "楼下早餐店", "description": "油条", "direction": "支出"},
            [{"counterparty": "楼下早餐店", "match_text": "", "category": "吃饭"}],
        )
        self.assertEqual((category, origin), ("吃饭", "记忆"))

    def test_generic_platform_does_not_blanket_classify_other_purchases(self):
        first = self.add("淘宝", "机械键盘")
        other = self.add("淘宝", "零食礼包")
        self.client.post(
            f"/api/transactions/{first}/category",
            json={"category": "购物", "remember": True},
        )
        self.assertEqual(self.row(other)["category"], "其他")
        conn = bill_app.get_db()
        try:
            rules = bill_app.load_merchant_rules(conn)
        finally:
            conn.close()
        self.assertFalse(any(rule["match_text"] == "" for rule in rules))
        self.assertEqual(
            bill_app.classify_transaction(
                {"counterparty": "淘宝", "description": "机械键盘", "direction": "支出"}, rules
            )[0], "购物",
        )

    def test_auto_classify_reuses_older_manual_history(self):
        self.add("小区书店", "教材", "学习", "人工")
        self.add("小区书店", "辅导资料", "学习", "人工")
        waiting = self.add("小区书店", "练习册")
        result = self.client.post("/api/auto-classify").get_json()
        self.assertEqual(result["changed"], 1)
        self.assertEqual(self.row(waiting)["category"], "学习")
        self.assertEqual(self.row(waiting)["category_source"], "记忆")

    def test_conflicting_confirmations_remove_broad_rule(self):
        first = self.add("社区小店", "午饭")
        second = self.add("社区小店", "文具")
        self.client.post(f"/api/transactions/{first}/category", json={"category": "吃饭"})
        self.client.post(f"/api/transactions/{second}/category", json={"category": "学习"})
        conn = bill_app.get_db()
        try:
            rules = bill_app.load_merchant_rules(conn)
            memory = bill_app.load_category_memory(conn)
        finally:
            conn.close()
        self.assertFalse(any(rule["match_text"] == "" for rule in rules))
        self.assertEqual(
            bill_app.classify_transaction(
                {"counterparty": "社区小店", "description": "文具", "direction": "支出"},
                rules, memory,
            )[0], "学习",
        )
        self.assertEqual(
            bill_app.classify_transaction(
                {"counterparty": "社区小店", "description": "未知商品", "direction": "支出"},
                rules, memory,
            )[0], "其他",
        )

    def test_ai_suggestions_are_reviewed_before_changing_ledger(self):
        first = self.add("陌生商户A", "午餐")
        second = self.add("陌生商户B", "文具")
        save = self.client.put("/api/ai-category/settings", json={
            "provider": "openai", "model": "gpt-4o-mini", "key": "dummy-secret-key"
        })
        self.assertEqual(save.status_code, 200)
        self.assertTrue(save.get_json()["providers"]["openai"]["has_key"])
        with open(bill_app.AI_SETTINGS_PATH, encoding="utf-8") as handle:
            self.assertNotIn("dummy-secret-key", handle.read())
        with patch.object(bill_app, "request_suggestions", return_value=[
            {"id": first, "category": "吃饭", "confidence": 0.7, "reason": "餐饮"},
            {"id": second, "category": "学习", "confidence": 0.93, "reason": "文具"},
            {"id": 999999, "category": "购物", "confidence": 1, "reason": "无效记录"},
        ]) as mocked:
            result = self.client.post("/api/ai-category/suggest", json={"limit": 25})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.get_json()["created"], 2)
            self.assertEqual(mocked.call_args.args[0]["provider"], "openai")
        self.assertEqual(self.row(first)["category"], "其他")
        self.assertEqual(self.row(second)["category"], "其他")
        self.assertEqual(len(self.client.get("/api/ai-category/suggestions").get_json()["items"]), 2)
        self.assertEqual(
            self.client.post(f"/api/ai-category/suggestions/{first}/accept").status_code, 200
        )
        self.assertEqual(self.row(first)["category_source"], "人工")
        self.assertEqual(
            self.client.post("/api/ai-category/accept-high").get_json()["accepted"], 1
        )
        self.assertEqual(self.row(second)["category_source"], "AI确认")
        self.assertEqual(self.client.get("/api/ai-category/suggestions").get_json()["items"], [])

    def test_provider_keys_are_kept_separately(self):
        self.client.put("/api/ai-category/settings", json={
            "provider": "openai", "model": "gpt-4o-mini", "key": "openai-test-key"
        })
        self.client.put("/api/ai-category/settings", json={
            "provider": "deepseek", "model": "deepseek-flash", "key": "deepseek-test-key"
        })
        current = self.client.get("/api/ai-category/settings").get_json()
        self.assertEqual(current["selected"], "deepseek")
        self.assertTrue(current["providers"]["openai"]["has_key"])
        self.assertNotIn("key_protected", str(current))
        self.assertEqual(
            self.client.put("/api/ai-category/settings", json={
                "provider": "openai", "model": "gpt-4o-mini", "key": ""
            }).status_code, 200
        )
        self.assertEqual(bill_app.read_settings(bill_app.AI_SETTINGS_PATH)["key"], "openai-test-key")

    def test_api_manager_can_delete_one_key_without_affecting_the_other(self):
        for provider, model in (("openai", "gpt-4o-mini"), ("deepseek", "deepseek-flash")):
            self.assertEqual(self.client.put("/api/ai-category/settings", json={
                "provider": provider, "model": model, "key": f"{provider}-test-key"
            }).status_code, 200)
        deleted = self.client.delete("/api/ai-category/settings/openai/key")
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(deleted.get_json()["providers"]["openai"]["has_key"])
        self.assertTrue(deleted.get_json()["providers"]["deepseek"]["has_key"])
        self.assertEqual(
            bill_app.read_settings(bill_app.AI_SETTINGS_PATH, "deepseek")["key"],
            "deepseek-test-key",
        )
        self.assertEqual(self.client.delete("/api/ai-category/settings/unknown/key").status_code, 400)
        self.assertEqual(
            self.client.post("/api/ai-category/settings/openai/reveal").status_code, 400,
        )

    def test_api_manager_connection_test_sends_no_bill_data(self):
        self.client.put("/api/ai-category/settings", json={
            "provider": "openai", "model": "gpt-4o-mini", "key": "openai-test-key"
        })
        self.assertEqual(
            self.client.post("/api/ai-category/settings/openai/reveal").get_json()["key"],
            "openai-test-key",
        )

        def fake_urlopen(request, timeout):
            self.assertEqual(request.full_url, "https://api.openai.com/v1/chat/completions")
            self.assertEqual(timeout, 20)
            body = json.loads(request.data)
            self.assertEqual(body["messages"], [{"role": "user", "content": "只回复 OK"}])
            self.assertFalse(body["store"])
            return io.BytesIO(b'{"choices":[{"message":{"content":"OK"}}]}')

        with patch.object(ai_category.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = self.client.post("/api/ai-category/settings/test", json={"provider": "openai"})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.get_json()["ok"])
        public = self.client.get("/api/ai-category/settings").get_json()
        self.assertEqual(public["providers"]["openai"]["key_hint"], "••••-key")
        self.assertEqual(public["providers"]["openai"]["last_test"]["status"], "success")
        self.assertNotIn("openai-test-key", json.dumps(public))
        revealed = self.client.post("/api/ai-category/settings/openai/reveal")
        self.assertEqual(revealed.get_json()["key"], "openai-test-key")
        self.assertEqual(revealed.headers["Cache-Control"], "no-store")
        deleted = self.client.delete("/api/ai-category/settings/openai/key").get_json()
        self.assertEqual(deleted["providers"]["openai"]["key_hint"], "")
        self.assertIsNone(deleted["providers"]["openai"]["last_test"])
        self.assertEqual(self.client.post("/api/ai-category/settings/openai/reveal").status_code, 400)
        self.assertEqual(
            self.client.post("/api/ai-category/settings/test", json={"provider": "deepseek"}).status_code,
            400,
        )

    def test_failed_test_and_key_change_clear_reveal_access(self):
        self.client.put("/api/ai-category/settings", json={
            "provider": "deepseek", "model": "deepseek-flash", "key": "old-deepseek-key"
        })
        with patch.object(bill_app, "test_connection", return_value=None):
            self.assertEqual(self.client.post(
                "/api/ai-category/settings/test", json={"provider": "deepseek"}
            ).status_code, 200)
        with patch.object(bill_app, "test_connection", side_effect=RuntimeError("测试失败")):
            self.assertEqual(self.client.post(
                "/api/ai-category/settings/test", json={"provider": "deepseek"}
            ).status_code, 400)
        public = self.client.get("/api/ai-category/settings").get_json()["providers"]["deepseek"]
        self.assertEqual(public["last_test"]["status"], "failure")
        self.assertEqual(
            self.client.post("/api/ai-category/settings/deepseek/reveal").get_json()["key"],
            "old-deepseek-key",
        )
        with patch.object(bill_app, "test_connection", return_value=None):
            self.client.post("/api/ai-category/settings/test", json={"provider": "deepseek"})
        changed = self.client.put("/api/ai-category/settings", json={
            "provider": "deepseek", "model": "deepseek-flash", "key": "new-deepseek-key"
        })
        self.assertIsNone(changed.get_json()["providers"]["deepseek"]["last_test"])
        self.assertEqual(
            self.client.post("/api/ai-category/settings/deepseek/reveal").get_json()["key"],
            "new-deepseek-key",
        )

    def test_ai_request_sends_only_redacted_classification_fields(self):
        def fake_urlopen(request, timeout):
            self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
            self.assertEqual(timeout, 45)
            body = json.loads(request.data)
            self.assertEqual(body["response_format"], {"type": "json_object"})
            sent = json.loads(body["messages"][1]["content"])
            self.assertNotIn("123456789012", str(sent))
            self.assertNotIn("https://example.com", str(sent))
            self.assertEqual(sent["待分类交易"][0]["商户"], "早餐店")
            return io.BytesIO(json.dumps({
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                    "suggestions": [{"id": 1, "category": "吃饭", "confidence": 0.9, "reason": "早餐"}]
                })}}]
            }).encode())

        with patch.object(ai_category.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = ai_category.request_suggestions(
                {"provider": "deepseek", "model": "deepseek-flash", "key": "dummy"},
                ["吃饭", "其他"],
                [{"id": 1, "counterparty": "早餐店", "description": "订单123456789012",
                  "note": "https://example.com", "amount": 12.5, "direction": "支出"}],
                [],
            )
        self.assertEqual(result[0]["category"], "吃饭")


if __name__ == "__main__":
    unittest.main()
