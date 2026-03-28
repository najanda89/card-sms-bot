import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "card_history.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_company TEXT,
            merchant TEXT,
            amount INTEGER,
            total_amount INTEGER,
            installment_months INTEGER,
            payment_type TEXT,
            tx_type TEXT DEFAULT '승인',
            date TEXT,
            time TEXT,
            memo TEXT DEFAULT '',
            category TEXT DEFAULT '',
            subcategory TEXT DEFAULT '',
            raw_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 기존 DB에 컬럼 없으면 추가 (마이그레이션)
    for col, definition in [
        ("memo", "TEXT DEFAULT ''"),
        ("total_amount", "INTEGER"),
        ("installment_months", "INTEGER"),
        ("telegram_msg_id", "INTEGER"),
        ("category", "TEXT DEFAULT ''"),
        ("subcategory", "TEXT DEFAULT ''"),
        ("classification", "TEXT DEFAULT ''"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE transactions ADD COLUMN {col} {definition}")
        except:
            pass
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_setting(key: str, default=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def get_monthly_total_by_company() -> dict:
    """이번 달 카드사별 순 지출 합계 반환. 할부는 총금액 기준."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT card_company,
               COALESCE(SUM(CASE WHEN tx_type = '승인'
                   THEN COALESCE(total_amount, amount)
                   ELSE -COALESCE(total_amount, amount) END), 0)
        FROM transactions
        WHERE date LIKE strftime('%Y.%m', 'now') || '.%'
        GROUP BY card_company
    """)
    rows = cursor.fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows if row[0]}


def save_transaction(parsed: dict, raw_text: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO transactions (card_company, merchant, amount, total_amount, installment_months, payment_type, tx_type, date, time, raw_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        parsed.get("카드사"),
        parsed.get("가맹점"),
        parsed.get("금액"),
        parsed.get("총금액"),
        parsed.get("할부개월"),
        parsed.get("결제방식"),
        parsed.get("거래유형", "승인"),
        parsed.get("날짜"),
        parsed.get("시간"),
        raw_text
    ))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return tx_id


def update_telegram_msg_id(tx_id: int, msg_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE transactions SET telegram_msg_id = ? WHERE id = ?", (msg_id, tx_id))
    conn.commit()
    conn.close()


def get_tx_id_by_msg_id(msg_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM transactions WHERE telegram_msg_id = ?", (msg_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def update_memo(tx_id: int, memo: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE transactions SET memo = ? WHERE id = ?", (memo, tx_id))
    conn.commit()
    conn.close()


def update_merchant(tx_id: int, merchant: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE transactions SET merchant = ? WHERE id = ?", (merchant, tx_id))
    conn.commit()
    conn.close()


def get_all_transactions():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM transactions ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_summary():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COALESCE(SUM(CASE WHEN tx_type = '승인'
            THEN COALESCE(total_amount, amount)
            ELSE -COALESCE(total_amount, amount) END), 0) as total
        FROM transactions
        WHERE date LIKE strftime('%Y.%m', 'now') || '.%'
    """)
    monthly_total = cursor.fetchone()["total"]

    cursor.execute("""
        SELECT card_company,
            SUM(CASE WHEN tx_type = '승인' THEN amount ELSE -amount END) as total,
            COUNT(*) as count
        FROM transactions
        GROUP BY card_company
    """)
    by_card = [dict(row) for row in cursor.fetchall()]

    cursor.execute("""
        SELECT merchant,
            SUM(CASE WHEN tx_type = '승인' THEN amount ELSE -amount END) as total,
            COUNT(*) as count
        FROM transactions
        GROUP BY merchant
        ORDER BY total DESC
        LIMIT 5
    """)
    by_merchant = [dict(row) for row in cursor.fetchall()]

    conn.close()
    return {
        "monthly_total": monthly_total,
        "by_card": by_card,
        "by_merchant": by_merchant
    }

def get_monthly_total() -> int:
    """이번 달 순 지출 합계 반환. 할부는 총금액 기준."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COALESCE(SUM(CASE WHEN tx_type = '승인'
            THEN COALESCE(total_amount, amount)
            ELSE -COALESCE(total_amount, amount) END), 0)
        FROM transactions
        WHERE date LIKE strftime('%Y.%m', 'now') || '.%'
    """)
    total = cursor.fetchone()[0]
    conn.close()
    return total


def update_amount(tx_id: int, amount: int, total_amount: int = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if total_amount is not None:
        cursor.execute(
            "UPDATE transactions SET amount = ?, total_amount = ? WHERE id = ?",
            (amount, total_amount, tx_id)
        )
    else:
        cursor.execute(
            "UPDATE transactions SET amount = ?, total_amount = NULL, installment_months = NULL WHERE id = ?",
            (amount, tx_id)
        )
    conn.commit()
    conn.close()


def update_classification(tx_id: int, classification: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE transactions SET classification = ? WHERE id = ?",
        (classification, tx_id)
    )
    conn.commit()
    conn.close()


def update_category(tx_id: int, category: str, subcategory: str = ""):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE transactions SET category = ?, subcategory = ? WHERE id = ?",
        (category, subcategory or "", tx_id)
    )
    conn.commit()
    conn.close()


def get_transactions_for_export(year_month: str = None):
    """year_month 형식: '2026-03' → date LIKE '2026.03.%'"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if year_month:
        date_prefix = year_month.replace("-", ".") + ".%"
        cursor.execute(
            "SELECT * FROM transactions WHERE date LIKE ? ORDER BY date, time",
            (date_prefix,)
        )
    else:
        cursor.execute("SELECT * FROM transactions ORDER BY date, time")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def delete_transaction(tx_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    conn.commit()
    conn.close()


def delete_all_transactions():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM transactions")
    conn.commit()
    conn.close()
