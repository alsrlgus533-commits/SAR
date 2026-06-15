# -*- coding: utf-8 -*-
"""주소중심(ADDR)으로 뭉친 도서 기항지 → 검색 API로 개별 좌표 재추출 (정제 2단계)
입력: 기항지_좌표.csv (geocode_ports.py 결과)
방식: 기항지마다 여러 검색어 시도(○○선착장 / ○○항 / 섬이름 / 시군구+이름) →
      주소중심 25km 이내 후보만 채택, 선착장>항>섬>근접 우선.
출력: 기항지_좌표_정제.csv (개선/유지/미해결 표시)
"""
import os, csv, io, re, time, json, urllib.parse, urllib.request
from math import radians, sin, cos, asin, sqrt
from dotenv import load_dotenv

load_dotenv()
K = os.environ["VWORLD_KEY"]
SRC = "기항지_좌표.csv"
OUT = "기항지_좌표_정제.csv"
RADIUS_KM = 25.0       # 주소중심 기준 채택 허용 반경
MOVED_M = 300          # 이만큼 이동하면 '개선'으로 표시


def _vw(req, **p):
    p.update({"key": K, "format": "json"})
    url = "https://api.vworld.kr/req/" + req + "?" + urllib.parse.urlencode(p)
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def search(q):
    try:
        d = _vw("search", service="search", request="search", version="2.0",
                crs="epsg:4326", size="5", page="1", query=q, type="place")
        if d["response"]["status"] == "OK":
            return [(float(i["point"]["y"]), float(i["point"]["x"]), i.get("title", ""))
                    for i in (d["response"].get("result", {}).get("items") or [])]
    except Exception:
        pass
    return []


def hav(a, b):
    (la1, lo1), (la2, lo2) = a, b
    h = sin(radians(la2 - la1) / 2) ** 2 + cos(radians(la1)) * cos(radians(la2)) * sin(radians(lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(h))


def dm(v, h):
    d = int(v)
    return f"{d}-{(v - d) * 60:04.1f}{h}"


def region_word(addr):
    """주소에서 시군구 추출 (예: '충청남도 보령시 ...' -> '보령')."""
    toks = str(addr).split()
    if len(toks) >= 2:
        return re.sub(r"(특별자치도|특별자치시|광역시|특별시|자치도|[시군구도])$", "", toks[1]) or toks[1]
    return ""


def score(title):
    if "선착장" in title: return 0
    if title.endswith("항") or "여객" in title or "터미널" in title: return 1
    if title.endswith("도"): return 2
    return 3


def refine(name, island, addr, cen):
    no_paren = re.sub(r"\([^)]*\)", "", str(name)).strip()
    reg = region_word(addr)
    queries = [f"{no_paren}선착장", f"{no_paren}항", island, f"{island}선착장",
               f"{reg} {no_paren}", f"{reg} {island}", no_paren]
    cands = []
    seen = set()
    for q in queries:
        q = q.strip()
        if not q or q in seen:
            continue
        seen.add(q)
        for la, lo, title in search(q):
            d = hav(cen, (la, lo))
            if d <= RADIUS_KM:
                cands.append((score(title), d, la, lo, title, q))
        time.sleep(0.04)
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1]))   # 선착장/항 우선, 그다음 근접
    s, d, la, lo, title, q = cands[0]
    return la, lo, title, q


def main():
    rows = list(csv.DictReader(io.open(SRC, encoding="utf-8-sig")))
    addr_rows = [r for r in rows if r["출처"] == "ADDR"]
    print(f"정제 대상(주소중심): {len(addr_rows)}개")
    improved = kept = unresolved = 0
    out = []
    for i, r in enumerate(rows, 1):
        if r["출처"] != "ADDR":
            r["정제상태"] = "원본채택(이름검색)"
            out.append(r)
            continue
        cen = (float(r["위도(십진)"]), float(r["경도(십진)"]))
        res = refine(r["기항지"], r["섬이름"], r["주소"], cen)
        if res:
            la, lo, title, q = res
            moved = hav(cen, (la, lo)) * 1000
            r["위도"], r["경도"] = dm(la, "N"), dm(lo, "E")
            r["위도(십진)"], r["경도(십진)"] = f"{la:.6f}", f"{lo:.6f}"
            r["출처"] = f"SEARCH2:{title}({q})"
            if moved >= MOVED_M:
                r["정제상태"] = f"개선(+{moved/1000:.1f}km)"; improved += 1
            else:
                r["정제상태"] = "유지(거의동일)"; kept += 1
        else:
            r["정제상태"] = "미해결(주소중심유지)"; unresolved += 1
        out.append(r)
        if i % 25 == 0:
            print(f"  ...{i}/{len(rows)}  개선 {improved}/유지 {kept}/미해결 {unresolved}")
    fields = list(out[0].keys())
    with io.open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out)
    print(f"\n완료: 개선 {improved} / 유지 {kept} / 미해결 {unresolved}  -> {OUT}")


if __name__ == "__main__":
    main()
