"""
utils.py — 공용 유틸리티
    - Telegram 동기 전송 (Flask 스레드용)
    - 카드 실적 목표 관리
    - 진행률 바
"""
import os
import json
import logging
from typing import Optional

import requests as req

from database import get_monthly_total_by_company, get_setting, set_setting

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 카드 실적 목표 {"광주카드": 300000, ...}
monthly_targets: dict = {}
_target_notified: set = set()  # {("2026.03", "광주카드"), ...}

# 카드 한도 {"광주카드": 500000, ...}
card_limits: dict = {}


# ── 실적 목표 영속화 ────────────────────────────────────────────

def load_targets():
    raw = get_setting("monthly_targets")
    if raw:
        try:
            monthly_targets.update(json.loads(raw))
        except Exception:
            pass


def save_targets():
    set_setting("monthly_targets", json.dumps(monthly_targets, ensure_ascii=False))


# ── 카드 한도 영속화 ────────────────────────────────────────────

def load_limits():
    raw = get_setting("card_limits")
    if raw:
        try:
            card_limits.update(json.loads(raw))
        except Exception:
            pass


def save_limits():
    set_setting("card_limits", json.dumps(card_limits, ensure_ascii=False))


def find_company(keyword: str) -> str:
    """키워드로 카드사명 매핑 (부분 일치)."""
    kw = keyword.replace(" ", "").lower()
    candidates = list(monthly_targets.keys()) + ["광주카드", "KB국민카드", "현대카드", "신한카드", "삼성카드"]
    for company in candidates:
        if kw in company.replace(" ", "").lower():
            return company
    return keyword


# ── Telegram 동기 전송 ──────────────────────────────────────────

def send_telegram_sync(text: str, reply_markup: dict = None) -> Optional[int]:
    """Flask 스레드에서 Telegram 메시지 전송. 성공 시 message_id 반환."""
    payload = {"chat_id": CHAT_ID, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = req.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json=payload,
            timeout=10
        )
        data = resp.json()
        if data.get("ok"):
            return data["result"]["message_id"]
    except Exception as e:
        logging.error(f"Telegram 전송 오류: {e}")
    return None


# ── 진행률 바 ───────────────────────────────────────────────────

def progress_bar(current: int, target: int, width: int = 10) -> str:
    ratio = min(current / target, 1.0) if target > 0 else 0
    filled = round(ratio * width)
    bar = "▓" * filled + "░" * (width - filled)
    return f"[{bar}] {ratio*100:.0f}%"


def monthly_progress_text(card_company: str, company_total: int) -> str:
    target = monthly_targets.get(card_company)
    if not target:
        return ""
    bar = progress_bar(company_total, target)
    return (
        f"\n\n📊 {card_company} 실적\n"
        f"{bar}\n"
        f"{company_total:,}원 / {target:,}원"
    )


def limit_remaining_text(card_company: str, company_total: Optional[int]) -> str:
    """카드 한도 대비 사용액/잔여 한도 텍스트."""
    limit = card_limits.get(card_company)
    if not limit:
        return ""
    if company_total is None:
        return f"\n\n💳 {card_company} 한도\n누적 데이터 없음 / 한도 {limit:,}원"
    remaining = limit - company_total
    bar = progress_bar(company_total, limit)
    if remaining < 0:
        return (
            f"\n\n⚠️ {card_company} 한도 초과!\n"
            f"{bar}\n"
            f"사용 {company_total:,}원 / 한도 {limit:,}원\n"
            f"초과 {abs(remaining):,}원"
        )
    return (
        f"\n\n💳 {card_company} 한도\n"
        f"{bar}\n"
        f"사용 {company_total:,}원 / 한도 {limit:,}원\n"
        f"잔여 {remaining:,}원"
    )


def check_and_notify_target(card_company: str, prev_total: int, new_total: int):
    """카드사별 실적 목표 달성 시 알림 (월 1회)."""
    from datetime import date
    month_key = date.today().strftime("%Y.%m")
    notify_key = (month_key, card_company)
    target = monthly_targets.get(card_company)
    if not target or notify_key in _target_notified:
        return
    if prev_total < target <= new_total:
        _target_notified.add(notify_key)
        over = new_total - target
        send_telegram_sync(
            f"🎉 {card_company} 실적 달성!\n\n"
            f"목표  :  {target:,}원\n"
            f"현재  :  {new_total:,}원\n"
            f"초과  :  +{over:,}원"
        )
