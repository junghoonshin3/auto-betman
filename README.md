# Auto Betman

Betman(베트맨) 구매내역/분석/발매경기를 조회하고, 신규 구매를 Discord 채널에 자동 알림하는 봇입니다.

## 주요 기능

- `/login`: 사용자별 Betman 로그인 세션 저장
- `/purchases`: 최근 구매내역 5건 조회
  - 표시 포맷: `승부식/승무패/기록식/기타` 유형별 섹션으로 정리
  - 경기 라인: 내 픽 팀은 `🎯` + 굵게(`**팀명**`)로 강조
  - 스냅샷: `발매중/발매마감` 상태 건의 `#paperArea` 상세를 로드 완료 후 캡처 시도
  - 일부 건 로드 실패 시 해당 건만 스킵하고, 나머지 건은 계속 첨부 전송
- `/analysis`: 최근 N개월 구매현황분석 조회
- `/games`: 발매중 전체 경기 스크린샷 조회
  - 기본 조회값: `승부식/전체`
  - 로그인 없이 조회 가능(공개 경기목록 페이지 기준)
  - 구매가능 목록의 `게임구매` 링크로 들어간 상세 페이지 경기리스트 전체를 캡처
  - 필터 매칭된 대상 링크를 전부 순회해 캡처
  - 출력 형식: 텍스트 요약 없이 이미지 파일만 전송(최대 10개씩 분할)
  - 캡처 실패/대상 없음 시 최소 안내 1줄 전송
- `/logout`: 사용자 세션 종료
- 자동 신규 구매 알림:
  - `/login` 성공 사용자를 자동 감시
  - `storage/session_state_*.json` 사용자 자동 복원
  - `POLLING_INTERVAL_MINUTES` 주기(기본 5분)로 신규 `slip_id` 감지
  - 감시 시작 시 기존 내역은 baseline 처리(재알림 없음)
  - 세션 만료 시 해당 사용자 감시 중지 + 채널 안내
  - 자세한 동작: `docs/AUTO_NOTIFY.md`

## 기술 스택

- Python 3.11+
- Playwright
- discord.py
- python-dotenv

## 환경 변수

| 변수 | 필수 | 설명 | 기본값 |
|------|:----:|------|:------:|
| `DISCORD_BOT_TOKEN` | O | Discord 봇 토큰 | - |
| `DISCORD_CHANNEL_ID` | O | 자동 신규 구매 알림 채널 ID | - |
| `DISCORD_GUILD_ID` |  | 슬래시 명령 즉시 동기화용 길드 ID | - |
| `HEADLESS` |  | Playwright 헤드리스 모드 | `true` |
| `POLLING_INTERVAL_MINUTES` |  | 자동 신규 구매 확인 주기(분, 1~60) | `5` |
| `FAKE_PURCHASES_FILE` |  | 테스트용 가짜 구매내역 JSON 파일 경로 | - |

## 실행

### 로컬 실행

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# .env 편집

python -m src.main
```

### Docker 실행

```bash
cp .env.example .env
docker-compose up -d --build
docker-compose logs -f
```

## 프로젝트 구조

```text
auto_betman/
├── src/
│   ├── main.py
│   ├── bot.py
│   ├── auth.py
│   ├── purchases.py
│   ├── analysis.py
│   ├── games.py
│   └── models.py
├── tests/
├── docs/
└── storage/  # 런타임 세션 파일
```

## 테스트

```bash
pytest tests/test_main_auto_notify.py tests/test_bot_purchases_format.py
```

## 실구매 없이 테스트

1. `FAKE_PURCHASES_FILE`에 JSON 파일 경로를 설정합니다.
2. 파일에 `slip_id`가 있는 구매내역을 넣으면 `/purchases`와 자동 알림이 해당 데이터를 사용합니다.
3. 신규 알림 테스트는 파일에 새로운 `slip_id`를 추가한 뒤 주기(기본 5분)를 기다리면 됩니다.
