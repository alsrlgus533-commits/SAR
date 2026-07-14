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
from concurrent.futures import ThreadPoolExecutor
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
    status = d.pop("_status", 200)   # 내부 상태코드는 응답 본문에서 제거 후 사용
    return jsonify(d), status


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
        "화물적재한도": str(o.get("frghtLoadngLmtWtDc", "")),   # 운항관리규정상 적재한도(M/T)
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


# ── 회사 실시간 여객수(수동 입력) ─────────────────────────────────────
# MTIS 출항전 점검표는 '출항 1회 스냅샷'이라 중간 기항지 승하선/차량 승하차가
# 반영되지 않거나 일부 선박은 값이 비어 있다. 회사 담당자가 외부망 PC의
# /pax/send 페이지에서 현재 여객수를 보내면 여기 저장하고, 보고서에서
# (신선하면) MTIS보다 우선 사용한다. 폴링 데몬 없음 — 사람 손 갱신뿐이라
# 회사 PC에 상주 부하가 없고, 받는 쪽도 사람 손 속도라 가볍다.
PAX_TOKEN = os.environ.get("PAX_TOKEN", "")                                   # 전송 인증 토큰(선택, 권장)
PAX_STORE = os.environ.get("PAX_STORE", os.path.join(BASE_DIR, "pax_store.json"))  # 영속화 파일
PAX_TTL = int(os.environ.get("PAX_TTL", 64800))                              # '신선' 기준(초). 기본 18시간

_PAX = {}                       # canonical_key -> entry
_PAX_LOCK = threading.Lock()
_PAX_FIELDS = ("여객", "대인", "소인", "유아", "승무원", "차량", "화물")   # 화물=실적재중량(M/T)


def _pax_norm(name: str) -> str:
    """선박명 정규화(공백·끝'호' 제거, 대문자) — 한글/영문 키 매칭용."""
    s = re.sub(r"\s+", "", str(name or "")).upper()
    return re.sub(r"호$", "", s)


def _fmt_mt(s):
    """화물 중량 표기 정리: 12.0→'12', 12.50→'12.5', 비수치는 원문."""
    try:
        return f"{float(s):,.1f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(s).strip()


def _pax_load():
    try:
        with open(PAX_STORE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _PAX.update(data)
    except (OSError, ValueError):
        pass


def _pax_save():
    try:
        tmp = PAX_STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_PAX, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PAX_STORE)                  # 원자적 교체(쓰다 깨져도 기존 파일 보존)
    except OSError:
        pass


def _pax_key(cd: str, name: str) -> str:
    cd = (cd or "").strip()
    return cd if cd else "@" + _pax_norm(name)


def _pax_set(cd, name, fields, memo=""):
    """현재 여객수 저장(타임스탬프 갱신). 제공된 항목만 정수로 보관."""
    entry = {"선박코드": (cd or "").strip(), "선박명": (name or "").strip(),
             "메모": (memo or "").strip(), "ts": time.time()}
    for k in _PAX_FIELDS:
        v = fields.get(k)
        if v is not None and str(v).strip() != "":
            try:
                if k == "화물":                                   # 화물은 M/T 소수 보존('12.5 M/T' 등 단위 허용)
                    m = re.search(r"-?\d[\d,]*\.?\d*", str(v))
                    if m:
                        fv = float(m.group(0).replace(",", ""))
                        entry[k] = int(fv) if fv == int(fv) else fv
                else:
                    entry[k] = int(float(v))
            except (TypeError, ValueError):
                pass
    with _PAX_LOCK:
        _PAX[_pax_key(cd, name)] = entry
        _pax_save()
    return entry


def _pax_lookup(cd: str = "", name: str = ""):
    """회사 실시간 여객수 — 신선(PAX_TTL 이내)한 최신 항목만 반환, 없으면 None.
    선박코드 정확일치 또는 정규화 선박명 일치로 찾는다."""
    cd = (cd or "").strip()
    nn = _pax_norm(name)
    now = time.time()
    best = None
    with _PAX_LOCK:
        for e in _PAX.values():
            if now - e.get("ts", 0) > PAX_TTL:
                continue
            if (cd and e.get("선박코드") == cd) or (nn and _pax_norm(e.get("선박명")) == nn):
                if best is None or e["ts"] > best["ts"]:
                    best = e
    return dict(best) if best else None


def _pax_auth_ok() -> bool:
    if not PAX_TOKEN:
        return True
    body = request.get_json(silent=True) or request.form
    provided = request.headers.get("X-Pax-Token") or str((body or {}).get("token") or "")
    return secrets.compare_digest(provided, PAX_TOKEN)


@app.post("/pax")
def pax_submit():
    """회사 담당자가 현재 여객수를 전송(수동 입력). JSON 또는 폼 모두 허용."""
    if not _pax_auth_ok():
        return jsonify({"error": "인증 실패(token)"}), 401
    body = request.get_json(silent=True) or request.form.to_dict() or {}
    cd = str(body.get("psnshpCd") or body.get("선박코드") or "").strip()
    name = str(body.get("name") or body.get("선박명") or "").strip()
    if not cd and not name:
        return jsonify({"error": "선박명 또는 선박코드가 필요합니다"}), 400
    fields = {
        "여객": body.get("여객", body.get("pax")),
        "대인": body.get("대인", body.get("adult")),
        "소인": body.get("소인", body.get("child")),
        "유아": body.get("유아", body.get("infant")),
        "승무원": body.get("승무원", body.get("crew")),
        "차량": body.get("차량", body.get("vehicle")),
        "화물": body.get("화물", body.get("cargo")),      # 실적재중량 M/T
    }
    if all(v is None or str(v).strip() == "" for v in fields.values()):
        return jsonify({"error": "여객수 등 값이 최소 하나는 필요합니다"}), 400
    entry = _pax_set(cd, name, fields, str(body.get("메모") or body.get("memo") or ""))
    return jsonify({"ok": True, "saved": entry})


@app.get("/pax")
def pax_list():
    """저장된 현재 여객수 목록(신선도 포함). 토큰 설정 시 인증 필요."""
    if not _pax_auth_ok():
        return jsonify({"error": "인증 실패(token)"}), 401
    now = time.time()
    out = []
    with _PAX_LOCK:
        for e in _PAX.values():
            age = int(now - e.get("ts", 0))
            out.append({**{k: v for k, v in e.items() if k != "ts"},
                        "갱신경과초": age, "신선": age <= PAX_TTL})
    out.sort(key=lambda x: x["갱신경과초"])
    return jsonify({"ttl초": PAX_TTL, "items": out})


@app.get("/pax/send")
def pax_send_page():
    """회사용 여객수 전송 페이지(단일 HTML). 토큰은 브라우저에 저장."""
    return Response(_PAX_SEND_HTML, mimetype="text/html; charset=utf-8")


_PAX_SEND_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>현재 여객수 전송</title>
<style>
 body{font-family:system-ui,'Malgun Gothic',sans-serif;max-width:520px;margin:24px auto;padding:0 16px;color:#1f2937}
 h1{font-size:20px} label{display:block;margin:10px 0 4px;font-size:14px;font-weight:600}
 input{width:100%;box-sizing:border-box;padding:9px;border:1px solid #cbd5e1;border-radius:8px;font-size:15px}
 .row{display:flex;gap:8px}.row>div{flex:1}
 button{margin-top:16px;width:100%;padding:12px;border:0;border-radius:8px;background:#2563eb;color:#fff;font-size:16px;font-weight:700;cursor:pointer}
 button:active{background:#1d4ed8}
 .hint{color:#6b7280;font-size:12px;margin-top:4px}
 #msg{margin-top:14px;padding:10px;border-radius:8px;display:none;font-size:14px}
 .ok{background:#ecfdf5;color:#065f46}.err{background:#fef2f2;color:#991b1b}
 table{width:100%;border-collapse:collapse;margin-top:22px;font-size:13px}
 th,td{border-bottom:1px solid #e5e7eb;padding:6px 4px;text-align:left}
 .stale{color:#9ca3af} h2{font-size:15px;margin-top:26px}
</style></head><body>
<h1>현재 여객수 전송</h1>
<p class="hint">중간 기항지에서 승하선/승하차로 인원이 바뀌면 이 화면에서 갱신해 주세요. 사고 시 보고서에 자동 반영됩니다.</p>
<label>선박명 *</label><input id="name" placeholder="예) 섬사랑12호" autocomplete="off">
<label>선박코드 <span class="hint">(알면 입력 — 더 정확)</span></label><input id="cd" placeholder="KOMSA 선박코드(선택)" autocomplete="off">
<div class="row">
 <div><label>여객(계)</label><input id="pax" type="number" inputmode="numeric" min="0"></div>
 <div><label>승무원</label><input id="crew" type="number" inputmode="numeric" min="0"></div>
 <div><label>차량</label><input id="veh" type="number" inputmode="numeric" min="0"></div>
</div>
<div class="row">
 <div><label>대인</label><input id="adult" type="number" inputmode="numeric" min="0"></div>
 <div><label>소인</label><input id="child" type="number" inputmode="numeric" min="0"></div>
 <div><label>유아</label><input id="infant" type="number" inputmode="numeric" min="0"></div>
</div>
<label>화물 <span class="hint">(실적재중량 M/T — 소수 가능)</span></label><input id="cargo" type="number" inputmode="decimal" min="0" step="0.1">
<label>메모 <span class="hint">(예: ○○항 출항 후)</span></label><input id="memo" placeholder="선택">
<label>전송 토큰 <span class="hint">(관리자에게 받은 값 — 1회 입력 후 저장됨)</span></label><input id="token" type="password" autocomplete="off">
<button id="send">전송</button>
<div id="msg"></div>
<h2>현재 저장된 값</h2>
<table id="cur"><thead><tr><th>선박</th><th>여객</th><th>승무원</th><th>차량</th><th>화물</th><th>경과</th></tr></thead><tbody></tbody></table>
<script>
const $=id=>document.getElementById(id);
$("token").value=localStorage.getItem("paxToken")||"";
function show(t,ok){const m=$("msg");m.textContent=t;m.className=ok?"ok":"err";m.style.display="block";}
function val(id){const v=$(id).value.trim();return v===""?undefined:Number(v);}
async function refresh(){
 try{const r=await fetch("/pax",{headers:{"X-Pax-Token":$("token").value}});
  if(!r.ok)return;const d=await r.json();
  const tb=$("cur").querySelector("tbody");tb.innerHTML="";
  (d.items||[]).forEach(it=>{const tr=document.createElement("tr");if(!it.신선)tr.className="stale";
   const ag=it.갱신경과초<3600?Math.round(it.갱신경과초/60)+"분 전":Math.round(it.갱신경과초/3600)+"시간 전";
   tr.innerHTML=`<td>${it.선박명||it.선박코드||"-"}</td><td>${it.여객??"-"}</td><td>${it.승무원??"-"}</td><td>${it.차량??"-"}</td><td>${it.화물??"-"}</td><td>${ag}</td>`;
   tb.appendChild(tr);});
 }catch(e){}
}
$("send").onclick=async()=>{
 const name=$("name").value.trim();
 if(!name&&!$("cd").value.trim()){show("선박명을 입력하세요",false);return;}
 const token=$("token").value.trim();localStorage.setItem("paxToken",token);
 const body={name,선박코드:$("cd").value.trim(),token,
  여객:val("pax"),승무원:val("crew"),차량:val("veh"),화물:val("cargo"),
  대인:val("adult"),소인:val("child"),유아:val("infant"),메모:$("memo").value.trim()};
 try{const r=await fetch("/pax",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d=await r.json();
  if(r.ok){show("✅ 전송 완료 — "+(d.saved.선박명||d.saved.선박코드)+" 여객 "+(d.saved.여객??"-")+"명",true);refresh();}
  else show("❌ "+(d.error||"전송 실패"),false);
 }catch(e){show("❌ 네트워크 오류: "+e.message,false);}
};
refresh();setInterval(refresh,30000);
</script></body></html>"""


_pax_load()


# ── /parse ──────────────────────────────────────────

PARSE_PROMPT = (
    "다음은 여객선 해양사고 보고자의 자유 입력입니다. "
    "핵심 정보를 추출해 JSON으로만 응답하세요. 마크다운·설명 없이 순수 JSON만 출력합니다.\n"
    "키: 사고일시(YYYY-MM-DD HH:MM, 모르면 빈문자열), 선박명(\"호\"까지 포함), "
    "사고위치(좌표·지명 포함), 여객(숫자만), 승무원(숫자만), 사고개요(한 문장).\n"
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
    with urllib.request.urlopen(req, timeout=12) as resp:   # 보고서 tail latency 제한(초과 시 규칙 폴백)
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
    with urllib.request.urlopen(req, timeout=12) as resp:   # 보고서 tail latency 제한(초과 시 규칙 폴백)
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


@app.post("/parse")
def parse_text():
    body = request.get_json(force=True, silent=True) or {}
    text = str(body.get("text", "")).strip()
    if not text:
        return jsonify({"error": "text 필드가 필요합니다"}), 400
    if not (GEMINI_KEY or ANTHROPIC_KEY):
        return jsonify({"error": "GEMINI_KEY 또는 ANTHROPIC_KEY가 설정되지 않았습니다"}), 503

    # Gemini 우선, 실패(429 등) 시 Claude 폴백 — 둘 다 실패해야 502 (_parse_nl과 동일한 체인)
    last_exc = None
    for fn, key in ((_gemini_parse, GEMINI_KEY), (_claude_parse, ANTHROPIC_KEY)):
        if not key:
            continue
        try:
            raw = fn(text).replace("```json", "").replace("```", "").strip()
            return jsonify(json.loads(raw))
        except Exception as exc:
            last_exc = exc
    return jsonify({"error": str(last_exc)}), 502


# ── 자연어 파싱(서버측) — LLM 시도 후 규칙 기반 폴백 ──────────


def _extract_accident_datetime(text: str, now: datetime = None):
    """신고문/확정값에서 실제 사고 일시를 KST datetime으로 추출한다.

    연도가 없으면 현재 연도, 날짜가 없고 시각만 있으면 사고 당일을 오늘로 두되
    최종 보고서 생성 전 검토 화면에서 사용자가 반드시 확인·수정한다.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    kst = timezone(timedelta(hours=9))
    base = now.astimezone(kst) if now else datetime.now(kst)

    # 웹 datetime-local 및 LLM 표준 출력.
    m = re.search(r"(20\d{2})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})(?:일)?"
                  r"(?:[T\s]+)(\d{1,2})(?::|시\s*)(\d{1,2})(?:분)?", raw)
    if m:
        try:
            return datetime(*(int(x) for x in m.groups()), tzinfo=kst)
        except ValueError:
            return None

    # 연도 없는 한국어/숫자 날짜 + 시각.
    m = re.search(r"(?<!\d)(\d{1,2})(?:월|[-./])\s*(\d{1,2})(?:일)?"
                  r"(?:[T\s]+)(\d{1,2})(?::|시\s*)(\d{1,2})(?:분)?", raw)
    if m:
        try:
            month, day, hour, minute = (int(x) for x in m.groups())
            return datetime(base.year, month, day, hour, minute, tzinfo=kst)
        except ValueError:
            return None

    # '오늘/금일/어제 14:20', 또는 신속보고에서 흔한 '14시 20분경/14:20경'.
    m = re.search(r"(?:(오늘|금일|어제)\s*)?(?<!\d)([01]?\d|2[0-3])(?::|시\s*)([0-5]\d)(?:분)?", raw)
    if m:
        day_word, hour, minute = m.groups()
        day = base - timedelta(days=1) if day_word == "어제" else base
        return day.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    return None


def _accident_iso(value: str) -> str:
    dt = _extract_accident_datetime(value)
    return dt.strftime("%Y-%m-%dT%H:%M") if dt else ""


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
    accident_dt = _extract_accident_datetime(text)
    accident_at = accident_dt.strftime("%Y-%m-%d %H:%M") if accident_dt else ""
    return {"사고일시": accident_at, "선박명": ship, "사고위치": loc,
            "여객": pax, "승무원": crew, "사고개요": summary}


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


def _hav_nm(lat1, lon1, lat2, lon2):
    """두 점 거리(해리)."""
    from math import radians, sin, cos, asin, sqrt
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 3440.065 * asin(sqrt(a))


def _bearing(lat1, lon1, lat2, lon2):
    """lat1,lon1 → lat2,lon2 초기 방위각(도)."""
    from math import radians, sin, cos, atan2, pi
    dlon = radians(lon2 - lon1)
    y = sin(dlon) * cos(radians(lat2))
    x = cos(radians(lat1)) * sin(radians(lat2)) - sin(radians(lat1)) * cos(radians(lat2)) * cos(dlon)
    return (atan2(y, x) * 180 / pi + 360) % 360


def _port_label(name: str) -> str:
    """기점(기항지)명을 '○○항' 표기로. 괄호(접안지 세부)는 떼고, 이미 항/등대/방파제면 그대로."""
    base = re.sub(r"\s*\(.*?\)", "", name).strip() or name
    if "항" in base or base.endswith("등대") or base.endswith("방파제"):
        return base
    return base + "항"


def _rel_position_detail(lat, lon):
    """사고 좌표 → 가장 가까운 여객선 기항지(항구) 기준 상대위치 dict.
    이름은 '○○항', 거리·방위는 그 항구 지점에서 계산. 좌표 없으면 None."""
    if lat is None or lon is None:
        return None
    best = None
    for name, rlat, rlon in _REF_POINTS:
        d = _hav_nm(lat, lon, rlat, rlon)
        if best is None or d < best[0]:
            best = (d, name, rlat, rlon)
    if best is None:
        return None
    d, name, rlat, rlon = best
    brg = _bearing(rlat, rlon, lat, lon)
    label = _port_label(name)
    dist_txt = f"{d:.1f}" if d < 10 else str(round(d))
    return {"name": label, "거리": round(d, 2), "방위": round(brg),
            "dir8": _DIR8[round(brg / 45) % 8],
            "text": f"{label} {_DIR8[round(brg / 45) % 8]}쪽 {dist_txt}마일"}


def _rel_position(lat, lon) -> str:
    """사고 좌표 → '○○항 ○쪽 N마일'(가장 가까운 기항지 기준). 좌표 없으면 ''."""
    d = _rel_position_detail(lat, lon)
    return d["text"] if d else ""


@app.get("/relpos")
def relpos():
    """사고 좌표의 상대위치(가장 가까운 기항지 '○○항' 기준). 프론트/카카오 공용."""
    try:
        lat = float(request.args["lat"]); lon = float(request.args["lon"])
    except (KeyError, ValueError):
        return jsonify({"error": "lat, lon 파라미터가 필요합니다"}), 400
    d = _rel_position_detail(lat, lon)
    if not d:
        return jsonify({"error": "계산 불가"}), 422
    return jsonify(d)


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


def _build_report_text(utterance: str, kakao_received_at: str = "") -> str:
    """사고 자유텍스트 → 1차(속보) 보고서 텍스트.

    카카오 경로는 첫 사고 메시지 수신시각을 넘겨 신고문 속 시각보다 우선한다.
    """
    parsed = _parse_nl(utterance)
    ship = str(parsed.get("선박명") or "").strip()
    loc = str(parsed.get("사고위치") or "").strip()
    pax = str(parsed.get("여객") or "").strip()
    crew = str(parsed.get("승무원") or "").strip()
    summary = str(parsed.get("사고개요") or "").strip()
    accident_dt = (_extract_accident_datetime(kakao_received_at)
                   or _extract_accident_datetime(parsed.get("사고일시") or utterance))

    lat, lon = _extract_latlon(loc)

    # 외부 조회 병렬 실행(_build_report_data와 동일 패턴) — 순차 합산 지연을 최댓값으로 단축.
    # 의존관계: predep ← vessel(선박코드), weather ← 최종 좌표(신고 or VMS), 나머지는 독립.
    def _try(fn, *a):
        try:
            return fn(*a)
        except Exception:
            return None

    vessel = route_info = mtis = vpos = None
    cd = ""
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_vessel = ex.submit(_try, _vessel_lookup, ship) if ship else None
        f_route = ex.submit(_try, _route_lookup, ship) if ship else None
        f_vpos = ex.submit(_vms_position_safe, ship) if ship else None

        vessel = f_vessel.result() if f_vessel else None
        # MTIS 출항전 점검표 — 실제 승선인원·화물 (KOMSA 선박코드 필요)
        cd = (vessel or {}).get("선박코드", "")
        f_predep = ex.submit(_try, _predep_lookup, cd) if cd else None

        vpos = f_vpos.result() if f_vpos else None
        if lat is None and vpos and vpos.get("위도") is not None:   # 신고 좌표 없으면 AIS 현위치로 기상조회
            lat, lon = vpos["위도"], vpos["경도"]
        f_wx = ex.submit(_weather_lookup, loc, "" if lat is None else str(lat),
                         "" if lon is None else str(lon))   # 최종 좌표 확정 후 제출(VMS 결과 반영)

        route_info = f_route.result() if f_route else None
        mtis = f_predep.result() if f_predep else None
        wx = f_wx.result()
    have_coord = lat is not None
    if wx.get("error"):
        wx = None

    kst = timezone(timedelta(hours=9))
    occurred = accident_dt.strftime("%Y-%m-%d %H:%M") if accident_dt else "확인 필요"

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
    L.append(f"▶ 발생: {occurred}")

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

    # 승선·화물 — 회사 실시간(신선) > MTIS 출항전 점검표(실제) > 보고자 입력값
    pax_ovr = _pax_lookup(cd, ship)
    if pax_ovr:
        bd = "·".join(f"{lbl} {pax_ovr[k]}" for lbl, k in
                      (("성인", "대인"), ("소아", "소인"), ("유아", "유아")) if pax_ovr.get(k) is not None)
        detail = f"({bd})" if bd else ""
        pax_v = pax_ovr.get("여객")
        crew_v = pax_ovr.get("승무원")
        if crew_v is None and mtis:
            crew_v = mtis.get("승무원")
        L.append(f"▶ 승선: 여객 {pax_v if pax_v is not None else (pax or '?')}명{detail}, "
                 f"선원 {crew_v if crew_v is not None else (crew or '?')}명")
        veh = pax_ovr.get("차량")
        if veh is None and mtis:
            veh = mtis.get("차량")
        cargo = pax_ovr.get("화물")                       # 회사 실시간 실적재중량 우선
        cargo_rt = cargo is not None
        if not cargo_rt:
            cargo = (mtis or {}).get("화물적재중량", "")
        lmt = (mtis or {}).get("화물적재한도", "")
        cargo_txt = " · ".join(x for x in (
            f"적재 {_fmt_mt(cargo)} M/T" if cargo not in ("", None) else "",
            f"적재한도 {lmt} M/T" if lmt else "",
            f"차량 {veh}대" if veh else "",
        ) if x)
        if cargo_txt:
            L.append(f"▶ 화물: {cargo_txt}")
    elif mtis:
        detail = f"(성인 {mtis['대인']}·소아 {mtis['소인']}·유아 {mtis['유아']})"
        tmp = f", 임시승선자 {mtis['임시승선자']}명" if mtis.get("임시승선자") else ""
        L.append(f"▶ 승선: 여객 {mtis['여객']}명{detail}, 선원 {mtis['승무원']}명{tmp} "
                 f"(실승선 계 {mtis['실제승선인원']}명)")
        cargo, lmt, veh = mtis.get("화물적재중량", ""), mtis.get("화물적재한도", ""), mtis.get("차량", 0)
        cargo_txt = " · ".join(x for x in (
            f"적재 {cargo} M/T" if cargo else "",
            f"적재한도 {lmt} M/T" if lmt else "",
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
    {"label": "🧹 부유물 제거 보고서", "action": "message", "messageText": "부유물 제거 보고서"},
    {"label": "📤 관계기관 전송", "action": "message", "messageText": "관계기관 전송"},
]


def _is_debris_incident(*texts: str) -> bool:
    """부유물 감김·추진기 이물질 유입 사고인지 보수적으로 판별한다."""
    blob = " ".join(str(text or "") for text in texts)
    return bool(re.search(r"부유물|폐그물|폐로프|감김|감겨|이물질.*(?:추진기|프로펠러|스크류)|(?:추진기|프로펠러|스크류).*이물질",
                          blob, re.I))


def _kakao_report(text: str) -> dict:
    """보고서 simpleText + 하단 바로가기 버튼(개요·조치사항 수정·전송)."""
    outputs = [{"simpleText": {"text": text}}]
    # 콜백 응답에서 quickReplies가 접혀 보이는 카카오 클라이언트도 있어,
    # 부유물 사고는 본문 바로 아래에 명시적인 서식 선택 카드를 함께 보낸다.
    if _is_debris_incident(text):
        outputs.append({"textCard": {
            "title": "📋 보고서 서식 선택",
            "description": "작성할 보고서 서식을 선택해 주세요.",
            "buttons": [
                {"action": "message", "label": "정식 보고서", "messageText": "정식 보고서"},
                {"action": "message", "label": "부유물 제거 보고서", "messageText": "부유물 제거 보고서"},
            ],
        }})
    return {"version": "2.0", "template": {
        "outputs": outputs,
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


def _kakao_callback(callback_url: str, utterance: str, uid: str = "anon", prefix: str = "",
                    accident_at: str = ""):
    """백그라운드: 보고서 작성 후 카카오 콜백 URL로 결과 전송. prefix는 복구 안내 등 1회성 머리말."""
    try:
        text = _build_report_text(utterance, accident_at)
    except Exception as exc:
        text = f"보고서 자동작성 중 오류가 발생했습니다: {exc}"
    _session_set(uid, report=text, utterance=utterance, accident_at=accident_at, mode=None,
                 confirmed=None, pending_fields=[], report_kind=None)  # 새 사고는 이전 확정값을 폐기
    _post_callback(callback_url, _kakao_report((prefix + text) if prefix else text))


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
    kakao_received_at = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
    print(f"[kakao] 요청 수신 utterance={utterance!r} callbackUrl={'있음' if callback_url else '없음(콜백 미전달)'}", flush=True)
    if not utterance:
        return jsonify(_kakao_text(
            "사고 내용을 한 문장으로 입력해 주세요.\n"
            "예) 섬사랑12호 추자도 북동방 2해리, 여객 28명 승무원 4명, 폐그물 감김"))

    # 정식/부유물 제거 보고서 필수정보를 한 항목씩 확인하는 대화 모드.
    if sess.get("mode") == "confirm_report_field" and sess.get("pending_fields"):
        pending = list(sess["pending_fields"])
        key = pending[0]
        value = _normalize_confirm_answer(key, utterance)
        if not value:
            return jsonify(_kakao_text("입력 형식을 확인해 주세요.\n" + _kakao_confirm_question(key, len(pending))))
        confirmed = dict(sess.get("confirmed") or {})
        confirmed[key] = value
        pending = pending[1:]
        if pending:
            _session_set(uid, confirmed=confirmed, pending_fields=pending, mode="confirm_report_field")
            return jsonify(_kakao_text(_kakao_confirm_question(pending[0], len(pending))))

        src_utt = sess.get("utterance") or ""
        base = _public_base()
        report_kind = sess.get("report_kind") or "formal"
        _session_set(uid, confirmed=confirmed, pending_fields=[], mode=None, report_kind=None)
        if callback_url:
            target = _kakao_debris_hwpx_callback if report_kind == "debris" else _kakao_hwpx_callback
            args = ((callback_url, uid, src_utt, base, confirmed, sess.get("report") or "")
                    if report_kind == "debris" else
                    (callback_url, uid, src_utt, base, confirmed))
            threading.Thread(target=target, args=args, daemon=True).start()
            label = "부유물 제거 조치사항 보고서" if report_kind == "debris" else "정식 보고서"
            return jsonify({"version": "2.0", "useCallback": True,
                            "data": {"text": f"✅ 필수정보 확인 완료. {label}(hwpx)를 작성 중입니다…"}})
        try:
            if report_kind == "debris":
                return jsonify(_kakao_debris_hwpx_message(
                    src_utt, base, confirmed=confirmed, first_report=sess.get("report") or ""))
            return jsonify(_kakao_hwpx_message(src_utt, base, confirmed=confirmed))
        except Exception as exc:
            return jsonify(_kakao_text(f"보고서(hwpx) 생성 중 오류가 발생했습니다: {exc}"))

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
            # 외부 API로 확정 후보를 준비하는 작업도 수초~수십초 걸릴 수 있으므로
            # 카카오 5초 제한 밖의 콜백 스레드에서 수행한다.
            threading.Thread(target=_kakao_prepare_hwpx_callback,
                             args=(callback_url, uid, src_utt, base,
                                   sess.get("report"), sess.get("confirmed")), daemon=True).start()
            return jsonify({"version": "2.0", "useCallback": True,
                            "data": {"text": "📋 정식 보고서 필수정보를 확인 중입니다… 잠시만 기다려 주세요."}})
        confirmed = _prepare_report_confirmation(src_utt, sess.get("report"), sess.get("confirmed"))
        pending = _pending_report_keys(confirmed)
        if pending:
            _session_set(uid, confirmed=confirmed, pending_fields=pending,
                         mode="confirm_report_field", report_kind="formal")
            return jsonify(_kakao_text(_kakao_confirm_question(pending[0], len(pending))))
        try:
            return jsonify(_kakao_hwpx_message(src_utt, base, confirmed=confirmed))
        except Exception as exc:
            return jsonify(_kakao_text(f"정식 보고서(hwpx) 생성 중 오류가 발생했습니다: {exc}"))

    # ②-c [부유물 제거 보고서] 버튼 → 산타모니카호 참고 서식의 전용 조치사항 보고서 생성
    if utterance in ("부유물 제거 보고서", "부유물 제거 조치사항", "부유물 제거 조치사항 보고서"):
        src_utt = sess.get("utterance")
        first_report = sess.get("report") or ""
        if not src_utt:
            return jsonify(_kakao_text("먼저 사고 내용을 입력해 주세요. 1차 보고서를 만든 뒤 부유물 제거 조치사항 보고서를 받을 수 있습니다."))
        if not _is_debris_incident(src_utt, first_report):
            return jsonify(_kakao_text("부유물 제거 조치사항 보고서는 부유물·폐그물 감김 또는 추진기 이물질 유입 사고에서 선택할 수 있습니다."))
        base = _public_base()
        if callback_url:
            threading.Thread(target=_kakao_prepare_debris_hwpx_callback,
                             args=(callback_url, uid, src_utt, base, first_report,
                                   sess.get("confirmed")), daemon=True).start()
            return jsonify({"version": "2.0", "useCallback": True,
                            "data": {"text": "📋 부유물 제거 조치사항 보고서 필수정보를 확인 중입니다… 잠시만 기다려 주세요."}})
        confirmed = _prepare_report_confirmation(src_utt, first_report, sess.get("confirmed"))
        pending = _pending_report_keys(confirmed)
        if pending:
            _session_set(uid, confirmed=confirmed, pending_fields=pending,
                         mode="confirm_report_field", report_kind="debris")
            return jsonify(_kakao_text(_kakao_confirm_question(pending[0], len(pending))))
        try:
            return jsonify(_kakao_debris_hwpx_message(
                src_utt, base, confirmed=confirmed, first_report=first_report))
        except Exception as exc:
            return jsonify(_kakao_text(f"부유물 제거 조치사항 보고서(hwpx) 생성 중 오류가 발생했습니다: {exc}"))

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

    # ④ 새 사고 — 외부 API 건강검진 기반 가용성 안내(장애면 차단, 복구 후 첫 사용이면 정상 안내)
    _health_maybe_refresh()                       # 건강상태가 오래됐으면 백그라운드 갱신(논블로킹)
    _down = _health_down_critical()
    if _down:
        _session_set(uid, outage_seen=True)
        return jsonify(_kakao_text(
            "⚠️ 현재 외부 연계 시스템(" + "·".join(_down) + ") 장애로\n"
            "신속보고 자동작성을 일시적으로 사용할 수 없습니다.\n"
            "잠시 후 다시 시도해 주세요. (복구되면 정상 이용 안내가 표시됩니다)"))
    prefix = _RECOVER_MSG if sess.get("outage_seen") else ""   # 장애를 겪었던 사용자에게 1회 복구 안내
    if prefix:
        _session_set(uid, outage_seen=False)

    # ④-a 콜백(비동기) 처리
    if callback_url:
        _session_set(uid, accident_at=kakao_received_at, mode=None,
                     confirmed=None, pending_fields=[], report_kind=None)
        threading.Thread(target=_kakao_callback,
                         args=(callback_url, utterance, uid, prefix, kakao_received_at), daemon=True).start()
        return jsonify({
            "version": "2.0",
            "useCallback": True,
            "data": {"text": "🚨 사고 정보를 분석 중입니다… 잠시만 기다려 주세요."},
        })
    # ④-b 콜백 미설정 폴백: 동기 처리(외부 API 지연 시 5초 초과 가능)
    try:
        text = _build_report_text(utterance, kakao_received_at)
        _session_set(uid, report=text, utterance=utterance, accident_at=kakao_received_at, mode=None,
                     confirmed=None, pending_fields=[], report_kind=None)
        return jsonify(_kakao_report(prefix + text if prefix else text))
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

_ACCIDENT_TYPE_LABELS = (
    "충돌", "접촉", "좌초", "전복", "화재", "폭발", "침몰", "행방불명", "기관손상",
    "추진축계손상", "조타장치손상", "속구손상", "침수", "부유물감김", "운항저해",
    "해양오염", "안전사고", "기타",
)

# 중앙해양안전심판원 분류에 따른 사고 원인 23종. 보고서에는 분류번호 없이 명칭만 표기한다.
_ACCIDENT_CAUSE_LABELS = (
    "선장업무 소홀(조선미숙 등)", "견시 소홀", "정비점검 소홀", "당직근무 소홀",
    "부적절한 충돌회피(조선)", "무리한 운항(기상)", "선위 부정확", "근접 항해",
    "추월 위반", "안전업무 소홀", "선저 파공(누수)", "기관계통 고장", "전기계통 고장",
    "항해계기 고장", "선박속구 고장", "기상악화", "해상 부유물",
    "외부 발화원(차량/화물)", "지병", "과로", "자살(추정)", "여객 부주의", "기타",
)

_ACCIDENT_CAUSE_PATTERNS = (
    (r"선저\s*파공|누수", "선저 파공(누수)"),
    (r"해상\s*부유물|부유물|폐그물|폐로프|로프.*감|감김|감겨", "해상 부유물"),
    (r"외부\s*발화|차량.*화재|화물.*화재", "외부 발화원(차량/화물)"),
    (r"전기|배전|발전기|축전지", "전기계통 고장"),
    (r"항해계기|레이더|GPS|AIS|나침반", "항해계기 고장"),
    (r"선박속구|속구", "선박속구 고장"),
    (r"기관계통|기관|엔진|주기관|보조기관|펌프|추진축|축계|추진기", "기관계통 고장"),
    (r"기상악화|황천|풍랑|태풍", "기상악화"),
    (r"무리한\s*운항", "무리한 운항(기상)"),
    (r"충돌회피|피항.*부적절|부적절.*조선", "부적절한 충돌회피(조선)"),
    (r"선위.*부정확|위치.*오인", "선위 부정확"),
    (r"근접\s*항해", "근접 항해"),
    (r"추월.*위반", "추월 위반"),
    (r"정비.*소홀|점검.*소홀|정비점검", "정비점검 소홀"),
    (r"견시.*소홀|전방주시.*소홀", "견시 소홀"),
    (r"당직.*소홀", "당직근무 소홀"),
    (r"선장.*소홀|조선미숙", "선장업무 소홀(조선미숙 등)"),
    (r"안전업무.*소홀|안전조치.*소홀", "안전업무 소홀"),
    (r"여객.*부주의", "여객 부주의"),
    (r"자살", "자살(추정)"),
    (r"과로", "과로"),
    (r"지병|질병", "지병"),
)


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


# ── KOMSA 공개 여객선 사진 (선박마스터에 사진이 없을 때 폴백) ──────
# www.komsa.or.kr '여객선 정보' 목록(sub03_0204)은 선명별 사진을 공개한다.
# searchKeyword=선명 으로 검색 → 목록 li의 썸네일 src(/thumbnail/psnShip/300_PS_*)에서
# '300_' 접두어를 떼면 원본 고해상도 이미지를 받을 수 있다. 회사 선박마스터에 사진이
# 없을 때만 폴백으로 사용(데이터 우선순위: 회사 선박마스터 > KOMSA 공개사진).
_KOMSA_PHOTO_LIST = "https://www.komsa.or.kr/prog/psnShip/kor/sub03_0204/list.do"
_KOMSA_PHOTO_BASE = "https://www.komsa.or.kr"
_KOMSA_PHOTO_CACHE: dict = {}            # 선박명 → {"path": str|None, "at": float}
_KOMSA_PHOTO_TTL = 3600                  # 1시간


def _komsa_vessel_photo(name: str) -> str:
    """KOMSA 공개 여객선 목록에서 선명으로 사진을 받아 임시파일 경로 반환(못 찾으면 "").
    1시간 메모리 캐시(내려받은 임시파일 경로 보관). 네트워크/매칭 실패 시 graceful 빈 문자열."""
    import time
    name = (name or "").strip()
    if not name:
        return ""
    now = time.time()
    c = _KOMSA_PHOTO_CACHE.get(name)
    if c and (now - c["at"]) < _KOMSA_PHOTO_TTL:
        p = c["path"]
        return p if (p and os.path.exists(p)) else ""
    path = ""
    try:
        url = _KOMSA_PHOTO_LIST + "?searchKeyword=" + urllib.parse.quote(name)
        req = urllib.request.Request(url, headers={"User-Agent": _MTIS_UA})
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        items = re.findall(
            r'<img src="(/thumbnail/psnShip/[^"]+)"[^>]*>\s*</span>\s*'
            r'<strong class="title">([^<]+)</strong>', html, re.S)
        norm = lambda s: str(s or "").replace(" ", "").rstrip("호")
        target = norm(name)
        src = ""
        for img, title in items:                      # 정규화(공백·끝 '호' 제거) 매칭
            nt = norm(title)
            if nt == target or (target and (target in nt or nt in target)):
                src = img
                break
        if src:
            full = src.replace("/300_", "/")           # 썸네일 접두어 제거 → 원본
            for cand in (full, src):                   # 원본 실패 시 썸네일 폴백
                try:
                    breq = urllib.request.Request(
                        _KOMSA_PHOTO_BASE + cand, headers={"User-Agent": _MTIS_UA})
                    blob = urllib.request.urlopen(breq, timeout=20).read()
                    if len(blob) > 1000:
                        ext = os.path.splitext(cand)[1] or ".jpg"
                        fd, path = tempfile.mkstemp(suffix=ext, prefix="komsa_photo_")
                        with os.fdopen(fd, "wb") as f:
                            f.write(blob)
                        break
                except Exception:
                    continue
    except Exception as exc:
        print(f"[report] KOMSA 공개사진 조회 실패({name}): {exc}", flush=True)
        path = ""
    _KOMSA_PHOTO_CACHE[name] = {"path": path or None, "at": now}
    return path or ""


def _accident_type(summary: str, utterance: str) -> str:
    """사고개요·원문에서 공폼 사고종류(18종) 추정. 폴백 '기타'."""
    blob = f"{summary} {utterance}"
    for pat, label in _ACC_TYPES:
        if re.search(pat, blob):
            return label
    return "기타"


def _classification_name(value: str) -> str:
    """'12 기관계통 고장', '12. 기관계통 고장' 같은 응답에서 분류번호를 제거한다."""
    return re.sub(r"^\s*\d+\s*[.)번:-]?\s*", "", str(value or "")).strip()


def _normalize_accident_type(value: str, summary: str = "", utterance: str = "") -> str:
    raw = _classification_name(value)
    if raw in _ACCIDENT_TYPE_LABELS:
        return raw
    for label in _ACCIDENT_TYPE_LABELS[:-1]:
        if label in raw:
            return label
    return _accident_type(summary, utterance)


def _accident_cause(value: str = "", utterance: str = "", summary: str = "") -> str:
    """어떤 입력도 공식 사고원인 명칭 하나로 정규화하며 분류번호는 출력하지 않는다."""
    raw = _classification_name(value)
    if raw in _ACCIDENT_CAUSE_LABELS:
        return raw
    blob = " ".join(x for x in (raw, summary, utterance) if x)
    for pattern, label in _ACCIDENT_CAUSE_PATTERNS:
        if re.search(pattern, blob, re.I):
            return label
    return "기타"


def _infer_fallback(utterance: str, summary: str) -> dict:
    """공폼 추정 항목의 안전 기본값(LLM 미설정/실패 시). _infer_report_fields·_parse_and_infer 공용."""
    now_hm = datetime.now(timezone(timedelta(hours=9))).strftime("%H:%M")
    return {
        "사고종류": _accident_type(summary, utterance),
        "추정원인": _accident_cause("", utterance, summary),
        "인명피해": "없음",
        "오염피해": "없음",
        "선박피해": "확인 중",
        "지연시간": "확인 중",
        "조치사항": [f"{now_hm} 사고 접수", "관계기관 상황전파(지방청·해경서)", _DEFAULT_ACTION],
        "조치계획": ["사고 부위 정밀 점검·수리 예정", "재발방지 대책 마련 및 교육 실시 예정"],
    }


def _merge_infer(d: dict, fallback: dict) -> dict:
    """LLM이 준 추정 dict를 기본값 위에 병합(빈 값은 무시, 조치사항/계획은 list화). 공용 헬퍼."""
    out = dict(fallback)
    for k in ("인명피해", "오염피해", "선박피해", "지연시간"):
        v = str(d.get(k, "") or "").strip()
        if v:
            out[k] = v
    out["사고종류"] = _normalize_accident_type(d.get("사고종류"), fallback.get("사고종류", "기타"), "")
    out["추정원인"] = _accident_cause(d.get("추정원인"), "", fallback.get("추정원인", "기타"))
    for k in ("조치사항", "조치계획"):
        v = d.get(k)
        if isinstance(v, list) and v:
            out[k] = [str(x).strip() for x in v if str(x).strip()]
        elif isinstance(v, str) and v.strip():
            out[k] = [v.strip()]
    return out


def _infer_report_fields(utterance: str, ship: str, summary: str, extra: dict) -> dict:
    """LLM으로 공폼의 빈 항목 추정: 사고종류·추정원인·인명/오염/선박 피해·지연시간·
    조치사항(list)·조치계획(list). 실패 시 안전 기본값."""
    fallback = _infer_fallback(utterance, summary)
    if not (GEMINI_KEY or ANTHROPIC_KEY):
        return fallback
    prompt = (
        "너는 해양사고 정식 보고서('해양사고 공폼') 작성을 돕는다. 아래 신고 내용으로 공폼의 항목을 "
        "추정해 JSON으로만 출력하라.\n"
        f"선박: {ship or '미상'}\n사고개요: {summary or utterance}\n신고 원문: {utterance}\n"
        f"운항관리자 보충 — 경위: {extra.get('경위','')} / 피해: {extra.get('피해','')} / 조치: {extra.get('조치','')}\n\n"
        "출력 형식(JSON, 설명·마크다운 금지):\n"
        f"{{\"사고종류\":\"다음 18종 명칭 중 하나만, 번호 금지: {'/'.join(_ACCIDENT_TYPE_LABELS)}\","
        f"\"추정원인\":\"다음 23종 명칭 중 하나만, 번호 금지: {'/'.join(_ACCIDENT_CAUSE_LABELS)}\","
        "\"인명피해\":\"없음 또는 내용\",\"오염피해\":\"없음 또는 내용\","
        "\"선박피해\":\"없음/확인 중 또는 내용\",\"지연시간\":\"확인 중 또는 내용\","
        "\"조치사항\":[\"시각 포함 한 줄씩\"],\"조치계획\":[\"한 줄씩\"]}\n"
        "사고종류와 추정원인에는 분류번호·설명문을 붙이지 않는다. 그 밖의 확인되지 않은 항목은 "
        "공폼 관례대로 '확인 중' 또는 '없음'으로 적는다. 한국어로."
    )
    try:
        raw = _llm_text(prompt, 700)
        raw = (raw or "").replace("```json", "").replace("```", "").strip()
        return _merge_infer(json.loads(raw), fallback)
    except Exception:
        return fallback


def _parse_and_infer(utterance: str, extra: dict = None):
    """1회 LLM 호출로 ① 파싱(사고일시·선박명·사고위치·여객·승무원·사고개요)과 ② 공폼 추정(사고종류·추정원인·
    인명/오염/선박 피해·지연시간·조치사항·조치계획)을 동시 수행 → (parsed, inferred) 반환.
    보고서당 LLM 왕복을 2→1회로 줄여 속도·429를 완화. LLM 미설정/실패 시 규칙 파싱+안전 기본값 폴백."""
    extra = extra or {}
    if not (GEMINI_KEY or ANTHROPIC_KEY):
        parsed = _rule_parse(utterance)
        return parsed, _infer_fallback(utterance, parsed.get("사고개요", ""))

    prompt = (
        "너는 여객선 해양사고 보고를 돕는다. 아래 신고 내용에서 ① 핵심 정보를 추출하고 ② 정식 보고서"
        "('해양사고 공폼') 항목을 추정해 **하나의 JSON으로만** 출력하라(설명·마크다운 금지).\n"
        f"신고 원문: {utterance}\n"
        f"운항관리자 보충 — 경위: {extra.get('경위','')} / 피해: {extra.get('피해','')} / 조치: {extra.get('조치','')}\n\n"
        "출력 형식(JSON):\n{"
        "\"사고일시\":\"YYYY-MM-DD HH:MM, 모르면 빈문자열\","
        "\"선박명\":\"'호'까지 포함, 모르면 빈문자열\","
        "\"사고위치\":\"좌표·지명 포함, 모르면 빈문자열\","
        "\"여객\":\"숫자만 또는 빈문자열\",\"승무원\":\"숫자만 또는 빈문자열\","
        "\"사고개요\":\"한 문장\","
        f"\"사고종류\":\"다음 18종 명칭 중 하나만, 번호 금지: {'/'.join(_ACCIDENT_TYPE_LABELS)}\","
        f"\"추정원인\":\"다음 23종 명칭 중 하나만, 번호 금지: {'/'.join(_ACCIDENT_CAUSE_LABELS)}\","
        "\"인명피해\":\"없음 또는 내용\",\"오염피해\":\"없음 또는 내용\","
        "\"선박피해\":\"없음/확인 중 또는 내용\",\"지연시간\":\"확인 중 또는 내용\","
        "\"조치사항\":[\"시각 포함 한 줄씩\"],\"조치계획\":[\"한 줄씩\"]}\n"
        "사고종류와 추정원인에는 분류번호·설명문을 붙이지 않는다. 그 밖의 확인되지 않은 항목은 "
        "공폼 관례대로 '확인 중' 또는 '없음'으로. 한국어로."
    )
    try:
        raw = _llm_text(prompt, 900)
        raw = (raw or "").replace("```json", "").replace("```", "").strip()
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("JSON dict 아님")
    except Exception:
        parsed = _rule_parse(utterance)             # 실패 시 추가 LLM 호출 없이 규칙 폴백
        return parsed, _infer_fallback(utterance, parsed.get("사고개요", ""))

    parsed = {k: str(d.get(k, "") or "").strip()
              for k in ("사고일시", "선박명", "사고위치", "여객", "승무원", "사고개요")}
    if not any(parsed.values()):                     # 파싱이 전부 비면 규칙 파싱으로 보강
        parsed = _rule_parse(utterance)
    inferred = _merge_infer(d, _infer_fallback(utterance, parsed.get("사고개요", "")))
    return parsed, inferred


def _kr_date(d: datetime) -> str:
    return f"{d.year}. {d.month}. {d.day}.({'월화수목금토일'[d.weekday()]})"


def _summary_narrative(f: dict) -> str:
    """확정된 필수정보로 공폼 형식의 '사고개요' 한 문장을 작성한다.

    공폼 예시: 2019. 1. 20.(일) 여수-거문 항로를 운항중인 여객선 섬나라2호(승무원 4명, 여객 32명,
    차량 8대)가 09:20 여수항을 출항하여 거문도항으로 운항 중 09:25경 초도항 북동쪽 0.5마일 지점에서
    좌현 주기관 손상 사고 발생
    슬롯: [날짜(요일)] [출발-도착]항로 운항중인 [선종] [선박명]([승선])가 [출항시각] [출발항]항을
          출항하여 [도착항]항으로 운항 중 [사고시각]경 [사고위치] 지점에서 [사고내용] 사고 발생
    필수정보가 하나라도 없으면 문장을 만들지 않고 호출자가 사용자에게 해당 정보를 요청한다.
    """
    date = (f.get("date") or "").strip()
    vtype = (f.get("vtype") or "여객선").strip()
    ship = (f.get("ship") or "").strip()
    route = (f.get("route") or "").strip()
    manifest = (f.get("manifest") or "").strip()
    dep = (f.get("dep") or "").strip()
    spot = (f.get("spot") or "").strip()
    acc_t = (f.get("acc_time") or "").strip()
    accident = (f.get("summary") or "").strip()

    # 항로 'A-B' → 출발항·도착항 유도.
    dep_port = arr_port = ""
    route_parts = [x.strip() for x in re.split(r"\s*[-~∼↔]\s*", route) if x.strip()] if route else []
    if len(route_parts) >= 2:
        dep_port, arr_port = route_parts[0], route_parts[-1]

    required = {
        "사고 일시(날짜)": date, "운항 항로": route, "출발항": dep_port, "도착항": arr_port,
        "선종": vtype, "선박명": ship, "승선인원": manifest, "출항 시각": dep,
        "사고 시각": acc_t, "사고 위치": spot, "사고 내용": accident,
    }
    missing = [label for label, value in required.items() if not str(value).strip()]
    if missing:
        raise ValueError("사고개요 필수정보 미확인: " + ", ".join(missing))

    def port_with_suffix(name):
        return name if not name or name.endswith("항") else name + "항"

    def fallback():
        s = (f"{date} {route} 항로를 운항중인 {vtype} {ship}({manifest})가 "
             f"{dep} {port_with_suffix(dep_port)}을 출항하여 {port_with_suffix(arr_port)}으로 운항 중 "
             f"{acc_t}경 {spot} 지점에서 ")
        detail = accident
        s += detail if detail.endswith("발생") else f"{detail} 사고 발생"
        return s

    if not (GEMINI_KEY or ANTHROPIC_KEY):
        return fallback()
    prompt = (
        "다음 사실로 해양사고 보고서의 '사고개요'를 한국어 한 문장으로 작성하라. "
        "아래 공폼 예시의 문체·구조·어순을 그대로 따른다.\n\n"
        "예시: \"2019. 1. 20.(일) 여수-거문 항로를 운항중인 여객선 섬나라2호(승무원 4명, 여객 32명, "
        "차량 8대)가 09:20 여수항을 출항하여 거문도항으로 운항 중 09:25경 초도항 북동쪽 0.5마일 지점에서 "
        "좌현 주기관 손상 사고 발생\"\n\n"
        "슬롯 순서: [날짜(요일)] [출발-도착]항로를 운항중인 [선종] [선박명]([승선])가 "
        "[출항시각] [출발항]항을 출항하여 [도착항]항으로 운항 중 [사고시각]경 [사고위치] 지점에서 "
        "[사고내용] 사고 발생\n\n"
        f"사실:\n- 날짜: {date}\n- 항로: {route}\n"
        f"- 출발항: {dep_port}\n- 도착항: {arr_port}\n"
        f"- 선종: {vtype}\n- 선박: {ship}\n- 승선: {manifest}\n"
        f"- 출항시각: {dep}\n- 사고시각: {acc_t}\n"
        f"- 사고위치: {spot}\n- 사고내용: {accident}\n\n"
        "규칙:\n"
        "- 반드시 한 문장, '…사고 발생'으로 끝낸다.\n"
        "- 선종(여객선 등)을 선박명 앞에 반드시 붙인다.\n"
        "- 모든 값은 보고자가 확인한 필수정보이므로 하나도 생략하지 않는다.\n"
        "- ○○·00·미상·확인 중 같은 자리표시자를 절대 쓰지 않는다.\n"
        "- 사고내용은 신고된 표현(예: '선저 파공')을 그대로 보존한다. 임의로 다른 사고종류로 바꾸거나 의역하지 않는다.\n"
        "- 아는 값은 빠짐없이 넣고 슬롯 순서를 바꾸지 않는다.\n"
        "- 따옴표·설명·접두어 없이 문장만 출력한다."
    )
    try:
        out = _llm_text(prompt, 400)
        out = (out or "").strip().strip('"').strip()
        line = out.splitlines()[0].strip() if out else ""
        return line or fallback()
    except Exception:
        return fallback()


_REPORT_REQUIRED = {
    "사고일시": "사고 일시",
    "선박명": "선박명",
    "사고위치": "사고 위치",
    "항로": "운항 항로(출발항-도착항)",
    "출항시각": "출항 시각",
    "여객": "여객 수",
    "승무원": "승무원 수",
    "사고개요": "사고 개요",
}


def _missing_report_fields(confirmed: dict) -> list:
    d = confirmed if isinstance(confirmed, dict) else {}
    missing = [label for key, label in _REPORT_REQUIRED.items()
               if str(d.get(key, "")).strip() == ""]
    route = str(d.get("항로", "")).strip()
    if route and len(re.split(r"\s*[-~∼↔]\s*", route, maxsplit=1)) != 2:
        missing.append("운항 항로 형식(예: 목포-제주)")
    if d.get("사고일시") and not _extract_accident_datetime(d.get("사고일시")):
        missing.append("사고 일시 형식(YYYY-MM-DD HH:MM)")
    if d.get("출항시각") and not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", str(d["출항시각"]).strip()):
        missing.append("출항 시각 형식(HH:MM)")
    return missing


def _pending_report_keys(confirmed: dict) -> list:
    d = confirmed if isinstance(confirmed, dict) else {}
    pending = [key for key in _REPORT_REQUIRED if str(d.get(key, "")).strip() == ""]
    route = str(d.get("항로", "")).strip()
    if route and len(re.split(r"\s*[-~∼↔]\s*", route, maxsplit=1)) != 2 and "항로" not in pending:
        pending.append("항로")
    if d.get("사고일시") and not _extract_accident_datetime(d.get("사고일시")) and "사고일시" not in pending:
        pending.append("사고일시")
    if d.get("출항시각") and not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", str(d["출항시각"]).strip()) and "출항시각" not in pending:
        pending.append("출항시각")
    return pending


_KAKAO_FIELD_ASK = {
    "사고일시": "사고 일시를 알려주세요.\n예) 2026-07-13 14:20",
    "선박명": "사고 선박명을 '호'까지 알려주세요.\n예) 섬사랑12호",
    "사고위치": "사고 위치를 좌표 또는 기준점 상대 위치로 알려주세요.\n예) 추자항 북동방 2해리",
    "항로": "운항 항로를 '출발항-도착항' 형식으로 알려주세요.\n예) 목포-제주",
    "출항시각": "출항 시각을 알려주세요.\n예) 13:40",
    "여객": "현재 승선한 여객 수를 숫자로 알려주세요.\n예) 28",
    "승무원": "현재 승선한 승무원 수를 숫자로 알려주세요.\n예) 4",
    "사고개요": "확인된 사고 내용을 한 문장으로 알려주세요.\n예) 폐그물이 프로펠러에 감겨 자력 항해 불가",
}


def _kakao_confirm_question(key: str, remaining: int = 1) -> str:
    return (f"📋 정식 보고서 필수정보 확인 ({remaining}건 남음)\n"
            f"{_KAKAO_FIELD_ASK.get(key, key + '을(를) 알려주세요.')}\n"
            "※ 답변한 내용은 정식 보고서의 확정값으로 반영됩니다.")


def _normalize_confirm_answer(key: str, answer: str) -> str:
    value = str(answer or "").strip()
    if key == "사고일시":
        return _accident_iso(value)
    if key == "출항시각":
        m = re.search(r"(?<!\d)([01]?\d|2[0-3])(?::|시\s*)([0-5]\d)(?:분)?", value)
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}" if m else ""
    if key in ("여객", "승무원"):
        m = re.search(r"\d+", value)
        return m.group(0) if m else ""
    if key == "항로" and len(re.split(r"\s*[-~∼↔]\s*", value, maxsplit=1)) != 2:
        return ""
    return value


def _confirmation_from_first_report(report: str) -> dict:
    """사용자에게 이미 보여준 1차 속보의 항목을 정식 보고서 확정 후보로 복원한다."""
    text = str(report or "")
    if not text:
        return {}
    out = {}

    occurred = _get_field(text, "발생")
    if occurred and "확인 필요" not in occurred:
        out["사고일시"] = _accident_iso(occurred)

    ship = _get_field(text, "선박")
    if ship:
        out["선박명"] = ship.split(" (", 1)[0].strip()

    loc = _get_field(text, "위치") or _get_field(text, "현재위치")
    if loc:
        # VMS 줄의 속력·침로·수신시각 꼬리는 위치값에서 제외한다.
        out["사고위치"] = re.sub(r"\s*\[.*$", "", loc).strip()

    departure = _get_field(text, "출항시각")
    if departure:
        tm = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)", departure)
        if tm:
            out["출항시각"] = f"{int(tm.group(1)):02d}:{tm.group(2)}"
        rm = re.search(r"\(([^()]*(?:-|~|∼|↔)[^()]*)\)", departure)
        if rm:
            out["항로"] = rm.group(1).strip()

    route_line = _get_field(text, "항로")
    if route_line and not out.get("항로"):
        rm = re.search(r"(?:^|\s)운항\s+(.+?)(?:\s+·|$)", route_line)
        if rm:
            out["항로"] = rm.group(1).strip()

    manifest = _get_field(text, "승선")
    if manifest:
        pax_m = re.search(r"여객\s+(\d+)\s*명", manifest)
        crew_m = re.search(r"(?:선원|승무원)\s+(\d+)\s*명", manifest)
        if pax_m:
            out["여객"] = pax_m.group(1)
        if crew_m:
            out["승무원"] = crew_m.group(1)

    summary = _get_field(text, "개요")
    if summary:
        out["사고개요"] = summary
    return out


def _prepare_report_confirmation(utterance: str, first_report: str = "",
                                 existing_confirmed: dict = None) -> dict:
    """1차 속보 → 기존 사용자 답변 → 필요한 경우에만 원문 분석/API 순으로 채운다."""
    candidate = {
        "사고일시": "", "선박명": "", "사고위치": "",
        "항로": "", "출항시각": "",
        "여객": "", "승무원": "", "사고개요": "",
    }
    # 이미 사용자에게 표출된 1차 보고서 값을 먼저 확정 후보로 사용한다.
    candidate.update({k: v for k, v in _confirmation_from_first_report(first_report).items() if v != ""})
    # 사용자가 누락 질문에 직접 답한 값은 가장 높은 우선순위로 보존한다.
    candidate.update({k: str(v).strip() for k, v in (existing_confirmed or {}).items()
                      if str(v).strip() != ""})

    pending = _pending_report_keys(candidate)

    # 1차 보고서에 없는 사고 원문 추출 항목이 있을 때만 LLM을 다시 호출한다.
    # 항로·출항시각만 비어 있으면 아래의 MTIS/선박 조회로 채우므로 재분석하지 않는다.
    parseable_keys = {"사고일시", "선박명", "사고위치", "여객", "승무원", "사고개요"}
    if any(key in parseable_keys and not candidate.get(key) for key in pending):
        parsed = _parse_nl(utterance)
        parsed_values = {
            "사고일시": _accident_iso(parsed.get("사고일시") or utterance),
            "선박명": str(parsed.get("선박명") or "").strip(),
            "사고위치": str(parsed.get("사고위치") or "").strip(),
            "여객": str(parsed.get("여객") or "").strip(),
            "승무원": str(parsed.get("승무원") or "").strip(),
            "사고개요": str(parsed.get("사고개요") or "").strip(),
        }
        for key, value in parsed_values.items():
            if not candidate.get(key) and value:
                candidate[key] = value
        pending = _pending_report_keys(candidate)

    if not pending:
        return candidate

    ship = candidate["선박명"]
    vessel = route_info = mtis = None
    if ship and any(k in pending for k in ("항로", "출항시각", "여객", "승무원")):
        with ThreadPoolExecutor(max_workers=2) as ex:
            fv = ex.submit(_vessel_lookup, ship)
            fr = ex.submit(_route_lookup, ship)
            try:
                vessel = fv.result()
            except Exception:
                vessel = None
            try:
                route_info = fr.result()
            except Exception:
                route_info = None
        cd = (vessel or {}).get("선박코드", "")
        if cd:
            try:
                mtis = _predep_lookup(cd)
            except Exception:
                mtis = None
    route = str((mtis or {}).get("항로") or (route_info or {}).get("운항항로")
                or (route_info or {}).get("면허항로") or (vessel or {}).get("항로") or "").strip()
    dep = str((mtis or {}).get("출항시간") or (route_info or {}).get("출발시각") or "").strip()
    if dep.isdigit():
        dep = f"{dep.zfill(4)[:2]}:{dep.zfill(4)[2:]}"
    if "항로" in pending and route:
        candidate["항로"] = route
    if "출항시각" in pending and dep:
        candidate["출항시각"] = dep
    if "여객" in pending and (mtis or {}).get("여객") not in (None, ""):
        candidate["여객"] = str(mtis["여객"])
    if "승무원" in pending and (mtis or {}).get("승무원") not in (None, ""):
        candidate["승무원"] = str(mtis["승무원"])
    return candidate


def _report_lines(value) -> list:
    """보고서 자유입력/LLM 결과를 빈 항목 없는 줄 목록으로 정규화한다."""
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[\r\n]+|\s*/\s*", str(value or ""))
    return [re.sub(r"^\s*[-•ㅇ]\s*", "", str(item)).strip()
            for item in items if str(item).strip()]


def _build_report_data(utterance: str, extra: dict = None, center: str = "", confirmed: dict = None) -> dict:
    """챗봇 입력 → 공폼 보고서용 데이터 dict 구성.
    사용자가 검토한 confirmed가 있으면 모든 자동추출·외부조회 값보다 우선한다."""
    extra = extra or {}
    confirmed = confirmed if isinstance(confirmed, dict) else {}
    missing = _missing_report_fields(confirmed)
    if missing:
        raise ValueError("정식 보고서 필수정보 미확인: " + ", ".join(missing))
    parsed, inf = _parse_and_infer(utterance, extra)   # 파싱+공폼추정 1회 LLM 호출(2→1)
    # 운항관리자가 직접 입력한 조치사항은 LLM 추정보다 우선한다.
    direct_actions = _report_lines(extra.get("조치"))
    if direct_actions:
        inf["조치사항"] = direct_actions
    for key in ("사고일시", "선박명", "사고위치", "여객", "승무원", "사고개요"):
        if key in confirmed:
            parsed[key] = str(confirmed.get(key, "")).strip()
    ship = str(parsed.get("선박명") or "").strip()
    loc = str(parsed.get("사고위치") or "").strip()
    pax = str(parsed.get("여객") or "").strip()
    crew = str(parsed.get("승무원") or "").strip()
    summary = str(parsed.get("사고개요") or "").strip()
    accident_dt = _extract_accident_datetime(parsed.get("사고일시") or utterance)

    lat, lon = _extract_latlon(loc)

    # 외부 조회를 병렬 실행해 1차 보고서 응답시간을 단축(순차 합산 → 최댓값).
    # 의존관계: predep·master ← vessel(선박코드), weather ← 최종 좌표(신고 or VMS), 나머지는 독립.
    def _try(fn, *a):
        try:
            return fn(*a)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=6) as ex:
        f_vessel = ex.submit(_try, _vessel_lookup, ship) if ship else None
        f_route = ex.submit(_try, _route_lookup, ship) if ship else None
        # 신고문에 좌표가 없을 때만 VMS(AIS) 현위치 조회 — 좌표 있으면 Chromium 로그인 비용 생략
        f_vpos = ex.submit(_vms_position_safe, ship) if (lat is None and ship) else None

        vessel = f_vessel.result() if f_vessel else None
        cd = (vessel or {}).get("선박코드", "")
        f_predep = ex.submit(_try, _predep_lookup, cd) if cd else None
        f_master = ex.submit(_vessel_master, ship, cd)

        route_info = f_route.result() if f_route else None
        mtis = f_predep.result() if f_predep else None
        master = f_master.result() or {}
        vpos = f_vpos.result() if f_vpos else None
        if lat is None and vpos and vpos.get("위도") is not None:   # 신고 좌표 없으면 AIS 현위치 사용
            lat, lon = vpos["위도"], vpos["경도"]

        f_wx = ex.submit(_weather_lookup, loc, "" if lat is None else str(lat),
                         "" if lon is None else str(lon))   # 최종 좌표 확정 후 제출(VMS 결과 반영)
        wx = f_wx.result()
    if wx and wx.get("error"):
        wx = None

    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)

    # 현지기상
    if wx:
        wparts = [f"풍향({wx.get('풍향','-')})", f"풍속({wx.get('풍속','-')})",
                  f"파고({wx.get('파고','-')})", "시정(양호)"]
        weather = ", ".join(wparts)
    else:
        weather = "풍향( ), 풍속( ), 파고( ), 시정( )"

    # 항로·출항
    route_nm = (str(confirmed.get("항로") or "").strip()
                or (mtis or {}).get("항로") or (route_info or {}).get("운항항로")
                or (route_info or {}).get("면허항로") or (vessel or {}).get("항로") or "").strip()
    dep = str(confirmed.get("출항시각") or (mtis or {}).get("출항시간")
              or (route_info or {}).get("출발시각") or "").strip()
    if dep and dep.isdigit():
        dep = f"{dep.zfill(4)[:2]}:{dep.zfill(4)[2:]}"

    # 승선 인원 — 회사 실시간(신선) > MTIS 출항전 > 보고자 입력값
    pax_ovr = _pax_lookup(cd, ship)

    def _pax_pick(field, fallback):
        if pax_ovr and pax_ovr.get(field) is not None:
            return str(pax_ovr[field])
        if mtis and mtis.get(field):
            return str(mtis[field])
        return str(fallback or "")

    crew_n = crew if "승무원" in confirmed else _pax_pick("승무원", crew)
    pax_n = pax if "여객" in confirmed else _pax_pick("여객", pax)
    if pax_ovr and pax_ovr.get("차량") is not None:
        veh_n = pax_ovr["차량"]
    else:
        veh_n = (mtis or {}).get("차량") or 0

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
        "date": _kr_date(accident_dt) if accident_dt else "",
        "route": route_nm,
        "vtype": (vessel or {}).get("선종") or "여객선",
        "ship": ship,
        "manifest": ", ".join(mani),
        "dep": dep,
        "acc_time": accident_dt.strftime("%H:%M") if accident_dt else "",
        "spot": spot,
        "summary": summary,
    })

    # 화물 — 선박제원 화물칸 = 운항관리규정상 적재한도 값만 표출(다른 글자 없이).
    # 없으면 회사 실시간 실적재중량 > MTIS 실적재중량 순으로 폴백. (_fmt_mt는 모듈 헬퍼)
    cargo_lmt = str((mtis or {}).get("화물적재한도") or "").strip()
    cargo_rt = pax_ovr.get("화물") if pax_ovr else None
    cargo_mt = str(cargo_rt if cargo_rt is not None else ((mtis or {}).get("화물적재중량") or "")).strip()
    cargo_val = cargo_lmt or cargo_mt
    cargo = f"{_fmt_mt(cargo_val)} M/T" if cargo_val else "없음"

    # 선박사진 경로 — ① 회사 선박마스터 우선, ② 없으면 KOMSA 공개 여객선 사진
    photo = ""
    fn = str(master.get("사진파일명") or "").strip()
    if fn:
        p = fn if os.path.isabs(fn) else os.path.join(_VESSEL_PHOTO_DIR, fn)
        if os.path.exists(p):
            photo = p
    if not photo and ship:
        photo = _komsa_vessel_photo(ship)

    # 최종 출력 직전에도 공식 분류로 강제해 LLM 설명문·번호가 보고서에 새지 않게 한다.
    accident_type = _normalize_accident_type(inf.get("사고종류"), summary, utterance)
    accident_cause = _accident_cause(inf.get("추정원인"), utterance, summary)
    ph = "[미확인]"
    return {
        "사고종류": accident_type,
        "기준일시": now.strftime("%Y년 %m월 %d일 %H:%M"),
        "보고센터": center or "운항관리센터",
        "사고개요": narr,
        "사고일시": accident_dt.strftime("%Y-%m-%d %H:%M") if accident_dt else "",
        "사고위치": spot or loc,
        "항로": route_nm,
        "출항시각": dep,
        "여객": pax_n,
        "차량": str(veh_n or ""),
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
        "추정원인": accident_cause,
        "조치사항": inf["조치사항"],
        "조치계획": inf["조치계획"],
        "작성일자": f"{now.year}. {now.month}. {now.day}.",
    }


def _img_size(path: str):
    """JPEG/PNG 파일의 (가로, 세로) 픽셀을 외부 라이브러리 없이 읽음. 실패 시 (0, 0).
    선박사진을 표 셀에 넣을 때 종횡비 왜곡을 막기 위해 실제 비율 산정에 사용."""
    import struct
    try:
        with open(path, "rb") as f:
            head = f.read(26)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", head[16:24])
                return w, h
            if head[:2] == b"\xff\xd8":               # JPEG: SOF 마커에서 치수
                f.seek(2)
                while True:
                    byte = f.read(1)
                    while byte and byte != b"\xff":
                        byte = f.read(1)
                    marker = f.read(1)
                    while marker == b"\xff":
                        marker = f.read(1)
                    if not marker:
                        break
                    if marker[0] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6,
                                     0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        f.read(3)                     # length(2) + precision(1)
                        h, w = struct.unpack(">HH", f.read(4))
                        return w, h
                    ln = struct.unpack(">H", f.read(2))[0]
                    f.seek(ln - 2, 1)
    except Exception:
        pass
    return 0, 0


def _postprocess_report_hwpx(hwpx_path: str) -> None:
    """저장된 hwpx를 공폼 서식에 맞게 XML 후처리(pyhwpxlib 미지원 항목 보정). 실패 시 조용히 통과.
    ① 결재 박스 표를 상단 우측 정렬(공폼처럼) — 우측정렬 문단(기준 일시)의 paraPrIDRef 재사용
    ② 선박사진(floating 그림)을 선박제원 표 좌측 첫 셀('선박사진' 칸) 안으로 이동(표 셀 이미지 미지원)."""
    import zipfile
    try:
        from lxml import etree   # 미설치 시 후처리만 생략(보고서 본문은 정상 생성)
        with zipfile.ZipFile(hwpx_path, "r") as zin:
            names = zin.namelist()
            sec = "Contents/section0.xml"
            if sec not in names:
                return
            entries = {n: zin.read(n) for n in names}

        root = etree.fromstring(entries[sec])
        HP = root.nsmap.get("hp")
        if not HP:
            return
        q = lambda t: f"{{{HP}}}{t}"
        text_of = lambda el: "".join(el.itertext())

        def _wrapping_p(node):
            p = node.getparent()
            while p is not None and etree.QName(p).localname != "p":
                p = p.getparent()
            return p

        # ① 결재 박스(텍스트 '결재', 선박제원 표 아님)를 우측 정렬
        try:
            appr_tbl = next((t for t in root.iter(q("tbl"))
                             if "결재" in text_of(t) and "총톤수" not in text_of(t)), None)
            if appr_tbl is not None:
                right_id = None              # 우측정렬 문단(기준 일시 줄)의 paraPr id 차용
                for p in root.iter(q("p")):
                    if "기준 일시" in "".join(p.itertext()):
                        right_id = p.get("paraPrIDRef")
                        break
                ap = _wrapping_p(appr_tbl)
                if right_id and ap is not None:
                    ap.set("paraPrIDRef", right_id)
        except Exception as exc:
            print(f"[report] 결재 박스 정렬 실패: {exc}", flush=True)

        # ② 선박사진 → 선박제원 표('총톤수' 포함) 좌측 첫 셀로 이동
        try:
            target_tbl = next((t for t in root.iter(q("tbl"))
                               if "총톤수" in text_of(t)), None)
            pic = next(iter(root.iter(q("pic"))), None)
            if target_tbl is not None and pic is not None:
                run = pic.getparent()        # pic의 run(이미지 run)을 찾아 셀로 이동

                while run is not None and etree.QName(run).localname != "run":
                    run = run.getparent()
                if run is not None:
                    src_p = run.getparent()
                    first_tc = target_tbl.find(f".//{q('tc')}")
                    cell_p = first_tc.find(f".//{q('p')}") if first_tc is not None else None
                    if cell_p is not None:
                        cell_p.append(run)   # lxml: 트리 간 이동(원래 위치에서 제거됨)
                        if src_p is not None and src_p.getparent() is not None:
                            src_p.getparent().remove(src_p)
        except Exception as exc:
            print(f"[report] 사진 셀 삽입 실패(그림은 표 위 유지): {exc}", flush=True)

        entries[sec] = etree.tostring(root, xml_declaration=True,
                                      encoding="UTF-8", standalone=True)
        with zipfile.ZipFile(hwpx_path, "w", zipfile.ZIP_DEFLATED) as zout:
            if "mimetype" in entries:    # hwpx 규약: mimetype 먼저·무압축
                zout.writestr("mimetype", entries.pop("mimetype"), zipfile.ZIP_STORED)
            for n, d in entries.items():
                zout.writestr(n, d)
    except Exception as exc:
        print(f"[report] hwpx 후처리 실패: {exc}", flush=True)


def _compose_report_hwpx(data: dict) -> bytes:
    """공폼 서식 hwpx 생성 → 바이트 반환. pyhwpxlib 필요(미설치 시 ImportError 전파)."""
    from pyhwpxlib import HwpxBuilder

    H1, H2 = 16, 12   # 제목 / 항목 글자크기(pt)
    b = HwpxBuilder()
    # 결재 박스(공폼 상단 우측) — 후처리(_postprocess_report_hwpx)에서 우측 정렬
    try:
        b.add_table([["", "", "결재"]], width=11000, col_widths=[4500, 4500, 2000],
                    row_heights=[2200], header_bg="",          # 칸 안 흰색(프리셋 파랑 제거), 테두리만
                    cell_aligns={(0, 0): "CENTER", (0, 1): "CENTER", (0, 2): "CENTER"},
                    cell_styles={(0, 2): {"text_color": "#000000", "bold": True}})  # '결재' 글자 검정
    except Exception:
        pass
    # 제목: 공폼 형식 "(○○호 사고종류) 사고 보고" — 선박명 + 사고종류
    ship_title = data["선명"] if data.get("선명") and data["선명"] != "00" else "○○호"
    b.add_paragraph(f"({ship_title} {data['사고종류']}) 사고 보고", bold=True, font_size=H1, alignment="CENTER")
    b.add_paragraph(f"기준 일시 : {data['기준일시']}", alignment="RIGHT")
    b.add_paragraph(f"보고 센터 : {data['보고센터']}", alignment="RIGHT")
    b.add_paragraph("")

    b.add_paragraph("□ 사고개요", bold=True, font_size=H2)
    b.add_table([[data["사고개요"]]], header_bg="",          # 공폼: 사고개요는 네모 박스(1칸 표)
                cell_styles={(0, 0): {"text_color": "#000000", "bold": False}},
                cell_aligns={(0, 0): "LEFT"})
    b.add_paragraph(f"** 현지기상 : {data['현지기상']}")

    b.add_paragraph("□ 선박제원", bold=True, font_size=H2)
    photo_h = 0                                            # 사진칸 높이 맞춤용
    cm = (getattr(b, "_table_preset", None) or {}).get("cell_margin", (283, 283, 200, 200))
    inner_w = 11000 - cm[0] - cm[1]                        # 1열 폭 - 셀 좌우 여백 = 사진 최대 가로
    vpad = cm[2] + cm[3]                                   # 셀 상하 여백
    if data.get("사진경로"):
        try:
            iw, ih = _img_size(data["사진경로"])           # 실제 비율로 크기 산정(왜곡 방지)
            W = inner_w                                    # 1열 폭 가로 꽉 채움
            if iw and ih:
                H = max(1, round(ih * W / iw))
                if H > 8000:                               # 세로가 너무 길면 높이 기준 축소(가로 여백 허용)
                    H = 8000
                    W = max(1, round(iw * H / ih))
            else:
                H = 6000
            b.add_image(data["사진경로"], width=W, height=H)
            photo_h = H
        except Exception as exc:
            print(f"[report] 선박사진 삽입 실패: {exc}", flush=True)
    # 공폼 서식: 6열 그리드 — 1열은 선박사진(세로 병합), 2개 라벨행(음영) + 2개 데이터행
    spec_label = "" if data.get("사진경로") else "선박사진"   # 사진 있으면 사진이 칸을 채움
    rows = [
        [spec_label, "선 명", "총톤수", "선 종", "승무정원", "소유자 또는\n선박회사"],   # 1열=사진칸
        ["", "선박번호", "화물", "선적항", "국적", "검사기관"],
        ["", data["선명"], data["총톤수"], data["선종"], data["승무정원"], data["소유자"]],
        ["", data["선박번호"], data["화물"], data["선적항"], data["국적"], data["검사기관"]],
    ]
    merge_info = [(0, 0, 3, 0)]   # 1열(선박사진) 4행 세로 병합
    label_bg = {(0, c): "#EFEFEF" for c in range(1, 6)}
    label_bg.update({(1, c): "#EFEFEF" for c in range(1, 6)})
    # 선박사진칸(0,0)·데이터칸(2·3행)은 흰색으로 명시 — 프리셋 머리행 파랑/줄무늬 회색 덮어쓰기
    label_bg[(0, 0)] = "#FFFFFF"
    for r in (2, 3):
        for c in range(6):
            label_bg[(r, c)] = "#FFFFFF"
    # 라벨·선박사진 글자 검정(프리셋이 머리행 글자를 흰색으로 넣어 흰배경서 안 보이던 문제 해결)
    cell_styles = {(0, c): {"text_color": "#000000", "bold": True} for c in range(1, 6)}
    cell_styles.update({(1, c): {"text_color": "#000000", "bold": True} for c in range(1, 6)})
    cell_styles[(0, 0)] = {"text_color": "#000000", "bold": True}
    cell_aligns = {(r, c): "CENTER" for r in range(4) for c in range(6)}
    col_widths = [11000, 6300, 6300, 6300, 6300, 6320]   # 합 42520 = A4 본문폭
    # 4행 합 = 사진 높이 + 셀 상하 여백 → 사진이 칸을 세로로 꽉 채움
    rh = max(850, round((photo_h + vpad) / 4)) if photo_h else 1500
    row_heights = [rh, rh, rh, rh]
    try:
        b.add_table(rows, cell_colors=label_bg, merge_info=merge_info, header_bg="",
                    cell_aligns=cell_aligns, cell_styles=cell_styles,
                    col_widths=col_widths, row_heights=row_heights)
    except Exception:
        b.add_table(rows)
    b.add_paragraph(f"** 보험 현황 : {data['보험현황']}", font_size=8)   # 한 줄에 들어가도록 축소

    b.add_paragraph("□ 피해사항", bold=True, font_size=H2)
    b.add_bullet_list([f"인명 : {data['인명피해']}", f"오염 : {data['오염피해']}",
                       f"선박·시설물 등 : {data['선박피해']}"], bullet_char="ㅇ")
    b.add_paragraph(f"** 지연시간 : {data['지연시간']}")
    b.add_paragraph(f"** 사고 추정원인 : {data['추정원인']}")

    b.add_paragraph("□ 조치사항", bold=True, font_size=H2)
    b.add_bullet_list(data["조치사항"] or ["확인 중"], bullet_char="ㅇ")

    b.add_paragraph("□ 조치계획", bold=True, font_size=H2)
    b.add_bullet_list(data["조치계획"] or ["확인 중"], bullet_char="ㅇ")

    # 공폼 마지막 항목: 사고현장 사진(없으면 운항관리자가 항목 삭제)
    b.add_paragraph("□ 사진 (필요시 추가, 없으면 항목 삭제)", bold=True, font_size=H2)

    b.add_paragraph("")
    b.add_paragraph(data["작성일자"], alignment="CENTER")

    fd, path = tempfile.mkstemp(suffix=".hwpx")
    os.close(fd)
    try:
        b.save(path)
        _postprocess_report_hwpx(path)           # 결재 박스 우측정렬 + 사진을 선박제원 표 셀로 이동(공폼식)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _compose_debris_report_hwpx(data: dict) -> bytes:
    """'부유물 제거 조치사항(산타모니카호)260711.hwp' 형식의 전용 HWPX를 생성한다."""
    from pyhwpxlib import HwpxBuilder

    H1, H2 = 16, 12
    b = HwpxBuilder()

    # 참고 서식 상단 붙임 표: 붙임 번호와 문서명을 한 줄로 배치한다.
    top_styles = {
        (0, 0): {"text_color": "#FFFFFF", "bold": True},
        (0, 1): {"text_color": "#000000", "bold": False},
        (0, 2): {"text_color": "#000000", "bold": False},
    }
    b.add_table([["붙임 2", "", "부유물 제거 조치사항"]], header_bg="",
                cell_colors={(0, 0): "#1769D2", (0, 1): "#FFFFFF", (0, 2): "#FFFFFF"},
                cell_aligns={(0, 0): "CENTER", (0, 1): "CENTER", (0, 2): "LEFT"},
                cell_styles=top_styles, col_widths=[6100, 800, 35620], row_heights=[2300],
                page_break="NONE")
    b.add_paragraph("")
    ship = data.get("선명") or "○○호"
    b.add_paragraph(f"({ship}) 부유물감김 조치사항 보고", bold=True,
                    font_size=H1, alignment="CENTER")
    b.add_paragraph(f"기준 일시 : {data.get('기준일시', '')}", alignment="RIGHT")
    b.add_paragraph(f"보고 센터 : {data.get('보고센터', '운항관리센터')}", alignment="RIGHT")
    b.add_paragraph("")

    b.add_paragraph("□ 개    요", bold=True, font_size=H2)
    b.add_table([[data.get("사고개요") or "[미확인]"]], header_bg="",
                cell_colors={(0, 0): "#FFFFFF"}, cell_aligns={(0, 0): "LEFT"},
                cell_styles={(0, 0): {"text_color": "#000000", "bold": False}},
                row_heights=[4200], page_break="NONE")
    b.add_paragraph(f"* 사고위치 : {data.get('사고위치') or '[미확인]'}")
    b.add_paragraph(f"** 현지기상 : {data.get('현지기상') or '[미확인]'}")
    b.add_paragraph("")

    b.add_paragraph("□ 선박제원", bold=True, font_size=H2)
    spec_rows = [
        ["선 명", "총톤수", "선 종", "승선원", "소유자 또는\n선박회사"],
        ["선박번호", "화물", "선적항", "국적", "검사기관"],
        [data.get("선명", ""), data.get("총톤수", ""), data.get("선종", ""),
         data.get("승무정원", ""), data.get("소유자", "")],
        [data.get("선박번호", ""), data.get("화물", ""), data.get("선적항", ""),
         data.get("국적", ""), data.get("검사기관", "")],
    ]
    cell_colors = {(r, c): ("#EFEFEF" if r in (0, 1) else "#FFFFFF")
                   for r in range(4) for c in range(5)}
    cell_styles = {(r, c): {"text_color": "#000000", "bold": r in (0, 1)}
                   for r in range(4) for c in range(5)}
    cell_aligns = {(r, c): "CENTER" for r in range(4) for c in range(5)}
    b.add_table(spec_rows, header_bg="", cell_colors=cell_colors,
                cell_styles=cell_styles, cell_aligns=cell_aligns,
                col_widths=[7050, 7050, 7050, 7050, 14320],
                row_heights=[1700, 1700, 2100, 2100], page_break="NONE")
    b.add_paragraph("")

    b.add_paragraph("□ 조치사항", bold=True, font_size=H2)
    for action in _report_lines(data.get("조치사항")) or ["확인 중"]:
        b.add_paragraph(f"  - {action}", alignment="LEFT")
    b.add_paragraph("")

    b.add_paragraph("□ 기타", bold=True, font_size=H2)
    b.add_paragraph("  ㅇ 부유물 등 현장사진", alignment="LEFT")
    b.add_table([["없음", ""]], header_bg="",
                cell_colors={(0, 0): "#FFFFFF", (0, 1): "#FFFFFF"},
                cell_aligns={(0, 0): "CENTER", (0, 1): "CENTER"},
                cell_styles={(0, 0): {"text_color": "#000000", "bold": False},
                             (0, 1): {"text_color": "#000000", "bold": False}},
                col_widths=[21260, 21260], row_heights=[6500], page_break="NONE")

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
    body: { utterance, center, extra:{경위,피해,조치}, confirmed:{검토 확정값} }"""
    body = request.get_json(force=True, silent=True) or {}
    utterance = str(body.get("utterance", "")).strip()
    if not utterance:
        return jsonify({"error": "utterance가 필요합니다"}), 400
    center = str(body.get("center", "")).strip()
    extra = body.get("extra") or {}
    confirmed = body.get("confirmed") or {}
    missing = _missing_report_fields(confirmed)
    if missing:
        return jsonify({"error": "필수정보를 확인·입력해 주세요.", "missing": missing}), 422
    try:
        data = _build_report_data(utterance, extra, center, confirmed)
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


@app.post("/report/debris-hwpx")
def report_debris_hwpx():
    """부유물 감김 사고 → '부유물 제거 조치사항' 전용 HWPX 다운로드."""
    body = request.get_json(force=True, silent=True) or {}
    utterance = str(body.get("utterance", "")).strip()
    if not utterance:
        return jsonify({"error": "utterance가 필요합니다"}), 400
    if not _is_debris_incident(utterance, (body.get("confirmed") or {}).get("사고개요", "")):
        return jsonify({"error": "부유물 감김·추진기 이물질 유입 사고에서만 작성할 수 있습니다."}), 422
    center = str(body.get("center", "")).strip()
    extra = body.get("extra") or {}
    confirmed = body.get("confirmed") or {}
    missing = _missing_report_fields(confirmed)
    if missing:
        return jsonify({"error": "필수정보를 확인·입력해 주세요.", "missing": missing}), 422
    try:
        data = _build_report_data(utterance, extra, center, confirmed)
        blob = _compose_debris_report_hwpx(data)
    except ImportError:
        return jsonify({"error": "hwpx 생성 라이브러리(pyhwpxlib)가 없습니다. "
                                 "pip install -r requirements.txt 후 다시 시도하세요."}), 503
    except Exception as exc:
        return jsonify({"error": f"부유물 제거 조치사항 보고서 생성 실패: {exc}"}), 500

    kst = timezone(timedelta(hours=9))
    fname = (f"{data.get('선명') or '해양사고'}_부유물제거조치사항_"
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
_REPORT_FILES_LOCK = threading.Lock()   # 카카오 백그라운드 스레드·요청 스레드 동시 접근 보호
_REPORT_FILES_TTL = 3600      # 1시간
_REPORT_FILES_MAX = 200


def _store_report_file(blob: bytes, filename: str) -> str:
    now = time.time()
    token = secrets.token_urlsafe(16)
    with _REPORT_FILES_LOCK:
        for k in [k for k, v in _REPORT_FILES.items() if v[2] < now]:   # 만료분 정리
            _REPORT_FILES.pop(k, None)
        if len(_REPORT_FILES) > _REPORT_FILES_MAX:
            for k in list(_REPORT_FILES)[:-_REPORT_FILES_MAX]:
                _REPORT_FILES.pop(k, None)
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
    with _REPORT_FILES_LOCK:
        item = _REPORT_FILES.get(token)
        if item and item[2] < time.time():
            _REPORT_FILES.pop(token, None)
            item = None
    if not item:
        return Response("다운로드 링크가 만료되었거나 잘못되었습니다. 챗봇에서 다시 요청해 주세요.",
                        status=404, mimetype="text/plain; charset=utf-8")
    blob, filename, _ = item
    quoted = urllib.parse.quote(filename)
    return Response(blob, mimetype="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
        "Content-Length": str(len(blob)),
    })


def _kakao_hwpx_message(utterance: str, base: str, center: str = "", confirmed: dict = None) -> dict:
    """utterance로 hwpx 생성·보관 후, 다운로드 버튼이 달린 카카오 textCard 반환."""
    missing = _missing_report_fields(confirmed or {})
    if missing:
        raise ValueError("필수정보 미확인: " + ", ".join(missing))
    data = _build_report_data(utterance, {}, center, confirmed)
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


def _kakao_debris_hwpx_message(utterance: str, base: str, center: str = "",
                               confirmed: dict = None, first_report: str = "") -> dict:
    """부유물 제거 조치사항 HWPX를 생성·보관하고 카카오 다운로드 카드를 반환한다."""
    if not _is_debris_incident(utterance, first_report):
        raise ValueError("부유물 감김·추진기 이물질 유입 사고가 아닙니다")
    missing = _missing_report_fields(confirmed or {})
    if missing:
        raise ValueError("필수정보 미확인: " + ", ".join(missing))
    action = _get_field(first_report, "조치사항")
    data = _build_report_data(utterance, {"조치": action} if action else {}, center, confirmed)
    blob = _compose_debris_report_hwpx(data)
    kst = timezone(timedelta(hours=9))
    fname = (f"{data.get('선명') or '해양사고'}_부유물제거조치사항_"
             f"{datetime.now(kst).strftime('%Y%m%d_%H%M')}.hwpx")
    token = _store_report_file(blob, fname)
    url = f"{base}/report/download/{token}"
    return {"version": "2.0", "template": {
        "outputs": [{"textCard": {
            "title": "🧹 부유물 제거 조치사항 보고서(hwpx)",
            "description": (f"{data.get('선명') or ''} · 부유물 제거 조치사항 서식으로 작성했습니다.\n"
                            "아래 버튼을 눌러 hwpx 파일을 받으세요.\n"
                            "※ 한글에서 조치사항·현장사진을 확인·보완하세요. (링크 1시간 후 만료)"),
            "buttons": [{"action": "webLink", "label": "보고서 다운로드", "webLinkUrl": url}],
        }}],
        "quickReplies": _KAKAO_QUICK,
    }}


def _kakao_hwpx_callback(callback_url: str, uid: str, utterance: str, base: str, confirmed: dict):
    """백그라운드: hwpx 생성·보관 후 다운로드 카드 콜백 전송."""
    try:
        payload = _kakao_hwpx_message(utterance, base, confirmed=confirmed)
    except ImportError:
        payload = _kakao_text("hwpx 생성 라이브러리(pyhwpxlib)가 서버에 설치되지 않았습니다. 관리자에게 문의해 주세요.")
    except Exception as exc:
        payload = _kakao_text(f"정식 보고서(hwpx) 생성 중 오류가 발생했습니다: {exc}")
    _post_callback(callback_url, payload)


def _kakao_debris_hwpx_callback(callback_url: str, uid: str, utterance: str, base: str,
                                confirmed: dict, first_report: str = ""):
    """백그라운드: 부유물 제거 조치사항 HWPX 다운로드 카드 콜백 전송."""
    try:
        payload = _kakao_debris_hwpx_message(
            utterance, base, confirmed=confirmed, first_report=first_report)
    except ImportError:
        payload = _kakao_text("hwpx 생성 라이브러리(pyhwpxlib)가 서버에 설치되지 않았습니다. 관리자에게 문의해 주세요.")
    except Exception as exc:
        payload = _kakao_text(f"부유물 제거 조치사항 보고서(hwpx) 생성 중 오류가 발생했습니다: {exc}")
    _post_callback(callback_url, payload)


def _kakao_prepare_hwpx_callback(callback_url: str, uid: str, utterance: str,
                                  base: str, first_report: str = "",
                                  existing_confirmed: dict = None):
    """정식 보고서 준비도 비동기 처리해 카카오 스킬의 5초 응답 제한을 지킨다.

    누락값이 있으면 다운로드 파일 대신 첫 번째 확인 질문을 콜백으로 보내고,
    모두 확보됐으면 바로 hwpx 다운로드 카드를 보낸다.
    """
    try:
        confirmed = _prepare_report_confirmation(utterance, first_report, existing_confirmed)
        pending = _pending_report_keys(confirmed)
        if pending:
            _session_set(uid, confirmed=confirmed, pending_fields=pending,
                         mode="confirm_report_field", report_kind="formal")
            payload = _kakao_text(_kakao_confirm_question(pending[0], len(pending)))
        else:
            _session_set(uid, confirmed=confirmed, pending_fields=[], mode=None, report_kind=None)
            payload = _kakao_hwpx_message(utterance, base, confirmed=confirmed)
    except ImportError:
        payload = _kakao_text("hwpx 생성 라이브러리(pyhwpxlib)가 서버에 설치되지 않았습니다. 관리자에게 문의해 주세요.")
    except Exception as exc:
        payload = _kakao_text(f"정식 보고서 준비 중 오류가 발생했습니다: {exc}")
    _post_callback(callback_url, payload)


def _kakao_prepare_debris_hwpx_callback(callback_url: str, uid: str, utterance: str,
                                         base: str, first_report: str = "",
                                         existing_confirmed: dict = None):
    """부유물 제거 보고서의 준비·누락값 확인도 카카오 5초 제한 밖에서 처리한다."""
    try:
        confirmed = _prepare_report_confirmation(utterance, first_report, existing_confirmed)
        pending = _pending_report_keys(confirmed)
        if pending:
            _session_set(uid, confirmed=confirmed, pending_fields=pending,
                         mode="confirm_report_field", report_kind="debris")
            payload = _kakao_text(_kakao_confirm_question(pending[0], len(pending)))
        else:
            _session_set(uid, confirmed=confirmed, pending_fields=[], mode=None, report_kind=None)
            payload = _kakao_debris_hwpx_message(
                utterance, base, confirmed=confirmed, first_report=first_report)
    except ImportError:
        payload = _kakao_text("hwpx 생성 라이브러리(pyhwpxlib)가 서버에 설치되지 않았습니다. 관리자에게 문의해 주세요.")
    except Exception as exc:
        payload = _kakao_text(f"부유물 제거 조치사항 보고서 준비 중 오류가 발생했습니다: {exc}")
    _post_callback(callback_url, payload)


# ── GICOMS VMS 실시간 선박위치 ───────────────────────
# 로그인은 RSA+키보드보안이라 Playwright(헤드리스 브라우저)로 수행 → 세션 쿠키만 추출해
# 가벼운 allShipTarget.json(전국 실시간 AIS) 호출에 재사용. 쿠키 만료 시 자동 재로그인.

_VMS = {"cookie": None, "at": None}          # JSESSIONID 캐시
_VMS_LOCK = threading.Lock()                  # 캐시 읽기/쓰기 보호(짧게만 점유)
_VMS_LOGIN_LOCK = threading.Lock()            # 로그인 직렬화(중복 로그인 방지) — 로그인 중 신선쿠키 읽기는 비차단
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


def _fresh_cookie():
    """캐시 쿠키가 신선하면 반환, 아니면 None (짧게 락)."""
    with _VMS_LOCK:
        if (_VMS["cookie"] and _VMS["at"]
                and (datetime.now() - _VMS["at"]).total_seconds() < _VMS_COOKIE_TTL):
            return _VMS["cookie"]
    return None


def _vms_cookie(force=False):
    """세션 쿠키 반환. 신선하면 즉시 반환(읽기 비차단). 로그인이 필요할 때만 로그인 락으로 직렬화 —
    워머의 선제 갱신(force) 중에도 신선한 쿠키 읽기는 대기하지 않는다(Chromium 로그인을 _VMS_LOCK 밖에서 수행)."""
    if not force:
        c = _fresh_cookie()
        if c:
            return c
    with _VMS_LOGIN_LOCK:                      # 로그인은 한 번에 하나
        if not force:                          # 락 대기 중 다른 스레드가 갱신했을 수 있음 — 재확인
            c = _fresh_cookie()
            if c:
                return c
        cookie = _vms_login()                  # _VMS_LOCK 밖에서 로그인 → 신선쿠키 읽기 비차단
        with _VMS_LOCK:
            _VMS["cookie"], _VMS["at"] = cookie, datetime.now()
        return cookie


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

def _vms_warm_loop():
    """백그라운드: JSESSIONID 로그인 쿠키를 만료 전에 갱신해, 보고서 경로에서 콜드 Chromium
    로그인(수초~십수초) 대기를 없앤다. 좌표 없이 '선명만' 입력해도 VMS 현위치 조회가 즉시 동작.
    ※ 로그인 쿠키만 갱신할 뿐 allShipTarget(AIS 선박위치)은 호출하지 않음 → '사고 시에만 조회' 원칙 유지."""
    interval = max(60, _VMS_COOKIE_TTL - 120)   # 쿠키 만료 2분 전 선제 갱신
    while True:
        try:
            _vms_cookie(force=True)              # force=True여야 신선쿠키도 선제 재로그인(만료 래스 방지)
        except Exception as exc:
            print(f"[vms] 쿠키 워밍 실패(다음 주기 재시도): {exc}", flush=True)
        time.sleep(interval)


def _start_vms_warmer():
    """gunicorn(워커별 import)에서도 동작하도록 모듈 로드 시 호출. 키 없거나 VMS_WARM=0이면 비활성."""
    if not (GICOMS_VMS_ID and GICOMS_VMS_PW) or os.environ.get("VMS_WARM", "1") == "0":
        return
    threading.Thread(target=_vms_warm_loop, name="vms-warmer", daemon=True).start()
    print("[vms] 세션 워머 시작(쿠키 선제 갱신)", flush=True)


_start_vms_warmer()


# ── 외부 API 자동 건강검진 ───────────────────────────
# 남의 사이트(KOMSA·기상청·MTIS·GICOMS)에 의존하므로, 사이트 개편 등으로 조용히 깨지면
# 사고 당일에야 발견된다. 백그라운드 데몬이 하루 1회 각 외부 API에 가벼운 스모크 호출을 보내
# 생존을 확인하고, 실패/복구 시 로그(+선택적 웹훅)로 알린다.
# ※ VMS는 로그인 쿠키 확보만 확인하고 allShipTarget(AIS)은 호출하지 않음 → '사고 시에만 AIS 조회' 원칙 유지.

_HEALTH = {"at": None, "results": {}, "ok": None}                    # 최근 검진 결과 캐시
_HEALTH_INTERVAL = int(os.environ.get("HEALTH_INTERVAL", "86400"))   # 기본 24시간
_HEALTH_FIRST_DELAY = int(os.environ.get("HEALTH_FIRST_DELAY", "60"))  # 기동 후 첫 검진 지연(부하 회피)
_HEALTH_WEBHOOK = os.environ.get("HEALTH_WEBHOOK_URL", "")           # 선택: 실패/복구 알림(Slack 호환 {"text":...})

# 챗봇 가용성 안내용 — 핵심 외부 API가 장애면 사용자에게 '일시 사용 불가'를 알린다.
_CRITICAL_CHECKS = ("KOMSA_제원", "기상청_해상관측")                   # 신속보고에 필수적인 외부 API
_HEALTH_BANNER_TTL = int(os.environ.get("HEALTH_BANNER_TTL", "300"))  # 챗봇 사용 중 허용하는 건강상태 신선도(초)
_RECOVER_MSG = "✅ 시스템이 정상 복구되어 현재 정상 이용 가능합니다.\n\n"
_health_refreshing = threading.Lock()


def _health_down_critical() -> list:
    """건강검진 캐시 기준, 핵심 외부 API 중 장애 목록. 검진 이력 없으면 빈 목록(차단하지 않음)."""
    res = _HEALTH.get("results") or {}
    return [n for n in _CRITICAL_CHECKS if (res.get(n) or {}).get("ok") is False]


def _health_maybe_refresh():
    """챗봇 요청 시 건강상태가 오래됐으면(>TTL) 백그라운드로 1회 갱신(논블로킹·중복방지).
    이 요청 자체는 현재 캐시값을 쓰고, 갱신 결과는 다음 요청부터 반영된다."""
    if os.environ.get("HEALTH_CHECK", "1") == "0":
        return
    at = _HEALTH.get("at")
    if at and (datetime.now() - at).total_seconds() < _HEALTH_BANNER_TTL:
        return
    if not _health_refreshing.acquire(blocking=False):
        return

    def job():
        try:
            _health_run()
        except Exception as exc:
            print(f"[health] 온디맨드 갱신 오류: {exc}", flush=True)
        finally:
            _health_refreshing.release()

    threading.Thread(target=job, name="health-refresh", daemon=True).start()


def _health_checks() -> dict:
    """각 외부 API에 가벼운 스모크 호출(병렬). {이름: {ok, detail}} 반환.
    ok=True 정상 / ok=False 장애 / ok=None 키 미설정으로 건너뜀. 예외는 잡아서 ok=False."""

    def komsa_spec():
        if not KOMSA_KEY:
            return None, "KOMSA_KEY 미설정(건너뜀)"
        r = _vessel_lookup("한라호")                # 예외=장애, None=정상응답(샘플 미일치)
        return True, ("조회 정상" if r else "응답 정상(샘플 미일치)")

    def komsa_route():
        if not KOMSA_KEY:
            return None, "KOMSA_KEY 미설정(건너뜀)"
        _route_lookup("한라호")
        return True, "응답 정상"

    def kma_weather():
        if not KMA_KEY:
            return None, "KMA_KEY 미설정(건너뜀)"
        r = _weather_lookup("해상", lat="34.4", lon="128.4")  # 좌표 직접 지정 → KMA만 검사(지오코딩 우회)
        if r.get("error"):
            return False, r.get("error")
        return True, ("캐시 폴백(일시 지연)" if r.get("_stale")
                      else f"부이 {r.get('지점', '?')} 관측 정상")

    def mtis_predep():                              # 익명 세션+CSRF 토큰 확보만으로 생존 확인(로그인 불필요)
        _mtis_post("selectQrForSfcstDeInfo", {"psnshpCd": "", "psnshpNm": "", "shipNo": ""})
        return True, "세션·CSRF 정상"

    def gicoms_vms():
        if not (GICOMS_VMS_ID and GICOMS_VMS_PW):
            return None, "GICOMS_VMS_ID/PW 미설정(건너뜀)"
        c = _vms_cookie(force=False)                # 로그인 쿠키 확보만(allShipTarget 미호출)
        return bool(c), ("로그인 세션 정상" if c else "쿠키 없음")

    funcs = {
        "KOMSA_제원": komsa_spec, "KOMSA_항로": komsa_route,
        "기상청_해상관측": kma_weather, "MTIS_출항전점검": mtis_predep,
        "GICOMS_VMS로그인": gicoms_vms,
    }
    def _run_check(fn):
        last = (False, "미실행")
        for _ in range(2):                         # 일시적 TLS/타임아웃 false-positive 방지(1회 재시도)
            try:
                ok, detail = fn()
            except Exception as exc:
                ok, detail = False, str(exc)
            if ok is not False:                    # 정상(True)·건너뜀(None)은 즉시 채택
                return ok, detail
            last = (ok, detail)
            time.sleep(0.8)
        return last

    checks = {}
    with ThreadPoolExecutor(max_workers=len(funcs)) as ex:
        futs = {name: ex.submit(_run_check, fn) for name, fn in funcs.items()}
        for name, fut in futs.items():
            try:
                ok, detail = fut.result(timeout=90)
            except Exception as exc:
                ok, detail = False, str(exc)
            checks[name] = {"ok": ok, "detail": detail}
    return checks


def _health_notify(text: str):
    """선택적 웹훅 알림(Slack 호환). HEALTH_WEBHOOK_URL 미설정이면 조용히 무시."""
    if not _HEALTH_WEBHOOK:
        return
    try:
        host = os.environ.get("HOSTNAME") or ""
        body = json.dumps(
            {"text": f"[해양사고 신속보고] {text}" + (f" @{host}" if host else "")},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(_HEALTH_WEBHOOK, data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:
        print(f"[health] 웹훅 통지 실패: {exc}", flush=True)


def _health_run() -> dict:
    """검진 1회 실행 → 결과 캐시 + 로그. 실패 시, 그리고 실패→정상 복구 시 웹훅 통지."""
    results = _health_checks()
    fails = [n for n, r in results.items() if r["ok"] is False]
    ok = not fails
    prev_ok = _HEALTH["ok"]
    _HEALTH["at"], _HEALTH["results"], _HEALTH["ok"] = datetime.now(), results, ok

    for name, r in results.items():
        mark = "✅" if r["ok"] else ("⏭️" if r["ok"] is None else "❌")
        print(f"[health] {mark} {name}: {r['detail']}", flush=True)
    if ok:
        print("[health] 외부 API 전체 정상", flush=True)
        if prev_ok is False:                        # 실패→정상 복구만 통지
            _health_notify("✅ 외부 API 복구: 전체 정상으로 돌아왔습니다.")
    else:
        msg = "외부 API 점검 실패 — " + ", ".join(
            f"{n}({results[n]['detail']})" for n in fails)
        print("[health] ❌ " + msg, flush=True)
        _health_notify("❌ " + msg)
    return results


def _health_loop():
    time.sleep(max(0, _HEALTH_FIRST_DELAY))         # 기동 직후 부하 회피
    while True:
        try:
            _health_run()
        except Exception as exc:
            print(f"[health] 검진 루프 오류(다음 주기 재시도): {exc}", flush=True)
        time.sleep(max(300, _HEALTH_INTERVAL))


def _start_health_checker():
    """모듈 로드 시 시작. HEALTH_CHECK=0이면 비활성. gunicorn 다중 워커에서는
    localhost 락 포트 바인딩으로 1개 워커만 검진(중복 알림 방지)."""
    if os.environ.get("HEALTH_CHECK", "1") == "0":
        return
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", int(os.environ.get("HEALTH_LOCK_PORT", "8401"))))
    except OSError:
        return                                      # 다른 워커가 이미 검진 담당 → 조용히 양보
    sock.listen(1)
    globals()["_HEALTH_LOCK_SOCK"] = sock           # GC로 닫히지 않게 모듈에 보관
    threading.Thread(target=_health_loop, name="health-checker", daemon=True).start()
    print(f"[health] 외부 API 건강검진 시작(주기 {_HEALTH_INTERVAL}s, "
          f"첫 검진 {_HEALTH_FIRST_DELAY}s 후)", flush=True)


@app.get("/health/external")
def health_external():
    """외부 API 건강검진 상태(JSON). ?run=1 이면 즉시 1회 실행 후 반환."""
    if request.args.get("run") == "1" or not _HEALTH["results"]:
        results = _health_run()
    else:
        results = _HEALTH["results"]
    fails = [n for n, r in results.items() if r["ok"] is False]
    return jsonify({
        "ok": not fails,
        "checked_at": _HEALTH["at"].isoformat() if _HEALTH["at"] else None,
        "fails": fails,
        "results": results,
    }), (200 if not fails else 503)


_start_health_checker()


if __name__ == "__main__":
    missing = [k for k in ("KOMSA_KEY", "KMA_KEY") if not os.environ.get(k)]
    if missing:
        print(f"[오류] .env에 다음 필수 키가 없습니다: {', '.join(missing)}")
        raise SystemExit(1)
    print(f"백엔드 실행 중 → http://localhost:{PORT}")
    print("종료: Ctrl+C")
    app.run(host="0.0.0.0", port=PORT, debug=False)
