# 무료 요금제 운영 가이드 (2026-02-13 기준)

이 문서는 디스코드 봇을 **24시간 상시 실행** 기준으로 무료/저비용 플랫폼을 비교하고,
권장 배포 대상을 정리합니다.

## 결론

- 1순위: **Oracle Cloud Always Free VM**
- 차선: **Railway Free/Trial** (월 크레딧 기반, 상시 운영 리스크 존재)
- 비권장(상시 디스코드 봇 기준): **Render Free**, **Koyeb Free** (scale-to-zero)

## 플랫폼 비교

| 플랫폼 | 상시 실행 적합성 | 무료 지속성 | 핵심 제약 |
|---|---|---|---|
| Oracle Cloud Always Free | 높음 | 높음 | 가입 시 카드 검증 필요 |
| Railway Free/Trial | 보통 | 낮음 | 월 크레딧 소진 시 중단 가능 |
| Render Free | 낮음 | 보통 | 유휴 시 슬립(Scale-to-zero) |
| Koyeb Free | 낮음 | 보통 | Scale-to-zero 정책 |
| Fly.io | 낮음 | 낮음 | 신규 사용자 무료 상시 운영 선택지 제한적 |

## Oracle Cloud Always Free 배포 절차

### 1) 계정 생성 및 리전 선택

1. Oracle Cloud Free Tier 계정을 생성합니다.
2. Always Free 리소스가 안정적으로 제공되는 홈 리전을 선택합니다.

### 2) Always Free VM 생성

권장 기본값:
- OS: Ubuntu 22.04 LTS
- Shape: Always Free 대상 VM (Ampere A1 또는 제공 가능한 Always Free 타입)
- 네트워크: 기본 VCN + 공인 IP

### 3) VM 초기 설정

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git
```

### 4) Docker / Docker Compose 설치

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```

다시 로그인한 후:

```bash
docker --version
docker compose version
```

### 5) 프로젝트 배포

```bash
git clone <YOUR_REPO_URL> auto_betman
cd auto_betman
cp .env.example .env
```

`.env` 설정:
- `DISCORD_BOT_TOKEN`
- `DISCORD_CHANNEL_ID`
- `DISCORD_GUILD_ID` (즉시 명령 동기화 권장)
- 필요 시 `HEADLESS=true`

실행:

```bash
docker compose up -d --build betman
docker compose logs -f betman
```

### 6) 재부팅 후 자동 복구 확인

현재 `docker-compose.yml`에 `restart: unless-stopped`가 설정되어 있으므로
Docker 데몬 시작 시 컨테이너가 자동으로 올라옵니다.

검증:

```bash
sudo reboot
# 재접속 후
docker compose ps
docker compose logs --tail=100 betman
```

### 7) 운영 점검 체크리스트

1. 디스코드에서 봇 온라인 확인
2. `/login` → `/purchases` → `/analysis` → `/logout` 순서로 동작 확인
3. `storage/` 볼륨 유지 확인 (`session_state_<discord_user_id>.json`, `betman.db`)
4. 주 1회 VM 디스크/메모리/로그 용량 확인

## 24시간 검증 시나리오

1. 24시간 동안 디스코드 연결 상태 유지 여부 확인
2. 중간에 `/purchases` 및 `/analysis` 응답 정상성 확인
3. VM 재부팅 후 봇 자동 복귀 확인
4. 사용자별 세션 파일이 섞이지 않는지 확인

## 참고 링크

- Oracle Always Free 리소스: https://docs.oracle.com/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- Oracle Free Tier 안내: https://docs.oracle.com/en/learn/cloud_free_tier/
- Oracle 결제수단/승인 안내: https://docs.oracle.com/iaas/Content/Billing/Tasks/changingpaymentmethod.htm
- Render Free 정책: https://render.com/docs/free
- Railway 가격/트라이얼: https://railway.com/pricing
- Railway Free Trial: https://docs.railway.com/pricing/free-trial
- Fly.io 가격/정책: https://fly.io/pricing
- Fly.io 과금 문서: https://fly.io/docs/about/pricing/
- Koyeb 가격: https://www.koyeb.com/pricing
- Koyeb scale-to-zero: https://www.koyeb.com/blog/scale-to-zero-optimize-gpu-and-cpu-workloads
