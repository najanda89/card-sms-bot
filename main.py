"""
main.py — 카드 SMS 봇 진입점
스레드 구성: Flask 웹서버 | SMS Watcher 모니터 | 스케줄러 | Telegram 봇
"""
import logging
import logging.handlers
import threading
import os
import sys
import atexit

from dotenv import load_dotenv
load_dotenv()

from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters,
)

VERSION = "1.0.0"

from database import init_db
from utils import load_targets, load_limits, send_telegram_sync
import watcher as watcher_mod
from scheduler import run_scheduler
from web import flask_app, run_flask, wait_for_flask
from handlers import (
    start, handle_message,
    cmd_status, cmd_memo, cmd_edit, cmd_budget, cmd_restart, cmd_amount, cmd_help, cmd_skip,
    cmd_watcher, cmd_reboot,
    handle_callback, cmd_export, cmd_limit, cmd_dev,
)

TOKEN = os.getenv("TELEGRAM_TOKEN")

PID_FILE = os.path.join(os.path.dirname(__file__), ".bot.pid")

def check_already_running():
    if os.path.exists(PID_FILE):
        try:
            pid = int(open(PID_FILE).read().strip())
            os.kill(pid, 0)
            print(f"❌ 이미 실행 중입니다 (PID: {pid}). 중복 실행을 막습니다.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(PID_FILE) and os.remove(PID_FILE))

_LOG_FILE = os.path.join(os.path.dirname(__file__), "card_bot.log")
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logging.getLogger().addHandler(_file_handler)

logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("werkzeug").propagate = False
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


def main():
    check_already_running()
    init_db()
    load_targets()
    load_limits()

    print(f"🚀 카드 SMS 봇 v{VERSION} 시작 중...")
    threading.Thread(target=run_flask, daemon=True).start()
    print("✅ Flask 서버 실행 중 (port 5001)...")
    print("🌐 대시보드: http://localhost:5001")

    if not wait_for_flask():
        print("❌ Flask 시작 실패 — 종료합니다.")
        raise SystemExit(1)

    watcher_mod.start_watcher()
    threading.Thread(target=watcher_mod.monitor_watcher, daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()

    send_telegram_sync("🚀 카드 SMS 봇 시작됐어요!\n\nFlask / SMS Watcher 모두 실행 중입니다.")

    async def error_handler(update, context):
        logging.error(f"텔레그램 오류: {context.error}", exc_info=context.error)

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("memo",    cmd_memo))
    app.add_handler(CommandHandler("edit",    cmd_edit))
    app.add_handler(CommandHandler("budget",  cmd_budget))
    app.add_handler(CommandHandler("limit",   cmd_limit))
    app.add_handler(CommandHandler("export",  cmd_export))
    app.add_handler(CommandHandler("amount",  cmd_amount))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("skip",    cmd_skip))
    app.add_handler(CommandHandler("watcher", cmd_watcher))
    app.add_handler(CommandHandler("reboot",  cmd_reboot))
    app.add_handler(CommandHandler("dev",     cmd_dev))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ 카드 SMS 봇 실행 중...")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
