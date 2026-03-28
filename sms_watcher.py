"""
sms_watcher.py
맥OS Messages DB 감시 → 카드 문자 감지 → Flask 서버로 자동 전송
"""

import sqlite3
import time
import requests
import os
import json
import logging
import shutil
import tempfile
import plistlib
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── 설정 ──────────────────────────────────────
_HERE          = os.path.dirname(os.path.abspath(__file__))
SERVER_URL     = "http://localhost:5001/card"
MESSAGES_DB    = os.path.expanduser("~/Library/Messages/chat.db")
STATE_FILE     = os.path.join(_HERE, "sms_watcher_state.json")
PID_FILE       = "/tmp/sms_watcher.pid"
CHECK_INTERVAL = 5      # 초마다 확인
LOOKBACK_SEC   = 600    # 최근 N초 이내 메시지는 ROWID 관계없이 재확인

def _notify(text: str):
    """Telegram으로 간단한 알림 전송."""
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
    except Exception:
        pass

# sms_patterns.json에서 키워드 로드
def _load_patterns():
    path = os.path.join(_HERE, "sms_patterns.json")
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("keywords", []), data.get("skip_keywords", [])
    except Exception as e:
        logging.warning(f"sms_patterns.json 로드 실패: {e} — 기본값 사용")
        return [], []

KEYWORDS, SKIP_KEYWORDS = _load_patterns()
# ──────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [SMS Watcher] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(_HERE, "sms_watcher.log"))
    ]
)

NOTIFY_URL    = "http://localhost:5001/internal/notify"
ALERT_COOLDOWN = 600  # 10분에 한 번만 알림
_last_alert_time = 0

def notify_telegram(msg: str):
    global _last_alert_time
    now = time.time()
    if now - _last_alert_time < ALERT_COOLDOWN:
        return
    _last_alert_time = now
    try:
        requests.post(NOTIFY_URL, json={"message": msg}, timeout=3)
    except Exception:
        pass


# ── Apple epoch ──────────────────────────────────────────────────

APPLE_EPOCH = 978307200  # 2001-01-01 UTC

def now_apple() -> int:
    return int((datetime.now(timezone.utc).timestamp() - APPLE_EPOCH) * 1_000_000_000)

def apple_date_to_unix(apple_date: int) -> float:
    return apple_date / 1e9 + APPLE_EPOCH


# ── State 관리 ───────────────────────────────────────────────────

def load_state() -> tuple[int, set]:
    if os.path.exists(STATE_FILE):
        try:
            data = json.load(open(STATE_FILE))
        except Exception:
            logging.warning("state 파일 손상 — 초기화")
            return -1, set()

        seen = set(data.get("seen_rowids", []))

        if "last_rowid" in data and "last_date" not in data:
            return data["last_rowid"], seen

        if "last_date" in data:
            last_date = data["last_date"]
            rowid = _find_max_rowid_before_date(last_date)
            logging.info(f"구버전 state 마이그레이션: date={last_date} → ROWID={rowid}")
            save_state(rowid, set())
            return rowid, set()

    return -1, set()


def save_state(last_rowid: int, seen_rowids: set):
    recent_seen = sorted(seen_rowids)[-(200):]
    with open(STATE_FILE, "w") as f:
        json.dump({"last_rowid": last_rowid, "seen_rowids": recent_seen}, f)


def _find_max_rowid_before_date(last_date: int) -> int:
    try:
        tmp_dir = tempfile.mkdtemp()
        tmp_db  = os.path.join(tmp_dir, "chat.db")
        shutil.copy2(MESSAGES_DB, tmp_db)
        conn = sqlite3.connect(tmp_db)
        cur  = conn.cursor()
        cur.execute(
            "SELECT COALESCE(MAX(ROWID), 0) FROM message WHERE date <= ? AND is_from_me = 0",
            (last_date,)
        )
        row = cur.fetchone()
        conn.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return row[0] if row else 0
    except Exception:
        return 0


# ── attributedBody 디코딩 ─────────────────────────────────────────

def _decode_attributed_body(data: bytes) -> str | None:
    if not data:
        return None
    try:
        plist = plistlib.loads(bytes(data))
        objects = plist.get('$objects', [])
        if len(objects) > 2 and isinstance(objects[2], str):
            return objects[2]
        skip = {'$null', 'NSAttributedString', 'NSString', 'NSMutableString',
                'NSColor', 'NSFont', 'NSParagraphStyle', 'NSShadow'}
        for obj in objects:
            if isinstance(obj, str) and obj not in skip and len(obj) > 0:
                return obj
    except Exception:
        pass
    return None


# ── DB 읽기 ──────────────────────────────────────────────────────

def _copy_db() -> str | None:
    try:
        tmp_dir = tempfile.mkdtemp()
        tmp_db  = os.path.join(tmp_dir, "chat.db")
        shutil.copy2(MESSAGES_DB, tmp_db)
        for ext in ["-wal", "-shm"]:
            src = MESSAGES_DB + ext
            if os.path.exists(src):
                shutil.copy2(src, tmp_db + ext)
        return tmp_db
    except Exception as e:
        import errno as _errno
        is_perm = isinstance(e, PermissionError) or (isinstance(e, OSError) and e.errno == _errno.EPERM)
        if is_perm:
            logging.error(f"❌ Messages DB 접근 권한 없음: {e}")
            notify_telegram(
                "❌ sms_watcher: Messages DB 접근 권한 없음\n\n"
                "해결: 시스템 설정 → 개인정보 보호 → 전체 디스크 접근\n"
                "→ Python 실행 파일 추가 필요"
            )
        else:
            logging.error(f"DB 복사 오류: {e}")
        return None


def get_new_messages(last_rowid: int, seen_rowids: set) -> list:
    tmp_db = _copy_db()
    if tmp_db is None:
        return []

    cutoff = now_apple() - int(LOOKBACK_SEC * 1_000_000_000)

    try:
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        cur  = conn.cursor()
        cur.execute("""
            SELECT m.ROWID, m.text, m.attributedBody, m.date, h.id AS sender
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE (m.ROWID > ? OR m.date >= ?)
              AND m.is_from_me = 0
              AND (m.text IS NOT NULL OR m.attributedBody IS NOT NULL)
            ORDER BY m.ROWID ASC
        """, (last_rowid, cutoff))
        raw_rows = cur.fetchall()
        rows = []
        for r in raw_rows:
            d = dict(r)
            if not d['text'] and d.get('attributedBody'):
                d['text'] = _decode_attributed_body(d['attributedBody'])
            d.pop('attributedBody', None)
            if d['text']:
                rows.append(d)
        conn.close()
    except Exception as e:
        logging.error(f"DB 읽기 오류: {e}")
        rows = []
    finally:
        shutil.rmtree(os.path.dirname(tmp_db), ignore_errors=True)

    new = [r for r in rows if r["ROWID"] not in seen_rowids]
    if len(rows) != len(new):
        logging.debug(f"lookback dedup: {len(rows) - len(new)}건 중복 제거")
    return new


# ── 메시지 분류 ──────────────────────────────────────────────────

def is_card_message(text: str) -> bool:
    if any(kw in text for kw in SKIP_KEYWORDS):
        return False
    return any(kw in text for kw in KEYWORDS)


# ── 서버 전송 ────────────────────────────────────────────────────

def send_to_server(text: str) -> bool:
    try:
        resp = requests.post(SERVER_URL, json={"text": text}, timeout=15)
        data = resp.json()
        if data.get("skipped"):
            logging.info(f"파싱 skip ({data['skipped']}): {text[:30]}...")
        elif data.get("ok"):
            logging.info(f"✅ 전송 성공: {text[:30]}...")
        else:
            logging.warning(f"서버 응답 이상: {data}")
        return True
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        logging.error("서버 연결 실패 (Flask 미준비?) — 이번 배치 중단")
        return False
    except ValueError:
        logging.error("서버 응답 파싱 실패 (빈 응답) — 3초 후 재시도")
        time.sleep(3)
        try:
            resp = requests.post(SERVER_URL, json={"text": text}, timeout=15)
            data = resp.json()
            if data.get("ok") or data.get("skipped"):
                logging.info(f"✅ 재시도 성공: {text[:30]}...")
                return True
        except Exception:
            pass
        return False
    except Exception as e:
        logging.error(f"서버 전송 오류: {e}")
        return False


# ── PID 잠금 ─────────────────────────────────────────────────────

def acquire_pid_lock() -> bool:
    if os.path.exists(PID_FILE):
        try:
            old_pid = int(open(PID_FILE).read().strip())
            os.kill(old_pid, 0)
            logging.error(f"❌ 이미 실행 중 (PID={old_pid}). 종료.")
            return False
        except (ProcessLookupError, OSError):
            pass
    open(PID_FILE, "w").write(str(os.getpid()))
    return True

def release_pid_lock():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


# ── 누락 내역 처리 ───────────────────────────────────────────────

def process_missed_messages(last_rowid: int, seen_rowids: set) -> int:
    msgs      = get_new_messages(last_rowid, seen_rowids)
    card_msgs = [m for m in msgs if is_card_message(m["text"] or "")]

    if not card_msgs:
        if msgs:
            last_rowid = msgs[-1]["ROWID"]
            seen_rowids.update(m["ROWID"] for m in msgs)
            save_state(last_rowid, seen_rowids)
        return last_rowid

    logging.info(f"⚠️ 누락 카드 문자 {len(card_msgs)}건 감지 — Flask 준비 대기 중...")

    for _ in range(12):
        try:
            requests.get("http://localhost:5001/", timeout=2)
            break
        except Exception:
            time.sleep(5)

    try:
        requests.post(NOTIFY_URL, json={
            "message": f"⚠️ 봇이 중단된 동안 카드 문자 {len(card_msgs)}건이 누락됐어요. 지금 전송합니다..."
        }, timeout=5)
    except Exception:
        pass

    for msg in card_msgs:
        text = msg["text"] or ""
        logging.info(f"📤 누락 내역 전송: (ROWID={msg['ROWID']}) {text[:40]}")
        if send_to_server(text):
            last_rowid = msg["ROWID"]
            seen_rowids.add(msg["ROWID"])
            save_state(last_rowid, seen_rowids)
        else:
            logging.error("누락 내역 전송 실패 — 다음 재시작 시 재시도")
            break

    for msg in msgs:
        seen_rowids.add(msg["ROWID"])
    if msgs:
        new_last = max(msg["ROWID"] for msg in msgs)
        if new_last > last_rowid:
            last_rowid = new_last
            save_state(last_rowid, seen_rowids)

    return last_rowid


# ── 메인 ─────────────────────────────────────────────────────────

def main():
    logging.info("🚀 SMS Watcher 시작")

    if not acquire_pid_lock():
        return

    if not os.path.exists(MESSAGES_DB):
        logging.error("Messages DB를 찾을 수 없어요.")
        return

    last_rowid, seen_rowids = load_state()

    if last_rowid == -1:
        msgs = get_new_messages(-1, set())
        if msgs:
            last_rowid = msgs[-1]["ROWID"]
            seen_rowids.update(m["ROWID"] for m in msgs)
        else:
            last_rowid = 0
        save_state(last_rowid, seen_rowids)
        logging.info(f"최초 실행 — ROWID={last_rowid}부터 감시 시작")
    else:
        last_rowid = process_missed_messages(last_rowid, seen_rowids)

    logging.info(f"👀 감시 중... (last_rowid={last_rowid})")
    _notify(f"👀 SMS Watcher 감시 시작\n~/Library/Messages/chat.db 모니터링 중")

    MAX_SEEN     = 1000
    SEND_RETRIES = 3
    SEND_DELAY   = 3

    try:
        while True:
            try:
                new_msgs = get_new_messages(last_rowid, seen_rowids)
                stop_loop = False
                for msg in new_msgs:
                    if stop_loop:
                        break
                    text  = msg["text"] or ""
                    rowid = msg["ROWID"]

                    if is_card_message(text):
                        logging.info(f"💳 카드 문자 감지! (ROWID={rowid}) {text[:30]!r}")
                        sent = False
                        for attempt in range(SEND_RETRIES):
                            if attempt > 0:
                                logging.warning(f"  ↻ 재시도 {attempt}/{SEND_RETRIES - 1} (ROWID={rowid})")
                                time.sleep(SEND_DELAY)
                            if send_to_server(text):
                                sent = True
                                break
                        if not sent:
                            logging.error(f"❌ 전송 최종 실패 (ROWID={rowid}) — 다음 폴링에서 재시도")
                            stop_loop = True
                            break

                    seen_rowids.add(rowid)
                    if rowid > last_rowid:
                        last_rowid = rowid
                        save_state(last_rowid, seen_rowids)

                if len(seen_rowids) > MAX_SEEN:
                    sorted_seen = sorted(seen_rowids)
                    seen_rowids = set(sorted_seen[-(MAX_SEEN // 2):])

            except Exception as e:
                logging.error(f"메인 루프 오류: {e}")
            time.sleep(CHECK_INTERVAL)
    finally:
        release_pid_lock()
        _notify("🛑 SMS Watcher 감시 종료")


if __name__ == "__main__":
    main()
