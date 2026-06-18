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
import csv
import http.cookiejar
import json
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from flask_cors import CORS

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
# GICOMS VMS(실시간 선박위치) — 로그인 세션 필요. Playwright로 로그인 후 쿠키 재사용.
GICOMS_VMS_ID = os.environ.get("GICOMS_VMS_ID", "")
GICOMS_VMS_PW = os.environ.get("GICOMS_VMS_PW", "")
GICOMS_BASE = os.environ.get("GICOMS_BASE", "http://www.gicoms.go.kr")
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


# 직전 정상 sea_obs 관측 캐시 — KMA 일시 500 장애 동안 대체 제공
# (stn=0은 전국 관측소를 일괄 반환하므로 전역 캐시 1개로 충분)
_SEA_OBS_CACHE = {"rows": None, "at": None}
_SEA_OBS_CACHE_TTL = 7200   # 2시간


def _weather_lookup(loc: str, lat: str = "", lon: str = "") -> dict:
    """해상부이 + 인근 AWS 기상 조회. 성공 시 결과 dict, 실패 시 {'error', '_status'}.
    KMA 일시 장애 시 직전 정상 관측(_SEA_OBS_CACHE)으로 대체(resp['_stale']=True)."""
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
        last = None
        for attempt in range(3):                  # 기상청 일시 5xx/타임아웃 대비 재시도
            try:
                return http_get(url)
            except Exception as exc:
                last = exc
                time.sleep(0.6 * (attempt + 1))
        raise last

    rows = None
    try:
        rows = _parse_sea_obs(fetch_obs(_tm_string(0)))
        if not rows:
            rows = _parse_sea_obs(fetch_obs(_tm_string(-1)))
    except Exception:
        rows = None

    stale = False
    if rows:
        _SEA_OBS_CACHE["rows"], _SEA_OBS_CACHE["at"] = rows, time.time()
    else:
        # 기상청 일시 장애(500 등) → 최근 정상 관측을 캐시에서 대체(빈 보고서 방지)
        cached, at = _SEA_OBS_CACHE["rows"], _SEA_OBS_CACHE["at"]
        if cached and at and time.time() - at < _SEA_OBS_CACHE_TTL:
            rows, stale = cached, True
        else:
            return {"error": "관측자료 없음(기상청 일시 장애)", "_status": 502}

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
    if stale:
        resp["_stale"] = True

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


def _gemini_generate(prompt: str, max_tokens: int = 256) -> str:
    """범용 Gemini 텍스트 생성(자유 프롬프트)."""
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    payload = json.dumps(
        {"contents": [{"parts": [{"text": prompt}]}],
         "generationConfig": {"maxOutputTokens": max_tokens}}, ensure_ascii=False
    ).encode("utf-8")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
           f"?key={urllib.parse.quote(GEMINI_KEY, safe='')}")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts)


def _claude_generate(prompt: str, max_tokens: int = 256) -> str:
    """범용 Claude 텍스트 생성(자유 프롬프트)."""
    payload = json.dumps(
        {"model": "claude-haiku-4-5-20251001", "max_tokens": max_tokens,
         "messages": [{"role": "user", "content": prompt}]}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY,
                 "anthropic-version": "2023-06-01"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return "".join(c["text"] for c in data.get("content", []) if c.get("type") == "text")


def _llm_edit(field: str, current: str, instruction: str) -> str:
    """LLM으로 보고서 항목(개요/조치사항)을 지시대로 편집. 한 줄 반환, 실패 시 ''."""
    prompt = (
        f"너는 해양사고 1차 보고서의 '{field}' 항목을 편집한다.\n"
        f"현재 '{field}': \"{current}\"\n"
        f"운항관리자 지시: \"{instruction}\"\n"
        f"지시를 반영한 새 '{field}' 내용만 한국어 한 줄로 출력하라. "
        "추가 지시면 기존 내용에 자연스럽게 더하고, 삭제·변경 지시면 새로 작성한다. "
        "설명·따옴표·접두어(예: '개요:') 없이 결과 문장만 출력."
    )
    try:
        if GEMINI_KEY:
            out = _gemini_generate(prompt)
        elif ANTHROPIC_KEY:
            out = _claude_generate(prompt)
        else:
            return ""
        out = (out or "").strip().strip('"').strip()
        return out.splitlines()[0].strip() if out else ""
    except Exception:
        return ""


def _llm_edit_multi(report: str, instruction: str):
    """LLM으로 개요·조치사항을 한 지시로 동시 편집. {'개요':.., '조치사항':..} 반환, 실패 시 None."""
    cur_o, cur_a = _get_field(report, "개요"), _get_field(report, "조치사항")
    prompt = (
        "해양사고 1차 보고서의 '개요'와 '조치사항'을 운항관리자 지시대로 수정한다.\n"
        f"현재 개요: \"{cur_o}\"\n현재 조치사항: \"{cur_a}\"\n"
        f"운항관리자 지시: \"{instruction}\"\n"
        "수정 결과를 JSON으로만 출력하라: {\"개요\":\"...\",\"조치사항\":\"...\"}\n"
        "지시에 언급되지 않은 항목은 현재 값을 그대로 유지한다. 설명·마크다운 없이 JSON만."
    )
    try:
        if GEMINI_KEY:
            raw = _gemini_generate(prompt)
        elif ANTHROPIC_KEY:
            raw = _claude_generate(prompt)
        else:
            return None
        raw = (raw or "").replace("```json", "").replace("```", "").strip()
        d = json.loads(raw)
        return {"개요": (str(d.get("개요", cur_o)).strip() or cur_o),
                "조치사항": (str(d.get("조치사항", cur_a)).strip() or cur_a)}
    except Exception:
        return None


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
    elif "좌주" in text:
        summary = "좌주 발생"
    elif "충돌" in text:
        summary = "충돌 발생"
    elif "화재" in text:
        summary = "화재 발생"
    elif "침수" in text:
        summary = "침수 발생"
    elif "전복" in text:
        summary = "전복 발생"
    elif re.search(r"타기|조타|조향|러더", text):
        summary = "조타장치(타기) 고장"
    elif re.search(r"추진기|프로펠러|스크류", text):
        summary = "추진기 고장으로 자력 항해 불가"
    elif re.search(r"기관|엔진", text):
        summary = "기관 고장으로 자력 항해 불가"
    elif re.search(r"정선|표류", text):
        summary = "자력 항해 불가 (정선·표류)"
    if not summary:
        # 사고유형 미인식 → 선박명·좌표를 제거한 입력문을 개요로 사용
        clean = text
        if ship:
            clean = clean.replace(ship, " ")
        clean = re.sub(r"\d{1,3}[-–]\d{1,3}(?:\.\d+)?\s*[NSEWnsew]?", " ", clean)  # 도-분 좌표
        clean = re.sub(r"\d{1,3}\.\d{2,}\s*[NSEWnsew]?", " ", clean)              # 십진 좌표
        clean = re.sub(r"\s+", " ", clean).strip(" ,/")
        summary = clean or text[:60]
    loc = " / ".join(x for x in (pos, area) if x)
    return {"선박명": ship, "사고위치": loc, "여객": pax, "승무원": crew, "사고개요": summary}


def _parse_nl(text: str) -> dict:
    """LLM 파싱 시도(Gemini→Claude), 모두 실패 시 규칙 파싱.
    Gemini 429 등 장애 시에도 Claude로 폴백해 추출 품질 유지."""
    for fn, key in ((_gemini_parse, GEMINI_KEY), (_claude_parse, ANTHROPIC_KEY)):
        if not key:
            continue
        try:
            raw = fn(text).replace("```json", "").replace("```", "").strip()
            d = json.loads(raw)
            if isinstance(d, dict) and any(d.values()):
                return d
        except Exception:
            continue
    return _rule_parse(text)


def _llm_text(prompt: str, max_tokens: int = 256) -> str:
    """범용 LLM 텍스트 생성: Gemini 우선 → 실패(429 등) 시 Claude → 둘 다 실패 시 ''."""
    if GEMINI_KEY:
        try:
            return _gemini_generate(prompt, max_tokens)
        except Exception as exc:
            print(f"[llm] Gemini 실패 → Claude 시도: {exc}", flush=True)
    if ANTHROPIC_KEY:
        try:
            return _claude_generate(prompt, max_tokens)
        except Exception as exc:
            print(f"[llm] Claude 실패: {exc}", flush=True)
    return ""


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
# KOMSA 연안여객선 기항지 공식 API(port-call-info) 좌표 + 동해안·외해 등대 보충. 섬명은 정식명(○○도)로 표기. 프론트와 동기화.
_REF_POINTS = [
    ("부산", 35 + 6.1 / 60, 129 + 2.6 / 60),
    ("제주(제주시)", 33 + 31.5 / 60, 126 + 32.4 / 60),
    ("인천", 37 + 27.3 / 60, 126 + 35.9 / 60),
    ("소청도", 37 + 46.6 / 60, 124 + 44.8 / 60),
    ("대청도", 37 + 49.6 / 60, 124 + 42.9 / 60),
    ("백령도", 37 + 57.3 / 60, 124 + 44.2 / 60),
    ("덕적", 37 + 13.6 / 60, 126 + 9.4 / 60),
    ("자월도", 37 + 14.6 / 60, 126 + 19.1 / 60),
    ("대이작도", 37 + 10.7 / 60, 126 + 14.9 / 60),
    ("승봉도", 37 + 10.2 / 60, 126 + 17.4 / 60),
    ("하리", 37 + 43.7 / 60, 126 + 17.2 / 60),
    ("미법도", 37 + 43.5 / 60, 126 + 16.2 / 60),
    ("서검도", 37 + 43.9 / 60, 126 + 14.2 / 60),
    ("문갑도", 37 + 10.3 / 60, 126 + 6.8 / 60),
    ("굴업도", 37 + 11.3 / 60, 125 + 59.2 / 60),
    ("백아도", 37 + 4.9 / 60, 125 + 57.9 / 60),
    ("울도", 37 + 2.1 / 60, 125 + 59.7 / 60),
    ("지도", 37 + 4.0 / 60, 126 + 0.7 / 60),
    ("선수", 37 + 38.4 / 60, 126 + 23.1 / 60),
    ("볼음도", 37 + 39.9 / 60, 126 + 12.7 / 60),
    ("아차도", 37 + 39.6 / 60, 126 + 14.1 / 60),
    ("주문(살곶이)", 37 + 37.9 / 60, 126 + 15.7 / 60),
    ("소연평도", 37 + 36.8 / 60, 125 + 42.5 / 60),
    ("대연평도", 37 + 39.4 / 60, 125 + 42.8 / 60),
    ("대부도", 37 + 17.9 / 60, 126 + 34.4 / 60),
    ("삼목", 37 + 30.0 / 60, 126 + 27.1 / 60),
    ("신도(옹진군)", 37 + 30.8 / 60, 126 + 26.4 / 60),
    ("장봉도", 37 + 31.8 / 60, 126 + 23.1 / 60),
    ("풍도", 37 + 6.7 / 60, 126 + 23.7 / 60),
    ("육도(안산시 단원구)", 37 + 5.8 / 60, 126 + 27.3 / 60),
    ("화흥포", 34 + 18.3 / 60, 126 + 40.7 / 60),
    ("동천", 34 + 12.2 / 60, 126 + 37.5 / 60),
    ("소안도", 34 + 10.8 / 60, 126 + 38.1 / 60),
    ("완도", 34 + 19.0 / 60, 126 + 45.6 / 60),
    ("청산", 34 + 10.9 / 60, 126 + 51.2 / 60),
    ("모황도", 34 + 17.3 / 60, 126 + 53.8 / 60),
    ("생일", 34 + 18.4 / 60, 126 + 59.6 / 60),
    ("덕우도", 34 + 14.9 / 60, 127 + 0.8 / 60),
    ("황제도", 34 + 11.4 / 60, 127 + 4.5 / 60),
    ("소모도", 34 + 13.8 / 60, 126 + 46.3 / 60),
    ("모도(모서)", 34 + 12.1 / 60, 126 + 45.1 / 60),
    ("모도(모동)", 34 + 12.0 / 60, 126 + 46.2 / 60),
    ("장도", 34 + 12.3 / 60, 126 + 50.9 / 60),
    ("여서도", 33 + 59.3 / 60, 126 + 55.3 / 60),
    ("땅끝", 34 + 17.9 / 60, 126 + 31.8 / 60),
    ("흑일도", 34 + 17.0 / 60, 126 + 33.6 / 60),
    ("산양", 34 + 13.6 / 60, 126 + 34.6 / 60),
    ("횡간도", 34 + 14.1 / 60, 126 + 36.6 / 60),
    ("넙도", 34 + 12.1 / 60, 126 + 31.0 / 60),
    ("서넙도", 34 + 11.5 / 60, 126 + 29.1 / 60),
    ("이목", 34 + 10.5 / 60, 126 + 34.4 / 60),
    ("당사(완도군)", 34 + 6.3 / 60, 126 + 35.9 / 60),
    ("노력도", 34 + 27.9 / 60, 126 + 58.0 / 60),
    ("가학", 34 + 27.3 / 60, 127 + 1.3 / 60),
    ("남성", 34 + 19.0 / 60, 126 + 36.4 / 60),
    ("동화도", 34 + 17.5 / 60, 126 + 36.5 / 60),
    ("백일도", 34 + 17.7 / 60, 126 + 35.5 / 60),
    ("마삭도", 34 + 14.6 / 60, 126 + 34.1 / 60),
    ("일정", 34 + 21.7 / 60, 126 + 59.3 / 60),
    ("당목", 34 + 22.7 / 60, 126 + 56.8 / 60),
    ("서성", 34 + 20.3 / 60, 126 + 59.7 / 60),
    ("화전", 34 + 21.1 / 60, 127 + 1.0 / 60),
    ("목포", 34 + 46.9 / 60, 126 + 23.1 / 60),
    ("비금‧도초", 34 + 43.0 / 60, 125 + 56.1 / 60),
    ("흑산", 34 + 41.1 / 60, 125 + 26.5 / 60),
    ("홍도", 34 + 41.0 / 60, 125 + 11.6 / 60),
    ("다물도(다촌)", 34 + 44.1 / 60, 125 + 26.8 / 60),
    ("상태도", 34 + 26.1 / 60, 125 + 17.1 / 60),
    ("하태도", 34 + 23.7 / 60, 125 + 17.8 / 60),
    ("가거도", 34 + 3.0 / 60, 125 + 7.7 / 60),
    ("만재도", 34 + 12.6 / 60, 125 + 28.3 / 60),
    ("시하도", 34 + 41.9 / 60, 126 + 14.6 / 60),
    ("마진도", 34 + 37.6 / 60, 126 + 12.3 / 60),
    ("백야도", 34 + 36.8 / 60, 126 + 10.8 / 60),
    ("율도(진도)", 34 + 34.6 / 60, 126 + 11.8 / 60),
    ("평사", 34 + 34.6 / 60, 126 + 9.0 / 60),
    ("쉬미", 34 + 30.3 / 60, 126 + 12.0 / 60),
    ("저도", 34 + 30.4 / 60, 126 + 10.0 / 60),
    ("광대도", 34 + 31.8 / 60, 126 + 6.2 / 60),
    ("송도(하태도)", 34 + 31.2 / 60, 126 + 5.7 / 60),
    ("혈도(가사)", 34 + 30.9 / 60, 126 + 5.2 / 60),
    ("양덕도", 34 + 29.7 / 60, 126 + 6.3 / 60),
    ("주지도", 34 + 29.2 / 60, 126 + 5.2 / 60),
    ("가사", 34 + 28.2 / 60, 126 + 3.2 / 60),
    ("소성남도", 34 + 24.0 / 60, 126 + 2.2 / 60),
    ("성남도", 34 + 23.7 / 60, 126 + 2.7 / 60),
    ("옥도(조도)", 34 + 21.0 / 60, 126 + 1.1 / 60),
    ("내병도", 34 + 22.6 / 60, 125 + 58.2 / 60),
    ("외병도", 34 + 22.5 / 60, 125 + 56.6 / 60),
    ("눌옥도", 34 + 20.8 / 60, 125 + 57.5 / 60),
    ("갈목도", 34 + 18.3 / 60, 125 + 56.9 / 60),
    ("진목도", 34 + 18.6 / 60, 125 + 57.7 / 60),
    ("창유", 34 + 18.4 / 60, 126 + 3.2 / 60),
    ("율목", 34 + 19.3 / 60, 126 + 1.2 / 60),
    ("라베", 34 + 18.6 / 60, 126 + 0.8 / 60),
    ("관사도", 34 + 18.5 / 60, 125 + 58.7 / 60),
    ("소마도(모도)", 34 + 18.1 / 60, 125 + 59.0 / 60),
    ("모도", 34 + 17.4 / 60, 125 + 59.9 / 60),
    ("대마도", 34 + 16.3 / 60, 125 + 59.9 / 60),
    ("관매도", 34 + 14.4 / 60, 126 + 2.7 / 60),
    ("동거차도", 34 + 14.5 / 60, 125 + 56.4 / 60),
    ("서거차도", 34 + 15.1 / 60, 125 + 55.0 / 60),
    ("복호", 34 + 42.0 / 60, 126 + 10.1 / 60),
    ("북강", 34 + 40.1 / 60, 126 + 9.8 / 60),
    ("웅곡", 34 + 36.5 / 60, 126 + 2.3 / 60),
    ("옥도(하의)", 34 + 41.0 / 60, 126 + 3.9 / 60),
    ("장병도", 34 + 39.2 / 60, 126 + 3.2 / 60),
    ("자라도", 34 + 41.5 / 60, 126 + 10.2 / 60),
    ("상태서리", 34 + 36.2 / 60, 126 + 4.0 / 60),
    ("축강", 34 + 37.8 / 60, 126 + 11.2 / 60),
    ("상태동리", 34 + 35.4 / 60, 126 + 6.7 / 60),
    ("진도", 34 + 22.5 / 60, 126 + 8.1 / 60),
    ("슬도", 34 + 15.7 / 60, 126 + 9.1 / 60),
    ("독거도", 34 + 15.4 / 60, 126 + 10.8 / 60),
    ("탄항(진도군)", 34 + 14.5 / 60, 126 + 10.4 / 60),
    ("혈도(진도)", 34 + 13.5 / 60, 126 + 9.7 / 60),
    ("청등도", 34 + 14.9 / 60, 126 + 4.5 / 60),
    ("죽항도", 34 + 16.1 / 60, 126 + 6.1 / 60),
    ("상하죽도", 34 + 15.0 / 60, 125 + 55.4 / 60),
    ("곽도", 34 + 11.9 / 60, 125 + 51.5 / 60),
    ("맹골도", 34 + 13.0 / 60, 125 + 51.2 / 60),
    ("죽도(맹골)", 34 + 13.2 / 60, 125 + 50.8 / 60),
    ("각흘", 34 + 15.4 / 60, 126 + 3.2 / 60),
    ("달리도", 34 + 46.7 / 60, 126 + 19.8 / 60),
    ("장좌도", 34 + 47.4 / 60, 126 + 20.1 / 60),
    ("율도(목포)", 34 + 47.7 / 60, 126 + 19.2 / 60),
    ("외달도", 34 + 47.0 / 60, 126 + 17.9 / 60),
    ("막금도", 34 + 37.3 / 60, 126 + 7.6 / 60),
    ("기도", 34 + 38.1 / 60, 126 + 5.2 / 60),
    ("부소", 34 + 41.5 / 60, 126 + 8.8 / 60),
    ("두리", 34 + 42.9 / 60, 126 + 7.1 / 60),
    ("반월도", 34 + 42.4 / 60, 126 + 5.6 / 60),
    ("문병도", 34 + 40.1 / 60, 126 + 2.4 / 60),
    ("개도", 34 + 38.2 / 60, 126 + 0.7 / 60),
    ("하의(당두)", 34 + 36.9 / 60, 126 + 0.8 / 60),
    ("대야도", 34 + 38.4 / 60, 125 + 58.2 / 60),
    ("신도(신안군)", 34 + 36.1 / 60, 125 + 58.7 / 60),
    ("계마", 35 + 23.4 / 60, 126 + 24.3 / 60),
    ("대석만도", 35 + 22.3 / 60, 126 + 3.3 / 60),
    ("안마도", 35 + 20.7 / 60, 126 + 1.1 / 60),
    ("우이1구", 34 + 37.2 / 60, 125 + 51.4 / 60),
    ("동소우이도", 34 + 36.6 / 60, 125 + 52.5 / 60),
    ("우이(예리)", 34 + 36.1 / 60, 125 + 50.8 / 60),
    ("우이2구", 34 + 36.3 / 60, 125 + 49.5 / 60),
    ("목포(북항)", 34 + 48.3 / 60, 126 + 21.9 / 60),
    ("가산", 34 + 45.7 / 60, 125 + 59.9 / 60),
    ("수치도", 34 + 44.7 / 60, 126 + 0.7 / 60),
    ("남강", 34 + 48.2 / 60, 126 + 7.2 / 60),
    ("읍동", 34 + 45.6 / 60, 126 + 8.1 / 60),
    ("사치", 34 + 45.3 / 60, 126 + 3.7 / 60),
    ("송공", 34 + 50.9 / 60, 126 + 13.6 / 60),
    ("당사(신안군)", 34 + 53.4 / 60, 126 + 11.3 / 60),
    ("소악도", 34 + 55.1 / 60, 126 + 12.1 / 60),
    ("매화(청돌)", 34 + 55.1 / 60, 126 + 13.1 / 60),
    ("대기점도", 34 + 56.6 / 60, 126 + 12.8 / 60),
    ("병풍(나리)", 34 + 57.3 / 60, 126 + 13.0 / 60),
    ("향화도", 35 + 10.1 / 60, 126 + 21.6 / 60),
    ("상낙월도", 35 + 12.0 / 60, 126 + 8.7 / 60),
    ("진리(신안군)", 35 + 4.9 / 60, 126 + 7.3 / 60),
    ("점암", 35 + 5.4 / 60, 126 + 9.4 / 60),
    ("봉리", 35 + 6.5 / 60, 126 + 12.2 / 60),
    ("어의도", 35 + 7.8 / 60, 126 + 11.3 / 60),
    ("목섬", 35 + 4.7 / 60, 126 + 2.5 / 60),
    ("재원도", 35 + 5.0 / 60, 126 + 1.9 / 60),
    ("송도(지도)", 35 + 2.5 / 60, 126 + 12.2 / 60),
    ("병풍(보기)", 34 + 59.1 / 60, 126 + 12.9 / 60),
    ("선도", 34 + 58.5 / 60, 126 + 16.2 / 60),
    ("가룡", 34 + 55.3 / 60, 126 + 18.3 / 60),
    ("매화(기섬)", 34 + 54.8 / 60, 126 + 15.3 / 60),
    ("마산도", 34 + 57.2 / 60, 126 + 15.0 / 60),
    ("신월", 34 + 57.6 / 60, 126 + 17.8 / 60),
    ("고이도", 34 + 57.6 / 60, 126 + 17.4 / 60),
    ("사옥도(지신개)", 35 + 1.5 / 60, 126 + 10.1 / 60),
    ("증도", 34 + 56.8 / 60, 126 + 7.4 / 60),
    ("자은", 34 + 55.1 / 60, 126 + 5.5 / 60),
    ("송이도", 35 + 16.3 / 60, 126 + 9.1 / 60),
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
    ("손죽도", 34 + 17.4 / 60, 127 + 21.7 / 60),
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
    ("자봉도", 34 + 35.3 / 60, 127 + 41.1 / 60),
    ("송고", 34 + 33.0 / 60, 127 + 43.7 / 60),
    ("함구미", 34 + 32.3 / 60, 127 + 42.6 / 60),
    ("백야도", 34 + 37.2 / 60, 127 + 38.5 / 60),
    ("하화도", 34 + 35.7 / 60, 127 + 37.1 / 60),
    ("사도", 34 + 35.6 / 60, 127 + 33.4 / 60),
    ("낭도", 34 + 36.2 / 60, 127 + 32.3 / 60),
    ("상화도", 34 + 35.8 / 60, 127 + 36.3 / 60),
    ("여석", 34 + 34.9 / 60, 127 + 39.0 / 60),
    ("모전", 34 + 34.6 / 60, 127 + 38.6 / 60),
    ("둔병도", 34 + 37.4 / 60, 127 + 32.2 / 60),
    ("소거문도", 34 + 17.1 / 60, 127 + 23.3 / 60),
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
    ("어청도", 36 + 7.1 / 60, 125 + 59.0 / 60),
    ("개야도", 36 + 1.9 / 60, 126 + 33.4 / 60),
    ("격포", 35 + 37.2 / 60, 126 + 28.2 / 60),
    ("위도", 35 + 37.1 / 60, 126 + 18.1 / 60),
    ("식도", 35 + 37.4 / 60, 126 + 17.4 / 60),
    ("하왕등도", 35 + 38.4 / 60, 126 + 7.1 / 60),
    ("상왕등도", 35 + 39.5 / 60, 126 + 6.7 / 60),
    ("대천", 36 + 19.7 / 60, 126 + 30.7 / 60),
    ("삽시도", 36 + 19.7 / 60, 126 + 21.8 / 60),
    ("장고도", 36 + 24.0 / 60, 126 + 21.3 / 60),
    ("고대도", 36 + 23.4 / 60, 126 + 22.3 / 60),
    ("영목", 36 + 24.0 / 60, 126 + 25.7 / 60),
    ("저두", 36 + 21.8 / 60, 126 + 27.4 / 60),
    ("효자도", 36 + 22.7 / 60, 126 + 26.4 / 60),
    ("선촌", 36 + 23.0 / 60, 126 + 26.1 / 60),
    ("안흥신항", 36 + 40.9 / 60, 126 + 8.0 / 60),
    ("가의(북항)", 36 + 40.7 / 60, 126 + 4.1 / 60),
    ("구도", 36 + 49.6 / 60, 126 + 19.4 / 60),
    ("고파도", 36 + 54.8 / 60, 126 + 20.4 / 60),
    ("호도", 36 + 18.2 / 60, 126 + 15.9 / 60),
    ("녹도", 36 + 16.7 / 60, 126 + 16.3 / 60),
    ("외연도", 36 + 13.4 / 60, 126 + 4.8 / 60),
    ("도비도", 37 + 1.0 / 60, 126 + 27.6 / 60),
    ("소난지도", 37 + 2.0 / 60, 126 + 27.3 / 60),
    ("대난지도", 37 + 3.2 / 60, 126 + 27.0 / 60),
    ("대난지도(해수욕장)", 37 + 2.6 / 60, 126 + 25.2 / 60),
    ("오천", 36 + 26.4 / 60, 126 + 31.3 / 60),
    ("월도", 36 + 24.5 / 60, 126 + 28.2 / 60),
    ("육도(보령시)", 36 + 24.6 / 60, 126 + 27.3 / 60),
    ("추도(보령시)", 36 + 24.3 / 60, 126 + 26.3 / 60),
    ("통영", 34 + 50.3 / 60, 128 + 25.2 / 60),
    ("욕지도", 34 + 38.0 / 60, 128 + 16.0 / 60),
    ("연화도", 34 + 39.0 / 60, 128 + 21.1 / 60),
    ("우도", 34 + 39.3 / 60, 128 + 20.7 / 60),
    ("한목", 34 + 45.5 / 60, 128 + 18.1 / 60),
    ("추도(미조)", 34 + 45.4 / 60, 128 + 17.3 / 60),
    ("비진내", 34 + 44.0 / 60, 128 + 27.6 / 60),
    ("비진외", 34 + 43.1 / 60, 128 + 27.5 / 60),
    ("소매물도", 34 + 37.8 / 60, 128 + 32.9 / 60),
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
    ("하노대도", 34 + 40.1 / 60, 128 + 15.1 / 60),
    ("삼천포", 34 + 55.4 / 60, 128 + 5.2 / 60),
    ("삼덕", 34 + 47.7 / 60, 128 + 23.0 / 60),
    ("저구", 34 + 43.9 / 60, 128 + 36.3 / 60),
    ("용초", 34 + 44.7 / 60, 128 + 28.9 / 60),
    ("호두", 34 + 44.4 / 60, 128 + 30.2 / 60),
    ("죽도", 34 + 44.1 / 60, 128 + 31.8 / 60),
    ("진두", 34 + 46.0 / 60, 128 + 30.5 / 60),
    ("동좌", 34 + 48.0 / 60, 128 + 30.5 / 60),
    ("서좌", 34 + 47.7 / 60, 128 + 29.9 / 60),
    ("비산도", 34 + 48.7 / 60, 128 + 29.8 / 60),
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
    ("연홍도", 34 + 27.6 / 60, 127 + 5.6 / 60),
    ("도장", 34 + 22.1 / 60, 127 + 0.6 / 60),
    ("신지(동고)", 34 + 20.6 / 60, 126 + 53.5 / 60),
    ("성산포", 33 + 28.4 / 60, 126 + 56.1 / 60),
    ("초도(의성)", 34 + 13.4 / 60, 127 + 15.2 / 60),
    ("고사", 34 + 34.3 / 60, 126 + 9.0 / 60),
    ("횡도", 35 + 20.1 / 60, 125 + 59.8 / 60),
    ("후장구도", 34 + 11.9 / 60, 126 + 29.5 / 60),
    ("하낙월도", 35 + 11.5 / 60, 126 + 7.9 / 60),
    ("소기점도", 34 + 55.7 / 60, 126 + 12.5 / 60),
    ("마안도", 34 + 12.5 / 60, 126 + 30.9 / 60),
    ("대각시도", 35 + 11.0 / 60, 126 + 12.7 / 60),
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


def _add_hemisphere(loc: str) -> str:
    """좌표 토큰에 방위(N/E)가 없으면 자동 부착. 위도(33~39)→N, 경도(124~132)→E.
    입력 정밀도는 그대로 두고 방위 글자만 추가 (도 값으로 판별 → 순서 무관)."""
    def repl(m):
        num, hemi = m.group(1), m.group(2)
        if hemi:
            return m.group(0)
        deg = int(re.match(r"\d+", num).group())
        if 30 <= deg <= 45:
            return num + "N"
        if 120 <= deg <= 135:
            return num + "E"
        return m.group(0)
    return re.sub(r"(\d{1,3}(?:[-–]\d{1,2}(?:\.\d+)?|\.\d+))([NSEWnsew])?", repl, loc)


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

    vpos = _vms_position_safe(ship)
    lat, lon = _extract_latlon(loc)
    if lat is None and vpos and vpos.get("위도") is not None:   # 신고 좌표 없으면 AIS 현위치로 기상조회
        lat, lon = vpos["위도"], vpos["경도"]
    have_coord = lat is not None
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
        if wx.get("_stale"):   # KMA 일시 장애로 직전 관측 대체
            t = wx.get("관측시각")
            weather_line += f" (기상청 지연—직전 관측{' ' + str(t) if t not in (None, '결측') else ''})"
    elif have_coord:
        # 위치는 확보(신고 좌표 또는 AIS)됐으나 기상청 연계가 일시 실패한 경우 — 위치 미상과 구분
        weather_line = "▶ 기상: 해상관측 일시 연계 지연 — 잠시 후 다시 시도해 주세요"
    else:
        weather_line = "▶ 기상: 위치 정보가 없어 해상관측을 특정하지 못했습니다 (사고 위치를 알려주시면 자동 조회됩니다)"

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

    # 출항시각 + 항로(출발-도착) — MTIS 점검표 우선(실제 항차), 없으면 운항항로 조회
    dep = str((mtis or {}).get("출항시간") or (route_info or {}).get("출발시각") or "").strip()
    if dep:
        dep = f"{dep.zfill(4)[:2]}:{dep.zfill(4)[2:]}" if dep.isdigit() else dep
        route_nm = ((mtis or {}).get("항로") or (route_info or {}).get("운항항로")
                    or (route_info or {}).get("면허항로") or (vessel or {}).get("항로") or "").strip()
        L.append(f"▶ 출항시각: {dep}" + (f" ({route_nm})" if route_nm else ""))

    # 위치 (+ 기준점 상대위치) → 바로 아래에 기상
    if loc:
        relpos = _rel_position(lat, lon)
        L.append(f"▶ 위치: {_add_hemisphere(loc)}" + (f" ({relpos})" if relpos else ""))
    if vpos and vpos.get("위도") is not None:
        L.append(_vms_line(vpos))
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


# ── 카카오 세션: 사용자별 직전 보고서 보관(개요 수정·전송용) ──
_SESSIONS = {}          # user_id -> {"report": str, "mode": None|"await_append"}
_SESSIONS_MAX = 500


def _session_set(uid: str, **kw):
    s = _SESSIONS.get(uid) or {}
    s.update(kw)
    _SESSIONS[uid] = s
    if len(_SESSIONS) > _SESSIONS_MAX:       # 메모리 보호: 오래된 항목 정리
        for k in list(_SESSIONS)[:-_SESSIONS_MAX]:
            _SESSIONS.pop(k, None)
    return s


def _get_field(report: str, label: str) -> str:
    """보고서에서 '▶ {label}: ' 줄의 값 반환."""
    m = re.search(rf"^▶ {label}: (.*)$", report, re.M)
    return m.group(1).strip() if m else ""


def _set_field(report: str, label: str, value: str) -> str:
    """보고서의 '▶ {label}:' 줄 값을 교체(없으면 조치사항 앞/끝에 추가)."""
    value = value.strip()
    if re.search(rf"^▶ {label}: ", report, re.M):
        return re.sub(rf"(^▶ {label}: ).*$", lambda m: m.group(1) + value,
                      report, count=1, flags=re.M)
    if label != "조치사항" and "▶ 조치사항:" in report:
        return report.replace("▶ 조치사항:", f"▶ {label}: {value}\n▶ 조치사항:", 1)
    return report.rstrip() + f"\n▶ {label}: {value}"


_KAKAO_QUICK = [
    {"label": "✏️ 개요 수정", "action": "message", "messageText": "개요 수정"},
    {"label": "🛠️ 조치사항 수정", "action": "message", "messageText": "조치사항 수정"},
    {"label": "📄 정식 보고서(hwpx)", "action": "message", "messageText": "정식 보고서"},
    {"label": "📤 관계기관 전송", "action": "message", "messageText": "관계기관 전송"},
]


def _kakao_report(text: str) -> dict:
    """보고서 simpleText + 하단 바로가기 버튼(개요·조치사항 수정·전송)."""
    return {"version": "2.0", "template": {
        "outputs": [{"simpleText": {"text": text}}],
        "quickReplies": _KAKAO_QUICK,
    }}


def _post_callback(callback_url: str, payload: dict):
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(callback_url, data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        resp = urllib.request.urlopen(req, timeout=25)
        print(f"[kakao] 콜백 전송 성공 status={resp.status}", flush=True)
    except Exception as exc:
        print(f"[kakao] 콜백 전송 실패: {exc}", flush=True)


def _kakao_callback(callback_url: str, utterance: str, uid: str = "anon"):
    """백그라운드: 보고서 작성 후 카카오 콜백 URL로 결과 전송."""
    try:
        text = _build_report_text(utterance)
    except Exception as exc:
        text = f"보고서 자동작성 중 오류가 발생했습니다: {exc}"
    _session_set(uid, report=text, utterance=utterance, mode=None)
    _post_callback(callback_url, _kakao_report(text))


def _kakao_edit_callback(callback_url: str, uid: str, field: str, report: str, instruction: str):
    """백그라운드: LLM으로 항목(개요/조치사항) 수정 후 콜백 전송."""
    cur = _get_field(report, field)
    new = _llm_edit(field, cur, instruction)
    if not new:                                   # LLM 실패 시 덧붙이기로 폴백
        new = (cur + " / " + instruction).strip(" /") if cur else instruction
    rep = _set_field(report, field, new)
    _session_set(uid, report=rep, mode=None)
    _post_callback(callback_url, _kakao_report(rep))


@app.post("/kakao")
def kakao_skill():
    body = request.get_json(force=True, silent=True) or {}
    ureq = body.get("userRequest") or {}
    utterance = str(ureq.get("utterance") or "").strip()
    callback_url = ureq.get("callbackUrl")
    uid = ((ureq.get("user") or {}).get("id")) or "anon"
    sess = _SESSIONS.get(uid) or {}
    print(f"[kakao] 요청 수신 utterance={utterance!r} callbackUrl={'있음' if callback_url else '없음(콜백 미전달)'}", flush=True)
    if not utterance:
        return jsonify(_kakao_text(
            "사고 내용을 한 문장으로 입력해 주세요.\n"
            "예) 섬사랑12호 추자도 북동방 2해리, 여객 28명 승무원 4명, 폐그물 감김"))

    # ① [개요/조치사항 수정] — 버튼(키워드만) 또는 "개요 수정 <내용>" 한 줄 모두 처리
    _edit_m = re.match(r"^(개요|조치사항)\s*수정\s*(.*)$", utterance, re.S)
    if _edit_m:
        if not sess.get("report"):
            return jsonify(_kakao_text("먼저 사고 내용을 입력해 주세요."))
        field, rest = _edit_m.group(1), _edit_m.group(2).strip()
        if not rest:                              # 키워드만 → 입력 대기 모드
            _session_set(uid, mode="edit_" + field)
            return jsonify(_kakao_text(
                f"✏️ {field}을(를) 어떻게 바꿀까요? 추가·삭제·교체 모두 가능합니다.\n"
                f"예) ‘예비타기 전환·인근 어선 지원요청 추가’\n"
                f"예) ‘삭제하고 「우현 타기 완전고장으로 자력항행 불가」로 변경’\n"
                f"(또는 ‘{field} 수정 …내용…’ 처럼 한 줄로 입력해도 됩니다)"))
        # 키워드 + 내용 한 줄 → 바로 수정 (선박·위치 등 기존 보고서 유지)
        report = sess["report"]
        if callback_url:
            _session_set(uid, mode=None)
            threading.Thread(target=_kakao_edit_callback,
                             args=(callback_url, uid, field, report, rest), daemon=True).start()
            return jsonify({"version": "2.0", "useCallback": True,
                            "data": {"text": f"✏️ {field} 수정 중입니다… 잠시만 기다려 주세요."}})
        cur = _get_field(report, field)
        new = _llm_edit(field, cur, rest) or ((cur + " / " + rest).strip(" /") if cur else rest)
        rep = _set_field(report, field, new)
        _session_set(uid, report=rep, mode=None)
        return jsonify(_kakao_report(rep))

    # ② [관계기관 전송] 버튼 → 전달(공유) 안내
    if utterance == "관계기관 전송":
        if not sess.get("report"):
            return jsonify(_kakao_text("먼저 사고 내용을 입력해 주세요."))
        return jsonify(_kakao_text(
            "📤 관계기관 전송 방법\n"
            "① 위 보고서 메시지를 길게 누르기 → ② [전달] → ③ 관계기관 채팅방 선택\n"
            "※ 카카오 정책상 자동 발송 대신 전달(공유) 방식으로 보냅니다."))

    # ②-b [정식 보고서(hwpx)] 버튼 → 공폼 서식 파일 생성 후 다운로드 링크 전달
    if utterance == "정식 보고서":
        src_utt = sess.get("utterance")
        if not src_utt:
            return jsonify(_kakao_text("먼저 사고 내용을 입력해 주세요. 1차 보고서를 만든 뒤 정식 보고서(hwpx)를 받을 수 있습니다."))
        base = _public_base()
        if callback_url:
            threading.Thread(target=_kakao_hwpx_callback,
                             args=(callback_url, uid, src_utt, base), daemon=True).start()
            return jsonify({"version": "2.0", "useCallback": True,
                            "data": {"text": "📄 정식 보고서(hwpx)를 작성 중입니다… 잠시만 기다려 주세요."}})
        try:
            return jsonify(_kakao_hwpx_message(src_utt, base))
        except Exception as exc:
            return jsonify(_kakao_text(f"정식 보고서(hwpx) 생성 중 오류가 발생했습니다: {exc}"))

    # ③ 수정 입력 모드 → LLM으로 항목 편집 후 보고서 재표출
    mode = sess.get("mode") or ""
    if mode.startswith("edit_") and sess.get("report"):
        field = mode[len("edit_"):]
        report = sess["report"]
        if callback_url:
            _session_set(uid, mode=None)
            threading.Thread(target=_kakao_edit_callback,
                             args=(callback_url, uid, field, report, utterance), daemon=True).start()
            return jsonify({"version": "2.0", "useCallback": True,
                            "data": {"text": f"✏️ {field} 수정 중입니다… 잠시만 기다려 주세요."}})
        # 동기 폴백
        cur = _get_field(report, field)
        new = _llm_edit(field, cur, utterance) or ((cur + " / " + utterance).strip(" /") if cur else utterance)
        rep = _set_field(report, field, new)
        _session_set(uid, report=rep, mode=None)
        return jsonify(_kakao_report(rep))

    # ④ 새 사고 — 콜백(비동기) 처리
    if callback_url:
        _session_set(uid, mode=None)
        threading.Thread(target=_kakao_callback, args=(callback_url, utterance, uid), daemon=True).start()
        return jsonify({
            "version": "2.0",
            "useCallback": True,
            "data": {"text": "🚨 사고 정보를 분석 중입니다… 잠시만 기다려 주세요."},
        })
    # 콜백 미설정 폴백: 동기 처리(외부 API 지연 시 5초 초과 가능)
    try:
        text = _build_report_text(utterance)
        _session_set(uid, report=text, utterance=utterance, mode=None)
        return jsonify(_kakao_report(text))
    except Exception as exc:
        return jsonify(_kakao_text(f"보고서 자동작성 중 오류가 발생했습니다: {exc}"))


# ── /report/hwpx (정식 해양사고 보고서 hwpx 자동 작성) ───────────
# 챗봇 데이터(제원·항로·MTIS·기상) + 회사 선박마스터(보험·선박번호·사진 등) + LLM 추정을
# 종합해 '해양사고 공폼' 서식의 hwpx 파일을 직접 생성(pyhwpxlib, 한글 오피스 불필요).

_VESSEL_MASTER_PATH = os.environ.get("VESSEL_MASTER", os.path.join(BASE_DIR, "선박마스터.csv"))
_VESSEL_PHOTO_DIR = os.environ.get("VESSEL_PHOTOS", os.path.join(BASE_DIR, "vessel_photos"))
_MASTER_CACHE = {"at": 0.0, "rows": None}

# 공폼 사고종류(18종) 키워드 매핑 — LLM 미사용 시 폴백 분류
_ACC_TYPES = [
    (r"충돌", "충돌"), (r"접촉", "접촉"), (r"좌초", "좌초"), (r"좌주|운항저해", "운항저해"),
    (r"전복", "전복"), (r"화재", "화재"), (r"폭발", "폭발"), (r"침몰", "침몰"),
    (r"행방불명|실종", "행방불명"), (r"기관|엔진", "기관손상"),
    (r"추진축|축계|동력전달", "추진축계손상"), (r"타기|조타|조향|러더", "조타장치손상"),
    (r"속구", "속구손상"), (r"침수", "침수"),
    (r"부유물|폐그물|폐로프|감김|감겨|이물질|프로펠러|스크류|추진기", "부유물감김"),
    (r"오염|기름|유출", "해양오염"), (r"안전사고|부상|추락", "안전사고"),
]


def _vessel_master(name: str = "", code: str = "") -> dict:
    """선박마스터.csv(회사 보유: 보험·선박번호·선적항·검사기관·국적·사진파일명) 조회.
    파일/행 없으면 빈 dict(graceful). 5분 메모리 캐시. 키는 선박코드 우선, 다음 선박명."""
    import time
    now = time.time()
    if _MASTER_CACHE["rows"] is None or (now - _MASTER_CACHE["at"]) > 300:
        rows = []
        try:
            with open(_VESSEL_MASTER_PATH, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
        except FileNotFoundError:
            rows = []
        except Exception as exc:
            print(f"[report] 선박마스터.csv 읽기 실패: {exc}", flush=True)
            rows = []
        _MASTER_CACHE["rows"], _MASTER_CACHE["at"] = rows, now
    rows = _MASTER_CACHE["rows"] or []
    norm = lambda s: str(s or "").replace(" ", "")
    if code:
        for r in rows:
            if norm(r.get("선박코드")) == norm(code):
                return dict(r)
    if name:
        for r in rows:
            if norm(r.get("선박명")) == norm(name):
                return dict(r)
        for r in rows:                       # 부분일치 폴백
            if norm(name) and norm(name) in norm(r.get("선박명")):
                return dict(r)
    return {}


def _accident_type(summary: str, utterance: str) -> str:
    """사고개요·원문에서 공폼 사고종류(18종) 추정. 폴백 '기타'."""
    blob = f"{summary} {utterance}"
    for pat, label in _ACC_TYPES:
        if re.search(pat, blob):
            return label
    return "기타"


def _infer_report_fields(utterance: str, ship: str, summary: str, extra: dict) -> dict:
    """LLM으로 공폼의 빈 항목 추정: 사고종류·추정원인·인명/오염/선박 피해·지연시간·
    조치사항(list)·조치계획(list). 실패 시 안전 기본값."""
    now_hm = datetime.now(timezone(timedelta(hours=9))).strftime("%H:%M")
    fallback = {
        "사고종류": _accident_type(summary, utterance),
        "추정원인": "확인 중",
        "인명피해": "없음",
        "오염피해": "없음",
        "선박피해": "확인 중",
        "지연시간": "확인 중",
        "조치사항": [f"{now_hm} 사고 접수", "관계기관 상황전파(지방청·해경서)", _DEFAULT_ACTION],
        "조치계획": ["사고 부위 정밀 점검·수리 예정", "재발방지 대책 마련 및 교육 실시 예정"],
    }
    if not (GEMINI_KEY or ANTHROPIC_KEY):
        return fallback
    prompt = (
        "너는 해양사고 정식 보고서('해양사고 공폼') 작성을 돕는다. 아래 신고 내용으로 공폼의 항목을 "
        "추정해 JSON으로만 출력하라.\n"
        f"선박: {ship or '미상'}\n사고개요: {summary or utterance}\n신고 원문: {utterance}\n"
        f"운항관리자 보충 — 경위: {extra.get('경위','')} / 피해: {extra.get('피해','')} / 조치: {extra.get('조치','')}\n\n"
        "출력 형식(JSON, 설명·마크다운 금지):\n"
        "{\"사고종류\":\"공폼 18종 중 하나(충돌/접촉/좌초/전복/화재/폭발/침몰/행방불명/기관손상/"
        "추진축계손상/조타장치손상/속구손상/침수/부유물감김/운항저해/해양오염/안전사고/기타)\","
        "\"추정원인\":\"한 줄\",\"인명피해\":\"없음 또는 내용\",\"오염피해\":\"없음 또는 내용\","
        "\"선박피해\":\"없음/확인 중 또는 내용\",\"지연시간\":\"확인 중 또는 내용\","
        "\"조치사항\":[\"시각 포함 한 줄씩\"],\"조치계획\":[\"한 줄씩\"]}\n"
        "확인되지 않은 항목은 공폼 관례대로 '확인 중' 또는 '없음'으로 적는다. 한국어로."
    )
    try:
        raw = _llm_text(prompt, 700)
        raw = (raw or "").replace("```json", "").replace("```", "").strip()
        d = json.loads(raw)
        out = dict(fallback)
        for k in ("사고종류", "추정원인", "인명피해", "오염피해", "선박피해", "지연시간"):
            v = str(d.get(k, "")).strip()
            if v:
                out[k] = v
        for k in ("조치사항", "조치계획"):
            v = d.get(k)
            if isinstance(v, list) and v:
                out[k] = [str(x).strip() for x in v if str(x).strip()]
            elif isinstance(v, str) and v.strip():
                out[k] = [v.strip()]
        return out
    except Exception:
        return fallback


def _kr_date(d: datetime) -> str:
    return f"{d.year}. {d.month}. {d.day}.({'월화수목금토일'[d.weekday()]})"


def _summary_narrative(f: dict) -> str:
    """공폼 예시 형식의 '사고개요' 한 문장 작성. LLM(Gemini→Claude) 우선, 실패 시 규칙 조립.

    예시: 2019. 1. 20.(일) 여수-거문 항로를 운항중인 여객선 섬나라2호(승무원 4명, 여객 32명,
    차량 8대)가 09:20 00항을 출항하여 초도항으로 운항 중 09:25경 00도 북동쪽 0.5마일 지점에서
    좌현 주기관 손상 사고 발생
    """
    def fallback():
        s = (f.get("date", "") + " ")
        if f.get("route"):
            s += f"{f['route']} 항로를 운항중인 "
        s += f"여객선 {f.get('ship') or '○○호'}"
        if f.get("manifest"):
            s += f"({f['manifest']})"
        dep = f.get("dep")
        if dep or f.get("route"):
            s += f"가 {dep + ' ' if dep else ''}○○항을 출항하여 ○○항으로 운항 중"
        else:
            s += "가 운항 중"
        s += f" {f.get('spot') or '○○ 부근'} 지점에서 {f.get('summary') or '사고'} 발생"
        return s

    if not (GEMINI_KEY or ANTHROPIC_KEY):
        return fallback()
    prompt = (
        "다음 사실로 해양사고 보고서의 '사고개요'를 한국어 한 문장으로 작성하라. "
        "아래 예시의 문체·구조를 그대로 따른다.\n\n"
        "예시: \"2019. 1. 20.(일) 여수-거문 항로를 운항중인 여객선 섬나라2호(승무원 4명, 여객 32명, "
        "차량 8대)가 09:20 00항을 출항하여 초도항으로 운항 중 09:25경 00도 북동쪽 0.5마일 지점에서 "
        "좌현 주기관 손상 사고 발생\"\n\n"
        f"사실:\n- 날짜: {f.get('date') or '미상'}\n- 항로: {f.get('route') or '미상'}\n"
        f"- 선박: {f.get('ship') or '미상'}\n- 승선: {f.get('manifest') or '미상'}\n"
        f"- 출항시각: {f.get('dep') or '미상'}\n- 사고위치: {f.get('spot') or '미상'}\n"
        f"- 사고내용: {f.get('summary') or '미상'}\n\n"
        "규칙:\n"
        "- 반드시 한 문장, '…사고 발생' 또는 '…발생'으로 끝낸다.\n"
        "- 출발항·도착항·사고시각처럼 사실에 없는 항목은 지어내지 말고 공폼처럼 '○○항','○○경' 빈칸으로 둔다.\n"
        "- 알고 있는 값(날짜·항로·선박·승선·출항시각·사고위치·사고내용)은 빠짐없이 넣는다.\n"
        "- 따옴표·설명·접두어 없이 문장만 출력한다."
    )
    try:
        out = _llm_text(prompt, 400)
        out = (out or "").strip().strip('"').strip()
        line = out.splitlines()[0].strip() if out else ""
        return line or fallback()
    except Exception:
        return fallback()


def _build_report_data(utterance: str, extra: dict = None, center: str = "") -> dict:
    """챗봇 입력 → 공폼 보고서용 데이터 dict 구성.
    우선순위: 회사 선박마스터 > KOMSA/MTIS > LLM 추정 > 공폼 자리표시자."""
    extra = extra or {}
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
        cd = (vessel or {}).get("선박코드", "")
        if cd:
            try:
                mtis = _predep_lookup(cd)
            except Exception:
                mtis = None

    vpos = _vms_position_safe(ship)
    lat, lon = _extract_latlon(loc)
    if lat is None and vpos and vpos.get("위도") is not None:   # 신고 좌표 없으면 AIS 현위치 사용
        lat, lon = vpos["위도"], vpos["경도"]
    wx = _weather_lookup(loc, "" if lat is None else str(lat), "" if lon is None else str(lon))
    if wx.get("error"):
        wx = None

    master = _vessel_master(ship, (vessel or {}).get("선박코드", ""))
    inf = _infer_report_fields(utterance, ship, summary, extra)

    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)

    # 현지기상
    if wx:
        wx_aws = wx.get("AWS") or {}
        wparts = [f"풍향({wx.get('풍향','-')})", f"풍속({wx.get('풍속','-')})",
                  f"파고({wx.get('파고','-')})", "시정(양호)"]
        weather = ", ".join(wparts)
    else:
        weather = "풍향( ), 풍속( ), 파고( ), 시정( )"

    # 항로·출항
    route_nm = ((mtis or {}).get("항로") or (route_info or {}).get("운항항로")
                or (route_info or {}).get("면허항로") or (vessel or {}).get("항로") or "").strip()
    dep = str((mtis or {}).get("출항시간") or (route_info or {}).get("출발시각") or "").strip()
    if dep and dep.isdigit():
        dep = f"{dep.zfill(4)[:2]}:{dep.zfill(4)[2:]}"

    # 승선 인원
    if mtis:
        crew_n = str(mtis.get("승무원") or crew or "")
        pax_n = str(mtis.get("여객") or pax or "")
        veh_n = mtis.get("차량") or 0
    else:
        crew_n, pax_n, veh_n = crew, pax, 0

    # 서술형 사고개요 (공폼 예시 형식) — Gemini 우선, 실패 시 규칙 조립
    mani = []
    if crew_n:
        mani.append(f"승무원 {crew_n}명")
    if pax_n:
        mani.append(f"여객 {pax_n}명")
    if veh_n:
        mani.append(f"차량 {veh_n}대")
    relpos = _rel_position(lat, lon)
    spot = relpos or (_add_hemisphere(loc) if loc else "")
    narr = _summary_narrative({
        "date": _kr_date(now),
        "route": route_nm,
        "ship": ship,
        "manifest": ", ".join(mani),
        "dep": dep,
        "spot": spot,
        "summary": summary,
    })

    # 화물
    cargo_mt = str((mtis or {}).get("화물적재중량") or "").strip()
    cargo = (f"{cargo_mt} M/T" if cargo_mt else "") + (f" / 차량 {veh_n}대" if veh_n else "")
    cargo = cargo.strip(" /") or "없음"

    # 선박사진 경로
    photo = ""
    fn = str(master.get("사진파일명") or "").strip()
    if fn:
        p = fn if os.path.isabs(fn) else os.path.join(_VESSEL_PHOTO_DIR, fn)
        if os.path.exists(p):
            photo = p

    ph = "00"  # 공폼 자리표시자(미확보 항목)
    return {
        "사고종류": inf["사고종류"],
        "기준일시": now.strftime("%Y년 %m월 %d일 %H:%M"),
        "보고센터": center or "운항관리센터",
        "사고개요": narr,
        "현지기상": weather,
        "선명": ship or ph,
        "총톤수": (vessel or {}).get("총톤수") or ph,
        "선종": (vessel or {}).get("선종") or "여객선",
        "승무정원": crew_n or ph,
        "소유자": (vessel or {}).get("선사") or master.get("소유자") or ph,
        "선박번호": master.get("선박번호") or (mtis or {}).get("선박번호") or ph,
        "화물": cargo,
        "선적항": master.get("선적항") or ph,
        "국적": master.get("국적") or "대한민국",
        "검사기관": master.get("검사기관") or ph,
        "보험현황": master.get("보험현황") or ph,
        "사진경로": photo,
        "인명피해": inf["인명피해"],
        "오염피해": inf["오염피해"],
        "선박피해": inf["선박피해"],
        "지연시간": inf["지연시간"],
        "추정원인": inf["추정원인"],
        "조치사항": inf["조치사항"],
        "조치계획": inf["조치계획"],
        "작성일자": f"{now.year}. {now.month}. {now.day}.",
    }


def _compose_report_hwpx(data: dict) -> bytes:
    """공폼 서식 hwpx 생성 → 바이트 반환. pyhwpxlib 필요(미설치 시 ImportError 전파)."""
    from pyhwpxlib import HwpxBuilder

    H1, H2 = 16, 12   # 제목 / 항목 글자크기(pt)
    b = HwpxBuilder()
    # 제목: 공폼 형식 "(○○호 사고종류) 사고 보고" — 선박명 + 사고종류
    ship_title = data["선명"] if data.get("선명") and data["선명"] != "00" else "○○호"
    b.add_paragraph(f"({ship_title} {data['사고종류']}) 사고 보고", bold=True, font_size=H1, alignment="CENTER")
    b.add_paragraph(f"기준 일시 : {data['기준일시']}", alignment="RIGHT")
    b.add_paragraph(f"보고 센터 : {data['보고센터']}", alignment="RIGHT")
    b.add_paragraph("")

    b.add_paragraph("□ 사고개요", bold=True, font_size=H2)
    b.add_paragraph(data["사고개요"])
    b.add_paragraph(f"** 현지기상 : {data['현지기상']}")

    b.add_paragraph("□ 선박제원", bold=True, font_size=H2)
    if data.get("사진경로"):
        try:
            b.add_image(data["사진경로"], width=12000, height=8000)
        except Exception as exc:
            print(f"[report] 선박사진 삽입 실패: {exc}", flush=True)
    # 공폼 서식: 6열 그리드 — 1열은 선박사진(세로 병합), 2개 라벨행(음영) + 2개 데이터행
    rows = [
        ["선박사진", "선 명", "총톤수", "선 종", "승무정원", "소유자 또는\n선박회사"],
        ["", "선박번호", "화물", "선적항", "국적", "검사기관"],
        ["", data["선명"], data["총톤수"], data["선종"], data["승무정원"], data["소유자"]],
        ["", data["선박번호"], data["화물"], data["선적항"], data["국적"], data["검사기관"]],
    ]
    merge_info = [(0, 0, 3, 0)]   # 1열(선박사진) 4행 세로 병합
    # 라벨행(0·1행)과 선박사진 라벨 셀 음영
    label_bg = {(0, 0): "#EFEFEF"}
    label_bg.update({(0, c): "#EFEFEF" for c in range(1, 6)})
    label_bg.update({(1, c): "#EFEFEF" for c in range(1, 6)})
    cell_aligns = {(r, c): "CENTER" for r in range(4) for c in range(6)}
    col_widths = [11000, 6300, 6300, 6300, 6300, 6320]   # 합 42520 = A4 본문폭
    try:
        b.add_table(rows, cell_colors=label_bg, merge_info=merge_info,
                    cell_aligns=cell_aligns, col_widths=col_widths)
    except Exception:
        b.add_table(rows)
    b.add_paragraph(f"** 보험 현황 : {data['보험현황']}")   # 공폼: 표 아래 별도 줄

    b.add_paragraph("□ 피해사항", bold=True, font_size=H2)
    b.add_bullet_list([f"인명 : {data['인명피해']}", f"오염 : {data['오염피해']}",
                       f"선박·시설물 등 : {data['선박피해']}"], bullet_char="ㅇ")
    b.add_paragraph(f"** 지연시간 : {data['지연시간']}")
    b.add_paragraph(f"** 사고 추정원인 : {data['추정원인']}")

    b.add_paragraph("□ 조치사항", bold=True, font_size=H2)
    b.add_bullet_list(data["조치사항"] or ["확인 중"], bullet_char="ㅇ")

    b.add_paragraph("□ 조치계획", bold=True, font_size=H2)
    b.add_bullet_list(data["조치계획"] or ["확인 중"], bullet_char="ㅇ")

    b.add_paragraph("")
    b.add_paragraph(data["작성일자"], alignment="CENTER")

    fd, path = tempfile.mkstemp(suffix=".hwpx")
    os.close(fd)
    try:
        b.save(path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@app.post("/report/hwpx")
def report_hwpx():
    """챗봇 데이터 → 정식 해양사고 보고서(hwpx) 다운로드.
    body: { utterance, center, extra:{경위,피해,조치} }"""
    body = request.get_json(force=True, silent=True) or {}
    utterance = str(body.get("utterance", "")).strip()
    if not utterance:
        return jsonify({"error": "utterance가 필요합니다"}), 400
    center = str(body.get("center", "")).strip()
    extra = body.get("extra") or {}
    try:
        data = _build_report_data(utterance, extra, center)
        blob = _compose_report_hwpx(data)
    except ImportError:
        return jsonify({"error": "hwpx 생성 라이브러리(pyhwpxlib)가 없습니다. "
                                 "pip install -r requirements.txt 후 다시 시도하세요."}), 503
    except Exception as exc:
        return jsonify({"error": f"보고서 생성 실패: {exc}"}), 500

    kst = timezone(timedelta(hours=9))
    fname = (f"{data.get('선명') or '해양사고'}_해양사고보고서_"
             f"{datetime.now(kst).strftime('%Y%m%d_%H%M')}.hwpx")
    quoted = urllib.parse.quote(fname)
    return Response(blob, mimetype="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
        "Content-Length": str(len(blob)),
    })


# ── 보고서 파일 임시 보관 + 다운로드 링크 (카카오용) ──────────────
# 카카오 스킬 서버는 파일 첨부를 보낼 수 없으므로, 생성한 hwpx를 토큰으로 임시 보관하고
# 공개 다운로드 URL을 카드 버튼으로 전달한다(1시간 후 만료).

_REPORT_FILES = {}            # token -> (blob, filename, expires_at)
_REPORT_FILES_TTL = 3600      # 1시간
_REPORT_FILES_MAX = 200


def _store_report_file(blob: bytes, filename: str) -> str:
    now = time.time()
    for k in [k for k, v in _REPORT_FILES.items() if v[2] < now]:   # 만료분 정리
        _REPORT_FILES.pop(k, None)
    if len(_REPORT_FILES) > _REPORT_FILES_MAX:
        for k in list(_REPORT_FILES)[:-_REPORT_FILES_MAX]:
            _REPORT_FILES.pop(k, None)
    token = secrets.token_urlsafe(16)
    _REPORT_FILES[token] = (blob, filename, now + _REPORT_FILES_TTL)
    return token


def _public_base() -> str:
    """카카오 카드 링크용 공개 베이스 URL. env 우선, 없으면 요청 헤더(프록시 포함)로 추정."""
    b = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if b:
        return b
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}"


@app.get("/report/download/<token>")
def report_download(token):
    item = _REPORT_FILES.get(token)
    if not item or item[2] < time.time():
        _REPORT_FILES.pop(token, None)
        return Response("다운로드 링크가 만료되었거나 잘못되었습니다. 챗봇에서 다시 요청해 주세요.",
                        status=404, mimetype="text/plain; charset=utf-8")
    blob, filename, _ = item
    quoted = urllib.parse.quote(filename)
    return Response(blob, mimetype="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
        "Content-Length": str(len(blob)),
    })


def _kakao_hwpx_message(utterance: str, base: str, center: str = "") -> dict:
    """utterance로 hwpx 생성·보관 후, 다운로드 버튼이 달린 카카오 textCard 반환."""
    data = _build_report_data(utterance, {}, center)
    blob = _compose_report_hwpx(data)
    kst = timezone(timedelta(hours=9))
    fname = (f"{data.get('선명') or '해양사고'}_해양사고보고서_"
             f"{datetime.now(kst).strftime('%Y%m%d_%H%M')}.hwpx")
    token = _store_report_file(blob, fname)
    url = f"{base}/report/download/{token}"
    return {"version": "2.0", "template": {
        "outputs": [{"textCard": {
            "title": "📄 정식 해양사고 보고서(hwpx)",
            "description": (f"{data.get('선명') or ''} · 공폼 서식으로 작성했습니다.\n"
                            "아래 버튼을 눌러 hwpx 파일을 받으세요.\n"
                            "※ 한글에서 열어 보완 후 본부 보고 (링크 1시간 후 만료)"),
            "buttons": [{"action": "webLink", "label": "보고서 다운로드", "webLinkUrl": url}],
        }}],
        "quickReplies": _KAKAO_QUICK,
    }}


def _kakao_hwpx_callback(callback_url: str, uid: str, utterance: str, base: str):
    """백그라운드: hwpx 생성·보관 후 다운로드 카드 콜백 전송."""
    try:
        payload = _kakao_hwpx_message(utterance, base)
    except ImportError:
        payload = _kakao_text("hwpx 생성 라이브러리(pyhwpxlib)가 서버에 설치되지 않았습니다. 관리자에게 문의해 주세요.")
    except Exception as exc:
        payload = _kakao_text(f"정식 보고서(hwpx) 생성 중 오류가 발생했습니다: {exc}")
    _post_callback(callback_url, payload)


# ── GICOMS VMS 실시간 선박위치 ───────────────────────
# 로그인은 RSA+키보드보안이라 Playwright(헤드리스 브라우저)로 수행 → 세션 쿠키만 추출해
# 가벼운 allShipTarget.json(전국 실시간 AIS) 호출에 재사용. 쿠키 만료 시 자동 재로그인.

_VMS = {"cookie": None, "at": None}          # JSESSIONID 캐시
_VMS_LOCK = threading.Lock()
_VMS_TARGETS = {"at": None, "items": []}     # allShipTarget 파싱결과 단기 캐시
_VMS_COOKIE_TTL = 1500                        # 25분
_VMS_TARGETS_TTL = 20                         # 20초


def _vms_login():
    """Playwright로 GICOMS 로그인 → JSESSIONID 쿠키 문자열 반환. 실패 시 예외."""
    if not (GICOMS_VMS_ID and GICOMS_VMS_PW):
        raise RuntimeError("GICOMS_VMS_ID/PW 미설정")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"))
            page = ctx.new_page()
            page.goto(f"{GICOMS_BASE}/", wait_until="domcontentloaded", timeout=30000)
            page.evaluate("""([vid, vpw]) => {
                const f = document.getElementById('loginForm');
                f.id.value = vid; f.password.value = vpw;
            }""", [GICOMS_VMS_ID, GICOMS_VMS_PW])
            page.evaluate("actionLogin('ID')")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            jsess = [c["value"] for c in ctx.cookies() if c["name"] == "JSESSIONID"]
            if not jsess:
                raise RuntimeError("로그인 실패(JSESSIONID 없음) — 계정/비밀번호 확인")
            return "JSESSIONID=" + jsess[-1]
        finally:
            browser.close()


def _vms_cookie(force=False):
    """캐시된 세션 쿠키 반환(없거나 만료/force면 재로그인). 동시 로그인 방지 락."""
    with _VMS_LOCK:
        fresh = (_VMS["cookie"] and _VMS["at"]
                 and (datetime.now() - _VMS["at"]).total_seconds() < _VMS_COOKIE_TTL)
        if force or not fresh:
            _VMS["cookie"] = _vms_login()
            _VMS["at"] = datetime.now()
        return _VMS["cookie"]


def _vms_all_targets(force_login=False):
    """allShipTarget.json(전국 실시간 위치) 리스트 반환. 20초 캐시. 세션 만료 시 1회 재로그인."""
    now = datetime.now()
    if (not force_login and _VMS_TARGETS["items"] and _VMS_TARGETS["at"]
            and (now - _VMS_TARGETS["at"]).total_seconds() < _VMS_TARGETS_TTL):
        return _VMS_TARGETS["items"]

    def fetch(cookie):
        data = urllib.parse.urlencode({"userId": GICOMS_VMS_ID}).encode()
        req = urllib.request.Request(
            f"{GICOMS_BASE}/WEB_VMS/WebVMS/allShipTarget.json", data=data,
            headers={"Cookie": cookie, "X-Requested-With": "XMLHttpRequest",
                     "Referer": f"{GICOMS_BASE}/WEB_VMS/WebVMS.do",
                     "User-Agent": "Mozilla/5.0",
                     "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.read().decode("utf-8", "replace")

    body = fetch(_vms_cookie(force=force_login))
    if not body.lstrip().startswith("{"):          # 세션 만료 → 재로그인 1회
        body = fetch(_vms_cookie(force=True))
    items = (json.loads(body) or {}).get("message") or []
    _VMS_TARGETS["items"], _VMS_TARGETS["at"] = items, now
    return items


def _norm_ship(s):
    s = str(s or "").upper().replace(" ", "")
    return s[:-1] if s.endswith("호") else s


_MMSI_MAP = {"at": None, "map": {}}


def _mmsi_map():
    """회사 권위 목록(선박명_MMSI.csv: 선박명,MMSI[,선박번호]) → {정규화선박명: MMSI}. 5분 캐시.
    한글명↔MMSI 확정 매핑이라 VMS 위치조회 정확도를 100%로 만든다(파일 없으면 빈 dict)."""
    path = os.environ.get("VESSEL_MMSI", os.path.join(BASE_DIR, "선박명_MMSI.csv"))
    now = time.time()
    if _MMSI_MAP["map"] and _MMSI_MAP["at"] and now - _MMSI_MAP["at"] < 300:
        return _MMSI_MAP["map"]
    m = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                nm = row.get("선박명") or row.get("선명")
                mm = row.get("MMSI") or row.get("mmsi")
                if nm and mm:
                    m[_norm_ship(nm)] = str(mm).strip()
    except FileNotFoundError:
        pass
    _MMSI_MAP["map"], _MMSI_MAP["at"] = m, now
    return m


def _vms_position(name=None, mmsi=None, force_login=False):
    """선박명 또는 MMSI → 실시간 위치 dict. 없으면 None.
    매칭: ① 권위목록으로 한글명→MMSI 해석 ② MMSI 정확일치
    ③ 목록에 없으면 선박명 정규화 정확일치 ④ '여객' 선종만 부분일치(화물선 오매칭 차단)."""
    items = _vms_all_targets(force_login=force_login)
    s = None
    if not mmsi and name:                          # 권위목록(CSV)으로 한글명→MMSI
        mmsi = _mmsi_map().get(_norm_ship(name))
    if mmsi:
        m = str(mmsi).strip()
        s = next((it for it in items if str(it.get("mmsi")) == m), None)
    if s is None and name:
        q = _norm_ship(name)
        if not q:
            return None
        exact = [it for it in items if _norm_ship(it.get("shipName")) == q]
        if exact:
            cands = exact
        else:   # 정확일치 없으면 '여객' 선종에 한해서만 부분일치 허용(엉뚱한 화물선 방지)
            cands = [it for it in items
                     if len(q) >= 3 and q in _norm_ship(it.get("shipName"))
                     and "여객" in str(it.get("shipType") or "")]
        if not cands:
            return None
        cands.sort(key=lambda it: ("여객" not in str(it.get("shipType") or ""),
                                   abs(len(_norm_ship(it.get("shipName"))) - len(q))))
        s = cands[0]
    if s is None:
        return None

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    lat, lon = num(s.get("latitude")), num(s.get("longitude"))
    cog, sog, hdg = num(s.get("cog")), num(s.get("sog")), num(s.get("heading"))
    # 원시 AIS 단위(×10) 보정: cog/sog는 0.1 단위, heading 511=미지정
    if cog is not None:
        cog = round(cog / 10, 1) if cog > 360 else cog
    if sog is not None:
        sog = round(sog / 10, 1) if sog > 102.2 else sog
    if hdg is not None and hdg >= 360:
        hdg = None
    return {
        "선박명": s.get("shipName"), "mmsi": s.get("mmsi"),
        "위도": lat, "경도": lon, "속력_kn": sog, "침로_deg": cog, "선수방위_deg": hdg,
        "수신시각": s.get("rcvDatetimeFormat"), "선종": s.get("shipType"),
        "항해상태": s.get("status"), "목적지": s.get("destination"),
        "여객선": "여객" in str(s.get("shipType") or "") or str(s.get("shipKind")) == "60",
    }


def _vms_position_safe(name):
    """보고 흐름용 graceful 래퍼 — 키 없거나 조회 실패 시 None(다른 처리에 영향 없음).
    콜드스타트/쿠키만료로 첫 호출(로그인·조회)이 실패하면 재로그인 후 1회 재시도."""
    if not (GICOMS_VMS_ID and GICOMS_VMS_PW and name):
        return None
    for attempt in range(2):
        try:
            return _vms_position(name, force_login=(attempt == 1))
        except Exception as exc:
            print(f"[vms] 실시간위치 조회 실패(시도 {attempt + 1}/2): {exc}", flush=True)
    return None


def _fmt_dm(lat, lon) -> str:
    """십진 위·경도 → 도-분 표기 'DD-MM.MN, DDD-MM.ME' (예: 34-18.4N, 126-07.8E)."""
    def one(v, pos, neg):
        h = pos if v >= 0 else neg
        v = abs(v)
        d = int(v)
        return f"{d}-{(v - d) * 60:04.1f}{h}"
    return f"{one(lat, 'N', 'S')}, {one(lon, 'E', 'W')}"


def _vms_line(vpos):
    """1차 속보용 '현재위치' 한 줄 (위·경도 도-분 표기)."""
    bits = []
    if vpos.get("속력_kn") is not None:
        bits.append(f"{vpos['속력_kn']}kn")
    if vpos.get("침로_deg") is not None:
        bits.append(f"침로 {vpos['침로_deg']}°")
    rel = _rel_position(vpos.get("위도"), vpos.get("경도"))   # 가까운 기준점 기준 사람이 읽을 상대위치
    head = _fmt_dm(vpos["위도"], vpos["경도"]) + (f" ({rel})" if rel else "")
    tail = (f" [{', '.join(bits)}]" if bits else "") + \
           (f" · {vpos.get('수신시각')}" if vpos.get("수신시각") else "")
    return f"▶ 현재위치: {head}{tail}"


@app.get("/vessel_position")
def vessel_position():
    """선박명 → GICOMS VMS 실시간 위치(lat/lon/속력/침로/수신시각)."""
    name = request.args.get("name", "").strip()
    mmsi = request.args.get("mmsi", "").strip()
    if not (name or mmsi):
        return jsonify({"error": "name 또는 mmsi 파라미터가 필요합니다"}), 400
    if not (GICOMS_VMS_ID and GICOMS_VMS_PW):
        return jsonify({"error": "GICOMS_VMS_ID/PW가 설정되지 않았습니다"}), 503
    try:
        pos = _vms_position(name or None, mmsi or None)
    except ImportError:
        return jsonify({"error": "playwright 미설치 — pip install playwright && "
                                 "python -m playwright install chromium"}), 503
    except Exception as exc:
        return jsonify({"error": f"VMS 조회 실패: {exc}"}), 502
    if pos is None:
        return jsonify({"error": "실시간 위치 없음(선박명 미일치 또는 AIS 미수신)"}), 404
    return jsonify(pos)


# ── 진입점 ──────────────────────────────────────────

if __name__ == "__main__":
    missing = [k for k in ("KOMSA_KEY", "KMA_KEY") if not os.environ.get(k)]
    if missing:
        print(f"[오류] .env에 다음 필수 키가 없습니다: {', '.join(missing)}")
        raise SystemExit(1)
    print(f"백엔드 실행 중 → http://localhost:{PORT}")
    print("종료: Ctrl+C")
    app.run(host="0.0.0.0", port=PORT, debug=False)
