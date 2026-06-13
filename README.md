# 해양사고 신속 보고 시스템

해양사고 발생 시 보고자가 자유 텍스트로 핵심 정보를 입력하면, 선박 제원(KOMSA)과 해상기상(기상청 API허브)을 실시간 자동 연계하여 1차 속보·최종 보고서를 자동 작성하는 시스템입니다.

## 사전 준비

| 항목 | 설명 |
|------|------|
| Python 3.9+ | 백엔드 서버 실행 |
| Node.js 18+ | React 프론트엔드 개발 서버 |
| KOMSA API 키 | [공공데이터포털](https://www.data.go.kr) → '여객선 제원 정보 서비스' 신청 |
| 기상청 API허브 키 | [apihub.kma.go.kr](https://apihub.kma.go.kr) → 해상관측(sea_obs) 서비스 신청 |
| Anthropic API 키 | 자연어 파싱용 (선택 — 없으면 규칙 기반 파싱으로 대체) |

## 빠른 시작

### 1. 환경변수 설정

`.env.example`을 복사해 `.env`를 만들고 발급받은 키를 입력합니다.

```powershell
copy .env.example .env
```

`.env` 파일 내용:

```dotenv
KOMSA_KEY=발급받은_KOMSA_인증키
KMA_KEY=발급받은_기상청_authKey
ANTHROPIC_KEY=발급받은_Anthropic_키   # 선택
```

### 2. 백엔드 실행

```powershell
pip install -r requirements.txt
python backend.py
# → http://localhost:8000 에서 실행됨
```

### 3. 프론트엔드 실행

**Vite 프로젝트로 구성 (권장)**

```powershell
npm create vite@latest sar-front -- --template react
cd sar-front
# 해양사고-신속보고-프로토타입.jsx 를 src/App.jsx 로 복사
npm install
npm run dev
# → http://localhost:5173 접속
```

브라우저에서 ⚙ 버튼을 누르면 백엔드 주소를 변경할 수 있습니다. 기본값은 `http://localhost:8000`입니다.

## 백엔드 엔드포인트

| 메서드 | 경로 | 설명 |
|--------|------|------|
| `GET` | `/vessel?name=<선박명>` | KOMSA 여객선 제원 조회 |
| `GET` | `/weather?loc=<지명>` | 기상청 해상관측 (가장 가까운 관측소 자동 매칭) |
| `POST` | `/parse` | 자연어 → 사고 정보 JSON 추출 (Claude API) |

### 응답 예시

```bash
# 선박 제원
curl "http://localhost:8000/vessel?name=섬사랑12호"
# {"총톤수":"152톤","여객정원":"92명","선종":"연안여객선","항로":"목포-도초","선사":"..."}

# 해상기상
curl "http://localhost:8000/weather?loc=추자도+북동방+2해리"
# {"지점":"추자도(파고부이)","풍향":"북동","풍속":"8.5m/s","파고":"1.2m","수온":"18.5℃","관측시각":"202406101400"}

# 자연어 파싱
curl -X POST http://localhost:8000/parse \
  -H "Content-Type: application/json" \
  -d "{\"text\":\"섬사랑12호 추자도 북동방 2해리 여객 28명 승무원 4명 부유물 감김\"}"
# {"선박명":"섬사랑12호","사고위치":"추자도 북동방 2해리","여객":"28","승무원":"4","사고개요":"..."}
```

## 파일 구조

```
D:\SAR\
├── backend.py                         # Flask 백엔드 (KOMSA·KMA·Claude API 연계)
├── 해양사고-신속보고-프로토타입.jsx     # React 프론트엔드
├── requirements.txt                   # Python 의존성
├── .env                               # API 키 (gitignore됨 — 커밋 금지)
├── .env.example                       # 키 이름 템플릿 (커밋 가능)
├── .gitignore
├── CLAUDE.md                          # 프로젝트 규칙 (Claude Code용)
└── proxy.py                           # 구 CORS 프록시 (backend.py로 대체됨)
```

## 보안

- API 키는 `backend.py` 서버에서만 보관하며 브라우저로 전달되지 않습니다.
- `.env`는 `.gitignore`에 등록되어 있습니다.
- 백엔드 CORS는 개발 편의상 전체 허용(`*`)으로 설정되어 있습니다. 운영 배포 시 `CORS(app, origins=["허용할_도메인"])`으로 제한하세요.

## 보고 흐름

```
① 챗봇 입력 (자유 텍스트)
      ↓ /parse → AI 파싱 (실패 시 규칙 기반 대체)
② /vessel → KOMSA 제원 + /weather → 기상청 관측
      ↓ 실패 시 모의 데이터로 자동 대체 (배지 표시)
③ 1차 속보 → 보고자 확인 → 운항상황센터 전파
④ 최종 보고서 → 운항관리자 검토 → 본부 정식 보고
```

목표 보고 소요시간: **5분 이내** (기존 평균 25분)
