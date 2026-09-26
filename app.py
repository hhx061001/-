# -*- coding: utf-8 -*-
"""个人账单分析工具 - 后端

本地运行：python app.py
本地服务默认只监听 127.0.0.1，不联网、不调用外部 API。
"""

import csv
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, render_template, request, send_file


APP_NAME = "个人账单分析工具"


def get_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_db_dir() -> str:
    # exe 场景下不要把数据库写进可能只读的安装目录，优先写 LocalAppData。
    local = os.environ.get("LOCALAPPDATA")
    if local:
        d = os.path.join(local, "BillAnalyzer")
    else:
        d = os.path.join(get_base_dir(), "data")
    os.makedirs(d, exist_ok=True)
    return d


def get_resource_path(rel: str) -> str:
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, rel)  # type: ignore[attr-defined]
    return os.path.join(get_base_dir(), rel)


DB_PATH = os.path.join(get_db_dir(), "bill_data.db")


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=get_resource_path("templates"),
        static_folder=get_resource_path("static"),
    )
    app.config["JSON_AS_ASCII"] = False
    app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    return app


app = create_app()


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

CATEGORIES = ["吃饭", "玩乐", "学习", "购物", "其他"]
SOURCES = {
    "wx": "微信",
    "alipay": "支付宝",
    "boc": "中国银行",
    "manual": "手动",
}

CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "吃饭": [
        "美团外卖", "饿了么", "美团", "肯德基", "麦当劳", "汉堡王", "必胜客",
        "星巴克", "瑞幸", "蜜雪冰城", "喜茶", "奈雪", "海底捞", "餐厅",
        "饭店", "火锅", "烧烤", "串串", "麻辣烫", "便利店", "全家", "罗森",
        "7-11", "711", "便利蜂", "生鲜", "超市", "菜市场", "奶茶", "面包",
        "烘焙", "早餐", "午餐", "晚餐", "夜宵", "咖啡", "小吃", "食堂",
        "外卖", "水果", "零食", "盒马", "山姆", "餐饮",
    ],
    "玩乐": [
        "淘票票", "猫眼", "电影", "影城", "KTV", "剧本杀", "桌游", "livehouse",
        "酒吧", "Steam", "游戏", "爱奇艺", "腾讯视频", "哔哩哔哩", "bilibili",
        "网易云音乐", "QQ音乐", "会员", "携程", "飞猪", "酒店", "健身房",
        "门票", "景区", "旅行", "旅游", "演出", "音乐节", "展览", "游乐园",
        "迪士尼", "环球影城", "电影院", "网吧", "台球", "露营", "民宿",
    ],
    "学习": [
        "得到", "知乎盐选", "Kindle", "微信读书", "当当", "多抓鱼", "图书",
        "书店", "Coursera", "慕课", "考试报名", "文具", "打印", "课程",
        "培训", "教育", "网课", "报名费", "学费", "教材", "学习", "知识付费",
        "樊登读书", "得到", "证书", "考研", "考公", "学习用品",
    ],
    "购物": [
        "淘宝", "天猫", "京东", "拼多多", "唯品会", "得物", "抖音商城",
        "小红书", "优衣库", "ZARA", "耐克", "阿迪", "数码", "家电", "服饰",
        "日用百货", "药店", "加油站", "商城", "旗舰店", "专柜", "超市购物",
        "亚马逊", "网易严选", "小米商城", "华为商城", "苹果", "Apple",
        "名创优品", "无印良品", "宜家", "美妆", "护肤品", "化妆品",
    ],
}

BANK_LINK_KEYWORDS = [
    "支付宝", "财付通", "微信支付", "微信", "银联", "网联", "快捷支付",
    "京东支付", "度小满", "美团支付", "抖音支付", "云闪付", "翼支付",
]
THIRD_PARTY_BANK_KEYWORDS = ["中国银行", "中行"]
HUABEI_BANK_KEYWORDS = ["支付宝", "花呗", "财付通", "微信", "信用卡"]

COLUMN_ALIASES: Dict[str, List[str]] = {
    "transaction_time": ["交易时间", "交易日期", "记账日期", "交易时间戳"],
    "type": ["交易类型", "交易分类", "业务类型"],
    "counterparty": ["交易对方", "对方账户名称", "对方户名", "商户名称"],
    "counterparty_account": ["对方账号", "对方账户账号", "对方账户"],
    "description": ["商品", "商品说明", "业务摘要", "摘要"],
    "direction": ["收-支", "收支", "收/支", "资金流向"],
    "amount": ["金额", "金额(元)", "交易金额"],
    "payment_method": [
        "支付方式", "收-付款方式", "交易渠道", "交易渠道-场所",
        "交易渠道/场所", "交易渠道及场所",
    ],
    "status": ["当前状态", "交易状态"],
    "transaction_no": ["交易单号", "交易订单号", "流水号"],
    "merchant_no": ["商户单号", "商家订单号"],
    "note": ["备注", "附言"],
    "currency": ["币种"],
    "cash_note": ["钞汇", "钞汇标志"],
    "income_amount": ["收入金额", "收入", "存入金额"],
    "expense_amount": ["支出金额", "支出", "支取金额"],
    "balance": ["余额", "账户余额"],
}


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------


def norm_header(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    s = s.replace("\ufeff", "").replace("\u3000", "").replace(" ", "")
    s = s.replace("（", "").replace("）", "").replace("(", "").replace(")", "")
    return s


def decode_bytes(raw: bytes) -> Tuple[str, str]:
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue

    # GBK/GB18030 是支付宝账单的常见编码；gb18030 是 GBK 的超集。
    for enc in ("gb18030", "gbk", "big5"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def clean_money(value: Any) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip()
    s = (
        s.replace("¥", "")
        .replace("￥", "")
        .replace(",", "")
        .replace("，", "")
        .replace("元", "")
        .replace(" ", "")
        .replace("\u3000", "")
    )
    if s in ("", "-", "--", "/", "—"):
        return None
    try:
        return round(float(s), 2)
    except (TypeError, ValueError):
        return None


def normalize_amount(value: Optional[float]) -> float:
    if value is None:
        return 0.0
    return round(abs(value), 2)


def parse_datetime(value: Any) -> Tuple[Optional[str], Optional[int]]:
    """返回 (标准化文本 YYYY-MM-DD HH:MM:SS, epoch 秒)。"""
    if value is None:
        return None, None
    s = str(value).strip()
    if not s:
        return None, None

    original = s
    s = (
        s.replace("年", "-")
        .replace("月", "-")
        .replace("日", " ")
        .replace("/", "-")
        .replace(".", "-")
        .replace("时", ":")
        .replace("分", ":")
        .replace("秒", "")
        .strip()
    )
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"-{2,}", "-", s)

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y%m%d %H:%M:%S",
        "%Y%m%d %H:%M",
        "%Y%m%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S"), int(dt.timestamp())
        except ValueError:
            continue

    # 兜底：从原始文本中提取日期和时间。
    m = re.search(
        r"(?P<y>\d{4})[年\-/.]?(?P<mo>\d{1,2})[月\-/.]?(?P<d>\d{1,2})日?"
        r"(?:\s*(?P<h>\d{1,2})[:时](?P<mi>\d{1,2})(?:[:分](?P<s>\d{1,2}))?)?",
        original,
    )
    if m:
        try:
            dt = datetime(
                int(m.group("y")),
                int(m.group("mo")),
                int(m.group("d")),
                int(m.group("h") or 0),
                int(m.group("mi") or 0),
                int(m.group("s") or 0),
            )
            return dt.strftime("%Y-%m-%d %H:%M:%S"), int(dt.timestamp())
        except ValueError:
            pass
    return original, None


def cell_value(cells: List[Any], canonical: Dict[str, int], key: str) -> str:
    idx = canonical.get(key)
    if idx is None or idx >= len(cells):
        return ""
    v = cells[idx]
    if v is None:
        return ""
    return str(v).strip()


def map_headers(cells: List[Any]) -> Dict[str, int]:
    result: Dict[str, int] = {}
    normalized = [norm_header(c) for c in cells]
    for key, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            target = norm_header(alias)
            for idx, value in enumerate(normalized):
                if value == target:
                    result[key] = idx
                    break
            if key in result:
                break
    return result


def is_header_row(cells: List[Any]) -> bool:
    normed = [norm_header(c) for c in cells if str(c or "").strip()]
    joined = "".join(normed)
    if "交易时间" not in joined:
        return False
    return "金额" in joined or "收入金额" in joined or "支出金额" in joined


def detect_source_from_cells(cells: List[Any]) -> Optional[str]:
    normed = [norm_header(c) for c in cells if str(c or "").strip()]
    joined = "".join(normed)
    if "交易订单号" in joined or "商家订单号" in joined:
        return "alipay"
    if "交易单号" in joined or "商户单号" in joined:
        return "wx"
    if "收入金额" in joined and "支出金额" in joined:
        return "boc"
    return None


def detect_source_from_filename(filename: str) -> Optional[str]:
    name = filename.lower()
    if any(k in name for k in ("微信", "wechat", "weixin")):
        return "wx"
    if any(k in name for k in ("支付宝", "alipay")):
        return "alipay"
    if any(k in name for k in ("中国银行", "中行", "boc")):
        return "boc"
    return None


def infer_direction(direction_field: str, amount: Optional[float]) -> str:
    d = direction_field or ""
    if "收入" in d:
        return "收入"
    if "支出" in d:
        return "支出"
    if "不计" in d:
        return "不计收支"
    if amount is None or amount == 0:
        return "不计收支"
    # 缺少方向字段时保守处理，交给用户手工修正。
    return "不计收支"


def stable_key(prefix: str, *parts: Any) -> str:
    material = "|".join(str(p if p is not None else "") for p in parts)
    digest = hashlib.sha1(material.encode("utf-8", errors="replace")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def month_from_time(time_str: Optional[str]) -> str:
    if time_str and len(time_str) >= 7:
        return time_str[:7]
    return ""


def month_add(month: str, delta: int) -> str:
    y, m = int(month[:4]), int(month[5:7])
    total = y * 12 + (m - 1) + delta
    ny, nm = divmod(total, 12)
    return f"{ny:04d}-{nm + 1:02d}"


def month_list_from_db(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT DISTINCT month FROM ("
        "SELECT month FROM transactions WHERE month != '' AND is_deleted=0 "
        "UNION SELECT month FROM monthly_budgets) "
        "ORDER BY month DESC"
    ).fetchall()
    return [r["month"] for r in rows]


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    conn = get_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                transaction_time TEXT,
                month TEXT,
                ts INTEGER,
                amount REAL NOT NULL DEFAULT 0,
                direction TEXT NOT NULL,
                counterparty TEXT,
                description TEXT,
                type TEXT,
                payment_method TEXT,
                status TEXT,
                transaction_no TEXT,
                merchant_no TEXT,
                note TEXT,
                currency TEXT,
                balance TEXT,
                raw_line TEXT,
                category TEXT NOT NULL DEFAULT '其他',
                category_source TEXT NOT NULL DEFAULT '自动',
                count_in_expense INTEGER NOT NULL DEFAULT 1,
                exclude_reason TEXT,
                branch TEXT NOT NULL DEFAULT '生活',
                tags TEXT NOT NULL DEFAULT '[]',
                is_duplicate INTEGER NOT NULL DEFAULT 0,
                duplicate_of INTEGER,
                merge_sources TEXT,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                unique_key TEXT UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS merchant_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                counterparty TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS dedup_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                a_id INTEGER NOT NULL,
                b_id INTEGER NOT NULL,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(a_id, b_id)
            );

            CREATE TABLE IF NOT EXISTS tag_defs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                color TEXT NOT NULL DEFAULT '#2563eb',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS custom_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                group_name TEXT NOT NULL DEFAULT '生活',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS merchant_rule_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                counterparty TEXT NOT NULL,
                match_text TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ledgers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                include_in_summary INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS monthly_budgets (
                month TEXT PRIMARY KEY,
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_merchant_rule_items_unique
                ON merchant_rule_items(counterparty, match_text);

            CREATE INDEX IF NOT EXISTS idx_transactions_month ON transactions(month);
            CREATE INDEX IF NOT EXISTS idx_transactions_source ON transactions(source);
            CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category);
            CREATE INDEX IF NOT EXISTS idx_transactions_duplicate ON transactions(duplicate_of);
            """
        )
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()]
        if "tags" not in cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        if "branch" not in cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN branch TEXT NOT NULL DEFAULT '生活'")
        cat_cols = [r["name"] for r in conn.execute("PRAGMA table_info(custom_categories)").fetchall()]
        if "group_name" not in cat_cols:
            conn.execute("ALTER TABLE custom_categories ADD COLUMN group_name TEXT NOT NULL DEFAULT '生活'")
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO custom_categories (name, created_at)
                SELECT name, created_at FROM tag_defs
                """
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO merchant_rule_items (counterparty, match_text, category, created_at)
                SELECT counterparty, '', category, created_at FROM merchant_rules
                """
            )
        except sqlite3.OperationalError:
            pass
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    if d.get("merge_sources"):
        try:
            d["merge_sources"] = json.loads(d["merge_sources"])
        except (TypeError, json.JSONDecodeError):
            d["merge_sources"] = []
    else:
        d["merge_sources"] = []
    if d.get("tags"):
        try:
            d["tags"] = json.loads(d["tags"])
        except (TypeError, json.JSONDecodeError):
            d["tags"] = []
    else:
        d["tags"] = []
    return d


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def _parse_rows(
    rows: List[List[str]],
    header_idx: int,
    source: str,
    filename: str,
    raw_builder,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    canonical = map_headers(rows[header_idx])
    transactions: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    for i in range(header_idx + 1, len(rows)):
        cells = rows[i]
        if not any(str(c or "").strip() for c in cells):
            continue
        raw_line = raw_builder(cells)
        row_no = i + 1

        time_raw = cell_value(cells, canonical, "transaction_time")
        time_str, ts = parse_datetime(time_raw)

        if source in ("wx", "alipay"):
            amount_raw = cell_value(cells, canonical, "amount")
            amount_float = clean_money(amount_raw)
            direction = infer_direction(
                cell_value(cells, canonical, "direction"), amount_float
            )
            amount = normalize_amount(amount_float)

            if not time_raw and amount_float is None:
                warnings.append(
                    {
                        "line": row_no,
                        "reason": "缺少交易时间与金额，已按不计收支保留",
                        "raw": raw_line,
                    }
                )
            elif amount_float is None:
                warnings.append(
                    {
                        "line": row_no,
                        "reason": "金额无法解析，已按 0 元保留",
                        "raw": raw_line,
                    }
                )
            elif not time_raw:
                warnings.append(
                    {
                        "line": row_no,
                        "reason": "交易时间无法解析",
                        "raw": raw_line,
                    }
                )

            rec: Dict[str, Any] = {
                "source": source,
                "transaction_time": time_str or time_raw,
                "month": month_from_time(time_str),
                "ts": ts,
                "amount": amount,
                "direction": direction,
                "counterparty": cell_value(cells, canonical, "counterparty"),
                "description": cell_value(cells, canonical, "description"),
                "type": cell_value(cells, canonical, "type"),
                "payment_method": cell_value(cells, canonical, "payment_method"),
                "status": cell_value(cells, canonical, "status"),
                "transaction_no": cell_value(cells, canonical, "transaction_no"),
                "merchant_no": cell_value(cells, canonical, "merchant_no"),
                "note": cell_value(cells, canonical, "note"),
                "currency": "",
                "balance": "",
                "raw_line": raw_line,
            }
            if rec["transaction_no"]:
                rec["unique_key"] = f"{source}:{rec['transaction_no']}"
            else:
                rec["unique_key"] = stable_key(
                    source,
                    time_str or time_raw,
                    amount,
                    rec["counterparty"],
                    rec["description"],
                    raw_line,
                )
        else:  # boc
            income = clean_money(cell_value(cells, canonical, "income_amount"))
            expense = clean_money(cell_value(cells, canonical, "expense_amount"))
            if income not in (None, 0) and (expense in (None, 0) or abs(income) >= abs(expense)):
                direction = "收入"
                amount = normalize_amount(income)
            elif expense not in (None, 0):
                direction = "支出"
                amount = normalize_amount(expense)
            elif income not in (None, 0):
                direction = "收入"
                amount = normalize_amount(income)
            else:
                direction = "不计收支"
                amount = 0.0

            if income not in (None, 0) and expense not in (None, 0):
                warnings.append(
                    {
                        "line": row_no,
                        "reason": "收入金额与支出金额同时有值，已按差额/较大方向保留",
                        "raw": raw_line,
                    }
                )
            if not time_raw:
                warnings.append(
                    {
                        "line": row_no,
                        "reason": "交易时间无法解析，仍保留原文",
                        "raw": raw_line,
                    }
                )
            if income is None and expense is None:
                warnings.append(
                    {
                        "line": row_no,
                        "reason": "收入/支出金额均无法解析，已按不计收支保留",
                        "raw": raw_line,
                    }
                )

            summary = cell_value(cells, canonical, "description")
            balance = cell_value(cells, canonical, "balance")
            rec = {
                "source": "boc",
                "transaction_time": time_str or time_raw,
                "month": month_from_time(time_str),
                "ts": ts,
                "amount": amount,
                "direction": direction,
                "counterparty": cell_value(cells, canonical, "counterparty"),
                "description": summary,
                "type": "",
                "payment_method": cell_value(cells, canonical, "payment_method"),
                "status": "",
                "transaction_no": "",
                "merchant_no": "",
                "note": cell_value(cells, canonical, "note"),
                "currency": cell_value(cells, canonical, "currency"),
                "balance": balance,
                "raw_line": raw_line,
                "unique_key": stable_key(
                    "boc", time_str or time_raw, amount, summary, balance
                ),
            }

        transactions.append(rec)

    return transactions, warnings


def parse_csv(filename: str, raw: bytes) -> Dict[str, Any]:
    text, encoding = decode_bytes(raw)
    reader = csv.reader(io.StringIO(text))
    rows = [[c if c is not None else "" for c in row] for row in reader]

    header_idx = None
    for i, row in enumerate(rows):
        if is_header_row(row):
            header_idx = i
            break
    if header_idx is None:
        return {
            "filename": filename,
            "source": None,
            "encoding": encoding,
            "error": "未找到以「交易时间」开头的表头行，请确认文件格式。",
            "transactions": [],
            "warnings": [],
        }

    source = detect_source_from_filename(filename) or detect_source_from_cells(rows[header_idx])
    if source not in SOURCES:
        return {
            "filename": filename,
            "source": None,
            "encoding": encoding,
            "error": "无法识别账单来源，请通过文件名包含“微信 / 支付宝 / 中国银行”或检查表头。",
            "transactions": [],
            "warnings": [],
        }

    transactions, warnings = _parse_rows(
        rows, header_idx, source, filename, lambda cells: ",".join(str(c) for c in cells)
    )
    return {
        "filename": filename,
        "source": source,
        "encoding": encoding,
        "error": None,
        "transactions": transactions,
        "warnings": warnings,
        "total_rows": max(0, len(rows) - header_idx - 1),
    }


def _read_xlsx(raw: bytes) -> List[List[str]]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb.active
    rows: List[List[str]] = []
    for row in ws.iter_rows(values_only=True):
        rows.append(["" if v is None else str(v) for v in row])
    wb.close()
    return rows


def _read_xls(raw: bytes) -> List[List[str]]:
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("读取 .xls 需要 xlrd，请将文件另存为 .xlsx。") from exc

    book = xlrd.open_workbook(file_contents=raw)
    sheet = book.sheet_by_index(0)
    rows: List[List[str]] = []
    for r in range(sheet.nrows):
        vals: List[str] = []
        for c in range(sheet.ncols):
            cell = sheet.cell(r, c)
            if cell.ctype == xlrd.XL_CELL_DATE:
                try:
                    dt = xlrd.xldate_as_datetime(cell.value, book.datemode)
                    if dt.hour or dt.minute or dt.second:
                        vals.append(dt.strftime("%Y-%m-%d %H:%M:%S"))
                    else:
                        vals.append(dt.strftime("%Y-%m-%d"))
                except Exception:
                    vals.append(str(cell.value))
            else:
                vals.append(str(cell.value))
        rows.append(vals)
    return rows


def parse_excel(filename: str, raw: bytes) -> Dict[str, Any]:
    ext = os.path.splitext(filename)[1].lower()
    try:
        rows = _read_xls(raw) if ext == ".xls" else _read_xlsx(raw)
    except Exception as exc:
        return {
            "filename": filename,
            "source": None,
            "encoding": "",
            "error": f"Excel 读取失败：{exc}",
            "transactions": [],
            "warnings": [],
        }

    header_idx = None
    for i, row in enumerate(rows[:40]):
        if is_header_row(row):
            header_idx = i
            break
    if header_idx is None:
        return {
            "filename": filename,
            "source": None,
            "encoding": "",
            "error": "未找到包含「交易时间 / 收入金额 / 支出金额」的表头行。",
            "transactions": [],
            "warnings": [],
        }

    source = detect_source_from_filename(filename) or detect_source_from_cells(rows[header_idx])
    if source not in SOURCES:
        return {
            "filename": filename,
            "source": None,
            "encoding": "",
            "error": "无法识别账单来源，请确认表头列名。",
            "transactions": [],
            "warnings": [],
        }

    transactions, warnings = _parse_rows(
        rows, header_idx, source, filename, lambda cells: " | ".join(str(c) for c in cells)
    )
    return {
        "filename": filename,
        "source": source,
        "encoding": "",
        "error": None,
        "transactions": transactions,
        "warnings": warnings,
        "total_rows": max(0, len(rows) - header_idx - 1),
    }


def parse_file(filename: str, raw: bytes) -> Dict[str, Any]:
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".csv":
        return parse_csv(filename, raw)
    if ext in (".xlsx", ".xls"):
        return parse_excel(filename, raw)
    return {
        "filename": filename,
        "source": None,
        "encoding": "",
        "error": "仅支持 .csv / .xlsx / .xls 文件。",
        "transactions": [],
        "warnings": [],
    }


# --------------------------------------------------------------------------
# 分类、花呗与去重
# --------------------------------------------------------------------------


def load_merchant_rules(conn: sqlite3.Connection) -> List[Dict[str, str]]:
    rows = conn.execute(
        """
        SELECT id, counterparty, match_text, category
        FROM merchant_rule_items
        ORDER BY match_text DESC, id ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_custom_categories(conn: sqlite3.Connection, group_name: Optional[str] = None) -> List[str]:
    if group_name:
        rows = conn.execute(
            "SELECT name FROM custom_categories WHERE group_name=? ORDER BY id ASC",
            (group_name,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT name FROM custom_categories ORDER BY id ASC"
        ).fetchall()
    return [r["name"] for r in rows]


def is_valid_category(conn: sqlite3.Connection, category: str) -> bool:
    return category in CATEGORIES or category in get_custom_categories(conn)


def is_valid_growth_category(conn: sqlite3.Connection, category: str) -> bool:
    return category in get_custom_categories(conn, "成长")


def classify_transaction(rec: Dict[str, Any], rules: List[Dict[str, str]]) -> Tuple[str, str]:
    cpty = (rec.get("counterparty") or "").strip()
    if cpty:
        search_text = " ".join([rec.get("note") or "", rec.get("description") or ""]).lower()
        specific = []
        general = []
        for rule in rules:
            if rule.get("counterparty") != cpty:
                continue
            mt = (rule.get("match_text") or "").strip().lower()
            if mt and mt in search_text:
                specific.append(rule)
            elif not mt:
                general.append(rule)
        if specific:
            return specific[0]["category"], "人工"
        if general:
            return general[0]["category"], "人工"

    fields = [
        (rec.get("note") or ""),
        (rec.get("description") or ""),
        (rec.get("counterparty") or ""),
    ]
    for field in fields:
        if not field:
            continue
        for category in ["吃饭", "玩乐", "学习", "购物"]:
            for kw in CATEGORY_KEYWORDS[category]:
                if kw.lower() in field.lower():
                    return category, "自动"
    return "其他", "自动"


def apply_huabei_rule(rec: Dict[str, Any]) -> bool:
    """命中花呗还款则标记不计入支出，返回是否命中。"""
    if rec.get("source") == "alipay":
        text = " ".join([rec.get("type") or "", rec.get("description") or ""])
        if "还款" in text:
            rec["count_in_expense"] = 0
            rec["exclude_reason"] = "花呗还款"
            return True
    elif rec.get("source") == "boc":
        summary = rec.get("description") or ""
        cpty_note = " ".join([rec.get("counterparty") or "", rec.get("note") or ""])
        if "还款" in summary and any(k in cpty_note for k in HUABEI_BANK_KEYWORDS):
            rec["count_in_expense"] = 0
            rec["exclude_reason"] = "花呗还款"
            return True
    return False


def finalize_record(rec: Dict[str, Any], rules: Dict[str, str]) -> Dict[str, Any]:
    rec["category"], rec["category_source"] = classify_transaction(rec, rules)
    if not apply_huabei_rule(rec):
        rec["count_in_expense"] = 1
        rec["exclude_reason"] = ""
    return rec


def insert_record(conn: sqlite3.Connection, rec: Dict[str, Any]) -> bool:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO transactions (
            source, transaction_time, month, ts, amount, direction, counterparty,
            description, type, payment_method, status, transaction_no, merchant_no,
            note, currency, balance, raw_line, category, category_source,
            count_in_expense, exclude_reason, is_duplicate, duplicate_of,
            merge_sources, is_deleted, unique_key, branch
        ) VALUES (
            :source, :transaction_time, :month, :ts, :amount, :direction, :counterparty,
            :description, :type, :payment_method, :status, :transaction_no, :merchant_no,
            :note, :currency, :balance, :raw_line, :category, :category_source,
            :count_in_expense, :exclude_reason, :is_duplicate, :duplicate_of,
            :merge_sources, :is_deleted, :unique_key, :branch
        )
        """,
        {
            "source": rec["source"],
            "transaction_time": rec.get("transaction_time") or "",
            "month": rec.get("month") or "",
            "ts": rec.get("ts"),
            "amount": rec.get("amount", 0),
            "direction": rec.get("direction", "不计收支"),
            "counterparty": rec.get("counterparty") or "",
            "description": rec.get("description") or "",
            "type": rec.get("type") or "",
            "payment_method": rec.get("payment_method") or "",
            "status": rec.get("status") or "",
            "transaction_no": rec.get("transaction_no") or "",
            "merchant_no": rec.get("merchant_no") or "",
            "note": rec.get("note") or "",
            "currency": rec.get("currency") or "",
            "balance": rec.get("balance") or "",
            "raw_line": rec.get("raw_line") or "",
            "category": rec.get("category", "其他"),
            "category_source": rec.get("category_source", "自动"),
            "count_in_expense": 1 if rec.get("count_in_expense", 1) else 0,
            "exclude_reason": rec.get("exclude_reason") or "",
            "is_duplicate": 0,
            "duplicate_of": None,
            "merge_sources": None,
            "is_deleted": 0,
            "unique_key": rec["unique_key"],
            "branch": rec.get("branch") or "生活",
        },
    )
    return cur.rowcount > 0


def has_link_evidence(boc: Dict[str, Any], third: Dict[str, Any]) -> bool:
    boc_text = " ".join(
        [
            boc.get("description") or "",
            boc.get("counterparty") or "",
            boc.get("note") or "",
        ]
    )
    if any(k in boc_text for k in BANK_LINK_KEYWORDS):
        return True
    third_pay = third.get("payment_method") or ""
    if any(k in third_pay for k in THIRD_PARTY_BANK_KEYWORDS):
        return True
    return False


def fetch_active_records(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM transactions
        WHERE is_deleted=0 AND is_duplicate=0 AND direction='支出' AND amount > 0
        """
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def add_candidate(
    conn: sqlite3.Connection, a_id: int, b_id: int, reason: str
) -> None:
    a, b = sorted([a_id, b_id])
    conn.execute(
        """
        INSERT OR IGNORE INTO dedup_candidates (a_id, b_id, reason, status)
        VALUES (?, ?, ?, 'pending')
        """,
        (a, b, reason),
    )


def refresh_merge_sources(conn: sqlite3.Connection, primary_id: int) -> None:
    primary = conn.execute(
        "SELECT * FROM transactions WHERE id=?", (primary_id,)
    ).fetchone()
    if not primary:
        return
    secondaries = conn.execute(
        "SELECT source FROM transactions WHERE is_duplicate=1 AND duplicate_of=?",
        (primary_id,),
    ).fetchall()
    sources = [primary["source"]]
    for s in secondaries:
        if s["source"] not in sources:
            sources.append(s["source"])
    payload = json.dumps(sources, ensure_ascii=False)
    conn.execute(
        "UPDATE transactions SET merge_sources=? WHERE id=?", (payload, primary_id)
    )


def merge_transactions(
    conn: sqlite3.Connection, primary_id: int, secondary_id: int
) -> Optional[Dict[str, Any]]:
    if primary_id == secondary_id:
        return None
    primary = conn.execute(
        "SELECT * FROM transactions WHERE id=?", (primary_id,)
    ).fetchone()
    secondary = conn.execute(
        "SELECT * FROM transactions WHERE id=?", (secondary_id,)
    ).fetchone()
    if not primary or not secondary:
        return None

    p = dict(primary)
    s = dict(secondary)
    if p.get("is_duplicate") or s.get("is_duplicate"):
        return None

    # 合并优先保留第三方（微信/支付宝）记录。
    if p["source"] == "boc" and s["source"] in ("wx", "alipay"):
        p, s = s, p
        primary_id, secondary_id = p["id"], s["id"]

    conn.execute(
        """
        UPDATE transactions
        SET is_duplicate=1, duplicate_of=?
        WHERE id=?
        """,
        (primary_id, secondary_id),
    )
    refresh_merge_sources(conn, primary_id)
    conn.execute(
        """
        UPDATE dedup_candidates SET status='resolved_merged'
        WHERE (a_id=? AND b_id=?) OR (a_id=? AND b_id=?)
        """,
        (primary_id, secondary_id, secondary_id, primary_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM transactions WHERE id=?", (primary_id,)).fetchone()
    return row_to_dict(row) if row else None


def unmerge_transactions(conn: sqlite3.Connection, primary_id: int, secondary_id: int) -> bool:
    secondary = conn.execute(
        "SELECT * FROM transactions WHERE id=?", (secondary_id,)
    ).fetchone()
    if not secondary or secondary["duplicate_of"] != primary_id:
        return False
    conn.execute(
        "UPDATE transactions SET is_duplicate=0, duplicate_of=NULL WHERE id=?",
        (secondary_id,),
    )
    refresh_merge_sources(conn, primary_id)
    conn.commit()
    return True


def run_cross_source_dedup() -> int:
    conn = get_db()
    try:
        records = fetch_active_records(conn)
        boc_records = [r for r in records if r["source"] == "boc"]
        third_records = [r for r in records if r["source"] in ("wx", "alipay")]
        merged = 0
        for boc in boc_records:
            for third in third_records:
                if abs((boc["amount"] or 0) - (third["amount"] or 0)) > 0.005:
                    continue
                ts_a, ts_b = boc.get("ts"), third.get("ts")
                if ts_a is None or ts_b is None:
                    continue
                if abs(ts_a - ts_b) > 600:
                    continue
                if has_link_evidence(boc, third):
                    if merge_transactions(conn, third["id"], boc["id"]):
                        merged += 1
                else:
                    add_candidate(
                        conn,
                        third["id"],
                        boc["id"],
                        "金额相同、时间接近，但缺少支付链路证据，请确认是否合并",
                    )
        conn.commit()
        return merged
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 导入结果与查询
# --------------------------------------------------------------------------


def import_file_object(file_storage, branch: Optional[str] = None) -> Dict[str, Any]:
    filename = file_storage.filename or "未命名文件"
    raw = file_storage.read()
    parsed = parse_file(filename, raw)
    if parsed.get("error"):
        return parsed

    conn = get_db()
    try:
        rules = load_merchant_rules(conn)
        inserted = 0
        skipped = 0
        for rec in parsed["transactions"]:
            if branch:
                rec["branch"] = branch
            rec = finalize_record(rec, rules)
            if insert_record(conn, rec):
                inserted += 1
            else:
                skipped += 1
        conn.commit()
        auto_merged = run_cross_source_dedup()
    finally:
        conn.close()

    parsed["inserted"] = inserted
    parsed["skipped"] = skipped
    parsed["auto_merged"] = auto_merged
    return parsed


def transaction_query(filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    clauses = ["is_deleted=0"]
    params: List[Any] = []
    if filters.get("month"):
        clauses.append("month=?")
        params.append(filters["month"])
    if filters.get("source"):
        clauses.append("source=?")
        params.append(filters["source"])
    if filters.get("category"):
        clauses.append("category=?")
        params.append(filters["category"])
    if filters.get("branch"):
        if filters["branch"] == "__summary__":
            clauses.append(
                "(branch='生活' OR branch IN (SELECT name FROM ledgers WHERE include_in_summary=1 AND deleted=0))"
            )
        else:
            clauses.append("branch=?")
            params.append(filters["branch"])
    if filters.get("count_in_expense") in ("0", "1"):
        clauses.append("count_in_expense=?")
        params.append(int(filters["count_in_expense"]))
    if filters.get("keyword"):
        kw = f"%{filters['keyword']}%"
        clauses.append(
            "(counterparty LIKE ? OR description LIKE ? OR note LIKE ? OR raw_line LIKE ?)"
        )
        params.extend([kw, kw, kw, kw])
    where = " AND ".join(clauses)
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT * FROM transactions WHERE {where} ORDER BY is_duplicate ASC, ts DESC, id DESC",
            params,
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


def compute_summary(month: Optional[str]) -> Dict[str, Any]:
    conn = get_db()
    try:
        available = month_list_from_db(conn)
        selected = month or (available[0] if available else datetime.now().strftime("%Y-%m"))

        def monthly_totals(m: str) -> Tuple[float, float, float]:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN direction='支出' THEN amount ELSE 0 END), 0) AS expense,
                    COALESCE(SUM(CASE WHEN direction='收入' THEN amount ELSE 0 END), 0) AS income
                FROM transactions
                WHERE month=? AND is_deleted=0 AND is_duplicate=0
                  AND count_in_expense=1
                  AND (branch='生活' OR branch IN (
                    SELECT name FROM ledgers WHERE include_in_summary=1 AND deleted=0
                  ))
                """,
                (m,),
            ).fetchone()
            income = float(row["income"] or 0)
            expense = float(row["expense"] or 0)
            return income, expense, round(expense - income, 2)

        total_income, total_expense, total = monthly_totals(selected)
        budget_row = conn.execute(
            "SELECT amount_cents FROM monthly_budgets WHERE month=?", (selected,)
        ).fetchone()
        budget = None
        if budget_row:
            budget_amount = budget_row["amount_cents"] / 100
            budget = {
                "amount": budget_amount,
                "spent": round(total_expense, 2),
                "remaining": round(budget_amount - total_expense, 2),
                "percent": round(total_expense / budget_amount * 100, 1),
            }
        by_category_rows = conn.execute(
            """
            SELECT category,
                COALESCE(SUM(CASE WHEN direction='支出' THEN amount ELSE 0 END), 0) AS expense,
                COALESCE(SUM(CASE WHEN direction='收入' THEN amount ELSE 0 END), 0) AS income,
                COUNT(*) AS cnt
            FROM transactions
            WHERE month=? AND is_deleted=0 AND is_duplicate=0
              AND count_in_expense=1
              AND (branch='生活' OR branch IN (
                SELECT name FROM ledgers WHERE include_in_summary=1 AND deleted=0
              ))
            GROUP BY category
            ORDER BY (expense - income) DESC
            """,
            (selected,),
        ).fetchall()
        by_category = []
        for r in by_category_rows:
            net_amount = round(float(r["expense"] or 0) - float(r["income"] or 0), 2)
            by_category.append(
                {
                    "category": r["category"],
                    "amount": net_amount,
                    "count": int(r["cnt"] or 0),
                    "percent": round(net_amount / total * 100, 1)
                    if total != 0
                    else 0.0,
                }
            )

        top_expenses = [dict(row) for row in conn.execute(
            """
            SELECT id, transaction_time, counterparty, description, category, amount
            FROM transactions
            WHERE month=? AND direction='支出' AND is_deleted=0 AND is_duplicate=0
              AND count_in_expense=1
              AND (branch='生活' OR branch IN (
                SELECT name FROM ledgers WHERE include_in_summary=1 AND deleted=0
              ))
            ORDER BY amount DESC, ts DESC, id DESC
            LIMIT 5
            """,
            (selected,),
        ).fetchall()]

        prev_month = month_add(selected, -1)
        _prev_income, _prev_expense, prev_total = monthly_totals(prev_month)
        prev_pct = (
            round((total - prev_total) / prev_total * 100, 1)
            if prev_total != 0
            else None
        )

        previous_months = [m for m in available if m < selected]
        if previous_months:
            sums = [monthly_totals(m)[2] for m in previous_months]
            hist_avg = round(sum(sums) / len(sums), 2)
            hist_pct = (
                round((total - hist_avg) / hist_avg * 100, 1) if hist_avg != 0 else None
            )
        else:
            hist_avg = None
            hist_pct = None

        return {
            "month": selected,
            "total": round(total, 2),
            "total_income": round(total_income, 2),
            "total_expense": round(total_expense, 2),
            "budget": budget,
            "categories": by_category,
            "top_expenses": top_expenses,
            "prev_month": prev_month,
            "prev_total": round(prev_total, 2),
            "prev_pct": prev_pct,
            "hist_avg": hist_avg,
            "hist_pct": hist_pct,
            "available_months": available,
        }
    finally:
        conn.close()


def pending_payload() -> Dict[str, Any]:
    conn = get_db()
    try:
        candidates = []
        rows = conn.execute(
            "SELECT * FROM dedup_candidates WHERE status='pending' ORDER BY id DESC"
        ).fetchall()
        for r in rows:
            a = conn.execute("SELECT * FROM transactions WHERE id=?", (r["a_id"],)).fetchone()
            b = conn.execute("SELECT * FROM transactions WHERE id=?", (r["b_id"],)).fetchone()
            if a and b:
                candidates.append(
                    {
                        "id": r["id"],
                        "a": row_to_dict(a),
                        "b": row_to_dict(b),
                        "reason": r["reason"],
                    }
                )

        merchants = conn.execute(
            """
            SELECT counterparty,
                   COUNT(*) AS cnt,
                   COALESCE(SUM(amount), 0) AS total,
                   MAX(month) AS last_month
            FROM transactions
            WHERE is_deleted=0 AND is_duplicate=0 AND category='其他'
              AND count_in_expense=1 AND direction='支出'
            GROUP BY counterparty
            ORDER BY cnt DESC, total DESC
            """
        ).fetchall()
        unclassified = [
            {
                "counterparty": r["counterparty"] or "(无对方)",
                "count": int(r["cnt"]),
                "total": round(float(r["total"] or 0), 2),
                "last_month": r["last_month"],
            }
            for r in merchants
        ]
        unclassified_rows = conn.execute(
            """
            SELECT * FROM transactions
            WHERE is_deleted=0 AND is_duplicate=0 AND category='其他'
              AND count_in_expense=1 AND direction='支出'
            ORDER BY ts DESC, id DESC
            """
        ).fetchall()
        unclassified_items = [row_to_dict(r) for r in unclassified_rows]
        return {
            "candidates": candidates,
            "unclassified": unclassified,
            "unclassified_items": unclassified_items,
        }
    finally:
        conn.close()


def _export_row_value(r: Dict[str, Any], key: str) -> Any:
    if key == "source":
        return SOURCES.get(r["source"], r["source"])
    if key == "count_in_expense":
        return "是" if r["count_in_expense"] else "否"
    if key == "is_duplicate":
        return "是" if r["is_duplicate"] else "否"
    if key == "merge_sources":
        return "+".join(r.get("merge_sources") or [])
    if key == "tags":
        return ",".join(r.get("tags") or [])
    return r.get(key, "")


def export_excel(
    month: Optional[str],
    columns: Optional[List[str]] = None,
    branch: Optional[str] = None,
) -> io.BytesIO:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    filters: Dict[str, Any] = {}
    if month:
        filters["month"] = month
    if branch:
        filters["branch"] = branch
    rows = transaction_query(filters)
    summary = compute_summary(month)

    wb = Workbook()
    ws = wb.active
    ws.title = "明细"
    field_specs = [
        {"key": "source", "label": "来源"},
        {"key": "transaction_time", "label": "交易时间"},
        {"key": "month", "label": "年-月"},
        {"key": "amount", "label": "金额"},
        {"key": "direction", "label": "收支方向"},
        {"key": "counterparty", "label": "交易对方"},
        {"key": "description", "label": "说明/摘要"},
        {"key": "type", "label": "交易类型"},
        {"key": "payment_method", "label": "支付方式"},
        {"key": "status", "label": "交易状态"},
        {"key": "transaction_no", "label": "交易单号"},
        {"key": "merchant_no", "label": "商户单号"},
        {"key": "note", "label": "备注"},
        {"key": "currency", "label": "币种"},
        {"key": "balance", "label": "余额"},
        {"key": "category", "label": "分类"},
        {"key": "category_source", "label": "分类来源"},
        {"key": "count_in_expense", "label": "是否计入支出"},
        {"key": "exclude_reason", "label": "不计入原因"},
        {"key": "tags", "label": "标签"},
        {"key": "is_duplicate", "label": "是否重复"},
        {"key": "duplicate_of", "label": "重复指向"},
        {"key": "merge_sources", "label": "合并来源"},
        {"key": "raw_line", "label": "原始行"},
    ]
    selected_keys = columns or [s["key"] for s in field_specs]
    selected_specs = [s for s in field_specs if s["key"] in selected_keys]
    headers = [s["label"] for s in selected_specs]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDEBF7")

    for r in rows:
        ws.append([_export_row_value(r, s["key"]) for s in selected_specs])

    ws2 = wb.create_sheet("月度汇总")
    ws2.append(["月份", "收入", "支出", "净消费", "环比上月", "环比历史月均"])
    for cell in ws2[1]:
        cell.font = Font(bold=True)
    for m in summary["available_months"] or [summary["month"]]:
        sub = compute_summary(m)
        prev_text = ""
        hist_text = ""
        if sub["prev_pct"] is not None:
            prev_text = f"{sub['prev_pct']:+.1f}%"
        if sub["hist_pct"] is not None:
            hist_text = f"{sub['hist_pct']:+.1f}%"
        ws2.append(
            [
                m,
                sub.get("total_income", 0),
                sub.get("total_expense", 0),
                sub["total"],
                prev_text,
                hist_text,
            ]
        )

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def api_error(message: str, status: int = 400):
    return jsonify({"error": message}), status


@app.errorhandler(404)
def handle_404(_exc):
    return api_error(f"接口不存在：{request.method} {request.path}", 404)


@app.errorhandler(500)
def handle_500(exc):
    logging.exception("unhandled server error")
    return api_error(f"服务器错误：{exc}", 500)


@app.route("/")
def index():
    return render_template("index.html", app_name=APP_NAME)


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "db": DB_PATH})


@app.post("/api/import")
def api_import():
    files = request.files.getlist("files")
    branch = request.form.get("branch") or None
    if not files:
        return api_error("未收到文件")
    results = []
    for f in files:
        try:
            results.append(import_file_object(f, branch))
        except Exception as exc:  # noqa: BLE001
            logging.exception("import failed: %s", f.filename)
            results.append(
                {
                    "filename": f.filename,
                    "source": None,
                    "encoding": "",
                    "error": str(exc),
                    "transactions": [],
                    "warnings": [],
                }
            )
    return jsonify({"results": results})


@app.post("/api/reset")
def api_reset():
    conn = get_db()
    try:
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM dedup_candidates")
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.get("/api/manual-income")
def api_manual_income_list():
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT * FROM transactions
            WHERE branch='成长' AND is_deleted=0
            ORDER BY ts DESC, id DESC
            """
        ).fetchall()
        return jsonify({"items": [row_to_dict(r) for r in rows]})
    finally:
        conn.close()


@app.post("/api/manual-income")
def api_manual_income_add():
    data = request.get_json(silent=True) or {}
    time_str, ts = parse_datetime(data.get("transaction_time"))
    amount = clean_money(data.get("amount"))
    if amount is None or amount <= 0:
        return api_error("请输入正确的收入金额")
    counterparty = (data.get("counterparty") or "").strip()
    if not counterparty:
        return api_error("请填写收入来源，例如实习工资、副业")
    description = (data.get("description") or "").strip()
    note = (data.get("note") or "").strip()
    category = data.get("category") or "其他"
    conn = get_db()
    try:
        if not is_valid_category(conn, category):
            category = "其他"
        rec = {
            "source": "manual",
            "transaction_time": time_str or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "month": month_from_time(time_str),
            "ts": ts,
            "amount": round(abs(amount), 2),
            "direction": "收入",
            "counterparty": counterparty,
            "description": description,
            "type": "手动收入",
            "payment_method": data.get("payment_method") or "",
            "status": "",
            "transaction_no": "",
            "merchant_no": "",
            "note": note,
            "currency": "人民币",
            "balance": "",
            "raw_line": f"手动收入 | {counterparty} | {description} | {amount}",
            "category": category,
            "category_source": "人工",
            "count_in_expense": 0,
            "exclude_reason": "手动独立记录",
            "unique_key": stable_key(
                "manual", time_str, amount, counterparty, description, datetime.now().isoformat()
            ),
        }
        inserted = insert_record(conn, rec)
        conn.commit()
        return jsonify({"ok": inserted, "item": rec})
    finally:
        conn.close()


@app.get("/api/ledgers")
def api_ledgers():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM ledgers ORDER BY created_at ASC, id ASC").fetchall()
        items = []
        for r in rows:
            stats = conn.execute(
                """
                SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total
                FROM transactions WHERE branch=? AND is_deleted=0
                """,
                (r["name"],),
            ).fetchone()
            items.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "include_in_summary": bool(r["include_in_summary"]),
                    "deleted": bool(r["deleted"]),
                    "count": int(stats["cnt"] or 0),
                    "total": round(float(stats["total"] or 0), 2),
                }
            )
        return jsonify({"items": items})
    finally:
        conn.close()


@app.post("/api/ledgers")
def api_create_ledger():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return api_error("账本名称不能为空")
    include = 1 if data.get("include_in_summary") else 0
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO ledgers (name, include_in_summary) VALUES (?, ?)",
            (name, include),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM ledgers WHERE name=?", (name,)).fetchone()
        return jsonify({"item": dict(row)})
    finally:
        conn.close()


@app.put("/api/ledgers/<int:ledger_id>")
def api_update_ledger(ledger_id: int):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM ledgers WHERE id=?", (ledger_id,)).fetchone()
        if not row:
            return api_error("账本不存在", 404)
        name = (data.get("name") or row["name"]).strip()
        include = 1 if data.get("include_in_summary") else 0
        old_name = row["name"]
        conn.execute(
            "UPDATE ledgers SET name=?, include_in_summary=? WHERE id=?",
            (name, include, ledger_id),
        )
        if name != old_name:
            conn.execute(
                "UPDATE transactions SET branch=? WHERE branch=?",
                (name, old_name),
            )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.delete("/api/ledgers/<int:ledger_id>")
def api_delete_ledger(ledger_id: int):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM ledgers WHERE id=?", (ledger_id,)).fetchone()
        if not row:
            return api_error("账本不存在", 404)
        conn.execute("UPDATE ledgers SET deleted=1, include_in_summary=0 WHERE id=?", (ledger_id,))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/ledgers/<int:ledger_id>/restore")
def api_restore_ledger(ledger_id: int):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM ledgers WHERE id=?", (ledger_id,)).fetchone()
        if not row:
            return api_error("账本不存在", 404)
        conn.execute("UPDATE ledgers SET deleted=0 WHERE id=?", (ledger_id,))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/ledgers/<int:ledger_id>/dissolve")
def api_dissolve_ledger(ledger_id: int):
    data = request.get_json(silent=True) or {}
    mode = data.get("mode") or "manual"
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM ledgers WHERE id=?", (ledger_id,)).fetchone()
        if not row:
            return api_error("账本不存在", 404)
        name = row["name"]
        tx_rows = conn.execute(
            "SELECT * FROM transactions WHERE branch=? AND is_deleted=0",
            (name,),
        ).fetchall()
        rules = load_merchant_rules(conn)
        for tx in tx_rows:
            if mode == "auto":
                rec = row_to_dict(tx)
                category, source = classify_transaction(rec, rules)
            else:
                category, source = "其他", "人工"
            conn.execute(
                "UPDATE transactions SET branch='生活', category=?, category_source=? WHERE id=?",
                (category, source, tx["id"]),
            )
        conn.execute("UPDATE ledgers SET deleted=1, include_in_summary=0 WHERE id=?", (ledger_id,))
        conn.commit()
        return jsonify({"ok": True, "moved": len(tx_rows)})
    finally:
        conn.close()


@app.post("/api/manual/add")
def api_manual_add():
    data = request.get_json(silent=True) or {}
    direction = data.get("direction")
    group_name = data.get("group") or "成长"
    branch = data.get("branch") or "生活"
    amount = clean_money(data.get("amount"))
    category = data.get("category")
    time_raw = data.get("transaction_time")
    counterparty = (data.get("counterparty") or "").strip()
    description = (data.get("description") or "").strip()
    note = (data.get("note") or "").strip()
    payment_method = (data.get("payment_method") or "").strip()
    if direction not in ("收入", "支出"):
        return api_error("收支方向不正确")
    if amount is None or amount <= 0:
        return api_error("金额必须大于 0")
    time_str, ts = parse_datetime(time_raw)
    if not time_str:
        return api_error("时间格式不正确")
    conn = get_db()
    try:
        if not is_valid_category(conn, category):
            return api_error("分类不合法")
        rec = {
            "source": "manual",
            "transaction_time": time_str,
            "month": month_from_time(time_str),
            "ts": ts,
            "amount": round(abs(float(amount)), 2),
            "direction": direction,
            "counterparty": counterparty,
            "description": description,
            "type": "",
            "payment_method": payment_method,
            "status": "手动添加",
            "transaction_no": "",
            "merchant_no": "",
            "note": note,
            "currency": "人民币",
            "balance": "",
            "raw_line": "手动添加 | " + (description or counterparty or direction),
            "category": category,
            "category_source": "人工",
            "count_in_expense": 1,
            "exclude_reason": "",
            "branch": branch,
            "unique_key": stable_key("manual", uuid.uuid4().hex),
        }
        if insert_record(conn, rec):
            conn.commit()
            return jsonify({"ok": True})
        return api_error("保存失败", 500)
    finally:
        conn.close()


@app.get("/api/transactions")
def api_transactions():
    return jsonify(
        {
            "items": transaction_query(
                {
                    "month": request.args.get("month"),
                    "source": request.args.get("source"),
                    "category": request.args.get("category"),
                    "count_in_expense": request.args.get("count_in_expense"),
                    "keyword": request.args.get("keyword"),
                    "branch": request.args.get("branch"),
                }
            )
        }
    )


@app.get("/api/summary")
def api_summary():
    return jsonify(compute_summary(request.args.get("month")))


@app.put("/api/budgets/<month>")
def api_set_budget(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return api_error("月份格式应为 YYYY-MM")
    data = request.get_json(silent=True) or {}
    try:
        amount = Decimal(str(data.get("amount", "")))
    except InvalidOperation:
        return api_error("请输入有效的预算金额")
    if not amount.is_finite() or amount <= 0 or amount > 1000000000 or amount.as_tuple().exponent < -2:
        return api_error("预算须为大于 0、最多两位小数的金额")
    cents = int(amount * 100)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO monthly_budgets(month, amount_cents) VALUES (?, ?) "
            "ON CONFLICT(month) DO UPDATE SET amount_cents=excluded.amount_cents",
            (month, cents),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.delete("/api/budgets/<month>")
def api_delete_budget(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return api_error("月份格式应为 YYYY-MM")
    conn = get_db()
    try:
        conn.execute("DELETE FROM monthly_budgets WHERE month=?", (month,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.get("/api/pending")
def api_pending():
    return jsonify(pending_payload())


@app.post("/api/transactions/<int:tx_id>/category")
def api_set_category(tx_id: int):
    data = request.get_json(silent=True) or {}
    category = data.get("category")
    remember = data.get("remember", True)
    conn = get_db()
    try:
        if not is_valid_category(conn, category):
            return api_error("分类不合法")
        row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        if not row:
            return api_error("记录不存在", 404)
        conn.execute(
            "UPDATE transactions SET category=?, category_source='人工' WHERE id=?",
            (category, tx_id),
        )
        cpty = (row["counterparty"] or "").strip()
        if remember and cpty:
            existing = conn.execute(
                "SELECT * FROM merchant_rule_items WHERE counterparty=?",
                (cpty,),
            ).fetchall()
            same_general = any(
                r["category"] == category and not (r["match_text"] or "").strip()
                for r in existing
            )
            if not same_general:
                # 若同一商户已经有不同分类，则用“说明/备注”生成更细的规则，
                # 避免覆盖该商户原来的分类。
                text = (row["description"] or "").strip() or (row["note"] or "").strip()
                match_text = text[:40] if text else ""
                conn.execute(
                    """
                    INSERT INTO merchant_rule_items (counterparty, match_text, category)
                    VALUES (?, ?, ?)
                    ON CONFLICT(counterparty, match_text)
                    DO UPDATE SET category=excluded.category
                    """,
                    (cpty, match_text, category),
                )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/transactions/<int:tx_id>/count")
def api_toggle_count(tx_id: int):
    data = request.get_json(silent=True) or {}
    count = 1 if data.get("count_in_expense") else 0
    reason = "" if count else data.get("exclude_reason") or "手工不计入"
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        if not row:
            return api_error("记录不存在", 404)
        conn.execute(
            "UPDATE transactions SET count_in_expense=?, exclude_reason=? WHERE id=?",
            (count, reason, tx_id),
        )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/transactions/<int:tx_id>/branch")
def api_set_branch(tx_id: int):
    data = request.get_json(silent=True) or {}
    branch = data.get("branch")
    conn = get_db()
    try:
        if branch != "生活":
            ledger = conn.execute(
                "SELECT id FROM ledgers WHERE name=? AND deleted=0",
                (branch,),
            ).fetchone()
            if not ledger:
                return api_error("独立账本不存在")
        row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        if not row:
            return api_error("记录不存在", 404)
        conn.execute("UPDATE transactions SET branch=? WHERE id=?", (branch, tx_id))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/transactions/<int:tx_id>/delete")
def api_delete(tx_id: int):
    conn = get_db()
    try:
        conn.execute("UPDATE transactions SET is_deleted=1 WHERE id=?", (tx_id,))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/transactions/batch-delete")
def api_batch_delete():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return api_error("未选择记录")
    conn = get_db()
    try:
        conn.executemany(
            "UPDATE transactions SET is_deleted=1 WHERE id=?",
            [(int(i),) for i in ids],
        )
        conn.commit()
        return jsonify({"ok": True, "deleted": len(ids)})
    finally:
        conn.close()


@app.post("/api/transactions/batch-category")
def api_batch_category():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    category = data.get("category")
    if not isinstance(ids, list) or not ids:
        return api_error("未选择记录")
    conn = get_db()
    try:
        if not is_valid_category(conn, category):
            return api_error("分类不合法")
        conn.executemany(
            "UPDATE transactions SET category=?, category_source='人工' WHERE id=?",
            [(category, int(i)) for i in ids],
        )
        conn.commit()
        return jsonify({"ok": True, "updated": len(ids)})
    finally:
        conn.close()


@app.post("/api/transactions/<int:tx_id>/restore")
def api_restore(tx_id: int):
    conn = get_db()
    try:
        conn.execute("UPDATE transactions SET is_deleted=0 WHERE id=?", (tx_id,))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/classify/merchant")
def api_classify_merchant():
    data = request.get_json(silent=True) or {}
    counterparty = (data.get("counterparty") or "").strip()
    category = data.get("category")
    if not counterparty:
        return api_error("商户名不能为空")
    conn = get_db()
    try:
        if not is_valid_category(conn, category):
            return api_error("分类不合法")
        conn.execute(
            """
            UPDATE transactions SET category=?, category_source='人工'
            WHERE counterparty=? AND is_deleted=0 AND is_duplicate=0 AND category='其他'
            """,
            (category, counterparty),
        )
        conn.execute(
            """
            INSERT INTO merchant_rule_items (counterparty, match_text, category)
            VALUES (?, '', ?)
            ON CONFLICT(counterparty, match_text)
            DO UPDATE SET category=excluded.category
            """,
            (counterparty, category),
        )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/auto-classify")
def api_auto_classify():
    conn = get_db()
    try:
        rules = load_merchant_rules(conn)
        rows = conn.execute(
            """
            SELECT * FROM transactions
            WHERE is_deleted=0 AND is_duplicate=0 AND category='其他'
            """
        ).fetchall()
        changed = 0
        for row in rows:
            rec = row_to_dict(row)
            category, category_source = classify_transaction(rec, rules)
            if category != "其他":
                conn.execute(
                    "UPDATE transactions SET category=?, category_source='自动' WHERE id=?",
                    (category, row["id"]),
                )
                changed += 1
        conn.commit()
        return jsonify({"changed": changed})
    finally:
        conn.close()


@app.get("/api/merchant-rules")
def api_merchant_rules():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM merchant_rule_items ORDER BY counterparty, match_text, id"
        ).fetchall()
        return jsonify({"items": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.delete("/api/merchant-rules/<int:rule_id>")
def api_delete_merchant_rule(rule_id: int):
    conn = get_db()
    try:
        conn.execute("DELETE FROM merchant_rule_items WHERE id=?", (rule_id,))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.get("/api/categories")
def api_categories():
    conn = get_db()
    try:
        custom_rows = conn.execute(
            "SELECT id, name FROM custom_categories ORDER BY id ASC"
        ).fetchall()
        return jsonify(
            {
                "base": CATEGORIES,
                "custom": [{"id": r["id"], "name": r["name"]} for r in custom_rows],
            }
        )
    finally:
        conn.close()


@app.post("/api/categories")
def api_create_category():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    group_name = data.get("group") or "生活"
    if group_name not in ("生活", "成长"):
        return api_error("分组不正确")
    if not name:
        return api_error("分类名不能为空")
    if name in CATEGORIES:
        return api_error("该分类已存在，无需添加")
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO custom_categories (name, group_name) VALUES (?, ?)",
            (name, group_name),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM custom_categories WHERE name=?", (name,)
        ).fetchone()
        return jsonify({"item": dict(row)})
    finally:
        conn.close()


@app.put("/api/categories/<int:cat_id>")
def api_rename_category(cat_id: int):
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return api_error("分类名不能为空")
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM custom_categories WHERE id=?", (cat_id,)).fetchone()
        if not row:
            return api_error("分类不存在", 404)
        old_name = row["name"]
        conn.execute(
            "UPDATE custom_categories SET name=? WHERE id=?",
            (name, cat_id),
        )
        conn.execute(
            "UPDATE transactions SET category=? WHERE category=?",
            (name, old_name),
        )
        conn.execute(
            "UPDATE merchant_rule_items SET category=? WHERE category=?",
            (name, old_name),
        )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.delete("/api/categories/<int:cat_id>")
def api_delete_category(cat_id: int):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM custom_categories WHERE id=?", (cat_id,)
        ).fetchone()
        if not row:
            return api_error("分类不存在", 404)
        name = row["name"]
        if name in CATEGORIES:
            return api_error("固定分类不能删除")
        conn.execute("DELETE FROM custom_categories WHERE id=?", (cat_id,))
        conn.execute(
            "UPDATE transactions SET category='其他' WHERE category=?",
            (name,),
        )
        conn.execute("DELETE FROM merchant_rule_items WHERE category=?", (name,))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.get("/api/tags")
def api_tags():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM tag_defs ORDER BY created_at ASC, id ASC"
        ).fetchall()
        return jsonify({"items": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.post("/api/tags")
def api_create_tag():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    color = (data.get("color") or "#2563eb").strip() or "#2563eb"
    if not name:
        return api_error("标签名不能为空")
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO tag_defs (name, color) VALUES (?, ?)",
            (name, color),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tag_defs WHERE name=?", (name,)).fetchone()
        return jsonify({"item": dict(row)})
    finally:
        conn.close()


@app.delete("/api/tags/<int:tag_id>")
def api_delete_tag(tag_id: int):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM tag_defs WHERE id=?", (tag_id,)).fetchone()
        if not row:
            return api_error("标签不存在", 404)
        name = row["name"]
        conn.execute("DELETE FROM tag_defs WHERE id=?", (tag_id,))
        tx_rows = conn.execute(
            "SELECT id, tags FROM transactions WHERE tags != '[]'"
        ).fetchall()
        for tx in tx_rows:
            try:
                tags = json.loads(tx["tags"])
            except (TypeError, json.JSONDecodeError):
                tags = []
            if name in tags:
                tags = [t for t in tags if t != name]
                conn.execute(
                    "UPDATE transactions SET tags=? WHERE id=?",
                    (json.dumps(tags, ensure_ascii=False), tx["id"]),
                )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/transactions/<int:tx_id>/tags")
def api_set_transaction_tags(tx_id: int):
    data = request.get_json(silent=True) or {}
    raw_tags = data.get("tags") or []
    if not isinstance(raw_tags, list):
        return api_error("tags 必须是数组")
    names: List[str] = []
    for item in raw_tags:
        name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        if not row:
            return api_error("记录不存在", 404)
        for name in names:
            conn.execute(
                "INSERT OR IGNORE INTO tag_defs (name, color) VALUES (?, '#2563eb')",
                (name,),
            )
        conn.execute(
            "UPDATE transactions SET tags=? WHERE id=?",
            (json.dumps(names, ensure_ascii=False), tx_id),
        )
        conn.commit()
        return jsonify({"ok": True, "tags": names})
    finally:
        conn.close()


@app.post("/api/dedup/candidates/<int:cid>/merge")
def api_candidate_merge(cid: int):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM dedup_candidates WHERE id=?", (cid,)).fetchone()
        if not row:
            return api_error("候选不存在", 404)
        primary_id = row["a_id"]
        secondary_id = row["b_id"]
        a = conn.execute("SELECT * FROM transactions WHERE id=?", (primary_id,)).fetchone()
        b = conn.execute("SELECT * FROM transactions WHERE id=?", (secondary_id,)).fetchone()
        if a and a["source"] == "boc" and b and b["source"] in ("wx", "alipay"):
            primary_id, secondary_id = secondary_id, primary_id
        result = merge_transactions(conn, primary_id, secondary_id)
        if not result:
            return api_error("合并失败")
        return jsonify({"ok": True, "item": result})
    finally:
        conn.close()


@app.post("/api/dedup/candidates/<int:cid>/ignore")
def api_candidate_ignore(cid: int):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE dedup_candidates SET status='resolved_ignored' WHERE id=?", (cid,)
        )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/dedup/run")
def api_dedup_run():
    return jsonify({"merged": run_cross_source_dedup()})


@app.get("/api/merge-groups")
def api_merge_groups():
    conn = get_db()
    try:
        primaries = conn.execute(
            "SELECT * FROM transactions WHERE is_deleted=0 AND is_duplicate=0"
        ).fetchall()
        groups = []
        for p in primaries:
            secondaries = conn.execute(
                """
                SELECT * FROM transactions
                WHERE is_deleted=0 AND is_duplicate=1 AND duplicate_of=?
                ORDER BY ts DESC, id DESC
                """,
                (p["id"],),
            ).fetchall()
            if secondaries:
                groups.append(
                    {
                        "primary": row_to_dict(p),
                        "secondaries": [row_to_dict(s) for s in secondaries],
                    }
                )
        groups.sort(key=lambda g: g["primary"].get("ts") or 0, reverse=True)
        return jsonify({"groups": groups})
    finally:
        conn.close()


@app.post("/api/merge")
def api_merge():
    data = request.get_json(silent=True) or {}
    a_id = data.get("primary_id")
    b_id = data.get("secondary_id")
    if not a_id or not b_id:
        return api_error("缺少记录 ID")
    conn = get_db()
    try:
        result = merge_transactions(conn, int(a_id), int(b_id))
        if not result:
            return api_error("合并失败，请检查两条记录是否都存在且不同")
        return jsonify({"ok": True, "item": result})
    finally:
        conn.close()


@app.post("/api/unmerge")
def api_unmerge():
    data = request.get_json(silent=True) or {}
    primary_id = data.get("primary_id")
    secondary_id = data.get("secondary_id")
    if not primary_id or not secondary_id:
        return api_error("缺少记录 ID")
    conn = get_db()
    try:
        ok = unmerge_transactions(conn, int(primary_id), int(secondary_id))
        if not ok:
            return api_error("撤销合并失败：该记录未合并到指定主记录")
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.get("/api/export")
def api_export():
    month = request.args.get("month")
    columns_raw = request.args.get("columns")
    columns = (
        [c.strip() for c in columns_raw.split(",") if c.strip()]
        if columns_raw
        else None
    )
    try:
        out = export_excel(month, columns)
    except Exception as exc:  # noqa: BLE001
        logging.exception("export failed")
        return api_error(f"导出失败：{exc}", 500)
    filename = f"账单分析_{month or '全部'}.xlsx"
    return send_file(
        out,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# --------------------------------------------------------------------------
# 桌面启动
# --------------------------------------------------------------------------


def find_free_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_desktop() -> None:
    init_db()
    port = find_free_port()
    url = f"http://127.0.0.1:{port}"
    threading.Thread(
        target=lambda: app.run(
            host="127.0.0.1",
            port=port,
            debug=False,
            use_reloader=False,
            threaded=True,
        ),
        daemon=True,
    ).start()

    # 给本地服务一点启动时间。
    for _ in range(50):
        try:
            import urllib.request

            urllib.request.urlopen(url + "/api/health", timeout=0.2)
            break
        except Exception:
            time.sleep(0.1)

    try:
        import webview  # type: ignore

        webview.create_window(APP_NAME, url, width=1280, height=860)
        webview.start()
        return
    except Exception:
        # 没有 pywebview 时直接调用系统浏览器，保留本地服务。
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    start_desktop()
