# 해양사고 신속 보고 시스템

해양사고 발생 시 보고자가 챗봇에 자유 텍스트로 핵심 정보를 입력하면, 선박 제원(KOMSA)과 해상기상(기상청 API허브)을 실시간으로 자동 연계하여 1차 속보 및 최종 보고서를 자동 작성·전파하는 시스템이다.

## 파일 구성

| 파일 | 역할 |
|------|------|
| `해양사고-신속보고-프로토타입.jsx` | React 프론트엔드. 챗봇 UI, 백엔드 호출, 보고서 출력 |
| `backend.py` | Flask 백엔드. API 키 보관, `/vessel` · `/route` · `/weather` · `/predep` · `/parse` · `/kakao` 엔드포인트 |
| `requirements.txt` | Python 의존성 (flask, flask-cors, python-dotenv) |
| `proxy.py` | 구 CORS 프록시 — `backend.py`로 대체됨 |

## 외부 API (모두 backend.py 서버에서 호출 — 브라우저에 키 미노출)

### KOMSA 여객선 제원 정보 (공공데이터포털)
- 엔드포인트: `https://apis.data.go.kr/B554035/psnshp-spec-v2/get-psnshp-spec-v2`
- (구버전 `B551171/passengerShipSpecInfo/...` 은 만료됨)
- 인증: `serviceKey` 쿼리 파라미터
- 서버 환경변수: `KOMSA_KEY`
- 주요 파라미터: `psnshpNm` (선박명 필터), `dataType=JSON`

### 기상청 API허브 해상관측 (sea_obs)
- 엔드포인트: `https://apihub.kma.go.kr/api/typ01/url/sea_obs.php`
- 인증: `authKey` 쿼리 파라미터
- 서버 환경변수: `KMA_KEY`
- 응답: 쉼표(,) 구분 CSV. `#`으로 시작하는 줄은 주석. 데이터 줄 컬럼: `TP, TM, STN_ID, STN_KO, LON, LAT, WH, WD, WS, WS_GST, TW, TA, PA, HM`
- 관측소 유형 코드: B=해양기상부이, C=파고부이, L=등표, N=조위관측소, F=연안방재, G=파랑계
- **부이 선택(`/weather`)**: 좌표(`lat`/`lon`) 또는 지명으로 앵커를 잡고 **부이류(B+C) 중 최단거리** 지점을 고른다(타입 우선 없이 거리 우선). 주 지점 파고가 결측이면 **가장 가까운 파고부이**의 파고로 보충(`파고출처`). 좌표·지명 모두 실패 시 임의 부이 대신 **422** 반환(엉뚱한 먼 부이 방지)
  - 프론트 `fetchWeather()`는 좌표가 없으면 `geocodeFromRefs()`로 ⚙기준점 목록에서 좌표를 추정해 전달

### 기상청 API허브 AWS 육상관측 (인근 풍향·풍속·기온 병기)
- 해안 근접 사고에서 가장 가까운 **육상 AWS**의 풍향·풍속·기온을 함께 제공. AWS가 해상부이보다 사고점에 더 가까우면 주 풍향·풍속을 AWS 값으로 우선(`풍향풍속출처`)
- 관측값: `https://apihub.kma.go.kr/api/typ01/cgi-bin/url/nph-aws2_min` (AWS 매분 — 지점번호만, 좌표·이름 없음). 컬럼: `TM STN WD1 WS1 WDS WSS WD10 WS10 TA …` (10분 평균 WD10/WS10 우선). 결측 sentinel은 -99 계열 → `-50` 미만을 결측 처리(기온 음수 보존)
- 좌표/이름: `https://apihub.kma.go.kr/api/typ01/url/stn_inf.php?inf=AWS|SFC` — **apihub '지점정보' 서비스 별도 신청 필요**(미신청 시 403). 한국 좌표 범위(경도124~132·위도33~39)로 LON/LAT 식별, 12시간 캐시
- **미신청(403)이면 AWS는 조용히 생략**되고 해상부이 응답만 반환(graceful degradation) — 기존 동작 유지
- 응답: `/weather` JSON에 `AWS` 객체(`지점`·`풍향`·`풍속`·`기온`) 추가

### 자연어 파싱 (Gemini / Claude)
- 선박명·위치·승선인원·사고개요 추출에 사용
- 백엔드 엔드포인트: `POST /parse` — 서버에서 `GEMINI_KEY` 우선, 없으면 `ANTHROPIC_KEY` 사용
- 서버 환경변수: `GEMINI_KEY`(우선), `ANTHROPIC_KEY`(대체), `GEMINI_MODEL`(선택, 기본 `gemini-2.5-flash`)
- 프론트 파싱 우선순위: **백엔드 `/parse` → 웹 직접(Gemini → Claude) → 규칙 `ruleParse()`**
  - 웹 직접 호출용 키는 프론트 ⚙설정(`geminiKey`/`anthropicKey`)에 입력 — 브라우저 `localStorage` 보관
  - Gemini 직접 호출: `generativelanguage.googleapis.com` (CORS 허용 — 프록시 불필요)

### MTIS 출항전 안전점검표 (실제 승선인원·화물)
- 해양교통안전정보체계(MTIS)의 출항전 점검표는 **공개 데이터(로그인 불필요)** — 단, 익명 세션 쿠키 + CSRF 토큰이 필요
- 백엔드 엔드포인트: `GET /predep?psnshpCd=<선박코드>[&name=&date=YYYYMMDD&time=HHMM]`
  - 공통: `mtis.komsa.or.kr/traffic/ferryInfo` GET으로 익명 세션+`<meta name="_csrf">` 토큰 확보 후 POST. 응답 `psnshpSloffBeforeSfcst`에서 승선인원/화물 파싱 (`_mtis_post()`)
  - **time 미지정(기본 동작): `selectQrForSfcstDeInfo`(payload `psnshpCd`만) → 그 선박의 '가장 최근' 점검표 자동 반환** (작성 시각 기준 최신 항차)
  - time 지정 시: `detailFerryPreDepCkForMoTraffic`(psnshpCd+psnshpNm+sloffDe+sloffTime)로 특정 항차 조회
  - `psnshpCd`는 **KOMSA `psnshp_cd`와 동일** (그래서 `/vessel`·`/vessels`가 `선박코드`를 함께 반환)
  - 주요 필드: `pasngrAdultHeadcnt`(대인)·`pasngrSmPersonHeadcnt`(소인)·`pasngrInfantHeadcnt`(유아)·`realCrewHeadcnt`(선원)·`realEmbrkPrsnCo`(실제승선)·`realFrghtLoadngWt`(화물 M/T)
  - 환경변수: `MTIS_BASE`(선택, 기본 `https://mtis.komsa.or.kr`)
  - 주의: KOMSA 화면용 내부 주소라 사이트 개편 시 깨질 수 있음 — 운영 전환 시 정식 연계 권장

## 카카오톡 챗봇 (카카오 i 오픈빌더 스킬 서버)

- 백엔드 엔드포인트: `POST /kakao` — 카카오 i 오픈빌더 **스킬 서버(웹훅)**. 사고 자유텍스트 → 1차 속보 자동 작성
- **콜백(비동기) 방식**: 카카오 5초 제한을 넘기므로 즉시 `{"version":"2.0","useCallback":true,...}`(접수 안내) 응답 후, 백그라운드 스레드가 보고서를 작성해 `userRequest.callbackUrl`로 최종 결과를 POST(`_kakao_callback`). 오픈빌더 블록에서 **콜백 사용 ON** 필요
- 오케스트레이션은 서버에서 수행: `_parse_nl`(LLM→규칙 폴백 `_rule_parse`) → `_vessel_lookup` · `_route_lookup` · `_weather_lookup`(AWS 포함) → `_build_report_text`로 simpleText 조립
  - 좌표는 `_extract_latlon`/`_parse_coord`(프론트 `extractLatLon`의 서버 포팅본)로 사고위치에서 추출
  - 기존 `/vessel`·`/route`·`/weather` 엔드포인트는 이 내부 함수들의 얇은 래퍼로 리팩터됨(웹 프론트 동작 동일)
- 콜백 미설정 시 동기 폴백(외부 API 지연 시 5초 초과 가능)
- 공개 노출 필요: 카카오는 **공개 HTTPS**로 호출 → 테스트는 `ngrok http 8000`(URL은 `https://…/kakao`), 운영은 클라우드 배포. 별도 키 불필요(기존 `.env` 사용)

## 보안 규칙 (필수)

**API 키는 코드에 직접 작성하지 않는다.**

- 모든 API 키는 `.env` 파일의 환경변수로만 관리한다.
- 코드에서는 `import.meta.env.VITE_*` (Vite) 또는 `process.env.*` (CRA/Node) 형식으로 참조한다.
- `.env` 파일은 반드시 `.gitignore`에 등록한다.
- `.env.example`에 키 이름만 적어 커밋한다 (값은 비움).

```
# .env (커밋 금지)
KOMSA_KEY=여기에_실제_키
KMA_KEY=여기에_실제_키
GEMINI_KEY=여기에_실제_키      # 선택 — 파싱 우선 사용
ANTHROPIC_KEY=여기에_실제_키   # 선택 — Gemini 미설정 시 대체
VWORLD_KEY=여기에_실제_키      # 선택 — 기점 좌표 지오코딩 유틸리티용(서버 런타임 미사용)
```

### 기점(기준점) 좌표 데이터 (정적)
- 사고위치 상대표기(`(○○ 북동쪽 N마일)`)에 쓰는 기준점 목록은 **정적 데이터**로 코드에 박혀 있다: 프론트 `해양사고-신속보고-프로토타입.jsx`의 `refPoints`, 백엔드 `backend.py`의 `_REF_POINTS` — **두 곳을 동일 값으로 동기화**한다(도-분 표기 ↔ 도-분 산술식). 서버 런타임은 외부 호출 없이 이 정적 목록만 사용
- **출처: KOMSA 연안여객선 기항지 공식 API** `port-call-info` (국가중점데이터) — `portcl_nm`(기항지명)·`lat`/`lot`(위·경도) 공식 좌표를 일괄 수신해 채움. 동해안·외해처럼 여객 기항지가 없는 구간은 주요 등대·항으로 보충(5km 내 중복 제외). 결과 마스터: `기항지_공식좌표.csv`, 목록: `기점목록.txt`
  - API: `https://mtisopenapi.komsa.or.kr/eopt/api/port-call-info?serviceKey=<키>&pageNo=1&numOfRows=2000`
  - **serviceKey 형식**: KOMSA MTIS 포털 발급 `<고정 hex키><JWT 액세스토큰>` 을 **이어붙인 값**. JWT는 **30분 만료**라 런타임 상시호출엔 부적합 → 좌표는 변하지 않으므로 **1회 수신 후 정적 반영**이 적절
  - 기항지가 추가/변경되면 위 API로 다시 받아 두 목록을 재생성(도-분 소수1자리, 좌표 십진값 그대로 변환)
  - (구) 브이월드 지오코딩 유틸 `geocode_ports.py`·`refine_island_coords.py`는 공식 API 도입 전 방식 — 참고용으로만 남김

## 백엔드 실행

```powershell
pip install -r requirements.txt
python backend.py
# http://localhost:8000 에서 실행됨
```

프론트엔드 ⚙ 설정의 '백엔드 주소'에서 포트 변경 가능 (환경변수 `PORT`로도 설정).

## 보고 흐름

```
① 챗봇 입력 (자유 텍스트)
      ↓ AI/규칙 기반 파싱
② KOMSA 제원 조회 + 기상청 해상관측 조회
      ↓ 실패 시 모의 데이터로 자동 대체 (배지로 구분 표시)
③ 1차 속보 보고서 자동 작성 → 보고자 확인 → 운항상황센터 전파
④ 최종 보고서(규정 서식) 자동 작성 → 운항관리자 검토 → 본부 정식 보고
```

목표 보고 소요시간: **5분 이내** (기존 평균 25분)

## 개발 시 주의사항

- `proxy.py`는 표준 라이브러리만 사용하므로 별도 `pip install` 불필요.
- CORS 프록시는 `ALLOWED_HOSTS`로 대상 도메인을 제한한다 — 새 API 도메인 추가 시 이 목록에 반드시 등록한다.
- 기상청 API 응답은 JSON이 아닌 공백 구분 텍스트이므로 `parseSeaObs()`로 파싱한다.
- 결측값은 `-9` 이하 숫자로 표시된다 (`num()` 함수에서 `null` 처리).
