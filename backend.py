# -*- coding: utf-8 -*-
"""
해양사고 신속 보고 시스템 — 백엔드 서버

엔드포인트:
  GET  /vessel?name=<선박명>     KOMSA 여객선 제원 조회
  GET  /weather?loc=<지명>       기상청 API허브 해상관측 조회
  POST /parse                    자연어 → 사고 정보 추출 (Claude API)

실행:
  pip install -r requirements.txt
  python backend.py
"""
import http.cookiejar
import json
import os
import re
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS

load_dotenv()

KOMSA_KEY = os.environ.get("KOMSA_KEY", "")
KOMSA_URL = os.environ.get(
    "KOMSA_URL",
    "https://apis.data.go.kr/B554035/psnshp-spec-v2/get-psnshp-spec-v2",
)
KOMSA_ROUTE_URL = os.environ.get(
    "KOMSA_ROUTE_URL",
    "https://apis.data.go.kr/B554035/ferry-route-info-v4/get-ferry-route-info-v4",
)
KMA_KEY = os.environ.get("KMA_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
PORT = int(os.environ.get("PORT", 8000))

app = Flask(__name__)
CORS(app)


@app.get("/")
def index():
    """헬스체크 / 안내 — 루트 접속 시 404 대신 상태 표시."""
    return jsonify({
        "service": "해양사고 신속보고 백엔드",
        "status": "ok",
        "endpoints": ["/vessel", "/route", "/weather", "/predep", "/parse", "/kakao"],
        "카카오_스킬_URL": "/kakao (POST)",
    })


# ── 내부 유틸 ──────────────────────────────────────────

def http_get(url: str, as_json: bool = False, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": "rapid-report-backend/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=")[-1].split(";")[0].strip()
    text = raw.decode(charset, errors="replace")
    return json.loads(text) if as_json else text


# ── /vessel ──────────────────────────────────────────

def _find_items(data) -> list:
    """API 응답 구조에 무관하게 item 딕셔너리 목록을 재귀 탐색."""
    items: list = []

    def walk(node):
        if isinstance(node, list):
            if node and isinstance(node[0], dict):
                items.extend(node)
            for child in node:
                walk(child)
        elif isinstance(node, dict):
            for child in node.values():
                walk(child)

    walk(data)
    return items


def _pick_field(item: dict, pattern: str) -> str:
    for k, v in item.items():
        if re.search(pattern, k, re.IGNORECASE) and str(v).strip():
            return str(v).strip()
    return ""


def _fmt_num(s: str) -> str:
    """'180.000' → '180' 처럼 불필요한 소수점/0 제거 (정수형 수치만)."""
    return s.rstrip("0").rstrip(".") if re.match(r"^\d+\.\d+$", s) else s


def _komsa_fetch_items(base_url: str, name: str, extra: str = "") -> list:
    """KOMSA psnshpNm 조회. prefix 매칭이라 0건이면 끝의 '호'를 떼고 1회 재시도.
    (보고자는 '한일골드스텔라호'로 입력하지만 KOMSA 등록명은 '한일골드스텔라'인 경우 대응)"""
    def fetch(nm: str) -> list:
        url = (
            f"{base_url}"
            f"?serviceKey={urllib.parse.quote(KOMSA_KEY, safe='')}"
            f"&pageNo=1&numOfRows=10&dataType=JSON{extra}"
            f"&psnshpNm={urllib.parse.quote(nm)}"
        )
        return _find_items(http_get(url, as_json=True))

    items = fetch(name)
    if not items and name.endswith("호"):
        items = fetch(name[:-1])
    return items


def _vessel_lookup(name: str):
    """KOMSA 여객선 제원 조회. dict 반환, 결과 없으면 None, 네트워크 오류 시 예외 전파."""
    items = _komsa_fetch_items(KOMSA_URL, name)
    norm = lambda s: str(s).replace(" ", "")
    cand = norm(name[:-1] if name.endswith("호") else name)
    item = next(
        (
            it for it in items
            if any(isinstance(v, str) and (norm(name) in norm(v) or cand in norm(v)) for v in it.values())
        ),
        items[0] if items else None,
    )
    if item is None:
        return None

    def field(pattern):
        return _pick_field(item, pattern)

    # psnshp-spec-v2 실제 필드: gt(총톤수), pasngr_pscp_cnt(여객정원),
    #   kdship_nm(선종명), shpcpn_nm(선사명). _cd/_telno 등 코드·부가필드는 제외.
    ton = _fmt_num(field(r"\bgt\b|tonnage|ton|톤수"))
    pax = _fmt_num(field(r"pasngr|pscp|psgr|passenger|정원"))
    return {
        "총톤수": f"{ton}톤" if ton else "",
        "여객정원": f"{pax}명" if pax else "",
        "선종": field(r"kdship_nm|선종명|선종|kind|type"),
        "항로": field(r"licns.*rout|rout|rute|항로"),
        "선사": field(r"shpcpn_nm|선사|entrps|cmpny|corp"),
        "선박코드": field(r"psnshp_cd|psnshpcd|선박코드"),
    }


@app.get("/vessel")
def vessel():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name 파라미터가 필요합니다"}), 400
    if not KOMSA_KEY:
        return jsonify({"error": "KOMSA_KEY가 설정되지 않았습니다"}), 503
    try:
        v = _vessel_lookup(name)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    if v is None:
        return jsonify({"error": "조회 결과 없음"}), 404
    return jsonify(v)


# ── /vessels (자동완성용 전체 목록) ──────────────────────

_VESSELS_CACHE = {"at": None, "items": []}


@app.get("/vessels")
def vessels():
    """KOMSA 전체 여객선 목록(약 700척) — 프론트 선명 자동완성용. 1시간 메모리 캐시."""
    if not KOMSA_KEY:
        return jsonify({"error": "KOMSA_KEY가 설정되지 않았습니다"}), 503

    now = datetime.now()
    if _VESSELS_CACHE["items"] and _VESSELS_CACHE["at"] \
            and (now - _VESSELS_CACHE["at"]).total_seconds() < 3600:
        return jsonify({"items": _VESSELS_CACHE["items"]})

    url = (
        f"{KOMSA_URL}"
        f"?serviceKey={urllib.parse.quote(KOMSA_KEY, safe='')}"
        f"&pageNo=1&numOfRows=2000&dataType=JSON"
    )
    try:
        raw = _find_items(http_get(url, as_json=True))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    seen, out = set(), []
    for it in raw:
        nm = _pick_field(it, r"psnshp_nm|선박명|여객선명")
        if not nm or nm in seen:
            continue
        seen.add(nm)
        ton = _fmt_num(_pick_field(it, r"\bgt\b|tonnage|ton|톤수"))
        pax = _fmt_num(_pick_field(it, r"pasngr|pscp|psgr|passenger|정원"))
        out.append(
            {
                "선박명": nm,
                "총톤수": f"{ton}톤" if ton else "",
                "여객정원": f"{pax}명" if pax else "",
                "선종": _pick_field(it, r"kdship_nm|선종명|선종|kind|type"),
                "선사": _pick_field(it, r"shpcpn_nm|선사|entrps|cmpny|corp"),
                "선박코드": _pick_field(it, r"psnshp_cd|psnshpcd|선박코드"),
            }
        )
    out.sort(key=lambda v: v["선박명"])
    _VESSELS_CACHE["items"] = out
    _VESSELS_CACHE["at"] = now
    return jsonify({"items": out})


# ── /route ──────────────────────────────────────────

def _route_lookup(name: str):
    """KOMSA 운항항로 조회. dict 반환, 정보 없으면 None, 네트워크 오류 시 예외 전파."""
    kst = timezone(timedelta(hours=9))
    today = datetime.now(kst).strftime("%Y%m%d")
    items = _komsa_fetch_items(KOMSA_ROUTE_URL, name, extra=f"&rlvtYmd={today}")
    if not items:
        return None
    item = items[0]

    def field(pattern):
        return _pick_field(item, pattern)

    return {
        "면허항로": field(r"licns.*rout|면허.*항로"),
        "운항항로": field(r"oper.*rout|운항.*항로"),
        "운항상태": field(r"oper.*stat|운항.*상태"),
        "출발시각": field(r"sail_tm|comm_tm|depart|출발.*시각|sailTm|commTm"),
    }


@app.get("/route")
def route():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name 파라미터가 필요합니다"}), 400
    if not KOMSA_KEY:
        return jsonify({"error": "KOMSA_KEY가 설정되지 않았습니다"}), 503
    try:
        r = _route_lookup(name)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    if r is None:
        return jsonify({"error": "운항 정보 없음"}), 404
    return jsonify(r)


# ── /weather ──────────────────────────────────────────

_DIRS = [
    "북", "북북동", "북동", "동북동", "동", "동남동", "남동", "남남동",
    "남", "남남서", "남서", "서남서", "서", "서북서", "북서", "북북서",
]
_TP_LABELS = {
    "B": "해양기상부이", "C": "파고부이", "L": "등표",
    "N": "조위관측소",  "F": "연안방재",  "G": "파랑계",
}


def _tm_string(offset_hours: int = 0) -> str:
    kst = timezone(timedelta(hours=9))
    d = datetime.now(kst) + timedelta(hours=offset_hours)
    return d.strftime("%Y%m%d%H00")


def _parse_sea_obs(text: str) -> list:
    def num(s):
        try:
            v = float(s)
            return None if v <= -9 else v
        except (ValueError, TypeError):
            return None

    # sea_obs 응답은 쉼표(,) 구분 CSV:
    # TP, TM, STN_ID, STN_KO, LON, LAT, WH, WD, WS, WS_GST, TW, TA, PA, HM
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        cols = [c.strip() for c in stripped.split(",")]
        if len(cols) < 11:
            continue
        rows.append(
            {
                "tp": cols[0], "name": cols[3], "tm": cols[1],
                "lon": num(cols[4]), "lat": num(cols[5]),
                "wh": num(cols[6]), "wd": num(cols[7]),
                "ws": num(cols[8]), "tw": num(cols[10]),
            }
        )
    return rows


def _haversine_nm(lat1, lon1, lat2, lon2):
    """두 좌표 간 거리(해리)."""
    from math import radians, sin, cos, asin, sqrt
    dla = radians(lat2 - lat1)
    dlo = radians(lon2 - lon1)
    a = sin(dla / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlo / 2) ** 2
    return 2 * 3440.065 * asin(sqrt(a))


# ── AWS(육상 자동기상관측) 연계 ───────────────────────────
# 해상부이가 멀고 사고가 해안 근접일 때 '가장 가까운 AWS'의 풍향·풍속·기온을 함께 제공한다.
#   · 관측값: nph-aws2_min (지점번호만 있고 좌표·이름 없음)
#   · 좌표/이름: stn_inf.php (apihub '지점정보' 서비스 신청 필요 — 미신청 시 403)
# 지점정보 미신청이면 조용히 AWS를 생략한다(기존 동작 유지).
_aws_stn_cache = {"at": None, "stations": {}}  # {stn_id: {"lat","lon","name"}}


def _parse_stn_inf(text: str) -> dict:
    """stn_inf.php 응답 → {지점번호: {lat, lon, name}}.
    컬럼 위치 변동에 견디도록 한국 좌표 범위(경도 124~132, 위도 33~39)로 lon/lat를 식별한다."""
    out = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        cols = s.split()
        if len(cols) < 3 or not cols[0].lstrip("-").isdigit():
            continue
        stn = cols[0]
        lon = lat = None
        for c in cols[1:]:
            try:
                v = float(c)
            except ValueError:
                continue
            if lon is None and 124.0 <= v <= 132.5:
                lon = v
            elif lat is None and 33.0 <= v <= 39.5:
                lat = v
        if lon is None or lat is None:
            continue
        name = next((c for c in cols if re.search(r"[가-힣]", c)), stn)
        out[stn] = {"lat": lat, "lon": lon, "name": name}
    return out


def _load_aws_stations() -> dict:
    """AWS/지상 지점 좌표표 (하루 1회 캐시). 미신청(403) 등 실패 시 빈 dict."""
    now = datetime.now(timezone.utc)
    if _aws_stn_cache["at"] and (now - _aws_stn_cache["at"]) < timedelta(hours=12):
        return _aws_stn_cache["stations"]
    stations = {}
    for inf in ("AWS", "SFC"):
        try:
            url = (
                f"https://apihub.kma.go.kr/api/typ01/url/stn_inf.php"
                f"?inf={inf}&stn=&authKey={urllib.parse.quote(KMA_KEY, safe='')}"
            )
            stations.update(_parse_stn_inf(http_get(url)))
        except Exception:
            continue
    if stations:  # 성공했을 때만 캐시 갱신 (일시 실패 시 다음 요청에서 재시도)
        _aws_stn_cache["at"] = now
        _aws_stn_cache["stations"] = stations
    return stations


def _fetch_aws_obs() -> list:
    """nph-aws2_min(AWS 매분) → [{stn, wd, ws, ta}]. 10분 평균 풍향·풍속 우선."""
    def num(s):  # AWS 결측 sentinel은 -99 계열 (기온은 음수 가능하므로 -50 미만만 결측 처리)
        try:
            v = float(s)
            return None if v <= -50 else v
        except (ValueError, TypeError):
            return None

    kst = timezone(timedelta(hours=9))
    rows = []
    for back in (5, 15):
        tm = (datetime.now(kst) - timedelta(minutes=back)).strftime("%Y%m%d%H%M")
        url = (
            f"https://apihub.kma.go.kr/api/typ01/cgi-bin/url/nph-aws2_min"
            f"?tm={tm}&stn=0&authKey={urllib.parse.quote(KMA_KEY, safe='')}"
        )
        text = http_get(url)
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            c = s.split()
            if len(c) < 9 or not c[1].lstrip("-").isdigit():
                continue
            # YYMMDDHHMI STN WD1 WS1 WDS WSS WD10 WS10 TA ...
            wd = num(c[6]) if num(c[6]) is not None else num(c[2])
            ws = num(c[7]) if num(c[7]) is not None else num(c[3])
            rows.append({"stn": c[1], "wd": wd, "ws": ws, "ta": num(c[8])})
        if rows:
            break
    return rows


def _nearest_aws(plat, plon):
    """사고 좌표에서 가장 가까운 AWS 관측. 미신청/실패 시 None."""
    if plat is None or plon is None:
        return None
    try:
        stations = _load_aws_stations()
        if not stations:
            return None
        obs = _fetch_aws_obs()
    except Exception:
        return None
    best = None
    for o in obs:
        st = stations.get(o["stn"])
        if not st:
            continue
        d = _haversine_nm(plat, plon, st["lat"], st["lon"])
        if best is None or d < best["dist"]:
            best = {"name": st["name"], "dist": d, "wd": o["wd"], "ws": o["ws"], "ta": o["ta"]}
    return best


def _weather_lookup(loc: str, lat: str = "", lon: str = "") -> dict:
    """해상부이 + 인근 AWS 기상 조회. 성공 시 결과 dict, 실패 시 {'error', '_status'}."""
    loc = (loc or "").strip()
    lat = (lat or "").strip()
    lon = (lon or "").strip()
    if not KMA_KEY:
        return {"error": "KMA_KEY가 설정되지 않았습니다", "_status": 503}

    def fetch_obs(tm: str) -> str:
        url = (
            f"https://apihub.kma.go.kr/api/typ01/url/sea_obs.php"
            f"?tm={tm}&stn=0&authKey={urllib.parse.quote(KMA_KEY, safe='')}"
        )
        return http_get(url)

    try:
        text = fetch_obs(_tm_string(0))
        rows = _parse_sea_obs(text)
        if not rows:
            text = fetch_obs(_tm_string(-1))
            rows = _parse_sea_obs(text)
        if not rows:
            return {"error": "관측자료 없음", "_status": 502}
    except Exception as exc:
        return {"error": str(exc), "_status": 502}

    coord_rows = [r for r in rows if r["lat"] is not None and r["lon"] is not None]

    # ── 1) 좌표 앵커 확보: 쿼리 좌표 → (없으면) 지명 매칭 관측소의 좌표 ──
    try:
        plat, plon = float(lat), float(lon)
    except ValueError:
        plat = plon = None

    name_row = None  # 지명 텍스트로 직접 매칭된 관측소
    if plat is None or plon is None:
        tokens = [t.rstrip("북동방남서방인근부근해상") for t in re.findall(r"[가-힣]{2,}", loc)]
        tokens = [t for t in tokens if len(t) >= 2]
        for tok in tokens:
            name_row = next((r for r in rows if tok[:2] in r["name"]), None)
            if name_row:
                break
        if name_row and name_row["lat"] is not None and name_row["lon"] is not None:
            plat, plon = name_row["lat"], name_row["lon"]

    # ── 2) 주 지점 = 부이류(해양기상부이 B + 파고부이 C) 중 최단거리 (타입 우선 없이 거리 우선) ──
    row = None
    dist_nm = None
    if plat is not None and plon is not None:
        buoys = [r for r in coord_rows if r["tp"] in ("B", "C")]
        if buoys:
            row = min(buoys, key=lambda r: _haversine_nm(plat, plon, r["lat"], r["lon"]))
            dist_nm = _haversine_nm(plat, plon, row["lat"], row["lon"])

    # 좌표 앵커가 없을 때만 지명 매칭 관측소를 그대로 사용 (거리 미상)
    if row is None and name_row is not None:
        row = name_row

    # 좌표·지명 모두 실패 → 임의의 먼 부이를 반환하지 않고 명확히 알린다
    if row is None:
        return {"error": "사고위치로 인근 관측소를 특정하지 못했습니다 (좌표 또는 지명 필요)", "_status": 422}

    # ── 3) 파고 보충: 주 지점 파고가 결측이면 가장 가까운 파고부이(C, 없으면 B)의 파고로 채운다 ──
    wh = row["wh"]
    wh_src = None
    if wh is None and plat is not None and plon is not None:
        for pref in (("C",), ("B",)):
            pool = [
                r for r in coord_rows
                if r["tp"] in pref and r["wh"] is not None and r is not row
            ]
            if pool:
                wsrc = min(pool, key=lambda r: _haversine_nm(plat, plon, r["lat"], r["lon"]))
                wh = wsrc["wh"]
                wh_src = f"{wsrc['name']} {_TP_LABELS.get(wsrc['tp'], wsrc['tp'])}"
                break

    wd_txt = "결측" if row["wd"] is None else _DIRS[round(row["wd"] / 22.5) % 16]
    ws_txt = "결측" if row["ws"] is None else f"{row['ws']}m/s"

    label = f"{row['name']}({_TP_LABELS.get(row['tp'], row['tp'])}"
    label += f", 약 {round(dist_nm)}해리)" if dist_nm is not None else ")"
    resp = {
        "지점": label,
        "풍향": wd_txt,
        "풍속": ws_txt,
        "파고": "결측" if wh is None else f"{wh}m",
        "수온": "결측" if row["tw"] is None else f"{row['tw']}℃",
        "관측시각": row["tm"] if row["tm"] is not None else "결측",
        "풍향풍속출처": row["name"],
    }
    if wh_src is not None:
        resp["파고출처"] = wh_src

    # ── 4) 인근 AWS(육상) 병기 + 거리상 AWS가 더 가까우면 풍향·풍속을 AWS 값으로 우선 ──
    aws = _nearest_aws(plat, plon)
    if aws is not None:
        resp["AWS"] = {
            "지점": f"{aws['name']}(AWS, 약 {round(aws['dist'])}해리)",
            "풍향": "결측" if aws["wd"] is None else _DIRS[round(aws["wd"] / 22.5) % 16],
            "풍속": "결측" if aws["ws"] is None else f"{aws['ws']}m/s",
            "기온": "결측" if aws["ta"] is None else f"{aws['ta']}℃",
        }
        # 사고점에 AWS가 더 가깝고 AWS 풍향·풍속이 유효하면 주 풍향·풍속을 AWS로 대체
        if (dist_nm is not None and aws["dist"] < dist_nm
                and aws["wd"] is not None and aws["ws"] is not None):
            resp["풍향"] = _DIRS[round(aws["wd"] / 22.5) % 16]
            resp["풍속"] = f"{aws['ws']}m/s"
            resp["풍향풍속출처"] = f"{aws['name']} AWS"

    return resp


@app.get("/weather")
def weather():
    d = _weather_lookup(
        request.args.get("loc", ""),
        request.args.get("lat", ""),
        request.args.get("lon", ""),
    )
    return jsonify(d), d.pop("_status", 200)


# ── /predep (MTIS 출항전 안전점검표 — 실제 승선인원/화물) ──────────────
# 공개 데이터(로그인 불필요). ferryInfo 페이지에서 익명 세션 쿠키 + CSRF 토큰을
# 받아 detailFerryPreDepCkForMoTraffic 를 호출한다.
MTIS_BASE = os.environ.get("MTIS_BASE", "https://mtis.komsa.or.kr")
_MTIS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) rapid-report-backend/1.0"


def _mtis_post(path: str, payload: dict) -> dict:
    """MTIS 공개 엔드포인트 호출 — ferryInfo 페이지에서 익명 세션+CSRF 토큰을 받아 POST."""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    # 1) 페이지 GET → 익명 세션 쿠키 + CSRF 토큰 확보
    req = urllib.request.Request(
        f"{MTIS_BASE}/traffic/ferryInfo", headers={"User-Agent": _MTIS_UA}
    )
    html = opener.open(req, timeout=20).read().decode("utf-8", "replace")
    m = re.search(r'name="_csrf"\s+content="([^"]+)"', html)
    if not m:
        raise RuntimeError("MTIS CSRF 토큰을 찾지 못했습니다")
    token = m.group(1)
    # 2) POST
    req2 = urllib.request.Request(
        f"{MTIS_BASE}/traffic/{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": _MTIS_UA,
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-TOKEN": token,
            "Origin": MTIS_BASE,
            "Referer": f"{MTIS_BASE}/traffic/ferryInfo",
        },
        method="POST",
    )
    raw = opener.open(req2, timeout=20).read().decode("utf-8", "replace")
    return json.loads(raw)


def _predep_lookup(cd: str, name: str = "", de: str = "", tm: str = ""):
    """MTIS 출항전 점검표(실제 승선/화물) 조회. dict 반환, 없으면 None, 오류 시 예외 전파."""
    if tm:
        # 특정 항차 지정(출항일+시간) → 상세 조회
        de = de or datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
        data = _mtis_post(
            "detailFerryPreDepCkForMoTraffic",
            {"psnshpCd": cd, "psnshpNm": name, "sloffDe": de, "sloffTime": tm.zfill(4)},
        )
    else:
        # 시간 미지정 → 그 선박의 '가장 최근' 출항전 점검표 (작성 시각 기준 최신)
        data = _mtis_post(
            "selectQrForSfcstDeInfo", {"psnshpCd": cd, "psnshpNm": "", "shipNo": ""}
        )

    o = (data or {}).get("psnshpSloffBeforeSfcst") or {}
    if not o:
        return None

    def num(key):
        try:
            return int(float(o.get(key)))
        except (TypeError, ValueError):
            return 0

    pax = num("pasngrAdultHeadcnt") + num("pasngrSmPersonHeadcnt") + num("pasngrInfantHeadcnt")
    return {
        "여객": pax,
        "대인": num("pasngrAdultHeadcnt"),
        "소인": num("pasngrSmPersonHeadcnt"),
        "유아": num("pasngrInfantHeadcnt"),
        "승무원": num("realCrewHeadcnt"),
        "임시승선자": num("realTmpEmbrkHeadcnt"),
        "실제승선인원": num("realEmbrkPrsnCo"),
        "항로": o.get("lcnsSeawyNm", ""),
        "선박번호": o.get("shipNo", ""),
        "출항일": str(o.get("sloffDe", "")),
        "출항시간": str(o.get("sloffTime", "")),
        "화물적재중량": str(o.get("realFrghtLoadngWt", "")),
        "차량": num("vhcleFrghtCo"),
    }


@app.get("/predep")
def predep():
    cd = request.args.get("psnshpCd", "").strip()
    if not cd:
        return jsonify({"error": "psnshpCd(선박코드)가 필요합니다"}), 400
    try:
        d = _predep_lookup(
            cd,
            request.args.get("name", "").strip(),
            request.args.get("date", "").strip(),
            request.args.get("time", "").strip(),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    if d is None:
        return jsonify({"error": "출항전 점검표를 찾지 못했습니다(선박코드 확인)"}), 404
    return jsonify(d)


# ── /parse ──────────────────────────────────────────

PARSE_PROMPT = (
    "다음은 여객선 해양사고 보고자의 자유 입력입니다. "
    "핵심 정보를 추출해 JSON으로만 응답하세요. 마크다운·설명 없이 순수 JSON만 출력합니다.\n"
    "키: 선박명(\"호\"까지 포함), 사고위치(좌표·지명 포함), 여객(숫자만), 승무원(숫자만), 사고개요(한 문장).\n"
    "값을 알 수 없으면 \"\".\n\n입력: "
)


def _gemini_parse(text: str) -> str:
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    payload = json.dumps(
        {
            "contents": [{"parts": [{"text": PARSE_PROMPT + text}]}],
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 512},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={urllib.parse.quote(GEMINI_KEY, safe='')}"
    )
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts)


def _claude_parse(text: str) -> str:
    payload = json.dumps(
        {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 512,
            "messages": [{"role": "user", "content": PARSE_PROMPT + text}],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return "".join(c["text"] for c in data.get("content", []) if c.get("type") == "text")


@app.post("/parse")
def parse_text():
    body = request.get_json(force=True, silent=True) or {}
    text = str(body.get("text", "")).strip()
    if not text:
        return jsonify({"error": "text 필드가 필요합니다"}), 400
    if not (GEMINI_KEY or ANTHROPIC_KEY):
        return jsonify({"error": "GEMINI_KEY 또는 ANTHROPIC_KEY가 설정되지 않았습니다"}), 503

    # Gemini 키가 있으면 Gemini, 없으면 Claude 사용
    try:
        raw = _gemini_parse(text) if GEMINI_KEY else _claude_parse(text)
        raw = raw.replace("```json", "").replace("```", "").strip()
        return jsonify(json.loads(raw))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


# ── 자연어 파싱(서버측) — LLM 시도 후 규칙 기반 폴백 ──────────

def _rule_parse(text: str) -> dict:
    """LLM 실패 시 규칙 기반 추출 (프론트 ruleParse의 서버 포팅본)."""
    ship = ""
    m = re.search(r"([가-힣A-Za-z0-9]+호)", text)
    if m:
        ship = m.group(1)
    if not ship:
        m = re.search(r"^([가-힣A-Za-z0-9]+)(?=\s*,)", text)
        if m:
            ship = m.group(1)
    if not ship:
        m = re.match(r"^\s*([가-힣A-Za-z][가-힣A-Za-z0-9]*)", text)
        if m and len(m.group(1)) >= 2:
            ship = re.sub(r"(에서|에게|으로|에|가|이|은|는|와|과|을|를|로)$", "", m.group(1))
    pos = ""
    m = re.search(
        r"(\d{1,3}[-–]\d{1,2}(?:[-–]\d{1,2})?(?:\.\d+)?\s*N[,，]?\s*"
        r"\d{1,3}[-–]\d{1,2}(?:[-–]\d{1,2})?(?:\.\d+)?\s*E)", text, re.I)
    if m:
        pos = m.group(1)
    if not pos:
        m = re.search(r"(\d{1,3}[-–]\d{1,2}(?:\.\d+)?)\s*[,，]?\s+(\d{2,3}[-–]\d{1,2}(?:\.\d+)?)", text)
        if m:
            pos = f"{m.group(1)}N {m.group(2)}E"
    m = re.search(r"([가-힣]+\s*(?:북동방|남동방|북서방|남서방|동방|서방|남방|북방|인근|부근)[^,.\n]*)", text)
    area = m.group(1) if m else ""
    m = re.search(r"여객\s*(\d+)\s*명", text)
    pax = m.group(1) if m else ""
    m = re.search(r"승무원\s*(\d+)\s*명", text)
    crew = m.group(1) if m else ""
    summary = ""
    if re.search(r"부유물|폐그물|감김|감겨", text):
        summary = "부유물(폐그물) 프로펠러 감김으로 자력 항해 불가"
    elif "이물질" in text:
        summary = "추진기 이물질 걸림으로 자력 항해 불가"
    elif "좌초" in text:
        summary = "좌초 발생"
    elif "충돌" in text:
        summary = "충돌 발생"
    elif "화재" in text:
        summary = "화재 발생"
    elif re.search(r"기관|엔진", text):
        summary = "기관 고장으로 자력 항해 불가"
    elif re.search(r"정선|표류", text):
        summary = "자력 항해 불가 (정선·표류)"
    loc = " / ".join(x for x in (pos, area) if x)
    return {"선박명": ship, "사고위치": loc, "여객": pax, "승무원": crew, "사고개요": summary or text[:60]}


def _parse_nl(text: str) -> dict:
    """LLM(Gemini→Claude) 파싱 시도, 실패 시 규칙 파싱."""
    try:
        if GEMINI_KEY or ANTHROPIC_KEY:
            raw = _gemini_parse(text) if GEMINI_KEY else _claude_parse(text)
            raw = raw.replace("```json", "").replace("```", "").strip()
            d = json.loads(raw)
            if isinstance(d, dict) and any(d.values()):
                return d
    except Exception:
        pass
    return _rule_parse(text)


def _parse_coord(s: str):
    """'33-58-12N'(도분초)·'33-58.2N'(도분)·십진수 → 십진 도."""
    s = str(s).strip()
    m = re.match(r"^(\d{1,3})[-–\s](\d{1,2})[-–\s](\d{1,2}(?:\.\d+)?)\s*([NSEW])?$", s, re.I)
    if m:
        v = int(m.group(1)) + int(m.group(2)) / 60 + float(m.group(3)) / 3600
        return -v if (m.group(4) or "").upper() in ("S", "W") else v
    m = re.match(r"^(\d{1,3})[-–\s](\d{1,2}(?:\.\d+)?)\s*([NSEW])?$", s, re.I)
    if m:
        v = int(m.group(1)) + float(m.group(2)) / 60
        return -v if (m.group(3) or "").upper() in ("S", "W") else v
    try:
        return float(s)
    except ValueError:
        return None


def _extract_latlon(text: str):
    """사고위치 문자열에서 (lat, lon) 추출. 실패 시 (None, None)."""
    text = str(text)
    m = re.search(
        r"(\d{1,3}(?:[-–]\d{1,2}){1,2}(?:\.\d+)?)\s*N[,，]?\s*"
        r"(\d{1,3}(?:[-–]\d{1,2}){1,2}(?:\.\d+)?)\s*E", text, re.I)
    if m:
        return _parse_coord(m.group(1) + "N"), _parse_coord(m.group(2) + "E")
    m = re.search(r"(\d{1,3})[-–](\d{1,2}(?:\.\d+)?)\s*[,，]?\s+(\d{2,3})[-–](\d{1,2}(?:\.\d+)?)", text)
    if m:
        return int(m.group(1)) + float(m.group(2)) / 60, int(m.group(3)) + float(m.group(4)) / 60
    m = re.search(r"(\d{2}\.\d+)[,，\s]+(\d{3}\.\d+)", text)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


# ── /kakao (카카오 i 오픈빌더 스킬 서버 — 콜백 비동기) ────────────
# 사고 자유텍스트 → 파싱·제원·기상/AWS·항로를 종합한 1차 속보를 자동 작성.
# 카카오 5초 제한을 넘기므로 콜백(useCallback)으로 즉시 접수 응답 후 결과를 콜백 URL로 전송.

# 1차 보고서 기본 조치사항 (웹 보고서와 동일한 자동완성 문구)
_DEFAULT_ACTION = "해경 및 해사안전 감독관 보고, 여객 안내방송 및 승객 구명조끼 착용 후 선내 대기 중"

# 기준점 목록 (이름, 위도, 경도) — 사고위치의 상대 방위·거리(마일) 표기용
# KOMSA 연안여객선 기항지 공식 API(port-call-info) 좌표 + 동해안·외해 등대 보충. 프론트 refPoints와 동기화.
_REF_POINTS = [
    ("부산", 35 + 6.1 / 60, 129 + 2.6 / 60),
    ("제주(제주시)", 33 + 31.5 / 60, 126 + 32.4 / 60),
    ("인천", 37 + 27.3 / 60, 126 + 35.9 / 60),
    ("소청", 37 + 46.6 / 60, 124 + 44.8 / 60),
    ("대청", 37 + 49.6 / 60, 124 + 42.9 / 60),
    ("백령", 37 + 57.3 / 60, 124 + 44.2 / 60),
    ("덕적", 37 + 13.6 / 60, 126 + 9.4 / 60),
    ("자월", 37 + 14.6 / 60, 126 + 19.1 / 60),
    ("대이작", 37 + 10.7 / 60, 126 + 14.9 / 60),
    ("승봉", 37 + 10.2 / 60, 126 + 17.4 / 60),
    ("하리", 37 + 43.7 / 60, 126 + 17.2 / 60),
    ("미법", 37 + 43.5 / 60, 126 + 16.2 / 60),
    ("서검", 37 + 43.9 / 60, 126 + 14.2 / 60),
    ("문갑", 37 + 10.3 / 60, 126 + 6.8 / 60),
    ("굴업", 37 + 11.3 / 60, 125 + 59.2 / 60),
    ("백아", 37 + 4.9 / 60, 125 + 57.9 / 60),
    ("울도", 37 + 2.1 / 60, 125 + 59.7 / 60),
    ("지도", 37 + 4.0 / 60, 126 + 0.7 / 60),
    ("선수", 37 + 38.4 / 60, 126 + 23.1 / 60),
    ("볼음", 37 + 39.9 / 60, 126 + 12.7 / 60),
    ("아차", 37 + 39.6 / 60, 126 + 14.1 / 60),
    ("주문(살곶이)", 37 + 37.9 / 60, 126 + 15.7 / 60),
    ("소연평", 37 + 36.8 / 60, 125 + 42.5 / 60),
    ("대연평", 37 + 39.4 / 60, 125 + 42.8 / 60),
    ("대부", 37 + 17.9 / 60, 126 + 34.4 / 60),
    ("삼목", 37 + 30.0 / 60, 126 + 27.1 / 60),
    ("신도(옹진군)", 37 + 30.8 / 60, 126 + 26.4 / 60),
    ("장봉", 37 + 31.8 / 60, 126 + 23.1 / 60),
    ("풍도", 37 + 6.7 / 60, 126 + 23.7 / 60),
    ("육도(안산시 단원구)", 37 + 5.8 / 60, 126 + 27.3 / 60),
    ("화흥포", 34 + 18.3 / 60, 126 + 40.7 / 60),
    ("동천", 34 + 12.2 / 60, 126 + 37.5 / 60),
    ("소안", 34 + 10.8 / 60, 126 + 38.1 / 60),
    ("완도", 34 + 19.0 / 60, 126 + 45.6 / 60),
    ("청산", 34 + 10.9 / 60, 126 + 51.2 / 60),
    ("모황", 34 + 17.3 / 60, 126 + 53.8 / 60),
    ("생일", 34 + 18.4 / 60, 126 + 59.6 / 60),
    ("덕우", 34 + 14.9 / 60, 127 + 0.8 / 60),
    ("황제", 34 + 11.4 / 60, 127 + 4.5 / 60),
    ("소모", 34 + 13.8 / 60, 126 + 46.3 / 60),
    ("모도(모서)", 34 + 12.1 / 60, 126 + 45.1 / 60),
    ("모도(모동)", 34 + 12.0 / 60, 126 + 46.2 / 60),
    ("장도", 34 + 12.3 / 60, 126 + 50.9 / 60),
    ("여서", 33 + 59.3 / 60, 126 + 55.3 / 60),
    ("땅끝", 34 + 17.9 / 60, 126 + 31.8 / 60),
    ("흑일", 34 + 17.0 / 60, 126 + 33.6 / 60),
    ("산양", 34 + 13.6 / 60, 126 + 34.6 / 60),
    ("횡간", 34 + 14.1 / 60, 126 + 36.6 / 60),
    ("넙도", 34 + 12.1 / 60, 126 + 31.0 / 60),
    ("서넙", 34 + 11.5 / 60, 126 + 29.1 / 60),
    ("이목", 34 + 10.5 / 60, 126 + 34.4 / 60),
    ("당사(완도군)", 34 + 6.3 / 60, 126 + 35.9 / 60),
    ("노력", 34 + 27.9 / 60, 126 + 58.0 / 60),
    ("가학", 34 + 27.3 / 60, 127 + 1.3 / 60),
    ("남성", 34 + 19.0 / 60, 126 + 36.4 / 60),
    ("동화", 34 + 17.5 / 60, 126 + 36.5 / 60),
    ("백일", 34 + 17.7 / 60, 126 + 35.5 / 60),
    ("마삭", 34 + 14.6 / 60, 126 + 34.1 / 60),
    ("일정", 34 + 21.7 / 60, 126 + 59.3 / 60),
    ("당목", 34 + 22.7 / 60, 126 + 56.8 / 60),
    ("서성", 34 + 20.3 / 60, 126 + 59.7 / 60),
    ("화전", 34 + 21.1 / 60, 127 + 1.0 / 60),
    ("목포", 34 + 46.9 / 60, 126 + 23.1 / 60),
    ("비금‧도초", 34 + 43.0 / 60, 125 + 56.1 / 60),
    ("흑산", 34 + 41.1 / 60, 125 + 26.5 / 60),
    ("홍도", 34 + 41.0 / 60, 125 + 11.6 / 60),
    ("다물(다촌)", 34 + 44.1 / 60, 125 + 26.8 / 60),
    ("상태", 34 + 26.1 / 60, 125 + 17.1 / 60),
    ("하태", 34 + 23.7 / 60, 125 + 17.8 / 60),
    ("가거", 34 + 3.0 / 60, 125 + 7.7 / 60),
    ("만재", 34 + 12.6 / 60, 125 + 28.3 / 60),
    ("시하", 34 + 41.9 / 60, 126 + 14.6 / 60),
    ("마진", 34 + 37.6 / 60, 126 + 12.3 / 60),
    ("백야도", 34 + 36.8 / 60, 126 + 10.8 / 60),
    ("율도(진도)", 34 + 34.6 / 60, 126 + 11.8 / 60),
    ("평사", 34 + 34.6 / 60, 126 + 9.0 / 60),
    ("쉬미", 34 + 30.3 / 60, 126 + 12.0 / 60),
    ("저도", 34 + 30.4 / 60, 126 + 10.0 / 60),
    ("광대", 34 + 31.8 / 60, 126 + 6.2 / 60),
    ("송도(하태도)", 34 + 31.2 / 60, 126 + 5.7 / 60),
    ("혈도(가사)", 34 + 30.9 / 60, 126 + 5.2 / 60),
    ("양덕", 34 + 29.7 / 60, 126 + 6.3 / 60),
    ("주지", 34 + 29.2 / 60, 126 + 5.2 / 60),
    ("가사", 34 + 28.2 / 60, 126 + 3.2 / 60),
    ("소성남", 34 + 24.0 / 60, 126 + 2.2 / 60),
    ("성남", 34 + 23.7 / 60, 126 + 2.7 / 60),
    ("옥도(조도)", 34 + 21.0 / 60, 126 + 1.1 / 60),
    ("내병", 34 + 22.6 / 60, 125 + 58.2 / 60),
    ("외병", 34 + 22.5 / 60, 125 + 56.6 / 60),
    ("눌옥", 34 + 20.8 / 60, 125 + 57.5 / 60),
    ("갈목", 34 + 18.3 / 60, 125 + 56.9 / 60),
    ("진목", 34 + 18.6 / 60, 125 + 57.7 / 60),
    ("창유", 34 + 18.4 / 60, 126 + 3.2 / 60),
    ("율목", 34 + 19.3 / 60, 126 + 1.2 / 60),
    ("라베", 34 + 18.6 / 60, 126 + 0.8 / 60),
    ("관사", 34 + 18.5 / 60, 125 + 58.7 / 60),
    ("소마(모도)", 34 + 18.1 / 60, 125 + 59.0 / 60),
    ("모도", 34 + 17.4 / 60, 125 + 59.9 / 60),
    ("대마", 34 + 16.3 / 60, 125 + 59.9 / 60),
    ("관매", 34 + 14.4 / 60, 126 + 2.7 / 60),
    ("동거차", 34 + 14.5 / 60, 125 + 56.4 / 60),
    ("서거차", 34 + 15.1 / 60, 125 + 55.0 / 60),
    ("복호", 34 + 42.0 / 60, 126 + 10.1 / 60),
    ("북강", 34 + 40.1 / 60, 126 + 9.8 / 60),
    ("웅곡", 34 + 36.5 / 60, 126 + 2.3 / 60),
    ("옥도(하의)", 34 + 41.0 / 60, 126 + 3.9 / 60),
    ("장병", 34 + 39.2 / 60, 126 + 3.2 / 60),
    ("자라", 34 + 41.5 / 60, 126 + 10.2 / 60),
    ("상태서리", 34 + 36.2 / 60, 126 + 4.0 / 60),
    ("축강", 34 + 37.8 / 60, 126 + 11.2 / 60),
    ("상태동리", 34 + 35.4 / 60, 126 + 6.7 / 60),
    ("진도", 34 + 22.5 / 60, 126 + 8.1 / 60),
    ("슬도", 34 + 15.7 / 60, 126 + 9.1 / 60),
    ("독거", 34 + 15.4 / 60, 126 + 10.8 / 60),
    ("탄항(진도군)", 34 + 14.5 / 60, 126 + 10.4 / 60),
    ("혈도(진도)", 34 + 13.5 / 60, 126 + 9.7 / 60),
    ("청등", 34 + 14.9 / 60, 126 + 4.5 / 60),
    ("죽항", 34 + 16.1 / 60, 126 + 6.1 / 60),
    ("상하죽", 34 + 15.0 / 60, 125 + 55.4 / 60),
    ("곽도", 34 + 11.9 / 60, 125 + 51.5 / 60),
    ("맹골", 34 + 13.0 / 60, 125 + 51.2 / 60),
    ("죽도(맹골)", 34 + 13.2 / 60, 125 + 50.8 / 60),
    ("각흘", 34 + 15.4 / 60, 126 + 3.2 / 60),
    ("달리", 34 + 46.7 / 60, 126 + 19.8 / 60),
    ("장좌", 34 + 47.4 / 60, 126 + 20.1 / 60),
    ("율도(목포)", 34 + 47.7 / 60, 126 + 19.2 / 60),
    ("외달", 34 + 47.0 / 60, 126 + 17.9 / 60),
    ("막금", 34 + 37.3 / 60, 126 + 7.6 / 60),
    ("기도", 34 + 38.1 / 60, 126 + 5.2 / 60),
    ("부소", 34 + 41.5 / 60, 126 + 8.8 / 60),
    ("두리", 34 + 42.9 / 60, 126 + 7.1 / 60),
    ("반월", 34 + 42.4 / 60, 126 + 5.6 / 60),
    ("문병", 34 + 40.1 / 60, 126 + 2.4 / 60),
    ("개도", 34 + 38.2 / 60, 126 + 0.7 / 60),
    ("하의(당두)", 34 + 36.9 / 60, 126 + 0.8 / 60),
    ("대야", 34 + 38.4 / 60, 125 + 58.2 / 60),
    ("신도(신안군)", 34 + 36.1 / 60, 125 + 58.7 / 60),
    ("계마", 35 + 23.4 / 60, 126 + 24.3 / 60),
    ("대석만", 35 + 22.3 / 60, 126 + 3.3 / 60),
    ("안마", 35 + 20.7 / 60, 126 + 1.1 / 60),
    ("우이1구", 34 + 37.2 / 60, 125 + 51.4 / 60),
    ("동소우이", 34 + 36.6 / 60, 125 + 52.5 / 60),
    ("우이(예리)", 34 + 36.1 / 60, 125 + 50.8 / 60),
    ("우이2구", 34 + 36.3 / 60, 125 + 49.5 / 60),
    ("목포(북항)", 34 + 48.3 / 60, 126 + 21.9 / 60),
    ("가산", 34 + 45.7 / 60, 125 + 59.9 / 60),
    ("수치", 34 + 44.7 / 60, 126 + 0.7 / 60),
    ("남강", 34 + 48.2 / 60, 126 + 7.2 / 60),
    ("읍동", 34 + 45.6 / 60, 126 + 8.1 / 60),
    ("사치", 34 + 45.3 / 60, 126 + 3.7 / 60),
    ("송공", 34 + 50.9 / 60, 126 + 13.6 / 60),
    ("당사(신안군)", 34 + 53.4 / 60, 126 + 11.3 / 60),
    ("소악", 34 + 55.1 / 60, 126 + 12.1 / 60),
    ("매화(청돌)", 34 + 55.1 / 60, 126 + 13.1 / 60),
    ("대기점", 34 + 56.6 / 60, 126 + 12.8 / 60),
    ("병풍(나리)", 34 + 57.3 / 60, 126 + 13.0 / 60),
    ("향화", 35 + 10.1 / 60, 126 + 21.6 / 60),
    ("상낙월", 35 + 12.0 / 60, 126 + 8.7 / 60),
    ("진리(신안군)", 35 + 4.9 / 60, 126 + 7.3 / 60),
    ("점암", 35 + 5.4 / 60, 126 + 9.4 / 60),
    ("봉리", 35 + 6.5 / 60, 126 + 12.2 / 60),
    ("어의", 35 + 7.8 / 60, 126 + 11.3 / 60),
    ("목섬", 35 + 4.7 / 60, 126 + 2.5 / 60),
    ("재원", 35 + 5.0 / 60, 126 + 1.9 / 60),
    ("송도(지도)", 35 + 2.5 / 60, 126 + 12.2 / 60),
    ("병풍(보기)", 34 + 59.1 / 60, 126 + 12.9 / 60),
    ("선도", 34 + 58.5 / 60, 126 + 16.2 / 60),
    ("가룡", 34 + 55.3 / 60, 126 + 18.3 / 60),
    ("매화(기섬)", 34 + 54.8 / 60, 126 + 15.3 / 60),
    ("마산", 34 + 57.2 / 60, 126 + 15.0 / 60),
    ("신월", 34 + 57.6 / 60, 126 + 17.8 / 60),
    ("고이", 34 + 57.6 / 60, 126 + 17.4 / 60),
    ("사옥도(지신개)", 35 + 1.5 / 60, 126 + 10.1 / 60),
    ("증도", 34 + 56.8 / 60, 126 + 7.4 / 60),
    ("자은", 34 + 55.1 / 60, 126 + 5.5 / 60),
    ("송이", 35 + 16.3 / 60, 126 + 9.1 / 60),
    ("도초(시목)", 34 + 40.1 / 60, 125 + 57.0 / 60),
    ("상추자도", 33 + 57.7 / 60, 126 + 17.9 / 60),
    ("우수영", 34 + 35.3 / 60, 126 + 18.6 / 60),
    ("하추자도", 33 + 56.6 / 60, 126 + 19.7 / 60),
    ("모슬포", 33 + 12.6 / 60, 126 + 15.5 / 60),
    ("마라도 살레덕", 33 + 7.3 / 60, 126 + 16.2 / 60),
    ("가파도", 33 + 10.5 / 60, 126 + 16.3 / 60),
    ("산이수동", 33 + 12.4 / 60, 126 + 17.5 / 60),
    ("여수", 34 + 44.3 / 60, 127 + 44.0 / 60),
    ("나로", 34 + 27.9 / 60, 127 + 27.2 / 60),
    ("손죽", 34 + 17.4 / 60, 127 + 21.7 / 60),
    ("대동", 34 + 14.5 / 60, 127 + 14.6 / 60),
    ("거문", 34 + 1.7 / 60, 127 + 18.5 / 60),
    ("여천", 34 + 33.1 / 60, 127 + 45.1 / 60),
    ("유송", 34 + 32.1 / 60, 127 + 45.8 / 60),
    ("우학", 34 + 30.5 / 60, 127 + 46.3 / 60),
    ("안도", 34 + 29.4 / 60, 127 + 48.2 / 60),
    ("서고지", 34 + 28.6 / 60, 127 + 47.8 / 60),
    ("역포", 34 + 27.2 / 60, 127 + 48.2 / 60),
    ("제도", 34 + 35.6 / 60, 127 + 39.6 / 60),
    ("개도(화산)", 34 + 35.0 / 60, 127 + 40.0 / 60),
    ("자봉", 34 + 35.3 / 60, 127 + 41.1 / 60),
    ("송고", 34 + 33.0 / 60, 127 + 43.7 / 60),
    ("함구미", 34 + 32.3 / 60, 127 + 42.6 / 60),
    ("백야", 34 + 37.2 / 60, 127 + 38.5 / 60),
    ("하화", 34 + 35.7 / 60, 127 + 37.1 / 60),
    ("사도", 34 + 35.6 / 60, 127 + 33.4 / 60),
    ("낭도", 34 + 36.2 / 60, 127 + 32.3 / 60),
    ("상화", 34 + 35.8 / 60, 127 + 36.3 / 60),
    ("여석", 34 + 34.9 / 60, 127 + 39.0 / 60),
    ("모전", 34 + 34.6 / 60, 127 + 38.6 / 60),
    ("둔병", 34 + 37.4 / 60, 127 + 32.2 / 60),
    ("소거문", 34 + 17.1 / 60, 127 + 23.3 / 60),
    ("평도", 34 + 14.7 / 60, 127 + 26.8 / 60),
    ("광도", 34 + 15.8 / 60, 127 + 31.8 / 60),
    ("엑스포", 34 + 45.2 / 60, 127 + 45.3 / 60),
    ("신기", 34 + 35.9 / 60, 127 + 44.6 / 60),
    ("화태(마족)", 34 + 35.1 / 60, 127 + 44.3 / 60),
    ("직포", 34 + 30.5 / 60, 127 + 44.3 / 60),
    ("군산", 35 + 58.7 / 60, 126 + 37.9 / 60),
    ("장자도", 35 + 48.6 / 60, 126 + 24.0 / 60),
    ("관리도", 35 + 49.1 / 60, 126 + 22.5 / 60),
    ("방축도", 35 + 50.9 / 60, 126 + 22.7 / 60),
    ("명도", 35 + 50.9 / 60, 126 + 21.0 / 60),
    ("말도", 35 + 51.2 / 60, 126 + 19.3 / 60),
    ("연도(군산시)", 36 + 4.9 / 60, 126 + 26.7 / 60),
    ("어청", 36 + 7.1 / 60, 125 + 59.0 / 60),
    ("개야", 36 + 1.9 / 60, 126 + 33.4 / 60),
    ("격포", 35 + 37.2 / 60, 126 + 28.2 / 60),
    ("위도", 35 + 37.1 / 60, 126 + 18.1 / 60),
    ("식도", 35 + 37.4 / 60, 126 + 17.4 / 60),
    ("하왕등", 35 + 38.4 / 60, 126 + 7.1 / 60),
    ("상왕등", 35 + 39.5 / 60, 126 + 6.7 / 60),
    ("대천", 36 + 19.7 / 60, 126 + 30.7 / 60),
    ("삽시", 36 + 19.7 / 60, 126 + 21.8 / 60),
    ("장고", 36 + 24.0 / 60, 126 + 21.3 / 60),
    ("고대", 36 + 23.4 / 60, 126 + 22.3 / 60),
    ("영목", 36 + 24.0 / 60, 126 + 25.7 / 60),
    ("저두", 36 + 21.8 / 60, 126 + 27.4 / 60),
    ("효자", 36 + 22.7 / 60, 126 + 26.4 / 60),
    ("선촌", 36 + 23.0 / 60, 126 + 26.1 / 60),
    ("안흥신항", 36 + 40.9 / 60, 126 + 8.0 / 60),
    ("가의(북항)", 36 + 40.7 / 60, 126 + 4.1 / 60),
    ("구도", 36 + 49.6 / 60, 126 + 19.4 / 60),
    ("고파", 36 + 54.8 / 60, 126 + 20.4 / 60),
    ("호도", 36 + 18.2 / 60, 126 + 15.9 / 60),
    ("녹도", 36 + 16.7 / 60, 126 + 16.3 / 60),
    ("외연", 36 + 13.4 / 60, 126 + 4.8 / 60),
    ("도비", 37 + 1.0 / 60, 126 + 27.6 / 60),
    ("소난지", 37 + 2.0 / 60, 126 + 27.3 / 60),
    ("대난지도", 37 + 3.2 / 60, 126 + 27.0 / 60),
    ("대난지도(해수욕장)", 37 + 2.6 / 60, 126 + 25.2 / 60),
    ("오천", 36 + 26.4 / 60, 126 + 31.3 / 60),
    ("월도", 36 + 24.5 / 60, 126 + 28.2 / 60),
    ("육도(보령시)", 36 + 24.6 / 60, 126 + 27.3 / 60),
    ("추도(보령시)", 36 + 24.3 / 60, 126 + 26.3 / 60),
    ("통영", 34 + 50.3 / 60, 128 + 25.2 / 60),
    ("욕지", 34 + 38.0 / 60, 128 + 16.0 / 60),
    ("연화", 34 + 39.0 / 60, 128 + 21.1 / 60),
    ("우도", 34 + 39.3 / 60, 128 + 20.7 / 60),
    ("한목", 34 + 45.5 / 60, 128 + 18.1 / 60),
    ("추도(미조)", 34 + 45.4 / 60, 128 + 17.3 / 60),
    ("비진내", 34 + 44.0 / 60, 128 + 27.6 / 60),
    ("비진외", 34 + 43.1 / 60, 128 + 27.5 / 60),
    ("소매물", 34 + 37.8 / 60, 128 + 32.9 / 60),
    ("대항", 34 + 38.5 / 60, 128 + 34.2 / 60),
    ("당금", 34 + 38.9 / 60, 128 + 34.5 / 60),
    ("문어포", 34 + 47.8 / 60, 128 + 27.8 / 60),
    ("제승당", 34 + 47.9 / 60, 128 + 28.4 / 60),
    ("의항", 34 + 47.4 / 60, 128 + 28.0 / 60),
    ("한산(관암)", 34 + 48.9 / 60, 128 + 28.1 / 60),
    ("가오치", 34 + 54.5 / 60, 128 + 18.9 / 60),
    ("사량", 34 + 50.6 / 60, 128 + 13.5 / 60),
    ("두미북구", 34 + 42.6 / 60, 128 + 10.9 / 60),
    ("두미남구", 34 + 41.9 / 60, 128 + 12.0 / 60),
    ("산등", 34 + 40.6 / 60, 128 + 13.9 / 60),
    ("탄항(통영시)", 34 + 40.4 / 60, 128 + 15.3 / 60),
    ("하노대", 34 + 40.1 / 60, 128 + 15.1 / 60),
    ("삼천포", 34 + 55.4 / 60, 128 + 5.2 / 60),
    ("삼덕", 34 + 47.7 / 60, 128 + 23.0 / 60),
    ("저구", 34 + 43.9 / 60, 128 + 36.3 / 60),
    ("용초", 34 + 44.7 / 60, 128 + 28.9 / 60),
    ("호두", 34 + 44.4 / 60, 128 + 30.2 / 60),
    ("죽도", 34 + 44.1 / 60, 128 + 31.8 / 60),
    ("진두", 34 + 46.0 / 60, 128 + 30.5 / 60),
    ("동좌", 34 + 48.0 / 60, 128 + 30.5 / 60),
    ("서좌", 34 + 47.7 / 60, 128 + 29.9 / 60),
    ("비산", 34 + 48.7 / 60, 128 + 29.8 / 60),
    ("화도", 34 + 49.7 / 60, 128 + 28.6 / 60),
    ("국도", 34 + 32.8 / 60, 128 + 26.6 / 60),
    ("중화", 34 + 47.4 / 60, 128 + 23.3 / 60),
    ("미수", 34 + 49.6 / 60, 128 + 23.8 / 60),
    ("포항", 36 + 3.1 / 60, 129 + 22.7 / 60),
    ("울릉", 37 + 28.8 / 60, 130 + 54.7 / 60),
    ("울릉(저동)(울릉군)", 37 + 29.8 / 60, 130 + 54.6 / 60),
    ("울릉(사동)", 37 + 27.7 / 60, 130 + 52.7 / 60),
    ("영일만신항", 36 + 5.8 / 60, 129 + 26.4 / 60),
    ("후포", 36 + 40.7 / 60, 129 + 27.7 / 60),
    ("독도(도착)(울릉군)", 37 + 14.4 / 60, 131 + 52.0 / 60),
    ("묵호", 37 + 33.0 / 60, 129 + 6.8 / 60),
    ("강릉", 37 + 46.4 / 60, 128 + 57.2 / 60),
    ("녹동(고흥군)", 34 + 31.4 / 60, 127 + 8.6 / 60),
    ("우두", 34 + 27.0 / 60, 127 + 5.9 / 60),
    ("거문(서도)", 34 + 3.2 / 60, 127 + 17.8 / 60),
    ("금진", 34 + 29.5 / 60, 127 + 7.4 / 60),
    ("금당(울포)(완도군)", 34 + 25.5 / 60, 127 + 4.5 / 60),
    ("신도(완도군)", 34 + 23.3 / 60, 127 + 3.0 / 60),
    ("충도", 34 + 22.8 / 60, 127 + 4.4 / 60),
    ("동송", 34 + 21.5 / 60, 127 + 3.8 / 60),
    ("연홍", 34 + 27.6 / 60, 127 + 5.6 / 60),
    ("도장", 34 + 22.1 / 60, 127 + 0.6 / 60),
    ("신지(동고)", 34 + 20.6 / 60, 126 + 53.5 / 60),
    ("성산포", 33 + 28.4 / 60, 126 + 56.1 / 60),
    ("초도(의성)", 34 + 13.4 / 60, 127 + 15.2 / 60),
    ("고사", 34 + 34.3 / 60, 126 + 9.0 / 60),
    ("횡도", 35 + 20.1 / 60, 125 + 59.8 / 60),
    ("후장구", 34 + 11.9 / 60, 126 + 29.5 / 60),
    ("하낙월", 35 + 11.5 / 60, 126 + 7.9 / 60),
    ("소기점", 34 + 55.7 / 60, 126 + 12.5 / 60),
    ("마안", 34 + 12.5 / 60, 126 + 30.9 / 60),
    ("대각시", 35 + 11.0 / 60, 126 + 12.7 / 60),
    ("규포", 34 + 37.0 / 60, 127 + 33.1 / 60),
    ("삼천포(구항)", 34 + 55.6 / 60, 128 + 3.9 / 60),
    ("동도(거문)", 34 + 2.8 / 60, 127 + 18.6 / 60),
    ("연도(여수시)", 34 + 25.7 / 60, 127 + 47.7 / 60),
    ("추도(여수시)", 34 + 35.6 / 60, 127 + 34.0 / 60),
    ("노대도", 34 + 46.4 / 60, 126 + 2.3 / 60),
    ("금평", 34 + 50.5 / 60, 128 + 13.0 / 60),
    ("부산항북방파제", 35 + 3.5 / 60, 129 + 4.5 / 60),
    ("오륙도등대", 35 + 5.5 / 60, 129 + 7.5 / 60),
    ("가덕도등대", 35 + 0.1 / 60, 128 + 49.7 / 60),
    ("격렬비열도등대", 36 + 36.5 / 60, 125 + 32.7 / 60),
    ("팔미도등대", 37 + 21.2 / 60, 126 + 31.0 / 60),
    ("간절곶등대", 35 + 21.5 / 60, 129 + 22.3 / 60),
    ("호미곶등대", 36 + 4.6 / 60, 129 + 34.1 / 60),
    ("울산항", 35 + 29.5 / 60, 129 + 23.0 / 60),
    ("속초항", 38 + 12.5 / 60, 128 + 35.7 / 60),
    ("동해항", 37 + 29.5 / 60, 129 + 8.5 / 60),
    ("주문진항", 37 + 54.0 / 60, 128 + 50.0 / 60),
    ("죽변항", 37 + 3.5 / 60, 129 + 25.0 / 60),
]
_DIR8 = ["북", "북동", "동", "남동", "남", "남서", "서", "북서"]


def _rel_position(lat, lon) -> str:
    """사고 좌표 → 가장 가까운 기준점 기준 '○○ ○쪽 N마일'. 좌표 없으면 ''."""
    if lat is None or lon is None:
        return ""
    from math import radians, sin, cos, asin, sqrt, atan2, pi
    best = None
    for name, rlat, rlon in _REF_POINTS:
        dlat, dlon = radians(lat - rlat), radians(lon - rlon)
        a = sin(dlat / 2) ** 2 + cos(radians(rlat)) * cos(radians(lat)) * sin(dlon / 2) ** 2
        dist = 2 * 3440.065 * asin(sqrt(a))
        y = sin(dlon) * cos(radians(lat))
        x = cos(radians(rlat)) * sin(radians(lat)) - sin(radians(rlat)) * cos(radians(lat)) * cos(dlon)
        brg = (atan2(y, x) * 180 / pi + 360) % 360
        if best is None or dist < best[1]:
            best = (name, dist, brg)
    name, dist, brg = best
    dist_txt = f"{dist:.1f}" if dist < 10 else str(round(dist))
    return f"{name} {_DIR8[round(brg / 45) % 8]}쪽 {dist_txt}마일"


def _build_report_text(utterance: str) -> str:
    """사고 자유텍스트 → 1차(속보) 보고서 텍스트."""
    parsed = _parse_nl(utterance)
    ship = str(parsed.get("선박명") or "").strip()
    loc = str(parsed.get("사고위치") or "").strip()
    pax = str(parsed.get("여객") or "").strip()
    crew = str(parsed.get("승무원") or "").strip()
    summary = str(parsed.get("사고개요") or "").strip()

    vessel = route_info = mtis = None
    if ship:
        try:
            vessel = _vessel_lookup(ship)
        except Exception:
            vessel = None
        try:
            route_info = _route_lookup(ship)
        except Exception:
            route_info = None
        # MTIS 출항전 점검표 — 실제 승선인원·화물 (KOMSA 선박코드 필요)
        cd = (vessel or {}).get("선박코드", "")
        if cd:
            try:
                mtis = _predep_lookup(cd)
            except Exception:
                mtis = None

    lat, lon = _extract_latlon(loc)
    wx = _weather_lookup(loc, "" if lat is None else str(lat), "" if lon is None else str(lon))
    if wx.get("error"):
        wx = None

    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst).strftime("%Y-%m-%d %H:%M")

    # 기상 줄 (위치 바로 아래에 배치) — 가장 가까운 1개로 통합 (파고·수온은 부이, 기온은 인근 AWS)
    if wx:
        parts = [f"풍향 {wx.get('풍향')}", f"풍속 {wx.get('풍속')}",
                 f"파고 {wx.get('파고')}", f"수온 {wx.get('수온')}"]
        a = wx.get("AWS")
        if a and a.get("기온"):
            parts.append(f"기온 {a.get('기온')}")
        weather_line = f"▶ 기상: {wx.get('지점','')} " + ", ".join(parts)
    else:
        weather_line = "▶ 기상: 위치 정보가 없어 해상관측을 특정하지 못했습니다"

    L = ["🚨 해양사고 1차(속보) — 자동작성", ""]

    # 선박 제원
    if vessel:
        spec = " · ".join(x for x in (
            vessel.get("선종"), vessel.get("총톤수"),
            f"정원 {vessel.get('여객정원')}" if vessel.get("여객정원") else "",
        ) if x)
        L.append(f"▶ 선박: {ship}" + (f" ({spec})" if spec else ""))
    elif ship:
        L.append(f"▶ 선박: {ship}")
    L.append(f"▶ 발생: {now}")

    # 위치 (+ 기준점 상대위치) → 바로 아래에 기상
    if loc:
        relpos = _rel_position(lat, lon)
        L.append(f"▶ 위치: {loc}" + (f" ({relpos})" if relpos else ""))
    L.append(weather_line)

    # 승선·화물 — MTIS 출항전 점검표(실제) 우선, 없으면 보고자 입력값
    if mtis:
        detail = f"(성인 {mtis['대인']}·소아 {mtis['소인']}·유아 {mtis['유아']})"
        tmp = f", 임시승선자 {mtis['임시승선자']}명" if mtis.get("임시승선자") else ""
        L.append(f"▶ 승선: 여객 {mtis['여객']}명{detail}, 선원 {mtis['승무원']}명{tmp} "
                 f"(실승선 계 {mtis['실제승선인원']}명)")
        cargo, veh = mtis.get("화물적재중량", ""), mtis.get("차량", 0)
        cargo_txt = " · ".join(x for x in (
            f"적재 {cargo} M/T" if cargo else "",
            f"차량 {veh}대" if veh else "",
        ) if x)
        if cargo_txt:
            L.append(f"▶ 화물: {cargo_txt}")
    elif pax or crew:
        try:
            total = int(pax or 0) + int(crew or 0)
        except ValueError:
            total = 0
        L.append(f"▶ 승선: 여객 {pax or '?'}명, 승무원 {crew or '?'}명" + (f" (계 {total}명)" if total else ""))

    if summary:
        L.append(f"▶ 개요: {summary}")
    L.append(f"▶ 조치사항: {_DEFAULT_ACTION}")

    if route_info:
        rr = " · ".join(x for x in (
            f"운항 {route_info['운항항로']}" if route_info.get("운항항로") else "",
            f"상태 {route_info['운항상태']}" if route_info.get("운항상태") else "",
        ) if x)
        if rr:
            L.append(f"▶ 항로: {rr}")
    L.append("")
    L.append("※ 자동 생성 초안 — 운항관리자 확인 후 정식 전파")
    return "\n".join(L)


def _kakao_text(text: str) -> dict:
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}


def _kakao_callback(callback_url: str, utterance: str):
    """백그라운드: 보고서 작성 후 카카오 콜백 URL로 결과 전송."""
    try:
        text = _build_report_text(utterance)
    except Exception as exc:
        text = f"보고서 자동작성 중 오류가 발생했습니다: {exc}"
    try:
        payload = json.dumps(_kakao_text(text), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            callback_url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=25)
        print(f"[kakao] 콜백 전송 성공 status={resp.status} url={callback_url}", flush=True)
    except Exception as exc:
        print(f"[kakao] 콜백 전송 실패: {exc} url={callback_url}", flush=True)


@app.post("/kakao")
def kakao_skill():
    body = request.get_json(force=True, silent=True) or {}
    ureq = body.get("userRequest") or {}
    utterance = str(ureq.get("utterance") or "").strip()
    callback_url = ureq.get("callbackUrl")
    print(f"[kakao] 요청 수신 utterance={utterance!r} callbackUrl={'있음' if callback_url else '없음(콜백 미전달)'}", flush=True)
    if not utterance:
        return jsonify(_kakao_text(
            "사고 내용을 한 문장으로 입력해 주세요.\n"
            "예) 섬사랑12호 추자도 북동방 2해리, 여객 28명 승무원 4명, 폐그물 감김"))
    # 콜백 사용(AI 챗봇 콜백 활성화) → 즉시 접수 응답 후 비동기로 결과 전송
    if callback_url:
        threading.Thread(target=_kakao_callback, args=(callback_url, utterance), daemon=True).start()
        return jsonify({
            "version": "2.0",
            "useCallback": True,
            "data": {"text": "🚨 사고 정보를 분석 중입니다… 잠시만 기다려 주세요."},
        })
    # 콜백 미설정 폴백: 동기 처리(외부 API 지연 시 5초 초과 가능)
    try:
        return jsonify(_kakao_text(_build_report_text(utterance)))
    except Exception as exc:
        return jsonify(_kakao_text(f"보고서 자동작성 중 오류가 발생했습니다: {exc}"))


# ── 진입점 ──────────────────────────────────────────

if __name__ == "__main__":
    missing = [k for k in ("KOMSA_KEY", "KMA_KEY") if not os.environ.get(k)]
    if missing:
        print(f"[오류] .env에 다음 필수 키가 없습니다: {', '.join(missing)}")
        raise SystemExit(1)
    print(f"백엔드 실행 중 → http://localhost:{PORT}")
    print("종료: Ctrl+C")
    app.run(host="0.0.0.0", port=PORT, debug=False)
