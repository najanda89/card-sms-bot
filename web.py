"""
web.py — Flask 웹 서버 + 대시보드
"""
import hashlib
import logging
import time
import threading
import urllib.request

from flask import Flask, request, jsonify, send_from_directory
import os

from categories import CATEGORIES, build_type_keyboard_dict
from database import (
    save_transaction, update_memo, update_merchant, update_telegram_msg_id,
    get_all_transactions, get_monthly_total_by_company,
    update_category, update_amount, delete_transaction, delete_all_transactions,
    update_classification,
)
from parser import parse_card_message, format_result, extract_cumulative
from utils import (
    send_telegram_sync, monthly_targets, card_limits,
    monthly_progress_text, limit_remaining_text, check_and_notify_target,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
flask_app = Flask(__name__, static_folder=os.path.join(_HERE, "static"))

# ── 중복 요청 방지 캐시 (300초 TTL) ────────────────────────────
# sms_watcher의 lookback 윈도우(120초)보다 길게 설정해야 재시도 시 중복 저장 방지
_card_dedup: dict = {}
_DEDUP_TTL = 300  # 초 (5분)

def _is_duplicate(text: str) -> bool:
    key = hashlib.md5(text.encode()).hexdigest()
    now = time.time()
    # 만료된 항목 정리
    expired = [k for k, t in list(_card_dedup.items()) if now - t > _DEDUP_TTL]
    for k in expired:
        del _card_dedup[k]
    if key in _card_dedup:
        return True
    _card_dedup[key] = now
    return False


# ── Flask 라우트 ────────────────────────────────────────────────

@flask_app.route("/")
def dashboard():
    return send_from_directory(flask_app.static_folder, "dashboard.html")


@flask_app.route("/api/categories")
def api_categories():
    return jsonify([{"name": n, "subs": s} for n, s in CATEGORIES])


@flask_app.route("/debug")
def debug_page():
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>body{font-family:sans-serif;padding:30px;font-size:18px;}
.ok{color:green;} .err{color:red;} .box{background:#f5f5f5;padding:15px;border-radius:8px;margin:10px 0;}
</style></head><body>
<h2>🔍 대시보드 진단</h2>
<div class="box" id="s1">1. 서버 응답: ✅ OK (이 메시지가 보이면 Flask 정상)</div>
<div class="box" id="s2">2. JavaScript: ⏳ 확인 중...</div>
<div class="box" id="s3">3. API /api/transactions: ⏳ 확인 중...</div>
<div class="box" id="s4">4. 데이터 건수: ⏳ 확인 중...</div>
<script>
document.getElementById('s2').innerHTML = '2. JavaScript: <span class="ok">✅ 정상 실행 중</span>';
fetch('/api/transactions')
  .then(function(r){
    if(!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  })
  .then(function(data){
    document.getElementById('s3').innerHTML = '3. API /api/transactions: <span class="ok">✅ 정상</span>';
    document.getElementById('s4').innerHTML = '4. 데이터 건수: <span class="ok">' + data.length + '건</span>';
  })
  .catch(function(e){
    document.getElementById('s3').innerHTML = '3. API /api/transactions: <span class="err">❌ 오류 - ' + e.message + '</span>';
    document.getElementById('s4').innerHTML = '4. 데이터: <span class="err">불러오기 실패</span>';
  });
</script>
</body></html>"""


@flask_app.route("/api/transactions")
def api_transactions():
    return jsonify(get_all_transactions())


@flask_app.route("/card", methods=["POST"])
def receive_card():
    data = request.get_json()
    text = data.get("text", "")
    if not text:
        return jsonify({"ok": False, "error": "text is empty"}), 400

    _SKIP_KW = ["한도초과", "한도 초과", "이용한도 초과", "한도가 초과"]
    if any(kw in text for kw in _SKIP_KW):
        logging.info(f"한도초과 메시지 skip: {text[:40]!r}")
        return jsonify({"ok": True, "skipped": "limit_exceeded"})

    if _is_duplicate(text):
        logging.warning(f"중복 요청 무시: {text[:40]!r}")
        return jsonify({"ok": True, "skipped": "duplicate"})

    parsed = parse_card_message(text)
    message = format_result(parsed)
    if message is None:
        return jsonify({"ok": True, "skipped": "unknown"})

    card_company  = parsed.get("카드사", "")
    prev_by_co    = get_monthly_total_by_company()
    tx_id         = save_transaction(parsed, text)
    new_by_co     = get_monthly_total_by_company()

    prev_co_total = prev_by_co.get(card_company, 0)
    new_co_total  = new_by_co.get(card_company, 0)
    is_cancel     = parsed.get("거래유형") == "취소"
    cumulative    = extract_cumulative(text)
    progress      = monthly_progress_text(card_company, new_co_total) if not is_cancel else ""
    limit_txt     = limit_remaining_text(card_company, cumulative) if not is_cancel else ""

    full_text = message + progress + limit_txt
    keyboard  = None

    # ── 텔레그램 전송을 별도 스레드에서 실행 ──────────────────────
    # Flask(단일 스레드)가 텔레그램 API 응답 대기로 블로킹되면
    # sms_watcher의 다음 요청이 빈 응답(JSONDecodeError)을 받는 버그 방지
    def _send_telegram():
        msg_id = send_telegram_sync(full_text, reply_markup=keyboard)
        if tx_id and msg_id:
            update_telegram_msg_id(tx_id, msg_id)
        check_and_notify_target(card_company, prev_co_total, new_co_total)

    threading.Thread(target=_send_telegram, daemon=True).start()
    return jsonify({"ok": True})


@flask_app.route("/api/budget")
def api_budget():
    from database import get_monthly_total_by_company as _by_co
    by_co = _by_co()
    companies = set(monthly_targets.keys()) | set(card_limits.keys())
    result = []
    for company in companies:
        current = by_co.get(company, 0)
        target  = monthly_targets.get(company)
        limit   = card_limits.get(company)
        result.append({
            "company":   company,
            "current":   current,
            "target":    target,
            "target_ratio": min(current / target, 1.0) if target else None,
            "limit":     limit,
            "remaining": (limit - current) if limit else None,
            "limit_ratio": min(current / limit, 1.0) if limit else None,
        })
    return jsonify(result)


@flask_app.route("/memo/<int:tx_id>", methods=["POST"])
def save_memo(tx_id):
    data = request.get_json()
    update_memo(tx_id, data.get("memo", ""))
    return jsonify({"ok": True})


@flask_app.route("/merchant/<int:tx_id>", methods=["POST"])
def save_merchant_route(tx_id):
    data = request.get_json()
    update_merchant(tx_id, data.get("merchant", ""))
    return jsonify({"ok": True})


@flask_app.route("/category/<int:tx_id>", methods=["POST"])
def save_category_route(tx_id):
    data = request.get_json()
    update_category(tx_id, data.get("category", ""), data.get("subcategory") or "")
    return jsonify({"ok": True})


@flask_app.route("/classification/<int:tx_id>", methods=["POST"])
def save_classification_route(tx_id):
    data = request.get_json()
    update_classification(tx_id, data.get("classification", ""))
    return jsonify({"ok": True})


@flask_app.route("/amount/<int:tx_id>", methods=["POST"])
def save_amount_route(tx_id):
    data = request.get_json()
    amount = int(data.get("amount", 0))
    total_amount = data.get("total_amount")
    if total_amount is not None:
        total_amount = int(total_amount)
    update_amount(tx_id, amount, total_amount)
    return jsonify({"ok": True})


@flask_app.route("/delete/<int:tx_id>", methods=["DELETE"])
def delete_tx(tx_id):
    delete_transaction(tx_id)
    return jsonify({"ok": True})


@flask_app.route("/delete/all", methods=["DELETE"])
def delete_all():
    delete_all_transactions()
    return jsonify({"ok": True})


@flask_app.route("/internal/notify", methods=["POST"])
def internal_notify():
    """sms_watcher 등 내부 컴포넌트 → Telegram 알림 전송용"""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    data = request.get_json()
    msg = data.get("message", "")
    if msg:
        send_telegram_sync(msg)
    return jsonify({"ok": True})


# ── 서버 기동 유틸 ──────────────────────────────────────────────

def run_flask():
    flask_app.run(host="0.0.0.0", port=5001)


def wait_for_flask(timeout: int = 15) -> bool:
    for _ in range(timeout):
        try:
            urllib.request.urlopen("http://localhost:5001", timeout=1)
            return True
        except Exception:
            time.sleep(1)
    return False
