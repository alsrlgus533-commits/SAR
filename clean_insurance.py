# -*- coding: utf-8 -*-
import urllib.request, re, csv, io, sys

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
SRC = "보험정보-러프데이터.csv"

# 원본 오기 수동 보정(여객 금액) — 확인된 값으로 덮어씀
PAX_OVERRIDE = {
    "매물도구경2호": "3.5억",
    "푸른나래호": "3.0억",
    "퍼스트엔젤": "1.5억",
}

# ── 1) 현재 운항 여객선 명단 (KOMSA 공개목록 sub03_0204, 25p) ──
def fetch_current_names():
    names = set()
    for idx in range(1, 30):
        url = f"https://www.komsa.or.kr/prog/psnShip/kor/sub03_0204/list.do?pageIndex={idx}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            h = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
        except Exception as e:
            print("page", idx, "err", e); continue
        page = re.findall(r'<strong class="title">([^<]+)</strong>', h)
        if not page:
            break
        names.update(page)
    return names

def nkey(s):
    return re.sub(r'\s|호$|\(구\)', '', (s or '').strip()).upper()

# ── 2) 보험사명 통일 ──
def canon_insurer(p):
    k = p.replace(" ", "").rstrip("외")
    if k == "KSA": return "한국해운조합"
    if re.search(r'해운조합|해운조함|헤운조합', k): return "한국해운조합"
    if k in ("조합", "해운조", "한국해운조"): return "한국해운조합"
    if re.search(r'DB손|디비손', k): return "DB손해보험"
    if "농협" in k: return "농협손해보험"
    if "메리츠" in k: return "메리츠화재"
    if "현대해상" in k: return "현대해상"
    if k.startswith("삼성"): return "삼성화재"
    if k.startswith("한화"): return "한화손해보험"
    if k.startswith("KB"): return "KB손해보험"
    return p.strip()

def norm_org(*cells):
    out = []
    for s in cells:
        if not s: continue
        s2 = re.sub(r'\([^)]*\)', '', s)            # (억)(만원)(명) 단위표시 제거
        for part in re.split(r'[,./·]+', s2):
            part = part.strip()
            if not part: continue
            if any(x in part for x in ("미가입", "법정", "담보", "선원법")): continue  # 비-보험사 토큰 제외
            c = canon_insurer(part)
            if c and c not in out: out.append(c)
    return "/".join(out)

# ── 3) 금액 → 억 ──
LEGAL = ("법정", "담보", "선원법", "미가입")
def to_won(s):
    s = s.replace(",", "").replace(" ", "")
    if not re.search(r'\d', s): return None
    total = 0.0; used = False; work = s
    def take(pat, mult):
        nonlocal total, used, work
        for m in re.findall(pat, work):
            total += float(m) * mult; used = True
        work = re.sub(pat, '', work)
    take(r'(\d+\.?\d*)억', 1e8)
    take(r'(\d+\.?\d*)천만', 1e7)
    take(r'(\d+\.?\d*)백만', 1e6)
    take(r'(\d+\.?\d*)천원', 1e3)
    for m in re.findall(r'(\d+\.?\d*)천', work):
        n = float(m); total += n*1e7 if n < 1000 else n*1e3; used = True
    work = re.sub(r'\d+\.?\d*천', '', work)
    take(r'(\d+\.?\d*)백', 1e6)
    take(r'(\d+\.?\d*)만', 1e4)
    leftover = re.findall(r'\d+\.?\d*', work)
    if leftover and not used:
        n = float(leftover[0])
        total += n * 1e8 if n < 10000 else n   # 단위 없는 작은 수는 억, 큰 수는 원
        used = True
    return total if used else None

def get_hint(org_cell):
    if '(억)' in org_cell or '(1명/억)' in org_cell or '(1명' in org_cell: return '억'
    if '(만원)' in org_cell: return '만원'
    if '(명)' in org_cell: return '명'
    return ''

def parse_amount(raw, hint, is_pax):
    s = (raw or '').strip()
    if not s: return ('', 'empty')
    per = bool(re.search(r'1인당|인당|/\s*1?인|/\s*1?명|[Xx]\s*여객|[Xx]\s*선원|여객정원', s)) or (is_pax and '명' in s)
    if any(k in s for k in LEGAL): return (s, 'legal')      # 법정액·담보 → 원문 그대로
    if re.search(r'\$|불|USD', s, re.I): return (s, 'foreign')  # 외화 → 원문 그대로
    core = s                                    # 금액만 남기도록 수식어를 '숫자까지' 제거(예 '3인당','/1인' → 통째 제거)
    core = re.sub(r'\d*\s*인당', '', core)       # 1인당·3인당
    core = re.sub(r'[/\s]\d*\s*인(?!\w)', '', core)   # /1인·/인
    core = re.sub(r'\d+\s*인(?!\w)', '', core)        # 1인 ·3인
    core = re.sub(r'[/\s]\d*\s*명', '', core)         # /1명
    core = re.sub(r'[Xx]\s*가입선원|[Xx]\s*선원|[Xx]\s*여객정원|[Xx]\s*여객', '', core)
    core = re.sub(r'가입선원|여객정원|선원|여객|정원|각각|동일|이상|합|원', '', core)
    core = re.sub(r'\s+', '', core)
    multi = bool(re.search(r'\d\s*[/,]\s*\d', core))
    val = None
    if not multi:
        if hint == '억':
            mm = re.findall(r'(\d+\.?\d*)\s*억', core)
            if mm: val = float(mm[0]) * 1e8
            else:
                m = re.findall(r'\d+\.?\d*', core)
                if m: val = float(m[0]) * 1e8
        elif hint == '만원':
            m = re.findall(r'\d+\.?\d*', core)
            if m: val = float(m[0]) * 1e4
        if val is None:
            val = to_won(core)
    if val is None or multi:
        return (s, 'review')
    eok = val / 1e8
    over = (eok > 10) if is_pax else (eok > 2000)   # 여객 10억·선체 2000억 초과 → 별도 파일
    disp = f"{eok:.1f}억"
    if is_pax and per: disp += "/인"
    return (disp, 'over' if over else 'ok')

# ── 실행 ──
current = fetch_current_names()
ck = {nkey(n) for n in current}
print(f"현재 운항 여객선 명단: {len(current)}척")

with open(SRC, encoding="utf-8-sig", newline="") as f:
    rows = list(csv.DictReader(f))
print(f"러프데이터: {len(rows)}행")

# 현재 운항선만 필터
kept = [r for r in rows if nkey(r["선박명"]) in ck]
# 중복 선박명 → 채워진 칸 많은 행 우선
best = {}
def filled(r): return sum(1 for k in ("보험금액_선체","보험금액_선원","보험금액_여객","보험기관_선체") if r.get(k,"").strip())
for r in kept:
    k = nkey(r["선박명"])
    if k not in best or filled(r) > filled(best[k]): best[k] = r
kept = list(best.values())
print(f"현재 운항선 매칭: {len(kept)}척 (중복 제거 후)")

out = []
over_rows = []
for r in kept:
    o_sh, o_sw, o_yg = r.get("보험기관_선체",""), r.get("보험기관_선원",""), r.get("보험기관_여객","")
    org = norm_org(o_sh, o_sw, o_yg)
    sh, sh_st = parse_amount(r.get("보험금액_선체",""), get_hint(o_sh), False)
    sw = (r.get("보험금액_선원","") or "").strip()      # 선원 원문 유지
    yg, yg_st = parse_amount(r.get("보험금액_여객",""), get_hint(o_yg), True)
    if r["선박명"] in PAX_OVERRIDE:                    # 수동 보정값 적용
        yg, yg_st = PAX_OVERRIDE[r["선박명"]], 'ok'
    parts = []
    if sh: parts.append(f"선체({sh})")
    if sw: parts.append(f"선원({sw})")
    if yg: parts.append(f"여객({yg})")
    hyun = (f"{org} - " if org else "") + "·".join(parts)
    row = {
        "선박명": r["선박명"], "선박코드": r["선박코드"], "보험기관": org,
        "보험금액_선체": sh, "보험금액_선원": sw, "보험금액_여객": yg,
        "선박번호": r.get("선박번호",""), "선적항": r.get("선적항",""),
        "검사기관": r.get("검사기관",""), "보험현황": hyun,
    }
    if yg_st == 'over' or sh_st == 'over':           # 여객 10억(또는 선체 2000억) 초과 → 별도 파일
        reasons = []
        if yg_st == 'over': reasons.append(f"여객 {yg}")
        if sh_st == 'over': reasons.append(f"선체 {sh}")
        rr = dict(row); rr["사유"] = " / ".join(reasons) + " 초과 — 원본 확인 필요"
        over_rows.append(rr)
    else:
        out.append(row)

out.sort(key=lambda x: x["선박명"])
over_rows.sort(key=lambda x: x["선박명"])
cols = ["선박명","선박코드","보험기관","보험금액_선체","보험금액_선원","보험금액_여객","선박번호","선적항","검사기관","보험현황"]
with open("보험정보-정리.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(out)
with open("보험정보-여객10억초과.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols + ["사유"]); w.writeheader(); w.writerows(over_rows)
print(f"저장: 보험정보-정리.csv ({len(out)}척)")
print(f"저장: 보험정보-여객10억초과.csv ({len(over_rows)}척)")
for r in over_rows:
    print("  [별도]", r["선박명"], "|", r["사유"])
