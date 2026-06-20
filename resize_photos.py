# -*- coding: utf-8 -*-
"""vessel_photos/ 사진을 보고서용으로 축소(가로 최대 1280px, JPEG q82).
   파일명·확장자는 그대로 유지(선박마스터.csv 사진파일명 변경 불필요). 원본은 덮어씀."""
import os, glob, io
from PIL import Image

MAXW = 1280
QUALITY = 82
files = glob.glob("vessel_photos/*")
before = after = 0
shrunk = 0
for f in files:
    sz0 = os.path.getsize(f); before += sz0
    try:
        im = Image.open(f)
        im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
        w, h = im.size
        if w > MAXW:
            im = im.resize((MAXW, round(h * MAXW / w)), Image.LANCZOS)
        ext = os.path.splitext(f)[1].lower()
        buf = io.BytesIO()
        if ext == ".png":
            im.save(buf, "JPEG", quality=QUALITY)   # png 사진도 jpeg로 재인코딩(파일명은 .png 유지)
        else:
            im.save(buf, "JPEG", quality=QUALITY)
        data = buf.getvalue()
        if len(data) < sz0:                          # 작아질 때만 교체
            with open(f, "wb") as out:
                out.write(data)
            shrunk += 1
        after += min(len(data), sz0)
    except Exception as e:
        print(f"  ! {os.path.basename(f)}: {e}")
        after += sz0

print(f"리사이즈 완료: {shrunk}/{len(files)}장 축소")
print(f"  {before/1e6:.0f}MB → {after/1e6:.0f}MB ({100*(1-after/before):.0f}% 감소)")
