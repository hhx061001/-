"""Different statement layouts import without changing the saved schema."""

import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook

import app as bill_app


def workbook_bytes(*sheets):
    book = Workbook()
    first = book.active
    for index, (name, rows) in enumerate(sheets):
        sheet = first if index == 0 else book.create_sheet()
        sheet.title = name
        for row in rows:
            sheet.append(row)
    data = io.BytesIO()
    book.save(data)
    return data.getvalue()


class ImportFormatTests(unittest.TestCase):
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

    def upload(self, filename, content, **fields):
        response = self.client.post(
            "/api/import",
            data={"files": (io.BytesIO(content), filename), **fields},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["results"][0]

    def records(self):
        conn = bill_app.get_db()
        try:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM transactions ORDER BY id"
            ).fetchall()]
        finally:
            conn.close()

    def test_original_wechat_and_alipay_formats_still_import(self):
        wx = ("微信支付账单\n"
              "交易时间,交易类型,交易对方,商品,收/支,金额(元),交易单号,商户单号\n"
              "2026-09-01 12:00:00,商户消费,早餐店,早餐,支出,18.50,wx-1,shop-1\n")
        alipay = ("支付宝交易记录\n"
                  "交易时间,交易类型,交易对方,商品说明,收/支,金额,交易订单号,商家订单号\n"
                  "2026-09-02 12:00:00,消费,书店,教材,支出,30,ali-1,shop-2\n")
        self.assertEqual(self.upload("微信.csv", wx.encode("utf-8"))["inserted"], 1)
        self.assertEqual(self.upload("支付宝.csv", alipay.encode("gb18030"))["inserted"], 1)
        self.assertEqual([r["source"] for r in self.records()], ["wx", "alipay"])

    def test_generic_csv_split_income_expense_and_duplicate_skip(self):
        content = ("账单说明\n日期;收入;支出;摘要\n"
                   "2026/09/03;100;;工资\n2026/09/04;;25.50;午餐\n"
                   "合计;100;25.50;\n").encode("utf-8")
        first = self.upload("某银行.csv", content)
        self.assertEqual(first["source"], "other")
        self.assertEqual(first["inserted"], 2)
        self.assertEqual(first["transactions"][0]["direction"], "收入")
        self.assertEqual(first["transactions"][1]["direction"], "支出")
        self.assertEqual(self.upload("某银行.csv", content)["skipped"], 2)
        self.assertEqual(len(self.records()), 2)

    def test_signed_amounts_infer_both_directions(self):
        content = ("Date,Amount,Description\n"
                   "2026-09-03,-12.50,Coffee\n"
                   "2026-09-04,50.00,Refund\n").encode()
        result = self.upload("card.csv", content)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual([r["direction"] for r in result["transactions"]], ["支出", "收入"])

    def test_ambiguous_positive_amount_requires_user_choice(self):
        content = b"Date,Amount,Description\n2026-09-03,12.50,Coffee\n"
        first = self.upload("card.csv", content)
        self.assertTrue(first["needs_mapping"])
        self.assertEqual(len(self.records()), 0)
        mapped = self.upload(
            "card.csv", content, mapping=json.dumps(first["suggested_mapping"]),
            header_row=str(first["header_row"]), default_direction="支出",
        )
        self.assertEqual(mapped["inserted"], 1)
        self.assertEqual(self.records()[0]["direction"], "支出")

    def test_unknown_headers_can_be_previewed_and_mapped(self):
        content = "订单日,付款数,店铺\n2026-09-05,35,文具店\n".encode()
        first = self.upload("statement.csv", content)
        self.assertTrue(first["needs_mapping"])
        preview = self.client.post(
            "/api/import/preview",
            data={"file": (io.BytesIO(content), "statement.csv"), "header_row": "0"},
            content_type="multipart/form-data",
        ).get_json()
        self.assertEqual(preview["headers"], ["订单日", "付款数", "店铺"])
        mapped = self.upload(
            "statement.csv", content,
            mapping=json.dumps({"transaction_time": 0, "amount": 1, "counterparty": 2}),
            header_row="0", default_direction="支出",
        )
        self.assertEqual(mapped["inserted"], 1)
        self.assertEqual(self.records()[0]["counterparty"], "文具店")

    def test_excel_uses_sheet_with_statement_and_preserves_boc(self):
        other = workbook_bytes(
            ("说明", [["账单"], ["请查看下一页"]]),
            ("流水", [["交易日期", "收入金额", "支出金额", "业务摘要"],
                    ["2026-09-06", None, 80, "买书"]]),
        )
        result = self.upload("中国银行.xlsx", other)
        self.assertEqual(result["sheet_name"], "流水")
        self.assertEqual(result["source"], "boc")
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(self.records()[0]["direction"], "支出")

    def test_manual_mapping_can_select_another_excel_sheet(self):
        data = workbook_bytes(
            ("说明", [["文件用途", "请查看数据页"]]),
            ("数据", [["说明", "示例"], ["日期字段", "付款数", "店名"],
                    ["2026-09-07", 42, "午餐店"]]),
        )
        first = self.upload("account.xlsx", data)
        self.assertTrue(first["needs_mapping"])
        self.assertEqual(first["sheet_names"], ["说明", "数据"])
        preview_response = self.client.post(
            "/api/import/preview",
            data={"file": (io.BytesIO(data), "account.xlsx"),
                  "sheet_index": "1", "header_row": "1"},
            content_type="multipart/form-data",
        )
        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(preview_response.get_json()["headers"], ["日期字段", "付款数", "店名"])
        result = self.upload(
            "account.xlsx", data,
            mapping=json.dumps({"transaction_time": 0, "amount": 1, "counterparty": 2}),
            sheet_index="1", header_row="1", default_direction="支出",
        )
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["sheet_name"], "数据")

    def test_invalid_mapping_does_not_write_records(self):
        content = b"a,b\n2026-09-01,10\n"
        result = self.upload(
            "table.csv", content,
            mapping=json.dumps({"transaction_time": 0, "amount": 0}),
            header_row="0", default_direction="支出",
        )
        self.assertIn("同一列", result["error"])
        self.assertEqual(self.records(), [])


if __name__ == "__main__":
    unittest.main()
