# -*- coding: utf-8 -*-
"""보험정보-정리.csv(+여객10억초과) → 선박마스터.csv 생성/갱신."""
import csv

def load(fn):
    try:
        return list(csv.DictReader(open(fn, encoding="utf-8-sig")))
    except FileNotFoundError:
        return []

rows = load("보험정보-정리.csv") + load("보험정보-여객10억초과.csv")
cols = ["선박명", "선박코드", "보험현황", "선박번호", "선적항", "검사기관", "국적", "사진파일명"]
out, seen = [], set()
for r in rows:
    nm = r["선박명"]
    if nm in seen:
        continue
    seen.add(nm)
    port = r.get("선적항", "") or ""
    nat = "파나마" if ("파나마" in port or "PANAMA" in port.upper()) else "대한민국"
    out.append({
        "선박명": nm,
        "선박코드": r.get("선박코드", ""),
        "보험현황": r.get("보험현황", ""),
        "선박번호": r.get("선박번호", ""),
        "선적항": port,
        "검사기관": "",                 # 보고서 미사용
        "국적": nat,
        "사진파일명": "",               # KOMSA 공개사진 자동조회로 대체
    })

out.sort(key=lambda x: x["선박명"])
with open("선박마스터.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    w.writerows(out)

ins = sum(1 for r in out if r["보험현황"].strip())
print(f"선박마스터.csv 생성: {len(out)}척 (보험현황 {ins}척 / 빈칸 {len(out)-ins}척)")
