# -*- coding: utf-8 -*-
"""여객선 기항지(엑셀) → 브이월드 지오코딩 → 이름+좌표 목록(CSV) 생성 (1회성 유틸)
방식: 이름 '검색 API(place)' 우선 + 주소 '지오코더'로 지역 검증.
 - 검색결과가 주소 중심점과 가까우면(<12km) 채택(섬 선착장까지 정확).
 - 멀면 동명이소로 보고 주소값 사용 + flag.
 - 둘 다 실패 시 실패목록에 기록.
키: .env 의 VWORLD_KEY (지오코더+검색 API 활성화 필요)
"""
import os, csv, io, re, time, json, urllib.parse, urllib.request
from math import radians, sin, cos, asin, sqrt
import openpyxl
from dotenv import load_dotenv

load_dotenv()
K = os.environ.get("VWORLD_KEY", "")
assert K, ".env 에 VWORLD_KEY 가 없습니다."

SRC_XLSX = "여객선 기항지.xlsx"
SHEET = "기항지 현황"          # 마스터 시트 (No./센터/기항지 이름/섬이름/.../기항지 소재지)
OUT_CSV = "기항지_좌표.csv"
FAIL_CSV = "기항지_좌표_실패.csv"


def _vw(req, **p):
    p.update({"key": K, "format": "json"})
    url = "https://api.vworld.kr/req/" + req + "?" + urllib.parse.urlencode(p)
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def geocode(addr):
    """주소 → (lat, lon). 지번/도로명 순. 실패 시 None."""
    for typ in ("PARCEL", "ROAD"):
        try:
            d = _vw("address", service="address", request="getcoord",
                    version="2.0", crs="epsg:4326", address=addr, type=typ)
            if d["response"]["status"] == "OK":
                pt = d["response"]["result"]["point"]
                return float(pt["y"]), float(pt["x"])
        except Exception:
            pass
    return None


def search(query):
    """지명 검색(place) → (lat, lon, title). 실패 시 None."""
    try:
        d = _vw("search", service="search", request="search", version="2.0",
                crs="epsg:4326", size="1", page="1", query=query, type="place")
        if d["response"]["status"] == "OK":
            items = d["response"].get("result", {}).get("items")
            if items:
                pt = items[0]["point"]
                return float(pt["y"]), float(pt["x"]), items[0].get("title", "")
    except Exception:
        pass
    return None


def hav(a, b):
    (la1, lo1), (la2, lo2) = a, b
    dla, dlo = radians(la2 - la1), radians(lo2 - lo1)
    h = sin(dla / 2) ** 2 + cos(radians(la1)) * cos(radians(la2)) * sin(dlo / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(h))   # km


def dm(v, hemi):
    d = int(v)
    return f"{d}-{(v - d) * 60:04.1f}{hemi}"


def name_candidates(name, island):
    """검색용 후보(중복 제거, 순서 유지)."""
    cands = []
    base = re.sub(r"\s+", "", str(name)).strip()
    paren = re.findall(r"\(([^)]+)\)", base)          # 괄호 안(섬/별칭)
    no_paren = re.sub(r"\([^)]*\)", "", base)         # 괄호 제거
    for c in [no_paren] + paren + ([str(island).strip()] if island else []):
        c = c.strip()
        if c and c not in cands:
            cands.append(c)
    # 접미사 제거형 추가
    stripped = re.sub(r"(방파제|남항|북항|신항|외항|내항|항|선착장|포구|부두)$", "", no_paren).strip()
    if stripped and stripped not in cands:
        cands.append(stripped)
    return cands


def resolve(name, island, addr):
    addr_pt = geocode(addr) if addr else None
    s = None
    for q in name_candidates(name, island):
        s = search(q)
        if s:
            break
    if s and addr_pt:
        if hav((s[0], s[1]), addr_pt) <= 12.0:
            return s[0], s[1], f"SEARCH:{s[2]}", ""
        return addr_pt[0], addr_pt[1], "ADDR", f"검색({s[2]})과 주소 {hav((s[0],s[1]),addr_pt):.1f}km 불일치"
    if s:
        return s[0], s[1], f"SEARCH:{s[2]}", "주소없음"
    if addr_pt:
        return addr_pt[0], addr_pt[1], "ADDR", "검색실패→주소"
    return None


def load_rows():
    wb = openpyxl.load_workbook(SRC_XLSX, data_only=True)
    ws = wb[SHEET]
    rows = list(ws.iter_rows(values_only=True))
    # 헤더 행(컬럼명 'No.' 포함) 탐색
    hi = next(i for i, r in enumerate(rows) if r and "No." in [str(c).strip() if c else "" for c in r])
    hdr = [str(c).strip() if c else "" for c in rows[hi]]
    ci = {n: hdr.index(n) for n in hdr}
    def col(*names):
        for n in names:
            for h in hdr:
                if h.replace("\n", "").startswith(n):
                    return hdr.index(h)
        return None
    c_no, c_ctr, c_name, c_isl, c_addr = (
        col("No."), col("센터", "지방청"), col("기항지 이름", "기항지"),
        col("섬이름"), col("기항지 소재지"))
    out = []
    for r in rows[hi + 1:]:
        if c_no is None or r[c_no] is None:
            continue
        try:
            int(r[c_no])
        except (TypeError, ValueError):
            continue
        out.append({
            "no": r[c_no],
            "center": r[c_ctr] if c_ctr is not None else "",
            "name": str(r[c_name]).strip() if c_name is not None and r[c_name] else "",
            "island": str(r[c_isl]).strip() if c_isl is not None and r[c_isl] else "",
            "addr": str(r[c_addr]).strip() if c_addr is not None and r[c_addr] else "",
        })
    return out


def main():
    rows = load_rows()
    print(f"대상 기항지: {len(rows)}개  (시트: {SHEET})")
    ok, fail = [], []
    for i, row in enumerate(rows, 1):
        if not row["name"]:
            continue
        res = resolve(row["name"], row["island"], row["addr"])
        if res:
            la, lo, src, flag = res
            ok.append({**row, "lat": la, "lon": lo,
                       "위도": dm(la, "N"), "경도": dm(lo, "E"), "source": src, "flag": flag})
        else:
            fail.append(row)
        if i % 25 == 0:
            print(f"  ...{i}/{len(rows)} (성공 {len(ok)} / 실패 {len(fail)})")
        time.sleep(0.05)
    with io.open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["No", "센터", "기항지", "섬이름", "위도", "경도", "위도(십진)", "경도(십진)", "출처", "검토플래그", "주소"])
        for r in ok:
            w.writerow([r["no"], r["center"], r["name"], r["island"], r["위도"], r["경도"],
                        f'{r["lat"]:.6f}', f'{r["lon"]:.6f}', r["source"], r["flag"], r["addr"]])
    with io.open(FAIL_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["No", "센터", "기항지", "섬이름", "주소"])
        for r in fail:
            w.writerow([r["no"], r["center"], r["name"], r["island"], r["addr"]])
    flagged = [r for r in ok if r["flag"]]
    print(f"\n완료: 성공 {len(ok)} / 실패 {len(fail)} / 검토필요(flag) {len(flagged)}")
    print(f"  → {OUT_CSV}, {FAIL_CSV}")


if __name__ == "__main__":
    main()
