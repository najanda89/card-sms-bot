"""
watcher.py — sms_watcher 프로세스 관리 및 자동 재시작 모니터링
"""
import os
import sys
import subprocess
import logging
import time
from typing import Optional

from utils import send_telegram_sync

watcher_proc: Optional[subprocess.Popen] = None
_intentional_stop = False  # 의도적 중지 플래그 — True면 monitor가 재시작 안 함


def _find_python() -> Optional[str]:
    """Python 실행 경로 탐색.
    PyInstaller 번들에서는 sys.executable이 앱 바이너리이므로 실제 python3를 별도 탐색."""
    # 일반 Python 환경 (개발/터미널 실행)
    if not getattr(sys, "frozen", False):
        return sys.executable

    # PyInstaller 번들 — venv 우선, 없으면 시스템 python3
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "venv/bin/python3"),
        os.path.join(script_dir, "venv/bin/python"),
        os.path.expanduser("~/Desktop/card-sms-bot/venv/bin/python3"),
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _find_watcher_script() -> Optional[str]:
    """sms_watcher.py 경로 탐색 (PyInstaller _MEIPASS → 소스 폴더 → Desktop 순)."""
    candidates = []

    # PyInstaller 번들 내 추출 경로
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        candidates.append(os.path.join(meipass, "sms_watcher.py"))

    # 현재 파일 기준 같은 폴더
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sms_watcher.py"))

    # Desktop fallback
    candidates.append(os.path.expanduser("~/Desktop/card-sms-bot/sms_watcher.py"))

    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def start_watcher() -> Optional[subprocess.Popen]:
    global watcher_proc, _intentional_stop
    _intentional_stop = False

    python = _find_python()
    script = _find_watcher_script()

    if not python or not script:
        logging.error(f"❌ sms_watcher 시작 불가 — python={python}, script={script}")
        return None

    # 혹시 살아남은 좀비 sms_watcher 정리 (PID 잠금 충돌 방지)
    subprocess.run(["pkill", "-f", "sms_watcher.py"], capture_output=True)
    time.sleep(0.3)

    watcher_proc = subprocess.Popen([python, script])
    logging.info(f"👀 sms_watcher 시작 (python={python}, PID: {watcher_proc.pid})")
    return watcher_proc


def stop_watcher():
    global watcher_proc, _intentional_stop
    _intentional_stop = True  # 재시작 억제
    if watcher_proc and watcher_proc.poll() is None:
        watcher_proc.terminate()
        watcher_proc.wait()
        logging.info("🛑 sms_watcher 종료")


def restart_watcher() -> subprocess.Popen:
    stop_watcher()
    return start_watcher()


def monitor_watcher():
    """백그라운드에서 sms_watcher 감시 — 크래시 시 Telegram 알림 + 자동 재시작.
    의도적 중지(_intentional_stop=True) 상태에서는 재시작하지 않음."""
    while True:
        time.sleep(10)
        if _intentional_stop:
            continue
        if watcher_proc and watcher_proc.poll() is not None:
            exit_code = watcher_proc.returncode
            logging.warning(f"❌ sms_watcher 종료 감지 (exit: {exit_code}) — 재시작 중...")
            send_telegram_sync(f"⚠️ sms_watcher가 종료됐어요 (exit: {exit_code})\n자동 재시작 중...")
            restart_watcher()
            send_telegram_sync(f"✅ sms_watcher 재시작 완료 (PID: {watcher_proc.pid})")
