# -*- coding: utf-8 -*-
"""선박마스터.csv의 선박들 사진을 KOMSA 공개목록에서 일괄 다운로드해 vessel_photos/에 저장하고
   사진파일명 칸을 채운다. 이미 받은 건 건너뜀(재실행 안전)."""
import urllib.request, urllib.parse, re, os, csv, time

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) rapid-report-backend/1.0"
LIST = "https://www.komsa.or.kr/prog/psnShip/kor/sub03_0204/list.do"
BASE = "https://www.komsa.or.kr"
PHOTO_DIR = "vessel_photos"
MASTER = "선박마스터.csv"
os.makedirs(PHOTO_DIR, exist_ok=True)

def get(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")

def norm(s):
    return re.sub(r'\s|호$', '', str(s or '')).rstrip("호").upper()

def find_photo(name):
    html = get(LIST + "?searchKeyword=" + urllib.parse.quote(name))
    items = re.findall(
        r'<img src="(/thumbnail/psnShip/[^"]+)"[^>]*>\s*</span>\s*'
        r'<strong class="title">([^<]+)</strong>', html, re.S)
    target = norm(name)
    src = ""
    for img, title in items:
        nt = norm(title)
        if nt == target or (target and (target in nt or nt in target)):
            src = img; break
    if not src:
        return None
    full = src.replace("/300_", "/")          # 썸네일 접두어 제거 → 원본
    for cand in (full, src):
        try:
            blob = get(BASE + cand, binary=True)
            if len(blob) > 1000:
                return os.path.splitext(cand)[1] or ".jpg", blob
        except Exception:
            continue
    return None

rows = list(csv.DictReader(open(MASTER, encoding="utf-8-sig")))
cols = list(rows[0].keys())
ok = skip = miss = 0
for r in rows:
    name = r["선박명"]
    fn = (r.get("사진파일명") or "").strip()
    if fn and os.path.exists(os.path.join(PHOTO_DIR, fn)):
        skip += 1; continue
    try:
        res = find_photo(name)
    except Exception as e:
        print(f"  ! {name} 오류: {e}"); res = None
    if not res:
        miss += 1; print(f"  - {name}: 사진 없음");
        time.sleep(0.25); continue
    ext, blob = res
    safe = re.sub(r'[\\/:*?"<>|]', '', name)
    fname = safe + ext
    with open(os.path.join(PHOTO_DIR, fname), "wb") as f:
        f.write(blob)
    r["사진파일명"] = fname
    ok += 1
    print(f"  + {name} → {fname} ({len(blob)//1024}KB)")
    time.sleep(0.25)

with open(MASTER, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
print(f"\n완료: 신규 {ok}장 / 기존 {skip}장 / 미발견 {miss}척 → 사진파일명 갱신")
