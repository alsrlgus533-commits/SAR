# -*- coding: utf-8 -*-
"""기준점(기항지) 좌표 동기화 검증.

프론트 `해양사고-신속보고-프로토타입.jsx`의 `refPoints`(도-분 표기 문자열)와
백엔드 `backend.py`의 `_REF_POINTS`(도-분 산술식 튜플)는 **표현은 다르지만 같은 값**이어야
한다(CLAUDE.md 규칙). 둘 다 십진좌표로 파싱해 이름·좌표를 대조하고, 불일치가 있으면 보고한다.

사용:
  python check_refpoints_sync.py          # 사람이 읽는 리포트, 동기화면 exit 0 / 불일치면 1
  python check_refpoints_sync.py --hook   # Claude Code PostToolUse 훅용(아래 참고)
      - stdin으로 받은 도구 입력의 파일이 두 대상 파일 중 하나일 때만 검사
      - 불일치면 stderr로 안내 + exit 2(Claude에 피드백), 동기화면 조용히 exit 0
"""
import ast
import json
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSX = os.path.join(BASE_DIR, "해양사고-신속보고-프로토타입.jsx")
PY = os.path.join(BASE_DIR, "backend.py")
TOL = 0.001  # 십진도 허용오차(≈0.06분 < 한 자리(0.1분) 차이 → 실제 숫자 차이는 잡고 부동소수 잡음은 무시)


def _to_decimal(token: str):
    """좌표 문자열 → 십진도. 도-분-초 / 도-분 / 십진수, 반구접미(N/S/E/W) 인식."""
    t = token.strip()
    m = re.fullmatch(r"(\d+)-(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\s*([NSEWnsew]?)", t)  # 도-분-초
    if m:
        d, mi, se, h = m.groups()
        val = float(d) + float(mi) / 60 + float(se) / 3600
    else:
        m = re.fullmatch(r"(\d+)-(\d+(?:\.\d+)?)\s*([NSEWnsew]?)", t)  # 도-분
        if m:
            d, mi, h = m.groups()
            val = float(d) + float(mi) / 60
        else:
            m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([NSEWnsew]?)", t)  # 십진수
            if not m:
                raise ValueError(f"좌표 형식 인식 불가: {token!r}")
            val, h = float(m.group(1)), m.group(2)
    if (h or "").upper() in ("S", "W"):
        val = -val
    return val


def parse_frontend():
    """jsx의 refPoints → {이름: (lat, lon)} (등장순 유지용 list도 반환)."""
    src = open(JSX, encoding="utf-8").read()
    m = re.search(r"refPoints\s*:\s*\[(.*?)\]", src, re.S)
    if not m:
        raise SystemExit("[refsync] 프론트 refPoints 배열을 찾지 못했습니다")
    items = re.findall(r'"([^"]+)"', m.group(1))
    out = {}
    for s in items:
        parts = [p.strip() for p in s.rsplit(",", 2)]  # 이름에 콤마가 있어도 우측 2개만 좌표
        if len(parts) != 3:
            raise SystemExit(f"[refsync] 프론트 항목 형식 오류: {s!r}")
        name, lat, lon = parts
        out[name] = (_to_decimal(lat), _to_decimal(lon))
    return out


def parse_backend():
    """backend.py의 _REF_POINTS → {이름: (lat, lon)} (산술식은 ast로 안전 계산)."""
    src = open(PY, encoding="utf-8").read()
    tree = ast.parse(src)
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", None) == "_REF_POINTS" for t in n.targets)), None)
    if node is None:
        raise SystemExit("[refsync] 백엔드 _REF_POINTS 정의를 찾지 못했습니다")
    out = {}
    for elt in node.value.elts:
        name = ast.literal_eval(elt.elts[0])
        lat = eval(compile(ast.Expression(elt.elts[1]), "<lat>", "eval"))  # 숫자·+·/ 만
        lon = eval(compile(ast.Expression(elt.elts[2]), "<lon>", "eval"))
        out[name] = (lat, lon)
    return out


def compare():
    fe, be = parse_frontend(), parse_backend()
    problems = []
    only_fe = [n for n in fe if n not in be]
    only_be = [n for n in be if n not in fe]
    if only_fe:
        problems.append(f"프론트에만 있음({len(only_fe)}): " + ", ".join(only_fe))
    if only_be:
        problems.append(f"백엔드에만 있음({len(only_be)}): " + ", ".join(only_be))
    for n in fe:
        if n in be:
            (a1, o1), (a2, o2) = fe[n], be[n]
            if abs(a1 - a2) > TOL or abs(o1 - o2) > TOL:
                problems.append(
                    f"좌표 불일치 '{n}': 프론트({a1:.4f},{o1:.4f}) ≠ 백엔드({a2:.4f},{o2:.4f})")
    return fe, be, problems


def main():
    for stream in (sys.stdout, sys.stderr):       # 콘솔 인코딩(cp949 등) 무관하게 한글·이모지 출력
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    hook = "--hook" in sys.argv
    if hook:
        try:
            data = json.load(sys.stdin)
        except Exception:
            return 0
        ti = data.get("tool_input") or {}
        paths = [ti.get("file_path", "")] + [e.get("file_path", "") for e in (ti.get("edits") or [])]
        targets = {os.path.normcase(os.path.abspath(p)) for p in (JSX, PY)}
        touched = {os.path.normcase(os.path.abspath(p)) for p in paths if p}
        if not (touched & targets):
            return 0  # 대상 파일이 아니면 조용히 통과

    fe, be, problems = compare()
    if not problems:
        if not hook:
            print(f"[refsync] ✅ 동기화 정상 — 프론트 {len(fe)}개 · 백엔드 {len(be)}개 좌표 일치")
        return 0

    head = "[refsync] ❌ 기준점 좌표 동기화 불일치 — refPoints(jsx) ↔ _REF_POINTS(backend.py)를 맞춰주세요:"
    body = "\n".join(" - " + p for p in problems)
    if hook:
        sys.stderr.write(head + "\n" + body + "\n")
        return 2  # PostToolUse: stderr를 Claude에 피드백
    print(head + "\n" + body)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
