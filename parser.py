import re
import json
import os
from datetime import datetime
from typing import Optional

_LEARNED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learned_patterns.json")


def _load_learned() -> dict:
    if not os.path.exists(_LEARNED_PATH):
        return {}
    try:
        with open(_LEARNED_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_learned_pattern(company: str, pattern: dict):
    """learned_patterns.json에 패턴 추가. 같은 카드사 여러 패턴 허용."""
    data = _load_learned()
    if company not in data:
        data[company] = []
    data[company].append(pattern)
    with open(_LEARNED_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _parse_learned(text: str) -> Optional[dict]:
    """등록된 패턴으로 SMS 파싱 시도."""
    data = _load_learned()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for company, patterns in data.items():
        if company not in text:
            continue
        for p in patterns:
            try:
                amount_line = lines[p["amount_line"]]
                m = re.search(r'([\d,]+)원', amount_line)
                if not m:
                    continue
                amount = int(m.group(1).replace(",", ""))
                result = {
                    "카드사": company,
                    "거래유형": p.get("tx_type", "승인"),
                    "결제방식": p.get("tx_type", "승인") if p.get("tx_type") == "취소" else "일시불",
                    "금액": amount,
                }
                # 날짜/시간
                date_idx = p.get("date_line")
                if date_idx is not None and date_idx < len(lines):
                    date_line = lines[date_idx]
                    dm = re.search(r'(\d{2}/\d{2})\s+(\d{2}:\d{2})', date_line)
                    if dm:
                        result["날짜"] = _convert_date(dm.group(1))
                        result["시간"] = dm.group(2)
                # 가맹점
                merchant_idx = p.get("merchant_line")
                if merchant_idx is not None and merchant_idx < len(lines):
                    result["가맹점"] = lines[merchant_idx]
                return result
            except (IndexError, Exception):
                continue
    return None


def _convert_date(mmdd: str) -> str:
    """MM/DD → YYYY.MM.DD 변환. 미래 날짜면 작년으로."""
    try:
        month, day = map(int, mmdd.split("/"))
        year = datetime.now().year
        # 현재 월보다 미래 월이면 작년
        if month > datetime.now().month:
            year -= 1
        return f"{year}.{month:02d}.{day:02d}"
    except:
        return mmdd


def extract_cumulative(text: str) -> Optional[int]:
    """SMS에서 누적 금액 추출. 예) '누적 2,228,821원' → 2228821"""
    m = re.search(r'누적\s*([\d,]+)원', text)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def parse_card_message(text: str) -> Optional[dict]:
    """
    카드 결제 문자를 파싱해서 딕셔너리로 반환.
    지원: 광주카드, KB국민카드
    반환값이 None이면 인식 불가 (저장/알림 skip)
    """

    text = text.strip()

    # ── 광주카드 ──────────────────────────────────────────
    # 일시불: 일시불 56,840원
    # 할부:   할부5 384,800원  (5개월 → 월 76,960원)
    # 취소:   취소 56,840원
    if "광주카드" in text and re.search(r'(일시불|할부\d+|취소)\s+[\d,]+원', text):
        result = {"카드사": "광주카드"}

        date_match = re.search(r'(\d{2}/\d{2})\s+(\d{2}:\d{2})', text)
        if date_match:
            result["날짜"] = _convert_date(date_match.group(1))
            result["시간"] = date_match.group(2)

        installment_match = re.search(r'할부(\d+)\s+([\d,]+)원', text)
        cancel_match = re.search(r'취소\s+([\d,]+)원', text)
        lump_match = re.search(r'일시불\s+([\d,]+)원', text)

        if cancel_match:
            result["거래유형"] = "취소"
            result["결제방식"] = "취소"
            result["금액"] = int(cancel_match.group(1).replace(",", ""))
        elif installment_match:
            months = int(installment_match.group(1))
            total = int(installment_match.group(2).replace(",", ""))
            result["거래유형"] = "승인"
            result["결제방식"] = f"할부{months}개월"
            result["할부개월"] = months
            result["총금액"] = total
            result["금액"] = round(total / months)  # 월 납입금
        elif lump_match:
            result["거래유형"] = "승인"
            result["결제방식"] = "일시불"
            result["금액"] = int(lump_match.group(1).replace(",", ""))

        # 가맹점: 누적 바로 아랫줄
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if "누적" in line and i + 1 < len(lines):
                result["가맹점"] = lines[i + 1]
                break

        return result

    # ── KB국민카드 ────────────────────────────────────────
    # 일시불: 129,857원 일시불
    # 할부:   309,980원 05개월  (5개월 → 월 61,996원)
    # 취소:   KB국민카드3559취소
    if 'KB국민카드' in text and re.search(r'\d{2}/\d{2}\s+\d{2}:\d{2}|[\d,]+원', text):
        result = {"카드사": "KB국민카드"}

        date_match = re.search(r'(\d{2}/\d{2})\s+(\d{2}:\d{2})', text)
        if date_match:
            result["날짜"] = _convert_date(date_match.group(1))
            result["시간"] = date_match.group(2)

        installment_match = re.search(r'([\d,]+)원\s+(\d{2})개월', text)
        is_cancel = bool(re.search(r'국민카드\d*취소', text) or "승인취소" in text)
        lump_match = re.search(r'([\d,]+)원\s+일시불', text)

        if is_cancel:
            result["거래유형"] = "취소"
            result["결제방식"] = "취소"
            amount_match = re.search(r'([\d,]+)원', text)
            if amount_match:
                result["금액"] = int(amount_match.group(1).replace(",", ""))
        elif installment_match:
            total = int(installment_match.group(1).replace(",", ""))
            months = int(installment_match.group(2))
            result["거래유형"] = "승인"
            result["결제방식"] = f"할부{months}개월"
            result["할부개월"] = months
            result["총금액"] = total
            result["금액"] = round(total / months)  # 월 납입금
        elif lump_match:
            result["거래유형"] = "승인"
            result["결제방식"] = "일시불"
            result["금액"] = int(lump_match.group(1).replace(",", ""))

        # 가맹점: 날짜 다음 줄 (금액 줄이면 건너뜀 — 신 형식 대응)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if re.match(r'\d{2}/\d{2}\s+\d{2}:\d{2}', line):
                for next_line in lines[i + 1:]:
                    if "누적" in next_line:
                        break
                    if re.search(r'[\d,]+원', next_line):
                        continue  # 금액 줄 건너뜀
                    result["가맹점"] = next_line
                    break
                break

        return result

    # ── 현대카드 ──────────────────────────────────────────
    # 승인: 현대카드 M 승인 / 현대카드 ZERO 승인 등
    # 취소: 현대카드 M 취소
    # 금액 형식: 22,800원 일시불 / 22,800원 03개월
    if re.search(r'현대카드.*(승인|취소)', text):
        result = {"카드사": "현대카드"}

        is_cancel = bool(re.search(r'현대카드.*취소', text))
        result["거래유형"] = "취소" if is_cancel else "승인"

        date_match = re.search(r'(\d{2}/\d{2})\s+(\d{2}:\d{2})', text)
        if date_match:
            result["날짜"] = _convert_date(date_match.group(1))
            result["시간"] = date_match.group(2)

        if is_cancel:
            amount_match = re.search(r'([\d,]+)원', text)
            if amount_match:
                result["결제방식"] = "취소"
                result["금액"] = int(amount_match.group(1).replace(",", ""))
        else:
            installment_match = re.search(r'([\d,]+)원\s+(\d{2})개월', text)
            lump_match = re.search(r'([\d,]+)원\s+일시불', text)
            if installment_match:
                total = int(installment_match.group(1).replace(",", ""))
                months = int(installment_match.group(2))
                result["결제방식"] = f"할부{months}개월"
                result["할부개월"] = months
                result["총금액"] = total
                result["금액"] = round(total / months)
            elif lump_match:
                result["결제방식"] = "일시불"
                result["금액"] = int(lump_match.group(1).replace(",", ""))

        # 가맹점: 날짜/시간 다음 줄
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if re.match(r'\d{2}/\d{2}\s+\d{2}:\d{2}', line) and i + 1 < len(lines):
                next_line = lines[i + 1]
                if not re.match(r'누적', next_line):
                    result["가맹점"] = next_line
                break

        return result

    # 기본 파서로 인식 못 하면 등록된 패턴으로 시도
    return _parse_learned(text)


def format_result(parsed: Optional[dict]) -> Optional[str]:
    if parsed is None:
        return None

    is_cancel = parsed.get("거래유형") == "취소"
    is_installment = bool(parsed.get("할부개월"))

    if is_cancel:
        header = "↩️ 취소"
    else:
        header = "✅ 승인"

    if is_cancel:
        payment = "취소"
    elif is_installment:
        months = parsed.get("할부개월")
        monthly = parsed.get("금액", 0)
        payment = f"할부 {months}개월  (월 {monthly:,}원)"
    else:
        payment = "일시불"

    if is_installment:
        amount = f"{parsed.get('총금액', 0):,}원"
    else:
        amount = f"{parsed.get('금액', 0):,}원"

    date = parsed.get('날짜', '-')
    time_ = parsed.get('시간', '')
    date_line = f"{date}  {time_}".strip()

    divider = "─" * 20

    return (
        f"{header}\n"
        f"{divider}\n"
        f"📅  날짜  :  {date_line}\n"
        f"🏦  카드사  :  {parsed.get('카드사', '-')}\n"
        f"💳  결제  :  {payment}\n"
        f"🏪  사용처  :  {parsed.get('가맹점', '-')}\n"
        f"💰  금액  :  {amount}"
    )


# 테스트
if __name__ == "__main__":
    messages = [
        # 광주카드 일시불
        ("[Web발신]\n광주카드 신용1234\n홍*동님\n02/28 13:14 \n일시불 56,840원\n누적 1,000,000원\n스타벅스", "광주카드 일시불"),
        # 광주카드 할부
        ("[Web발신]\n광주카드 신용1234\n홍*동님\n02/23 16:58\n할부5 384,800원\n누적 900,000원\n엘지전자", "광주카드 할부5"),
        # 광주카드 취소
        ("[Web발신]\n광주카드 신용1234\n홍*동님\n02/28 15:00 \n취소 56,840원\n누적 950,000원\n스타벅스", "광주카드 취소"),
        # KB국민카드 일시불
        ("[Web발신]\nKB국민카드1234승인\n김*수님\n129,857원 일시불\n02/08 22:39\n마켓컬리\n누적800,000원", "KB 일시불"),
        # KB국민카드 할부
        ("[Web발신]\nKB국민카드5678승인\n김*수님\n309,980원 05개월\n01/21 13:29\n삼성화재\n누적500,000원", "KB 할부5"),
        # KB국민카드 취소
        ("[Web발신]\nKB국민카드1234취소\n김*수님\n129,857원\n02/08 23:00\n마켓컬리\n누적670,000원", "KB 취소"),
        # 현대카드 일시불
        ("[Web발신]\n현대카드 M 승인\n박*준\n22,800원 일시불\n03/04 23:47\n쿠팡\n누적300,000원", "현대카드 일시불"),
        # 현대카드 할부
        ("[Web발신]\n현대카드 M 승인\n박*준\n309,000원 03개월\n03/04 23:47\n애플\n누적600,000원", "현대카드 할부3"),
        # 현대카드 취소
        ("[Web발신]\n현대카드 M 취소\n박*준\n22,800원\n03/04 23:55\n쿠팡\n누적280,000원", "현대카드 취소"),
        # 인식 불가
        ("신한카드 1234 승인 50,000원", "인식불가"),
    ]

    for msg, label in messages:
        result = format_result(parse_card_message(msg))
        print(f"[{label}]")
        print(result if result else "⏭️ skip (인식 불가)")
        print()