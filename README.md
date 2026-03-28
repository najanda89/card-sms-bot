# 💳 Card SMS Bot

macOS의 Messages 앱 DB를 감시하여 카드 결제 문자를 자동으로 파싱하고, 텔레그램 알림과 웹 대시보드로 지출 내역을 관리하는 봇입니다.

## 요구사항

- macOS (Messages 앱 사용 중인 Mac)
- Python 3.11+
- 텔레그램 봇 토큰 ([BotFather](https://t.me/botfather)에서 발급)
- 시스템 설정 → 개인정보 보호 → **전체 디스크 접근** 권한 (Python 실행 파일에 부여 - main.py)

## iPhone → Mac 문자 수신 설정

iPhone에서 수신한 카드 SMS가 Mac에서도 보이려면 다음 설정이 필요합니다:

1. **iPhone** → 설정 → 메시지 → **문자 메시지 전달** → Mac 이름 활성화
2. **iPhone과 Mac이 같은 Apple ID**로 로그인되어 있어야 함
3. Mac에서 **메시지 앱** 실행 후 Apple ID로 로그인

설정 완료 후 iPhone으로 카드 결제가 오면 Mac 메시지 앱에서도 수신되며, 봇이 자동으로 감지합니다.

## 지원 카드사

기본 내장: **광주카드, KB국민카드, 현대카드**

**다른 카드사 추가**: 텔레그램에서 `/learn` 명령어를 실행하고 안내에 따라 카드 문자를 붙여넣으면 자동으로 패턴이 등록됩니다. 
같은 카드사라도 승인/취소/할부 형식이 다르면 각각 등록할 수 있습니다.

## 설치

```bash
git clone https://github.com/your-username/card-sms-bot.git
cd card-sms-bot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 설정

```bash
cp .env.example .env
```

`.env` 파일을 열어 텔레그램 정보를 입력하세요:

```
TELEGRAM_TOKEN=your_telegram_bot_token_here
CHAT_ID=your_telegram_chat_id_here
```

**카드사 키워드 설정** (`sms_patterns.json`):

```json
{
  "keywords": ["광주카드", "KB국민카드", "국민카드", "현대카드"],
  "skip_keywords": ["한도초과", "한도 초과"]
}
```

자신의 카드사 SMS에 포함된 키워드로 수정하세요.

## 실행

```bash
python3 main.py
```

봇이 시작되면 텔레그램으로 시작 메시지가 전송됩니다.

## 웹 대시보드

봇 실행 중 브라우저에서 접속:

```
http://localhost:5001
```

- 전체 카드 지출 내역 조회
- 카드사별 월 지출 현황
- 카테고리 / 메모 관리

## 텔레그램 명령어

| 명령어 | 설명 |
|--------|------|
| `/learn` | 새 카드사 문자 패턴 등록 |
| `/status` | 봇 및 SMS Watcher 상태 확인 |
| `/memo [내용]` | 최근 거래에 메모 추가 |
| `/memo [ID] [내용]` | 특정 거래에 메모 추가 |
| `/edit` | 최근 거래 카테고리 분류 |
| `/edit [ID]` | 특정 거래 카테고리 분류 |
| `/amount [금액]` | 최근 거래 금액 수정 |
| `/budget` | 카드사별 월 예산 현황 |
| `/limit [카드사] [한도]` | 카드 한도 설정 |
| `/export` | 전체 내역 CSV 다운로드 |
| `/export [YYYY-MM]` | 특정 월 CSV 다운로드 |
| `/watcher` | SMS Watcher 상태/재시작 |
| `/restart` | SMS Watcher 재시작 |
| `/reboot` | 봇 전체 재시작 |

## 동작 원리

```
iPhone (카드 SMS 수신)
    ↓  iMessage/SMS 동기화
Mac Messages 앱 (~/Library/Messages/chat.db)
    ↓  5초마다 폴링
sms_watcher.py
    ↓  카드 키워드 감지 → HTTP POST
Flask 서버 (port 5001)
    ↓  SMS 파싱 → SQLite 저장
Telegram 봇 → 알림 전송
```

> **주의**: iPhone과 Mac이 같은 Apple ID로 연결되어 iMessage/SMS 동기화가 활성화되어 있어야 합니다.

## 파일 구조

```
card-sms-bot/
├── main.py              # 진입점 (Telegram 봇 + Flask + SMS Watcher 실행)
├── sms_watcher.py       # Messages DB 감시 프로세스
├── parser.py            # 카드 SMS 파싱 로직
├── database.py          # SQLite CRUD
├── handlers.py          # Telegram 명령어 핸들러
├── web.py               # Flask 웹서버 + 대시보드 API
├── watcher.py           # SMS Watcher 프로세스 관리
├── utils.py             # 공통 유틸리티
├── categories.py        # 지출 카테고리 정의
├── scheduler.py         # 정기 작업 스케줄러
├── sms_patterns.json    # 카드사 키워드 설정
├── learned_patterns.json  # /learn 으로 등록한 패턴 (자동 생성)
├── static/              # 웹 대시보드 정적 파일
├── .env.example         # 환경변수 예시
└── requirements.txt     # 의존성 목록
```

## 라이선스

MIT
