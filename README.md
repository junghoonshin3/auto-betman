# Auto Betman

Betman(베트맨) 구매내역을 자동으로 스크래핑하여 Discord로 알림을 보내는 봇.

## 기술 스택

- **Python 3.11+**
- **Playwright** — 브라우저 자동화 (로그인 + 스크래핑)
- **discord.py** — Discord 봇 / 슬래시 명령어
- **aiosqlite** — 비동기 SQLite (결과 추적, 통계)
- **APScheduler** — 주기적 스크래핑
- **Docker** — 컨테이너 배포

## 주요 기능

- 구매내역 자동 스크래핑 (XHR 캡처 + DOM 파싱 폴백)
- Discord 실시간 알림 (신규 구매, 적중/미적중 결과)
- `/betman` — 수동 즉시 조회
- `/stats` — 적중률, 손익 통계 대시보드
- `/filter` — 알림 필터 (최소 금액, 게임 유형, 종목)
- 세션 영속화 (재로그인 최소화)

## 설치 및 실행

### 로컬 실행

```bash
# 1. 가상환경 생성
python -m venv .venv && source .venv/bin/activate

# 2. 의존성 설치
pip install -r requirements.txt
playwright install chromium

# 3. 환경 변수 설정
cp .env.example .env
# .env 파일 편집

# 4. 실행
python -m src.main              # 1회 스크래핑
python -m src.main --schedule   # 주기적 반복 실행
```

### Docker 실행

```bash
cp .env.example .env
# .env 파일 편집

docker-compose up -d
docker-compose logs -f
```

## 환경 변수

| 변수 | 필수 | 설명 | 기본값 |
|------|------|------|--------|
| `BETMAN_USER_ID` | O | 베트맨 로그인 ID | - |
| `BETMAN_USER_PW` | O | 베트맨 로그인 비밀번호 | - |
| `DISCORD_BOT_TOKEN` | O | Discord 봇 토큰 | - |
| `DISCORD_CHANNEL_ID` | O | 알림 채널 ID | - |
| `HEADLESS` | | 헤드리스 브라우저 모드 | `true` |
| `POLLING_INTERVAL_MINUTES` | | 스크래핑 주기 (분) | `30` |

## 프로젝트 구조

```
auto_betman/
├── src/
│   ├── main.py          # 엔트리포인트, Orchestrator
│   ├── config.py        # 환경 변수 설정
│   ├── models.py        # BetSlip, MatchBet 데이터 모델
│   ├── browser.py       # Playwright 브라우저 관리
│   ├── auth.py          # 베트맨 로그인
│   ├── scraper.py       # 구매내역 스크래핑
│   ├── discord_bot.py   # Discord 봇 + 슬래시 명령어
│   └── database.py      # SQLite 영속 저장소
├── tests/               # pytest 테스트
├── storage/             # 세션 + DB (gitignore)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 테스트

```bash
pytest tests/
```
