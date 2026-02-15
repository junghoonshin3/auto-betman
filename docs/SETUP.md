# Auto Betman 실행 가이드

## 1. Discord 준비

1. [Discord Developer Portal](https://discord.com/developers/applications)에서 앱 생성
2. **Bot** 탭에서 토큰 발급 후 `.env`의 `DISCORD_BOT_TOKEN`에 설정
3. 아래 URL로 서버 초대 (`{CLIENT_ID}` 교체)

```text
https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&permissions=2048&scope=bot+applications.commands
```

## 2. 환경 변수 설정

```bash
cp .env.example .env
```

예시:

```env
DISCORD_BOT_TOKEN=your_bot_token_here
DISCORD_GUILD_ID=
HEADLESS=true
FAKE_PURCHASES_FILE=/app/storage/fake_purchases.json
```

## 3. 실행

### 로컬

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python -m src.main
```

### Docker

```bash
docker-compose up -d --build
docker-compose logs -f
```

## 4. 디스코드 사용 흐름

1. `/login` 으로 Betman 로그인
2. 필요 시 `/purchases`, `/analysis`, `/games` 수동 조회
3. `/logout` 으로 세션/자동 로그인 자격증명을 함께 정리

### 실구매 없이 테스트하기

- `FAKE_PURCHASES_FILE` JSON을 사용하면 Betman 실구매 없이 구매내역을 시뮬레이션할 수 있습니다.
- JSON 예시:

```json
[
  {
    "slip_id": "TEST-0001",
    "game_type": "프로토 승부식",
    "round_number": "테스트",
    "status": "발매중",
    "purchase_datetime": "2026.02.14 10:00",
    "total_amount": 5000,
    "potential_payout": 12000,
    "combined_odds": 2.4,
    "matches": [
      {
        "match_number": 1,
        "sport": "축구",
        "league": "K리그1",
        "home_team": "전북",
        "away_team": "울산",
        "bet_selection": "승",
        "odds": 2.1,
        "match_datetime": "2026.02.15 19:00"
      }
    ]
  }
]
```

## 5. 트러블슈팅

### 슬래시 명령이 안 보일 때

- 초대 URL에 `applications.commands` 스코프 포함 여부 확인
- `DISCORD_GUILD_ID` 설정 시 길드 동기화가 더 빠름

### Playwright 브라우저 오류

```bash
playwright install chromium
```

### 세션 초기화

```bash
rm -f storage/session_state_*.json
```
