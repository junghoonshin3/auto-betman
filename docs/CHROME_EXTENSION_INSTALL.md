# 응큼픽 딱걸렸네 설치 가이드 (Chrome Extension)

## 1. 준비물

1. Chrome 또는 Edge
2. 확장 저장소
- `https://github.com/junghoonshin3/betman-purchase-push-extension`
3. Discord Webhook URL

## 2. 배포 파일 준비

1. 확장 저장소를 clone 또는 zip 다운로드
2. 로컬 폴더에 압축 해제

## 3. 설치

1. `chrome://extensions` 접속
2. 우측 상단 `개발자 모드` ON
3. `압축해제된 확장 프로그램을 로드합니다` 클릭
4. 저장소 폴더(`betman-purchase-push-extension`) 선택
5. 확장 이름 `응큼픽 딱걸렸네` 확인

## 4. Webhook 설정

1. `응큼픽 딱걸렸네` -> `세부정보` -> `확장 프로그램 옵션`
2. `Discord Webhook URL` 입력 후 저장

주의:
- Webhook URL은 채널 쓰기 권한과 동일하므로 외부 공유 금지

## 5. 실제 사용 방법

1. Betman 로그인
2. 평소대로 구매 진행
3. `myPaymentResult.do` 감지 후 자동으로 구매내역 이동/투표지 캡처/Discord 전송
4. Discord 채널 이미지 + 고정 경고문구 도착 확인

## 6. 업데이트

1. 새 버전으로 저장소 pull 또는 재다운로드
2. `chrome://extensions`에서 확장 `새로고침`
3. Betman 탭 강력 새로고침 (`Ctrl+Shift+R` / `Cmd+Shift+R`)

## 7. 제거

1. `chrome://extensions` 진입
2. `응큼픽 딱걸렸네` 카드에서 `삭제`

## 8. 문제 해결

1. 전송이 안 올 때
- Webhook URL 확인
- 확장 Reload + 페이지 새로고침

2. 실시간 누락이 있을 때
- 콘솔에서 `[BetmanPushExt] payment_result_detected` 로그 확인
- `openGamePaper route=bridge`, `paperArea ready`, `webhook send ok` 순서 확인

3. 캡처 실패 코드
- `screenshot_capture_failed(capture_permission_denied)`
- `screenshot_capture_failed(capture_tab_not_active)`
- `screenshot_capture_failed(capture_visible_tab_failed)`
