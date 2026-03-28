"""
scheduler.py — DB 자동 백업 및 지출 요약 전송 스케줄러
"""
import os
import shutil
import time
import calendar
import logging
from datetime import datetime, date, timedelta

from database import get_all_transactions, DB_PATH
from utils import send_telegram_sync


# ── DB 백업 ────────────────────────────────────────────────────

def backup_db():
    """DB 백업. 최근 7일치만 보관."""
    backup_dir = os.path.join(os.path.dirname(DB_PATH), "backups")
    os.makedirs(backup_dir, exist_ok=True)

    today = date.today().strftime("%Y-%m-%d")
    dst = os.path.join(backup_dir, f"card_history_{today}.db")
    try:
        shutil.copy2(DB_PATH, dst)
        logging.info(f"✅ DB 백업 완료: {dst}")

        cutoff = date.today() - timedelta(days=7)
        deleted = []
        for fname in os.listdir(backup_dir):
            if not fname.startswith("card_history_") or not fname.endswith(".db"):
                continue
            try:
                fdate = datetime.strptime(fname, "card_history_%Y-%m-%d.db").date()
                if fdate < cutoff:
                    os.remove(os.path.join(backup_dir, fname))
                    deleted.append(fname)
            except ValueError:
                pass
        if deleted:
            logging.info(f"🗑️ 오래된 백업 삭제: {deleted}")
    except Exception as e:
        logging.error(f"DB 백업 실패: {e}")
        send_telegram_sync(f"⚠️ DB 백업 실패\n{e}")


# ── 지출 요약 ──────────────────────────────────────────────────

def _tx_in_range(txs: list, from_date: date, to_date: date) -> list:
    result = []
    for t in txs:
        if not t.get("date"):
            continue
        try:
            parts = t["date"].split(".")
            d = date(int(parts[0]), int(parts[1]), int(parts[2]))
            if from_date <= d <= to_date:
                result.append(t)
        except Exception:
            pass
    return result


def _effective_amount(t: dict) -> int:
    """할부는 총금액, 일시불은 금액 사용."""
    a = t.get("total_amount") or t["amount"]
    return a if t.get("tx_type") != "취소" else -a


def _build_summary_msg(txs: list, title: str) -> str:
    if not txs:
        return f"{title}\n\n내역이 없어요 💸"

    total = sum(_effective_amount(t) for t in txs)
    approve_cnt = sum(1 for t in txs if t.get("tx_type") != "취소")
    cancel_cnt  = sum(1 for t in txs if t.get("tx_type") == "취소")

    # 카테고리별
    by_cat: dict = {}
    for t in txs:
        cat = t.get("category") or "미분류"
        by_cat[cat] = by_cat.get(cat, 0) + _effective_amount(t)

    # 가맹점별
    by_merchant: dict = {}
    for t in txs:
        m = t.get("merchant") or "기타"
        by_merchant[m] = by_merchant.get(m, 0) + _effective_amount(t)

    top_cats     = sorted(by_cat.items(),      key=lambda x: x[1], reverse=True)[:5]
    top_merchants = sorted(by_merchant.items(), key=lambda x: x[1], reverse=True)[:3]

    lines = [
        title,
        "━" * 22,
        f"💰 합계: {total:,}원  ({approve_cnt}건" + (f" / 취소 {cancel_cnt}건" if cancel_cnt else "") + ")",
        "",
        "📂 카테고리별",
    ]
    for cat, amt in top_cats:
        lines.append(f"  • {cat}  {amt:,}원")

    lines.append("")
    lines.append("🏪 많이 쓴 가맹점")
    for merchant, amt in top_merchants:
        lines.append(f"  • {merchant}  {amt:,}원")

    return "\n".join(lines)


def send_daily_summary():
    today = date.today()
    txs = _tx_in_range(get_all_transactions(), today, today)
    title = f"📅 일별 요약 ({today.strftime('%Y.%m.%d')})"
    send_telegram_sync(_build_summary_msg(txs, title))


def send_weekly_summary():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    txs = _tx_in_range(get_all_transactions(), monday, today)
    title = f"📆 주간 요약 ({monday.strftime('%m.%d')} ~ {today.strftime('%m.%d')})"
    send_telegram_sync(_build_summary_msg(txs, title))


def send_monthly_summary():
    today = date.today()
    first = date(today.year, today.month, 1)
    txs = _tx_in_range(get_all_transactions(), first, today)
    title = f"🗓️ 월간 요약 ({today.year}년 {today.month}월)"
    send_telegram_sync(_build_summary_msg(txs, title))


# ── 스케줄러 루프 ───────────────────────────────────────────────

def run_scheduler():
    """매일 특정 시각에 작업 실행.
    - 03:00 DB 백업
    - 22:00 지출 요약 (우선순위: 월말 > 주간(토요일) > 일별)
    """
    last_summary_date = None
    last_backup_date = None
    while True:
        now = datetime.now()
        today = now.date()
        last_day = calendar.monthrange(today.year, today.month)[1]

        if now.hour == 3 and now.minute == 0 and last_backup_date != today:
            last_backup_date = today
            backup_db()

        if now.hour == 22 and now.minute == 0 and last_summary_date != today:
            last_summary_date = today
            if today.day == last_day:
                send_monthly_summary()
            elif today.weekday() == 5:  # 토요일
                send_weekly_summary()
            else:
                send_daily_summary()

        time.sleep(60 - now.second)
