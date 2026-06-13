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


@app.get("/predep")
def predep():
    cd = request.args.get("psnshpCd", "").strip()
    nm = request.args.get("name", "").strip()
    de = request.args.get("date", "").strip()
    tm = request.args.get("time", "").strip()
    if not cd:
        return jsonify({"error": "psnshpCd(선박코드)가 필요합니다"}), 400

    try:
        if tm:
            # 특정 항차 지정(출항일+시간) → 상세 조회
            de = de or datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
            data = _mtis_post(
                "detailFerryPreDepCkForMoTraffic",
                {"psnshpCd": cd, "psnshpNm": nm, "sloffDe": de, "sloffTime": tm.zfill(4)},
            )
        else:
            # 시간 미지정 → 그 선박의 '가장 최근' 출항전 점검표 (작성 시각 기준 최신)
            data = _mtis_post(
                "selectQrForSfcstDeInfo", {"psnshpCd": cd, "psnshpNm": "", "shipNo": ""}
            )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    o = (data or {}).get("psnshpSloffBeforeSfcst") or {}
    if not o:
        return jsonify({"error": "출항전 점검표를 찾지 못했습니다(선박코드 확인)"}), 404

    def num(key):
        try:
            return int(float(o.get(key)))
        except (TypeError, ValueError):
            return 0

    pax = num("pasngrAdultHeadcnt") + num("pasngrSmPersonHeadcnt") + num("pasngrInfantHeadcnt")
    return jsonify(
        {
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
    )


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

def _build_report_text(utterance: str) -> str:
    """사고 자유텍스트 → 1차(속보) 보고서 텍스트."""
    parsed = _parse_nl(utterance)
    ship = str(parsed.get("선박명") or "").strip()
    loc = str(parsed.get("사고위치") or "").strip()
    pax = str(parsed.get("여객") or "").strip()
    crew = str(parsed.get("승무원") or "").strip()
    summary = str(parsed.get("사고개요") or "").strip()

    vessel = route_info = None
    if ship:
        try:
            vessel = _vessel_lookup(ship)
        except Exception:
            vessel = None
        try:
            route_info = _route_lookup(ship)
        except Exception:
            route_info = None

    lat, lon = _extract_latlon(loc)
    wx = _weather_lookup(loc, "" if lat is None else str(lat), "" if lon is None else str(lon))
    if wx.get("error"):
        wx = None

    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst).strftime("%Y-%m-%d %H:%M")
    L = ["🚨 해양사고 1차(속보) — 자동작성", ""]
    if vessel:
        spec = " · ".join(x for x in (
            vessel.get("선종"), vessel.get("총톤수"),
            f"정원 {vessel.get('여객정원')}" if vessel.get("여객정원") else "",
        ) if x)
        L.append(f"▶ 선박: {ship}" + (f" ({spec})" if spec else ""))
    elif ship:
        L.append(f"▶ 선박: {ship}")
    L.append(f"▶ 발생: {now}")
    if loc:
        L.append(f"▶ 위치: {loc}")
    try:
        total = int(pax or 0) + int(crew or 0)
    except ValueError:
        total = 0
    if pax or crew:
        L.append(f"▶ 승선: 여객 {pax or '?'}명, 승무원 {crew or '?'}명" + (f" (계 {total}명)" if total else ""))
    if summary:
        L.append(f"▶ 개요: {summary}")
    L.append("")
    if wx:
        L.append(f"[기상] {wx.get('지점','')} 풍향 {wx.get('풍향')}, 풍속 {wx.get('풍속')}, "
                 f"파고 {wx.get('파고')}, 수온 {wx.get('수온')}")
        a = wx.get("AWS")
        if a:
            L.append(f"[인근 AWS] {a.get('지점')} 풍향 {a.get('풍향')}, 풍속 {a.get('풍속')}, 기온 {a.get('기온')}")
    else:
        L.append("[기상] 위치 정보가 없어 해상관측을 특정하지 못했습니다")
    if route_info:
        rr = " · ".join(x for x in (
            f"운항 {route_info['운항항로']}" if route_info.get("운항항로") else "",
            f"상태 {route_info['운항상태']}" if route_info.get("운항상태") else "",
            f"출발 {route_info['출발시각']}" if route_info.get("출발시각") else "",
        ) if x)
        if rr:
            L.append(f"[항로] {rr}")
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
        urllib.request.urlopen(req, timeout=25).read()
    except Exception:
        pass


@app.post("/kakao")
def kakao_skill():
    body = request.get_json(force=True, silent=True) or {}
    ureq = body.get("userRequest") or {}
    utterance = str(ureq.get("utterance") or "").strip()
    callback_url = ureq.get("callbackUrl")
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
