# Auto Betman 실행 가이드

## 사전 준비

### 무료 서버 배포 참고

- 24시간 상시 운영 기준 무료/저비용 서버 선택과 Oracle 배포 절차는 `docs/HOSTING_FREE.md`를 참고하세요.

### 1. Discord 봇 생성 및 토큰 발급

1. [Discord Developer Portal](https://discord.com/developers/applications)에 접속
2. **New Application** 클릭 → 이름 입력 후 생성
3. 왼쪽 메뉴에서 **Bot** 클릭
4. **Reset Token** 클릭하여 토큰 복사 → `.env`의 `DISCORD_BOT_TOKEN`에 입력
5. **Privileged Gateway Intents** 에서 **Message Content Intent** 활성화

### 2. 봇 서버 초대

아래 URL에서 `{CLIENT_ID}` 부분을 애플리케이션 ID로 교체 후 브라우저에서 접속:

```
https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&permissions=2048&scope=bot+applications.commands
```

> `applications.commands` 스코프가 반드시 포함되어야 슬래시 명령어가 등록됩니다.

### 3. 채널 ID 확인

1. Discord 설정 → 고급 → **개발자 모드** 활성화
2. 알림을 받을 채널 우클릭 → **채널 ID 복사**
3. `.env`의 `DISCORD_CHANNEL_ID`에 입력

---

## 환경 변수 설정

```bash
cp .env.example .env
```

`.env` 파일을 편집:

```env
DISCORD_BOT_TOKEN=your_bot_token_here
DISCORD_CHANNEL_ID=123456789012345678
HEADLESS=true
POLLING_INTERVAL_MINUTES=30
```

| 변수 | 필수 | 설명 | 기본값 |
|------|:----:|------|:------:|
| `DISCORD_BOT_TOKEN` | O | Discord 봇 토큰 | - |
| `DISCORD_CHANNEL_ID` | O | 알림을 보낼 채널 ID | - |
| `HEADLESS` | | 브라우저 헤드리스 모드 | `true` |
| `POLLING_INTERVAL_MINUTES` | | 자동 스크래핑 주기 (분) | `30` |

---

## 실행 방법

### Docker 실행 (권장)

```bash
# 1. storage 디렉토리 생성
mkdir -p ./storage

# 2. 빌드 & 포그라운드 실행 (로그 확인용)
docker-compose up --build

# 3. 정상 확인 후 Ctrl+C로 중단, 백그라운드 실행
docker-compose up -d
```

#### Docker 관리 명령어

```bash
# 실시간 로그 확인
docker-compose logs -f

# 최근 로그 50줄 확인
docker-compose logs --tail=50

# 봇 재시작
docker-compose restart

# 봇 중지
docker-compose down

# 이미지 재빌드 후 실행
docker-compose up --build -d
```

### 로컬 실행

```bash
# 1. 가상환경 생성 & 활성화
python -m venv .venv && source .venv/bin/activate

# 2. 의존성 설치
pip install -r requirements.txt
playwright install chromium

# 3. 실행
python -m src.main --schedule    # 주기적 반복 실행 (상시 운영)
python -m src.main               # 1회 스크래핑 후 종료
```

---

## 실행 확인

### 정상 시작 로그

아래 로그가 순서대로 출력되면 정상입니다:

```
Starting in SCHEDULE mode (every 30 min)
Database initialized at /app/storage/betman.db
Slash commands synced globally
Discord bot logged in as BotName#1234
Slash commands synced to guild: 서버이름 (instant)
```

### 디스코드에서 확인

1. 설정한 채널에 **"Betman Tracker 시작됨 ✔"** 메시지가 표시됨
2. 채팅창에 `/`를 입력하면 자동완성 목록에 `setup`, `betman`, `stats`, `filter` 명령어가 표시됨

---

## 트러블슈팅

### 슬래시 명령어가 안 보일 때

- 봇 초대 URL에 `applications.commands` 스코프가 포함되어 있는지 확인
- 봇을 서버에서 추방 후 위 URL로 재초대
- 글로벌 명령어 동기화는 최대 1시간 소요 → 길드 동기화는 즉시 반영됨

### Playwright 버전 에러

```
Executable doesn't exist at /ms-playwright/chromium_headless_shell-...
```

`Dockerfile`의 이미지 태그와 `requirements.txt`의 playwright 버전이 일치해야 합니다:

```dockerfile
# Dockerfile — 현재 설정
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble
```

버전 불일치 시 Dockerfile의 이미지 태그를 에러 메시지에 표시된 버전으로 수정 후 재빌드:

```bash
docker-compose up --build -d
```

### Docker 데몬 연결 실패

```
Cannot connect to the Docker daemon
```

Docker Desktop이 실행 중인지 확인합니다.

### 데이터 초기화

세션이나 DB에 문제가 생긴 경우:

```bash
# 세션 파일만 삭제 (재로그인 필요)
rm -f ./storage/session_*.json

# 전체 초기화 (DB 포함, 모든 데이터 삭제)
rm -rf ./storage/*
docker-compose restart
```

---

## 프로젝트 구조

```
auto_betman/
├── src/
│   ├── main.py          # 엔트리포인트, Orchestrator
│   ├── config.py        # 환경 변수 설정
│   ├── models.py        # BetSlip, MatchBet 데이터 모델
│   ├── browser.py       # Playwright 브라우저 관리
│   ├── auth.py          # 베트맨 로그인/세션 관리
│   ├── scraper.py       # 구매내역 스크래핑 (XHR + DOM)
│   ├── discord_bot.py   # Discord 봇 + 슬래시 명령어
│   └── database.py      # SQLite 비동기 저장소
├── tests/               # pytest 테스트
├── storage/             # 런타임 데이터 (세션, DB)
│   ├── betman.db        # SQLite 데이터베이스
│   └── session_*.json   # 유저별 브라우저 세션
├── .env                 # 환경 변수 (비공개)
├── .env.example         # 환경 변수 템플릿
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## 테스트

```bash
pytest tests/
pytest tests/ -v          # 상세 출력
pytest tests/test_filters.py -v  # 특정 테스트만
```
