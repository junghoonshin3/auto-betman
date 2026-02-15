# Auto Betman

Betman(베트맨) 구매내역/분석/발매경기를 조회하는 Discord 봇입니다.

## 주요 기능

- `/login`: 사용자별 Betman 로그인 세션 저장
  - 로그인 성공 시 로컬(`storage/login_credentials_map.json`)에 아이디/비밀번호를 저장
  - 이후 `/purchases`, `/analysis` 실행 시 세션 만료 상태면 자동 재로그인 1회 시도
  - 자동 재로그인 실패 시 저장 자격증명 삭제 후 `/login` 재요청
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
  - 저장된 자동 로그인 자격증명도 함께 삭제
- 실시간 구매 푸시(옵션):
  - 봇과 별개로 Chrome Extension에서 구매 완료 화면을 감지
  - Discord Webhook으로 스크린샷만 즉시 전송
  - 구매 완료 화면 감지 시 자동 전송(수동 테스트 버튼 없음)
  - 브라우저 콘솔 로그로 상태 확인 가능
  - 중복로그인 제약을 피하기 위해 Betman 웹과 동일 세션에서만 동작
  - 설치/설정: `docs/REALTIME_PURCHASE_PUSH.md`

## 기술 스택

- Python 3.11+
- Playwright
- discord.py
- python-dotenv

## 환경 변수

| 변수 | 필수 | 설명 | 기본값 |
|------|:----:|------|:------:|
| `DISCORD_BOT_TOKEN` | O | Discord 봇 토큰 | - |
| `DISCORD_GUILD_ID` |  | 슬래시 명령 즉시 동기화용 길드 ID | - |
| `HEADLESS` |  | Playwright 헤드리스 모드 | `true` |
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
├── scripts/
├── tests/
├── docs/
└── storage/  # 런타임 세션 파일
```

## 실시간 구매 푸시(옵션)

봇의 자동감시 대신, 사용자가 실제로 Betman 웹에서 구매할 때 브라우저에서 즉시 감지해 Discord로 스크린샷을 보낼 수 있습니다.

1. Chrome Extension 저장소: [junghoonshin3/betman-purchase-push-extension](https://github.com/junghoonshin3/betman-purchase-push-extension)
2. 해당 저장소를 로컬로 clone/download 후 `chrome://extensions`에 로드
3. Discord Webhook URL 설정
4. Betman 구매 완료 시 해당 완료 영역 스크린샷 자동 전송
5. 문제 발생 시 `docs/REALTIME_PURCHASE_PUSH.md`의 실패 코드별 조치 확인

상세 절차: `docs/REALTIME_PURCHASE_PUSH.md`

## 테스트

```bash
pytest tests/test_main_auto_login.py tests/test_bot_purchases_format.py
```

## 실구매 없이 테스트

1. `FAKE_PURCHASES_FILE`에 JSON 파일 경로를 설정합니다.
2. 파일에 `slip_id`가 있는 구매내역을 넣으면 `/purchases`가 해당 데이터를 사용합니다.
