"""
handlers.py — Telegram 봇 핸들러 (커맨드, 메시지, 콜백)
"""
import io
import csv
import os
import sys
import asyncio
import logging
import subprocess
import threading

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import watcher as watcher_mod
from categories import CATEGORIES, CLASSIFICATIONS, build_type_keyboard, build_main_keyboard, build_sub_keyboard

# 카테고리 선택 후 메모 대기 상태 {chat_id: tx_id}
pending_memo: dict = {}

# /learn 대화 상태 {chat_id: {step, data}}
learn_state: dict = {}
from database import (
    update_memo, update_category, update_classification, update_amount,
    get_all_transactions, get_tx_id_by_msg_id, get_monthly_total_by_company,
    get_transactions_for_export, save_transaction, update_telegram_msg_id,
)
from parser import parse_card_message, format_result, save_learned_pattern
from utils import (
    CHAT_ID,
    monthly_targets, find_company, save_targets,
    progress_bar, monthly_progress_text, check_and_notify_target,
    card_limits, save_limits, limit_remaining_text,
)


# ── /start ─────────────────────────────────────────────────────

HELP_TEXT = (
    "💳 카드 지출 봇 사용 안내\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "🚀 시작하기\n"
    "1. 카드 결제 문자가 오면 자동으로 감지해요\n"
    "2. 기본 지원: 광주카드, KB국민카드, 현대카드\n"
    "3. 다른 카드사는 /learn 으로 직접 등록하세요\n\n"
    "📲 카드사 패턴 등록\n"
    "/learn — 새 카드사 문자 패턴 등록\n\n"
    "📝 내역 수정\n"
    "/memo 내용 — 최근 내역에 메모\n"
    "/memo [ID] 내용 — 특정 내역에 메모\n"
    "/edit — 최근 내역 카테고리 수정\n"
    "/edit [ID] — 특정 내역 카테고리 수정\n"
    "/amount — 최근 5개 내역 ID 조회\n"
    "/amount [금액] — 최근 내역 금액 수정\n"
    "/amount [ID] [금액] — 특정 내역 금액 수정\n"
    "/skip — 메모 입력 건너뛰기\n\n"
    "🎯 실적 관리\n"
    "/budget — 실적 목표 조회\n"
    "/budget [카드사] [금액] — 목표 설정\n\n"
    "📊 내보내기\n"
    "/export — 전체 CSV 전송\n"
    "/export [YYYY-MM] — 특정 월 CSV 전송\n\n"
    "⚙️ 시스템\n"
    "/status — 프로세스 상태 확인\n"
    "/watcher — SMS Watcher 제어 (시작/중지/재시작)\n"
    "/restart — SMS Watcher 재시작\n"
    "/reboot — 봇 전체 재시작\n"
    "/help — 이 메시지 다시 보기"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 카드 지출 봇 시작!\n\n" + HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


# ── 일반 메시지 ─────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.message.chat_id

    # /learn 대화 진행 중
    if chat_id in learn_state:
        await _handle_learn_step(update, context, text)
        return

    # 카테고리 선택 후 메모 대기 중이면 메모로 처리
    if chat_id in pending_memo:
        tx_id = pending_memo.pop(chat_id)
        update_memo(tx_id, text)
        await update.message.reply_text(f"✅ 메모 저장됐어요!\n📝 {text}")
        return

    # 답장이면 메모로 처리
    if update.message.reply_to_message:
        replied_msg_id = update.message.reply_to_message.message_id
        tx_id = get_tx_id_by_msg_id(replied_msg_id)
        if tx_id:
            update_memo(tx_id, text)
            await update.message.reply_text(f"✅ 메모 저장됐어요!\n📝 {text}")
        else:
            await update.message.reply_text("⚠️ 해당 내역을 찾을 수 없어요.")
        return

    # 일반 메시지 → 카드 문자 파싱
    parsed = parse_card_message(text)
    message = format_result(parsed)
    if message is None:
        await update.message.reply_text("⏭️ 인식되지 않는 형식이에요.")
        return

    card_company = parsed.get("카드사", "")
    prev_by_co = get_monthly_total_by_company()
    tx_id = save_transaction(parsed, text)
    new_by_co = get_monthly_total_by_company()
    prev_co_total = prev_by_co.get(card_company, 0)
    new_co_total = new_by_co.get(card_company, 0)

    progress = monthly_progress_text(card_company, new_co_total) if parsed.get("거래유형") != "취소" else ""
    full_text = message + progress + "\n\n👤🏠 개인/생활비를 선택해주세요:"
    keyboard = build_type_keyboard(tx_id) if tx_id else None
    sent_msg = await update.message.reply_text(full_text, reply_markup=keyboard)
    if tx_id:
        update_telegram_msg_id(tx_id, sent_msg.message_id)
    check_and_notify_target(card_company, prev_co_total, new_co_total)


# ── /learn ─────────────────────────────────────────────────────

async def cmd_learn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    learn_state[chat_id] = {"step": "waiting_sms"}
    await update.message.reply_text(
        "📲 새 카드사 문자 패턴 등록\n\n"
        "카드 결제 문자를 그대로 붙여넣어 주세요.\n"
        "(/cancel 로 취소)"
    )


async def _handle_learn_step(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.message.chat_id
    state = learn_state[chat_id]
    step = state["step"]

    if text.strip() == "/cancel":
        learn_state.pop(chat_id, None)
        await update.message.reply_text("❌ 패턴 등록이 취소됐어요.")
        return

    if step == "waiting_sms":
        lines = [l for l in text.splitlines() if l.strip()]
        if len(lines) < 2:
            await update.message.reply_text("문자가 너무 짧아요. 다시 붙여넣어 주세요.")
            return
        state["sms"] = text
        state["lines"] = lines
        numbered = "\n".join(f"{i+1}. {l}" for i, l in enumerate(lines))
        state["step"] = "waiting_company"
        await update.message.reply_text(
            f"📋 문자 내용:\n\n{numbered}\n\n"
            "카드사 이름을 입력해주세요.\n예) 신한카드, 삼성카드, 우리카드"
        )

    elif step == "waiting_company":
        state["company"] = text.strip()
        state["step"] = "waiting_tx_type"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 승인", callback_data="learn_type:승인"),
            InlineKeyboardButton("↩️ 취소", callback_data="learn_type:취소"),
        ]])
        await update.message.reply_text("이 문자는 승인인가요, 취소인가요?", reply_markup=keyboard)

    elif step in ("waiting_amount_line", "waiting_date_line", "waiting_merchant_line"):
        try:
            idx = int(text.strip()) - 1
            lines = state["lines"]
            if idx < 0 or idx >= len(lines):
                raise ValueError
        except ValueError:
            await update.message.reply_text(f"1~{len(state['lines'])} 사이 숫자를 입력해주세요.")
            return

        numbered = "\n".join(f"{i+1}. {l}" for i, l in enumerate(state["lines"]))

        if step == "waiting_amount_line":
            state["amount_line"] = idx
            state["step"] = "waiting_date_line"
            await update.message.reply_text(
                f"📋 문자 내용:\n\n{numbered}\n\n"
                "날짜/시간이 있는 줄 번호는? (없으면 0 입력)"
            )
        elif step == "waiting_date_line":
            state["date_line"] = idx if int(text.strip()) != 0 else None
            state["step"] = "waiting_merchant_line"
            await update.message.reply_text(
                f"📋 문자 내용:\n\n{numbered}\n\n"
                "가맹점(사용처)이 있는 줄 번호는?"
            )
        elif step == "waiting_merchant_line":
            state["merchant_line"] = idx
            # 저장
            pattern = {
                "tx_type": state["tx_type"],
                "amount_line": state["amount_line"],
                "date_line": state.get("date_line"),
                "merchant_line": state["merchant_line"],
            }
            save_learned_pattern(state["company"], pattern)
            learn_state.pop(chat_id, None)
            await update.message.reply_text(
                f"✅ '{state['company']}' 패턴이 등록됐어요!\n\n"
                f"{'승인' if state['tx_type'] == '승인' else '취소'} 문자 형식 1개 저장.\n"
                f"다른 형식(할부/취소 등)이 있으면 /learn 으로 추가 등록하세요."
            )


# learn 타입 선택 콜백은 handle_callback에서 처리

# ── /status ────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alive = watcher_mod.watcher_proc and watcher_mod.watcher_proc.poll() is None
    watcher_str = f"✅ 실행 중 (PID: {watcher_mod.watcher_proc.pid})" if alive else "❌ 중단됨"
    await update.message.reply_text(
        f"📊 프로세스 상태\n\n"
        f"Flask      : ✅ 실행 중\n"
        f"SMS Watcher: {watcher_str}"
    )


# ── /memo ──────────────────────────────────────────────────────

async def cmd_memo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "사용법:\n"
            "/memo 내용 — 최근 내역에 메모\n"
            "/memo [ID] 내용 — 특정 내역에 메모\n\n"
            "또는 봇 메시지에 답장하면 자동으로 메모 저장"
        )
        return

    txs = get_all_transactions()
    if not txs:
        await update.message.reply_text("저장된 내역이 없어요.")
        return

    target = None
    try:
        tx_id = int(context.args[0])
        memo_text = " ".join(context.args[1:])
        if not memo_text:
            raise ValueError("no memo text")
        target = next((t for t in txs if t["id"] == tx_id), None)
        if not target:
            await update.message.reply_text(f"ID {tx_id} 내역을 찾을 수 없어요.")
            return
    except (ValueError, IndexError):
        memo_text = " ".join(context.args)
        target = txs[0]

    update_memo(target["id"], memo_text)
    await update.message.reply_text(
        f"✅ 메모 저장됐어요!\n"
        f"📝 {memo_text}\n"
        f"└ {target.get('merchant', '-')} · {target.get('amount', 0):,}원"
    )


# ── /edit ──────────────────────────────────────────────────────

async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txs = get_all_transactions()
    if not txs:
        await update.message.reply_text("저장된 내역이 없어요.")
        return

    if context.args:
        try:
            tx_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("사용법: /edit 또는 /edit [내역ID]")
            return
        tx = next((t for t in txs if t["id"] == tx_id), None)
        if not tx:
            await update.message.reply_text(f"ID {tx_id} 내역을 찾을 수 없어요.")
            return
        cat_text = tx.get("category", "")
        if tx.get("subcategory"):
            cat_text += " > " + tx["subcategory"]
        await update.message.reply_text(
            f"✏️ 카테고리 재선택\n"
            f"🏪 {tx.get('merchant', '-')}\n"
            f"💰 {tx.get('amount', 0):,}원\n"
            f"📂 현재: {cat_text or '없음'}\n\n"
            f"카테고리를 선택해주세요:",
            reply_markup=build_main_keyboard(tx_id)
        )
    else:
        rows = []
        for t in txs[:5]:
            cat = t.get("category", "")
            if t.get("subcategory"):
                cat += " > " + t["subcategory"]
            label = f"{t.get('date', '')} {t.get('merchant', '-')} {t.get('amount', 0):,}원"
            if cat:
                label += f" [{cat}]"
            rows.append([InlineKeyboardButton(label, callback_data=f"recat_{t['id']}")])
        await update.message.reply_text(
            "✏️ 카테고리 수정할 내역을 선택하세요:",
            reply_markup=InlineKeyboardMarkup(rows)
        )


# ── /budget ────────────────────────────────────────────────────

async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    by_co = get_monthly_total_by_company()

    if not context.args:
        if not monthly_targets:
            await update.message.reply_text(
                "설정된 실적 목표가 없어요.\n\n"
                "설정 예시:\n/budget 광주 300000\n/budget 국민 200000"
            )
            return
        lines = ["🎯 카드 실적 목표\n"]
        for company, target in monthly_targets.items():
            current = by_co.get(company, 0)
            bar = progress_bar(current, target)
            lines.append(f"[ {company} ]")
            lines.append(f"{bar}")
            lines.append(f"{current:,}원 / {target:,}원\n")
        await update.message.reply_text("\n".join(lines))
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "사용법:\n"
            "/budget 광주 300000  — 목표 설정\n"
            "/budget 국민 200000\n"
            "/budget           — 전체 조회"
        )
        return

    company = find_company(context.args[0])
    try:
        target = int(context.args[1].replace(",", ""))
    except ValueError:
        await update.message.reply_text("금액은 숫자로 입력해주세요.\n예) /budget 광주 300000")
        return

    if target == 0:
        monthly_targets.pop(company, None)
        save_targets()
        await update.message.reply_text(f"🗑️ {company} 실적 목표 삭제됐어요.")
    else:
        monthly_targets[company] = target
        save_targets()
        current = by_co.get(company, 0)
        bar = progress_bar(current, target)
        await update.message.reply_text(
            f"✅ {company} 실적 목표 설정!\n\n{bar}\n{current:,}원 / {target:,}원"
        )


# ── /limit ─────────────────────────────────────────────────────

async def cmd_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    by_co = get_monthly_total_by_company()

    if not context.args:
        if not card_limits:
            await update.message.reply_text(
                "설정된 카드 한도가 없어요.\n\n"
                "설정 예시:\n/limit 광주 500000\n/limit 국민 300000"
            )
            return
        lines = ["💳 카드 한도 현황\n"]
        for company, limit in card_limits.items():
            current = by_co.get(company, 0)
            remaining = limit - current
            bar = progress_bar(current, limit)
            lines.append(f"[ {company} ]")
            lines.append(f"{bar}")
            lines.append(f"사용 {current:,}원 / 한도 {limit:,}원")
            if remaining < 0:
                lines.append(f"⚠️ 한도 초과 {abs(remaining):,}원\n")
            else:
                lines.append(f"잔여 {remaining:,}원\n")
        await update.message.reply_text("\n".join(lines))
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "사용법:\n"
            "/limit 광주 500000  — 한도 설정\n"
            "/limit 국민 300000\n"
            "/limit 광주 0       — 한도 삭제\n"
            "/limit              — 전체 조회"
        )
        return

    company = find_company(context.args[0])
    try:
        limit = int(context.args[1].replace(",", ""))
    except ValueError:
        await update.message.reply_text("금액은 숫자로 입력해주세요.\n예) /limit 광주 500000")
        return

    if limit == 0:
        card_limits.pop(company, None)
        save_limits()
        await update.message.reply_text(f"🗑️ {company} 한도 삭제됐어요.")
    else:
        card_limits[company] = limit
        save_limits()
        current = by_co.get(company, 0)
        remaining = limit - current
        bar = progress_bar(current, limit)
        await update.message.reply_text(
            f"✅ {company} 한도 설정!\n\n"
            f"{bar}\n"
            f"사용 {current:,}원 / 한도 {limit:,}원\n"
            f"잔여 {remaining:,}원"
        )


# ── /amount ────────────────────────────────────────────────────

async def cmd_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        txs = get_all_transactions()
        if not txs:
            await update.message.reply_text("저장된 내역이 없어요.")
            return
        lines = ["📋 최근 내역 (ID 확인용)\n"]
        for t in txs[:5]:
            amt = t.get("total_amount") or t.get("amount", 0)
            cat = t.get("category", "")
            lines.append(
                f"ID {t['id']} | {t.get('date','')} {t.get('merchant','-')} "
                f"{amt:,}원" + (f" [{cat}]" if cat else "")
            )
        lines.append("\n사용법:\n/amount 50000 — 최근 내역 금액 수정\n/amount [ID] 50000 — 특정 내역 금액 수정")
        await update.message.reply_text("\n".join(lines))
        return

    txs = get_all_transactions()
    if not txs:
        await update.message.reply_text("저장된 내역이 없어요.")
        return

    target = None
    new_amount = None
    if len(context.args) == 1:
        # 금액만 입력 → 최근 내역에 적용
        try:
            new_amount = int(context.args[0].replace(",", ""))
            target = txs[0]
        except ValueError:
            await update.message.reply_text("금액은 숫자로 입력해주세요.\n예) /amount 50000")
            return
    else:
        # ID + 금액
        try:
            tx_id = int(context.args[0])
            new_amount = int(context.args[1].replace(",", ""))
            target = next((t for t in txs if t["id"] == tx_id), None)
            if not target:
                await update.message.reply_text(f"ID {tx_id} 내역을 찾을 수 없어요.")
                return
        except ValueError:
            await update.message.reply_text("사용법: /amount [ID] [금액]\n예) /amount 5 50000")
            return

    old_amount = target.get("total_amount") or target.get("amount", 0)
    # 할부인 경우 total_amount 수정, 일반은 amount 수정
    is_install = bool(target.get("installment_months") and target.get("total_amount"))
    if is_install:
        update_amount(target["id"], target["amount"], new_amount)
        await update.message.reply_text(
            f"✅ 금액 수정됐어요!\n"
            f"🏪 {target.get('merchant', '-')}\n"
            f"💰 총금액 {old_amount:,}원 → {new_amount:,}원\n"
            f"   ({target.get('installment_months')}개월 할부 · 월 {target.get('amount', 0):,}원)"
        )
    else:
        update_amount(target["id"], new_amount)
        await update.message.reply_text(
            f"✅ 금액 수정됐어요!\n"
            f"🏪 {target.get('merchant', '-')}\n"
            f"💰 {old_amount:,}원 → {new_amount:,}원"
        )


# ── /skip ──────────────────────────────────────────────────────

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if chat_id in pending_memo:
        pending_memo.pop(chat_id)
        await update.message.reply_text("⏭️ 메모 건너뛰었어요.")
    else:
        await update.message.reply_text("건너뛸 항목이 없어요.")


# ── /restart ───────────────────────────────────────────────────

async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 sms_watcher 재시작 중...")
    watcher_mod.restart_watcher()
    await update.message.reply_text(f"✅ sms_watcher 재시작 완료 (PID: {watcher_mod.watcher_proc.pid})")


# ── /watcher ────────────────────────────────────────────────────

async def cmd_watcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """SMS Watcher 상태 표시 + 인라인 제어 버튼"""
    alive = watcher_mod.watcher_proc and watcher_mod.watcher_proc.poll() is None
    pid_str = f" (PID: {watcher_mod.watcher_proc.pid})" if alive else ""
    status = f"✅ 실행 중{pid_str}" if alive else "❌ 중단됨"
    buttons = [
        [
            InlineKeyboardButton("▶️ 시작",   callback_data="watcher_start"),
            InlineKeyboardButton("⏹️ 중지",   callback_data="watcher_stop"),
            InlineKeyboardButton("🔄 재시작",  callback_data="watcher_restart"),
        ]
    ]
    await update.message.reply_text(
        f"📡 SMS Watcher 상태: {status}",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ── /reboot ─────────────────────────────────────────────────────

async def cmd_reboot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Python 봇 프로세스 전체 재시작 (os.execv)"""
    await update.message.reply_text(
        "🔄 봇을 재시작합니다...\n잠시 후 시작 메시지가 올 거예요!"
    )
    await asyncio.sleep(2)
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ── 인라인 키보드 콜백 ──────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    parts = data.split("_")
    action = parts[0]

    if data.startswith("learn_type:"):
        tx_type = data.split(":")[1]
        chat_id = query.message.chat_id
        state = learn_state.get(chat_id)
        if not state:
            await query.answer("세션이 만료됐어요. /learn 을 다시 실행해주세요.")
            return
        state["tx_type"] = tx_type
        state["step"] = "waiting_amount_line"
        numbered = "\n".join(f"{i+1}. {l}" for i, l in enumerate(state["lines"]))
        await query.edit_message_text(
            f"📋 문자 내용:\n\n{numbered}\n\n"
            "금액이 있는 줄 번호는? (예: 4)"
        )
        await query.answer()
        return

    if action == "watcher":
        sub = parts[1] if len(parts) > 1 else ""
        if sub == "start":
            if watcher_mod.watcher_proc and watcher_mod.watcher_proc.poll() is None:
                await query.answer("이미 실행 중이에요")
            else:
                watcher_mod.start_watcher()
                await query.answer("▶️ 시작!")
            alive = watcher_mod.watcher_proc and watcher_mod.watcher_proc.poll() is None
            pid_str = f" (PID: {watcher_mod.watcher_proc.pid})" if alive else ""
            await query.edit_message_text(
                f"📡 SMS Watcher 상태: {'✅ 실행 중' + pid_str if alive else '❌ 중단됨'}",
                reply_markup=query.message.reply_markup
            )
        elif sub == "stop":
            watcher_mod.stop_watcher()
            await query.answer("⏹️ 중지됨")
            await query.edit_message_text(
                "📡 SMS Watcher 상태: ❌ 중단됨 (수동 중지)",
                reply_markup=query.message.reply_markup
            )
        elif sub == "restart":
            watcher_mod.restart_watcher()
            await query.answer("🔄 재시작!")
            await query.edit_message_text(
                f"📡 SMS Watcher 상태: ✅ 실행 중 (PID: {watcher_mod.watcher_proc.pid})",
                reply_markup=query.message.reply_markup
            )
        return

    if action == "skip":
        await query.answer("⏭️ 건너뛰었어요")
        await query.edit_message_reply_markup(reply_markup=None)

    elif action == "t":
        # 개인(0) / 생활비(1) 선택
        try:
            tx_id, type_idx = int(parts[1]), int(parts[2])
            cls_name = CLASSIFICATIONS[type_idx]  # "👤 개인" or "🏠 생활비"
            await query.answer(f"{cls_name} 선택!")   # 먼저 응답 → 버튼 먹통 방지
            update_classification(tx_id, cls_name)
            orig_text = query.message.text or ""
            base_text = orig_text.rsplit("\n\n", 1)[0]  # 마지막 안내줄 제거
            await query.edit_message_text(
                base_text + f"\n\n{cls_name} ✅  |  📂 카테고리를 선택해주세요:",
                reply_markup=build_main_keyboard(tx_id)
            )
        except Exception as e:
            logger.error(f"분류 선택 오류 (data={data}): {e}", exc_info=True)

    elif action == "typeback":
        tx_id = int(parts[1])
        await query.answer()
        await query.edit_message_text(
            query.message.text.rsplit("\n", 1)[0] + "\n\n👤🏠 개인/생활비를 선택해주세요:",
            reply_markup=build_type_keyboard(tx_id)
        )

    elif action == "back":
        tx_id = int(parts[1])
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=build_main_keyboard(tx_id))

    elif action == "recat":
        tx_id = int(parts[1])
        txs = get_all_transactions()
        tx = next((t for t in txs if t["id"] == tx_id), None)
        cat_text = ""
        if tx:
            cat_text = tx.get("category", "")
            if tx.get("subcategory"):
                cat_text += " > " + tx["subcategory"]
        info = (
            f"✏️ 카테고리 재선택\n"
            f"🏪 {tx.get('merchant', '-') if tx else '-'} · {tx.get('amount', 0):,}원\n"
            f"📂 현재: {cat_text or '없음'}\n\n카테고리를 선택해주세요:"
        ) if tx else "✏️ 카테고리 재선택"
        await query.answer()
        await query.edit_message_text(info, reply_markup=build_main_keyboard(tx_id))

    elif action == "m":
        tx_id, main_idx = int(parts[1]), int(parts[2])
        main_name, sub_cats = CATEGORIES[main_idx]
        if not sub_cats:
            update_category(tx_id, main_name)
            await query.answer("✅ 저장!")
            await query.edit_message_reply_markup(reply_markup=None)
            pending_memo[query.message.chat_id] = tx_id
            # 분류 정보 조회
            txs = get_all_transactions()
            tx = next((t for t in txs if t["id"] == tx_id), None)
            cls = tx.get("classification", "") if tx else ""
            cls_str = f"{cls}  /  " if cls else ""
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"📂 {cls_str}{main_name} 저장됐어요!\n\n💬 메모를 입력하세요 (건너뛰려면 /skip)"
            )
        else:
            await query.answer()
            await query.edit_message_reply_markup(reply_markup=build_sub_keyboard(tx_id, main_idx))

    elif action == "s":
        tx_id, main_idx, sub_idx = int(parts[1]), int(parts[2]), int(parts[3])
        main_name, sub_cats = CATEGORIES[main_idx]
        sub_name = sub_cats[sub_idx]
        update_category(tx_id, main_name, sub_name)
        await query.answer("✅ 저장!")
        await query.edit_message_reply_markup(reply_markup=None)
        pending_memo[query.message.chat_id] = tx_id
        # 분류 정보 조회
        txs = get_all_transactions()
        tx = next((t for t in txs if t["id"] == tx_id), None)
        cls = tx.get("classification", "") if tx else ""
        cls_str = f"{cls}  /  " if cls else ""
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"📂 {cls_str}{main_name} > {sub_name} 저장됐어요!\n\n💬 메모를 입력하세요 (건너뛰려면 /skip)"
        )


# ── /export ────────────────────────────────────────────────────

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    year_month = context.args[0] if context.args else None
    txs = get_transactions_for_export(year_month)
    period = year_month or "전체"

    if not txs:
        await update.message.reply_text(f"📂 {period} 내역이 없어요.")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["날짜", "금액", "분류", "소분류", "자산", "내용", "메모", "유형"])
    for t in txs:
        writer.writerow([
            t.get("date", ""),
            t.get("total_amount") or t.get("amount", 0),
            t.get("category", ""),
            t.get("subcategory", ""),
            t.get("card_company", ""),
            t.get("memo", ""),
            t.get("merchant", ""),
            t.get("tx_type", "승인"),
        ])

    csv_bytes = output.getvalue().encode("utf-8-sig")
    await update.message.reply_document(
        document=io.BytesIO(csv_bytes),
        filename=f"card_export_{period}.csv",
        caption=f"📊 {len(txs)}건 ({period})\nMoneyManager → 더보기 → 가져오기로 import하세요!"
    )



# ── /dev ─────────────────────────────────────────────────────────────────────
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

def _find_claude_bin() -> str:
    """claude CLI 경로 탐색 (쉘 PATH 밖에서도 찾기 위해)"""
    candidates = [
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        os.path.expanduser("~/.claude/local/claude"),
        os.path.expanduser("~/.nvm/versions/node/*/bin/claude"),
    ]
    # which 명령으로도 시도
    try:
        r = subprocess.run(["which", "claude"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return "claude"  # 마지막 시도

async def cmd_dev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """텔레그램으로 Claude Code에 개발 명령"""
    if str(update.effective_user.id) != str(CHAT_ID):
        await update.message.reply_text("❌ 권한 없음")
        return

    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text(
            "사용법: /dev <명령>\n\n예시:\n"
            "/dev handlers.py에 /ping 명령어 추가해줘\n"
            "/dev 현재 파일 목록 보여줘"
        )
        return

    msg = await update.message.reply_text("⏳ Claude Code 실행 중...")
    loop = asyncio.get_event_loop()   # 비동기 컨텍스트에서 미리 캡처

    def run_claude():
        claude_bin = _find_claude_bin()
        try:
            result = subprocess.run(
                [claude_bin, "-p", prompt, "--dangerously-skip-permissions"],
                cwd=_PROJECT_DIR,
                capture_output=True,
                text=True,
                timeout=180,
                env={**os.environ, "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")},
            )
            output = (result.stdout or result.stderr or "출력 없음").strip()
            if len(output) > 3800:
                output = output[-3800:] + "\n...(앞부분 생략)"
            reply = f"✅ 완료\n\n{output}"
        except subprocess.TimeoutExpired:
            reply = "⏰ 시간 초과 (3분)"
        except FileNotFoundError:
            reply = f"❌ claude CLI 없음 ({claude_bin})\n터미널에서 `which claude` 확인해주세요."
        except Exception as e:
            reply = f"❌ 오류: {e}"

        asyncio.run_coroutine_threadsafe(msg.edit_text(reply), loop)

    threading.Thread(target=run_claude, daemon=True).start()
