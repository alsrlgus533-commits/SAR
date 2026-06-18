# 클라우드 배포 가이드 (Cloudtype)

카카오 챗봇 스킬 서버(`/kakao`)를 항상 켜진 HTTPS 주소로 띄우기 위한 배포 절차.

## 0. 사전 점검
- `.env`는 `.gitignore`에 등록됨 → **키는 GitHub에 올라가지 않음**. 키 값은 Cloudtype 환경변수로 따로 입력한다.
- 운영 서버는 `gunicorn`으로 구동 (로컬 Windows 개발은 `python backend.py` 그대로).

## 1. GitHub에 올리기 (최초 1회)
```powershell
cd D:\SAR
git init
git add .
git commit -m "해양사고 신속보고 백엔드 + 카카오 스킬서버"
git branch -M main
git remote add origin https://github.com/<내계정>/<레포명>.git
git push -u origin main
```
> 푸시 후 GitHub 레포에 `.env`가 **없는지** 반드시 확인.

## 2. Cloudtype 배포
1. https://cloudtype.io 로그인 → **프로젝트 생성**
2. **+ 서비스 → GitHub 저장소 연결** → 위 레포 선택
3. 프레임워크: **Python** 선택 (Python 3.12 권장)
4. **시작 명령어(Start Command)**:
   ```
   gunicorn backend:app --bind 0.0.0.0:8000 --workers 1 --threads 8 --timeout 120
   ```
   (Procfile에도 동일 명령이 있어 자동 인식될 수 있음)
   > ⚠️ Cloudtype는 `$PORT`를 자동 주입하지 않는다 → `$PORT`로 두면 `'' is not a valid port number` 오류로 크래시. **포트를 8000으로 고정**할 것.
5. **포트**: 서비스 설정의 **포트 = `8000`** (위 bind와 동일하게).
6. **환경변수(Variables)** 등록 — `.env` 내용을 그대로:
   - `KOMSA_KEY` (필수)
   - `KMA_KEY` (필수)
   - `GEMINI_KEY` 또는 `ANTHROPIC_KEY` (자연어 파싱용 — 없으면 규칙 파서로 동작)
7. **배포(Deploy)** → 완료되면 `https://<서비스>.cloudtype.app` 형태의 HTTPS 주소 발급

## 3. 카카오 오픈빌더 연결
- 스킬 URL = `https://<서비스>.cloudtype.app/kakao`
- 오픈빌더 → [스킬] 생성에 위 URL 입력 → 블록 봇 응답에 스킬 연결 → **콜백 사용 ON** → 배포
- 자세한 절차는 `CLAUDE.md`의 "카카오톡 챗봇" 절 참조

## 4. 확인
```powershell
curl.exe -X POST "https://<서비스>.cloudtype.app/kakao" -H "Content-Type: application/json" -d "{\"userRequest\":{\"utterance\":\"섬사랑12호 추자도 북동방 2해리 여객 28명 승무원 4명 폐그물 감김\"}}"
```
→ 보고서 JSON이 오면 정상. (콜백 없는 동기 폴백 응답으로 내용 확인 가능)

## 주의
- 무료/슬립 인스턴스는 콜드스타트로 카카오 5초 제한을 넘길 수 있음 → **상시 가동 인스턴스** 사용 권장.
- `/kakao`는 인증이 없으므로, 운영 시 추측 어려운 경로 사용 또는 토큰 검증 추가 권장.
- 코드 수정 후 `git push` 하면 Cloudtype가 자동 재배포(설정 시).

---

# 오라클 클라우드 평생무료 VM 배포 (24시간 안 꺼짐, 월 0원)

Cloudtype 무료의 "24시간마다 종료"가 불편할 때의 대안. Oracle **Always Free** VM은 기간 제한 없이 영구 무료(유료 전환 버튼을 누르지 않는 한 청구 없음). 가입 시 카드 등록은 본인확인용(실제 청구 X).

## 1. VM 생성
1. https://www.oracle.com/kr/cloud/free 가입 (카드 등록 = 본인확인용)
2. 콘솔 → **Compute → Instances → Create instance**
3. **Image**: Canonical **Ubuntu 22.04**
4. **Shape**: **Always Free eligible** 표시된 것만 선택
   - `VM.Standard.A1.Flex` (ARM, 1~4 OCPU / 6~24GB — 넉넉) 권장. "용량 부족" 뜨면 재시도하거나 다른 리전, 또는 `VM.Standard.E2.1.Micro`(AMD, 1GB)로
5. **SSH 키**: "Save private key" 다운로드(또는 보유한 공개키 업로드)
6. 생성 후 **Public IP** 메모

## 2. 방화벽 열기 (HTTPS용 80·443)
- **(a) VCN 보안목록**: 콘솔 → 인스턴스 → Subnet → Security List → **Add Ingress Rules**
  - Source `0.0.0.0/0`, TCP, Dest port **80** / **443** 각각 추가
- **(b) 인스턴스 내부 방화벽**(SSH 접속 후):
  ```bash
  sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
  sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
  sudo netfilter-persistent save
  ```

## 3. 접속 + 코드/의존성 설치
```bash
ssh -i 받은키.key ubuntu@<Public IP>

sudo apt update && sudo apt install -y python3-pip python3-venv git
git clone https://github.com/alsrlgus533-commits/SAR.git
cd SAR
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt gunicorn
```

## 4. .env 작성 (키는 직접 입력 — GitHub엔 없음)
```bash
nano .env
```
```
KOMSA_KEY=실제키
KMA_KEY=실제키
GEMINI_KEY=실제키        # 또는 ANTHROPIC_KEY
# ── VMS 실시간 선박위치(AIS)까지 켜려면 아래도 입력 (선택) ──
GICOMS_VMS_ID=GICOMS_userId
GICOMS_VMS_PW=GICOMS_비밀번호
```
> ⚠️ VMS 키를 안 넣으면 신고문에 **위치가 없을 때 위경도를 자동으로 못 불러온다**(`/vessel_position` 503, 1차 속보에 AIS 위치줄 누락). 위치 자동조회가 필요하면 반드시 입력.

## 4-1. VMS 실시간 위치 켜기 (선택 — 위 키를 넣었다면 필수 세트)
VMS는 GICOMS 로그인을 **헤드리스 크롬(Playwright)** 으로 하고, 한글 선박명→MMSI 변환에 **비공개 권위목록 CSV**가 필요하다. 둘 다 GitHub엔 없으므로 VM에서 따로 준비한다.

**(a) 크롬 + OS 의존성 설치** (venv 활성화 상태에서):
```bash
cd ~/SAR && source venv/bin/activate
pip install -r requirements.txt            # playwright 포함 재확인
sudo ~/SAR/venv/bin/playwright install-deps chromium   # apt 의존성(루트 필요)
playwright install chromium                 # 브라우저 본체(ubuntu 사용자 ~/.cache 에 설치)
```
> 서비스가 `User=ubuntu`로 돌므로 브라우저 본체는 **sudo 없이** ubuntu로 설치해야 systemd가 찾는다. 메모리는 최소 1GB 이상 권장(`E2.1.Micro`(1GB)는 빠듯 → `A1.Flex` 권장).

**(b) 권위목록 CSV 업로드** — 로컬 PC(PowerShell)에서:
```powershell
scp -i 받은키.key "D:\SAR - NEX-N2-MINI\선박명_MMSI.csv" ubuntu@<Public IP>:/home/ubuntu/SAR/선박명_MMSI.csv
```
> 헤더 `선박명,MMSI[,선박번호]`. 파일이 없으면 한글명↔MMSI 매핑이 비어 VMS 매칭이 거의 실패한다(VMS의 선박명은 100% 영문). 경로를 바꾸려면 `.env`에 `VESSEL_MMSI=/path/...` 지정.

**(c) 반영**:
```bash
sudo systemctl restart sar
# 확인: 위치 없는 신고문으로도 AIS 위치줄이 나오는지
curl -X POST http://127.0.0.1:8000/kakao -H "Content-Type: application/json" \
  -d '{"userRequest":{"utterance":"퀸제누비아2호 우현 타기 고장으로 표류중"}}'
```
→ 응답에 `현재위치(AIS): 34.xxxx, 126.xxxx (...)` 가 보이면 성공.

## 5. 자동실행 등록 (systemd — 부팅·크래시 시 자동 재시작)
```bash
sudo nano /etc/systemd/system/sar.service
```
```ini
[Unit]
Description=SAR kakao skill server
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/SAR
EnvironmentFile=/home/ubuntu/SAR/.env
ExecStart=/home/ubuntu/SAR/venv/bin/gunicorn backend:app --bind 127.0.0.1:8000 --workers 1 --threads 8 --timeout 120
Restart=always

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now sar
sudo systemctl status sar      # active (running) 확인
```

## 6. 공개 HTTPS 주소 (무료) — DuckDNS + Caddy
도메인이 없으면 **DuckDNS**(무료 서브도메인) + **Caddy**(Let's Encrypt 자동)로 해결.
1. https://www.duckdns.org 로그인 → 서브도메인 생성(예: `mysar`) → **current ip = VM Public IP** 저장
2. Caddy 설치 + 설정:
   ```bash
   sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
   sudo apt update && sudo apt install -y caddy
   sudo nano /etc/caddy/Caddyfile
   ```
   ```
   mysar.duckdns.org {
       reverse_proxy 127.0.0.1:8000
   }
   ```
   ```bash
   sudo systemctl restart caddy
   ```
   → `https://mysar.duckdns.org` 로 HTTPS 자동 적용(인증서 자동 발급).

## 7. 카카오 스킬 URL 교체
- 오픈빌더 스킬 URL = `https://mysar.duckdns.org/kakao` 로 변경 → 저장/배포 (콜백 사용 ON 유지)

## 8. 확인 / 업데이트
```bash
curl -X POST https://mysar.duckdns.org/kakao -H "Content-Type: application/json" \
  -d '{"userRequest":{"utterance":"퀸제누비아2호 34-45.5 126-21.0 좌현 추진기 고장 표류"}}'
```
- 코드 갱신: `cd ~/SAR && git pull && sudo systemctl restart sar`
- (선택) cron-job.org로 5~10분마다 URL 핑 → 유휴 회수 방지

## 비용 안전수칙
- VM/스토리지 만들 때 **"Always Free eligible"** 표시된 것만 선택
- 콘솔 우상단 계정이 **"Always Free"** 모드면 청구 없음 — "Upgrade to Pay As You Go"는 누르지 말 것
