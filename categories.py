"""
categories.py — 카테고리 정의 및 Telegram 인라인 키보드 빌더

카테고리 수정: categories.json 파일을 편집하세요.
"""
import json
import os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def _load_categories():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "categories.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return [(item["name"], item.get("subs", [])) for item in data]
    except Exception as e:
        print(f"categories.json 로드 실패: {e} — 기본값 사용")
        return [("기타", [])]

CATEGORIES = _load_categories()

CATEGORIES_JSON = json.dumps(
    [{"name": n, "subs": s} for n, s in CATEGORIES],
    ensure_ascii=False
)

# 분류 옵션
CLASSIFICATIONS = ["👤 개인", "🏠 생활비"]


def build_type_keyboard(tx_id: int) -> InlineKeyboardMarkup:
    """개인 / 생활비 선택 키보드"""
    rows = [
        [
            InlineKeyboardButton("👤 개인", callback_data=f"t_{tx_id}_0"),
            InlineKeyboardButton("🏠 생활비", callback_data=f"t_{tx_id}_1"),
        ],
        [InlineKeyboardButton("⏭️ 건너뛰기", callback_data=f"skip_{tx_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def build_main_keyboard(tx_id: int) -> InlineKeyboardMarkup:
    """카테고리 선택 키보드 (← 분류로 돌아가기 포함)"""
    rows, row = [], []
    for idx, (cat_name, _) in enumerate(CATEGORIES):
        row.append(InlineKeyboardButton(cat_name, callback_data=f"m_{tx_id}_{idx}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("← 분류로", callback_data=f"typeback_{tx_id}"),
        InlineKeyboardButton("⏭️ 건너뛰기", callback_data=f"skip_{tx_id}"),
    ])
    return InlineKeyboardMarkup(rows)


def build_sub_keyboard(tx_id: int, main_idx: int) -> InlineKeyboardMarkup:
    _, sub_cats = CATEGORIES[main_idx]
    rows, row = [], []
    for idx, sub_name in enumerate(sub_cats):
        row.append(InlineKeyboardButton(sub_name, callback_data=f"s_{tx_id}_{main_idx}_{idx}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("← 카테고리로", callback_data=f"back_{tx_id}")])
    return InlineKeyboardMarkup(rows)


def build_type_keyboard_dict(tx_id: int) -> dict:
    """requests용 JSON-serializable — 개인/생활비 선택"""
    return {
        "inline_keyboard": [
            [
                {"text": "👤 개인",   "callback_data": f"t_{tx_id}_0"},
                {"text": "🏠 생활비", "callback_data": f"t_{tx_id}_1"},
            ],
            [{"text": "⏭️ 건너뛰기", "callback_data": f"skip_{tx_id}"}],
        ]
    }


def build_main_keyboard_dict(tx_id: int) -> dict:
    """requests용 JSON-serializable — 카테고리 선택"""
    rows, row = [], []
    for idx, (cat_name, _) in enumerate(CATEGORIES):
        row.append({"text": cat_name, "callback_data": f"m_{tx_id}_{idx}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        {"text": "← 분류로", "callback_data": f"typeback_{tx_id}"},
        {"text": "⏭️ 건너뛰기", "callback_data": f"skip_{tx_id}"},
    ])
    return {"inline_keyboard": rows}
