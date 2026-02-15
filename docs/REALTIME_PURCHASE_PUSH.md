# Betman 실시간 구매 푸시 (Chrome Extension)

이 문서는 Chrome Extension이 Betman 구매를 **실시간 감지**해서 Discord Webhook으로 투표지 스크린샷을 전송하는 방식과 점검 방법을 설명합니다.

## 1) 동작 방식

확장은 `myPaymentResult.do` 진입을 기준으로 즉시 동작합니다.

1. 구매완료 결과 페이지 즉시 감지
- `/main/mainPage/mypage/myPaymentResult.do` 진입 + `#purchaseSuccess` 표시 + `#purchaseResultTableBody tr` 존재를 구매완료 확정 이벤트로 인식
- `requestClient.requestPostMethod('/mypgPayment/paymentResult.do')` 호출로 `buyList`를 수집
- API 실패 시 DOM 파싱(`goMyPurWinDetail` 4번째 인자)로 slip ID fallback
- slip 목록을 큐(`betman_pending_capture_queue_v1`)에 저장
- `myPurchaseWinList.do`로 즉시 강제 이동 후 slip별 `#paperArea` 순차 캡처

중복 전송 방지:
- `sent_map_v1` 기준으로 24시간 dedupe
- pending 큐 스키마: `{ slipIds, createdAt, reason, attemptsBySlip, fingerprint }`
- 레거시 단건 키 `betman_pending_capture_v1`는 자동으로 큐로 마이그레이션

## 2) 준비물

1. Chrome/Edge
2. Discord Webhook URL
3. 확장 경로
- `/Users/junghoon/.codex/worktrees/b995/auto_betman/scripts/chrome_extension/betman_purchase_push`

## 3) 설치/설정

1. `chrome://extensions` 열기
2. 개발자 모드 ON
3. `압축해제된 확장 프로그램을 로드합니다`
4. 확장 폴더 선택
5. 옵션에서 Webhook URL 저장
6. 확장 Reload + Betman 탭 강력 새로고침

## 4) 운영 확인 포인트

1. `myPaymentResult.do` 노출 직후 API 로그가 찍히는지
2. slip 목록 저장 후 즉시 `myPurchaseWinList.do`로 강제 이동하는지
3. 큐에 쌓인 slip이 순차로 `#paperArea` 캡처/전송되는지
4. Discord에 이미지와 함께 고정 경고문구(`🚨 응큼픽 감지!!!!!!!!!!!!!!!!!!!!!!! 🚨`)만 올라오는지

## 5) 로그 키

부트:
- `[BetmanPushExt] boot version=... ext_id=... frame=top url=...`

결과페이지 API/강제 이동/큐:
- `[BetmanPushExt] payment_result_detected ...`
- `[BetmanPushExt] payment_result_api_fetch_start ...`
- `[BetmanPushExt] payment_result_api_fetch_success ...`
- `[BetmanPushExt] payment_result_api_fetch_fail ...`
- `[BetmanPushExt] payment_result_force_nav ...`
- `[BetmanPushExt] pending_queue_saved ...`
- `[BetmanPushExt] pending_queue_item_sent ...`
- `[BetmanPushExt] pending_queue_item_retry ...`
- `[BetmanPushExt] pending_queue_item_dropped ...`
- `[BetmanPushExt] pending_queue_completed ...`

투표지 열기/캡처:
- `[BetmanPushExt] openGamePaper route=bridge ok|fail`
- `[BetmanPushExt] paperArea ready|timeout`
- `[BetmanPushExt] webhook send ok|fail`

## 6) 실패 코드 가이드

1. `payment_result_api_fetch_fail`
- 결제결과 API 호출 실패

2. `row_not_found`
- 구매내역에서 대상 row/slip를 찾지 못함

3. `openGamePaper_failed`
- 투표지 열기 호출 실패

4. `paperArea_not_ready`
- 투표지 로딩 완료 조건 미충족

5. `screenshot_capture_failed(capture_permission_denied|capture_tab_not_active|capture_visible_tab_failed)`
- 브라우저 탭 캡처 단계 실패

6. `webhook_send_failed`
- Discord 전송 실패(권한/429/네트워크)

## 7) 보안 주의

1. Webhook URL은 채널 쓰기 권한과 동일하므로 외부 공유 금지
2. 공용 Webhook 사용 시 전용 채널 분리 및 주기적 재발급 권장
