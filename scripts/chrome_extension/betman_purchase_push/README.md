# 응큼픽딱걸렸노 (Chrome Extension)

## 설치

1. Chrome 주소창에서 `chrome://extensions` 열기
2. 우측 상단 `개발자 모드` 활성화
3. `압축해제된 확장 프로그램을 로드합니다` 클릭
4. 이 폴더 선택
- `/Users/junghoon/.codex/worktrees/b995/auto_betman/scripts/chrome_extension/betman_purchase_push`
5. 확장 옵션에서 Discord Webhook URL 저장

## 사용

1. Betman 로그인 후 구매
2. 확장이 자동으로 감지해서 Discord로 이미지 전송

## 감지 방식

1. 구매완료 결과 페이지 즉시 감지(핵심)
- `/main/mainPage/mypage/myPaymentResult.do`를 구매완료 이벤트로 인식
- `requestClient.requestPostMethod('/mypgPayment/paymentResult.do')`로 `buyList`를 즉시 수집
- API 실패 시 DOM에서 slip ID를 fallback 추출
- 큐(`betman_pending_capture_queue_v1`) 저장
- 즉시 `myPurchaseWinList.do`로 강제 이동 후 slip별 `#paperArea` 순차 캡처/전송

2. 이벤트 트리거
- `load`
- URL 전환(`pushState`, `replaceState`, `popstate`)
- `myPaymentResult.do` 렌더 대기용 mutation

## 전송/중복 정책

- 전송 형식: 이미지 파일 1장 + 고정 경고문구(`🚨 응큼픽 감지!!!!!!!!!!!!!!!!!!!!!!! 🚨`)
- 중복 방지: `slip_id` 기반 dedupe (24시간)
- baseline 저장: `last_seen_head_slip_id_v1`
- 큐 스키마: `{ slipIds, createdAt, reason, attemptsBySlip, fingerprint }`
- 구 단건 키(`betman_pending_capture_v1`)는 자동 마이그레이션

## 주요 실패 코드

- `payment_result_api_fetch_fail`
- `history_poll_failed`
- `row_not_found`
- `openGamePaper_failed`
- `paperArea_not_ready`
- `webhook_send_failed`
- `screenshot_capture_failed(capture_permission_denied|capture_tab_not_active|capture_visible_tab_failed)`
