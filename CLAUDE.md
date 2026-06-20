# 해양사고 신속 보고 시스템

해양사고 발생 시 보고자가 챗봇에 자유 텍스트로 핵심 정보를 입력하면, 선박 제원(KOMSA)과 해상기상(기상청 API허브)을 실시간으로 자동 연계하여 1차 속보 및 최종 보고서를 자동 작성·전파하는 시스템이다.

## 파일 구성

| 파일 | 역할 |
|------|------|
| `해양사고-신속보고-프로토타입.jsx` | React 프론트엔드. 챗봇 UI, 백엔드 호출, 보고서 출력 |
| `backend.py` | Flask 백엔드. API 키 보관, `/vessel` · `/route` · `/weather` · `/predep` · `/pax` · `/parse` · `/kakao` · `/report/hwpx` 엔드포인트 |
| `requirements.txt` | Python 의존성 (flask, flask-cors, python-dotenv, pyhwpxlib) |
| `선박마스터.csv` | (회사 보유·비공개) 선박별 보험·선박번호·선적항·검사기관·국적·사진파일명. `선박마스터.csv.example` 참고 |
| `vessel_photos/` | (회사 보유·비공개) 선박 사진 이미지. `선박마스터.csv`의 `사진파일명`이 가리킴 |
| `pax_agent.py` | 회사 내부망 PC용 중계 에이전트. 내부 시스템(웹 화면) 로그인→선박별 현재 승선인원 조회→백엔드 `POST /pax` 전송. 설정은 `pax_agent.env`(비공개, `pax_agent.env.example` 참고) |
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

### 회사 실시간 여객수 (중간 기항지 승하선 반영)
- MTIS 출항전 점검표는 **출항 1회 스냅샷**이라, 중간 기항지에서 여객 승하선·차량 승하차가 발생하는 선박은 실제 승선인원과 어긋나거나 일부 선박은 값이 아예 비어 있다. 실제 현재 승선인원은 **전국 각 센터가 회사 '내부 전용 시스템'(웹 화면, ID/PW 로그인)에 입력**하는데, 이 자료는 **내부망에서만** 보이고 외부망(클라우드 백엔드)에서 직접 못 본다
- **중계 에이전트 `pax_agent.py`(주 경로)**: 내부망+외부망이 모두 연결된 **회사 PC에서 실행**. ① 내부 시스템 로그인(`requests` 세션) → 선박 목록 표 → 센터별 상세에서 승선인원 파싱 → ② 결과를 외부망으로 백엔드 `POST /pax` 전송(주기 기본 3분). **내부 ID/PW는 회사 PC의 `pax_agent.env`에만**(클라우드 미전송), 백엔드로는 결과 숫자+`PAX_TOKEN`만. 화면별로 다른 파싱 2곳(`parse_vessel_list`/`parse_pax_detail`)은 실제 HTML에 맞춰 확정. 설정 예시 `pax_agent.env.example`, 회사 PC 의존성 `pip install requests python-dotenv`
- **수동 전송 페이지 `GET /pax/send`(폴백)**: 에이전트가 못 읽는 선박을 담당자가 직접 보정 입력하는 단일 HTML(선박명·선박코드·여객/대인/소인/유아·승무원·차량·메모 → [전송]). 토큰은 브라우저 `localStorage` 저장, 30초마다 현재 저장값 표시
- **수신/조회**: `POST /pax`(JSON 또는 폼; 키 `name`/`선박코드`·`여객`·`대인`·`소인`·`유아`·`승무원`·`차량`·`메모`·`token`) · `GET /pax`(저장 목록+신선도). `PAX_TOKEN` 설정 시 `X-Pax-Token` 헤더 또는 body `token` 필요(`secrets.compare_digest`)
- **저장**: 메모리 `_PAX` + JSON 파일 영속화(`PAX_STORE`, 기본 `pax_store.json`, 원자적 교체). 선박코드 정확일치 또는 정규화 선박명(공백·끝'호' 제거·대문자) 일치로 조회(`_pax_lookup`)
- **신선도**: `PAX_TTL`(초, 기본 18시간) 이내 값만 우선 적용, 지나면 자동으로 MTIS 폴백 — 지난 항차의 낡은 값 오적용 방지
- **보고서 반영**: `_build_report_text`(1차 속보, '회사 실시간 현황' 표기)·`_build_report_data`(hwpx) 두 곳 모두 **회사 실시간(신선) > MTIS 출항전 > 보고자 입력/LLM** 순으로 여객·승무원·차량을 병합
- 환경변수(모두 선택): `PAX_TOKEN`·`PAX_TTL`·`PAX_STORE`. `pax_store.json`은 회사 실데이터라 `.gitignore` 등록

### GICOMS VMS 실시간 선박위치 (allShipTarget)
- 해양안전종합정보시스템(GICOMS)의 VMS 화면이 쓰는 내부 API `WEB_VMS/WebVMS/allShipTarget.json` 이 **전국 실시간 AIS(~7천척)** 를 한 번에 반환 — 각 선박에 `mmsi·shipName·latitude·longitude·sog·cog·heading·rcvDatetimeFormat·shipType` 등. (구버전 PUBDATA `pubdatareq`는 6개월 이상 과거 전용이라 부적합)
- 백엔드 엔드포인트: `GET /vessel_position?name=<선박명>` 또는 `?mmsi=<MMSI>`
- **로그인 필요**: GICOMS 로그인은 RSA 암호화(`jsbn`)+TouchEn nxKey/transkey라 단순 POST 불가 → **Playwright(헤드리스 크롬)로 로그인** 후 `JSESSIONID` 쿠키만 추출(`_vms_login`), 가벼운 `allShipTarget.json` 호출에 재사용(`_vms_all_targets`). 쿠키 25분 캐시·만료 시 자동 재로그인, 결과 20초 캐시
  - 로그인 구현: 홈에서 `loginForm.id/password` 값을 JS로 세팅 후 `actionLogin('ID')` 호출(가시성 우회). **반드시 `http://www.gicoms.go.kr`** (https는 404)
- **매칭(`_vms_position`)**: ① **권위목록(`선박명_MMSI.csv`)으로 한글명→MMSI 해석**(`_mmsi_map`) → ② MMSI 정확일치 → ③ 목록에 없으면 선박명 정규화('호'·공백 제거, 대문자) 정확일치 → ④ '여객' 선종에 한해서만 부분일치(화물선 오매칭 차단). AIS 원시단위 보정: `cog`/`sog`는 ×10(>360/>102.2면 ÷10), `heading=511`은 미지정(None)
  - **VMS `shipName`은 여객선도 100% 영문/로마자**(`SEASTAR 1`,`ARION JEJU`)라 한국어 사고신고명과 직접 매칭 불가 → **회사 권위 목록 `선박명_MMSI.csv`(헤더 `선박명,MMSI[,선박번호]`, 비공개·`.gitignore`)** 로 한글명→MMSI를 확정해 **MMSI 정확조회**(이게 100% 정확). env `VESSEL_MMSI`로 경로 지정 가능, 5분 캐시. 목록 출처: 운항관리 관리대장(`선명,mmsi 목록.xlsx`의 '26년 현재' 시트)에서 추출
- 환경변수: `GICOMS_VMS_ID`·`GICOMS_VMS_PW`(= GICOMS userId와 동일), `GICOMS_BASE`(선택, 기본 `http://www.gicoms.go.kr`), `VMS_WARM`(선택, 기본 ON; `0`이면 세션 워머 비활성)
- **세션 워머(`_vms_warm_loop`/`_start_vms_warmer`)**: 모듈 로드 시(gunicorn 워커별) 데몬 스레드가 JSESSIONID 쿠키를 만료 2분 전(~23분 주기)에 선제 갱신 → 보고서 경로에서 **콜드 Chromium 로그인(수초~십수초) 대기 제거**. 좌표 없이 '선명만' 입력해도 VMS 현위치 조회가 즉시 동작(보고서 ~14초→~2-3초). **로그인 쿠키만 갱신하고 `allShipTarget`은 호출 안 함** → '사고 시에만 AIS 조회' 원칙 유지
- 의존성: `playwright` + `python -m playwright install chromium`. 미설치/키 없음 시 `/vessel_position`만 503(다른 기능 영향 없음 — graceful degradation)
- 주의: 내부 화면용 주소라 사이트 개편 시 깨질 수 있음 — AIS 선박위치 폴링 금지(사고 시에만 조회; 워머는 쿠키만 갱신), 운영 전환 시 정식 연계 권장

### 외부 API 자동 건강검진 (daily health check)
- 외부 시스템(KOMSA·기상청·MTIS·GICOMS)이 사이트 개편 등으로 **조용히 깨지면 사고 당일에야 발견**되는 문제를 막기 위해, 백그라운드 데몬이 **하루 1회** 각 외부 API에 가벼운 스모크 호출을 보내 생존을 확인한다(`_health_loop`/`_start_health_checker`, `_vms_warm_loop` 패턴 차용)
- 검진 항목(`_health_checks`, **병렬 실행**): KOMSA 제원(`_vessel_lookup`)·항로(`_route_lookup`)·기상청 해상관측(`_weather_lookup`, 좌표 직접지정으로 지오코딩 우회·KMA만 검사)·MTIS 출항전점검(`_mtis_post`로 익명 세션+CSRF 확보만)·GICOMS VMS(`_vms_cookie`로 **로그인 쿠키 확보만 확인**, `allShipTarget`(AIS) 미호출 → '사고 시에만 AIS 조회' 원칙 유지). 각 항목 `ok=True 정상 / False 장애 / None 키미설정 건너뜀`, 예외는 잡아서 장애 처리
- **알림(`_health_notify`)**: 매 검진 결과를 `[health]` 로그(systemd 저널)에 남기고, **실패 시 + 실패→정상 복구 시에만** 웹훅(`HEALTH_WEBHOOK_URL`, Slack/Discord 호환 `{"text":...}`)으로 통지(매일 정상 노이즈 방지). 웹훅 미설정이면 로그만
- 엔드포인트: **`GET /health/external`**(JSON, `?run=1`이면 즉시 1회 실행) — 수동 점검·외부 업타임 모니터용. 실패 있으면 HTTP 503
- gunicorn 다중 워커에선 **localhost 락 포트(`HEALTH_LOCK_PORT`, 기본 8401) 바인딩으로 1개 워커만 검진**(중복 알림 방지). 환경변수(모두 선택): `HEALTH_CHECK`(기본 1, `0`이면 비활성)·`HEALTH_INTERVAL`(기본 86400, 최소 300)·`HEALTH_FIRST_DELAY`(기본 60)·`HEALTH_WEBHOOK_URL`·`HEALTH_LOCK_PORT`
- 한계: 키 만료로 외부 API가 예외 대신 '빈 결과'를 반환하는 경우는 정상으로 보일 수 있음(주로 엔드포인트 이전/404/DNS/TLS 등 치명적 깨짐 탐지에 초점)

## 카카오톡 챗봇 (카카오 i 오픈빌더 스킬 서버)

- 백엔드 엔드포인트: `POST /kakao` — 카카오 i 오픈빌더 **스킬 서버(웹훅)**. 사고 자유텍스트 → 1차 속보 자동 작성
- **콜백(비동기) 방식**: 카카오 5초 제한을 넘기므로 즉시 `{"version":"2.0","useCallback":true,...}`(접수 안내) 응답 후, 백그라운드 스레드가 보고서를 작성해 `userRequest.callbackUrl`로 최종 결과를 POST(`_kakao_callback`). 오픈빌더 블록에서 **콜백 사용 ON** 필요
- 오케스트레이션은 서버에서 수행: `_parse_nl`(LLM→규칙 폴백 `_rule_parse`) → `_vessel_lookup` · `_route_lookup` · `_weather_lookup`(AWS 포함) → `_build_report_text`로 simpleText 조립
  - 좌표는 `_extract_latlon`/`_parse_coord`(프론트 `extractLatLon`의 서버 포팅본)로 사고위치에서 추출
  - 기존 `/vessel`·`/route`·`/weather` 엔드포인트는 이 내부 함수들의 얇은 래퍼로 리팩터됨(웹 프론트 동작 동일)
- 콜백 미설정 시 동기 폴백(외부 API 지연 시 5초 초과 가능)
- 공개 노출 필요: 카카오는 **공개 HTTPS**로 호출 → 테스트는 `ngrok http 8000`(URL은 `https://…/kakao`), 운영은 클라우드 배포. 별도 키 불필요(기존 `.env` 사용)

## 정식 해양사고 보고서(hwpx) 자동 작성 (2단계)

챗봇이 모은 데이터를 **운항관리센터 정식 서식(`해양사고 공폼.pdf`)** 에 맞춘 **hwpx 보고서**로 변환·다운로드한다.

- 백엔드 엔드포인트: `POST /report/hwpx` — body `{ utterance, center, extra:{경위,피해,조치} }`. 응답은 hwpx 바이트(`Content-Disposition: attachment`). 프론트 ③단계 **`📄 정식 보고서(hwpx) 다운로드`** 버튼이 호출
- **hwpx 생성 = `pyhwpxlib`(HwpxBuilder)로 직접 작성** — 한글 오피스/템플릿 파일 불필요. `_compose_report_hwpx()`가 결재 박스(상단 우측)·제목·□사고개요·□선박제원(표, 1열 '선박사진' 칸·라벨 음영·보험현황 병합)·□피해사항·□조치사항·□조치계획·□사진(현장사진, 없으면 운항관리자가 삭제)·날짜를 공폼 순서대로 조립. 저장 후 `_postprocess_report_hwpx()`가 결재 박스 우측정렬 + 선박사진을 선박제원 표 셀로 이동(XML 후처리, pyhwpxlib 미지원 보정). 산출은 유효 hwpx(zip, `mimetype=application/hwp+zip`)
- **데이터 우선순위: 회사 선박마스터 > 회사 실시간 여객수(신선·`/pax`) > KOMSA/MTIS > LLM 추정 > 공폼 자리표시자(`00`/`확인 중`/`없음`)** (여객·승무원·차량은 신선한 `/pax` 값이 MTIS보다 우선) — `_build_report_data()`가 `_parse_nl`·`_vessel_lookup`·`_route_lookup`·`_predep_lookup`·`_weather_lookup`(기존 재사용) + `_vessel_master` + `_infer_report_fields`를 병합. **외부 조회는 `ThreadPoolExecutor`로 병렬 실행**(파싱 후 vessel/route/infer/VMS 동시 → predep·master는 선박코드 의존, weather는 최종 좌표 의존)해 응답시간을 순차 합산→최댓값으로 단축. 신고문에 좌표가 있으면 VMS(Chromium) 호출은 생략
  - `_vessel_master(name, code)`: `선박마스터.csv`(UTF-8, 헤더 `선박명,선박코드,보험현황,선박번호,선적항,검사기관,국적,사진파일명`)에서 보험·선박번호·선적항·검사기관·국적·사진을 조회(5분 캐시, 없으면 graceful 빈 dict). 키는 선박코드 우선, 다음 선박명(부분일치 폴백)
  - 사진: ① 회사 `vessel_photos/<사진파일명>`(jpg/png) 우선 → ② 없으면 **KOMSA 공개 여객선 사진** 폴백(`_komsa_vessel_photo`). 표 위에 삽입, 둘 다 없으면 생략
    - KOMSA 공개사진: `www.komsa.or.kr` '여객선 정보' 목록(`prog/psnShip/kor/sub03_0204/list.do`)을 `searchKeyword=선명`으로 조회 → 목록 썸네일 `src(/thumbnail/psnShip/300_PS_*)`에서 `300_` 접두어를 떼면 원본 고해상도 이미지. 선명 정규화(공백·끝'호' 제거) 매칭, 임시파일로 받아 삽입, 1시간 캐시. 키 불필요(공개 페이지)
  - `_parse_and_infer()`: **파싱(선박명·사고위치·여객·승무원·사고개요)과 공폼 추정(사고종류 18종·추정원인·인명/오염/선박 피해·지연시간·조치사항·조치계획)을 1회 LLM 호출로 동시 수행**(보고서당 LLM 왕복 2→1, 429 완화). 실패/키없음 시 `_rule_parse`+`_infer_fallback` 규칙 폴백(추가 LLM 호출 없음). 추정 병합은 `_merge_infer`. (별도 `_infer_report_fields()`는 추정만 하는 단독 헬퍼로 유지 — 파싱이 이미 끝난 컨텍스트용)
  - 환경변수(선택): `VESSEL_MASTER`(CSV 경로), `VESSEL_PHOTOS`(사진 폴더 경로)
- 회사 데이터는 **비공개**: `선박마스터.csv`·`vessel_photos/`는 `.gitignore` 등록. 커밋용 예시는 `선박마스터.csv.example`

### 카카오톡에서 hwpx 받기 (다운로드 링크 방식)
- 카카오 스킬 서버는 **파일 첨부를 보낼 수 없으므로**, 생성한 hwpx를 토큰으로 임시 보관(`_REPORT_FILES`, 1시간 TTL·메모리)하고 **공개 다운로드 URL을 textCard 버튼으로 전달**한다
- 흐름: 1차 보고서 하단 **`📄 정식 보고서(hwpx)`** 퀵리플라이 → `/kakao`가 세션의 원문(`utterance`)으로 `_kakao_hwpx_message()` 실행(콜백 비동기) → `_store_report_file()` 토큰 발급 → `GET /report/download/<token>`(`Content-Disposition: attachment`) 링크 카드 응답
  - 세션에 원문 보관 필요: `_kakao_callback`/동기 폴백이 `_session_set(uid, utterance=...)` 저장
  - 카카오는 1차 보고서의 **원문**으로 hwpx를 생성한다(카카오에서 개요/조치사항을 수정한 내용은 hwpx에 미반영 — hwpx는 새 정식 초안이며 한글에서 보완)
- 공개 링크 베이스: env **`PUBLIC_BASE_URL`**(예: `https://sarchatbot.duckdns.org`) 우선, 없으면 요청 헤더(`X-Forwarded-Proto/Host`)로 추정. 카카오 webLink는 **https 필수**라 운영 서버는 `PUBLIC_BASE_URL`을 https로 설정 권장

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
  - **동기화 검증**: `check_refpoints_sync.py`가 두 목록을 십진좌표로 파싱해 이름·좌표 일치를 대조(`python check_refpoints_sync.py` 수동 실행, 불일치 시 exit 1). `.claude/settings.json`의 **PostToolUse 훅**이 두 파일 편집 시 자동 실행해 불일치를 즉시 경고(허용오차 0.001°≈한 자리 미만)
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
