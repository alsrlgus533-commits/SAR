# -*- coding: utf-8 -*-
"""회사 내부망 PC용 '현재 승선인원' 중계 에이전트.

[배경] 전국 각 센터가 회사 '내부 전용 시스템'(웹 화면, ID/PW 로그인)에 항차 중
       선박별 현재 승선인원을 입력한다. 이 자료는 외부망(클라우드 백엔드)에서
       직접 못 보고 내부망에서만 보인다. 그래서 내부망+외부망이 모두 연결된
       이 회사 PC에서 에이전트가:
         ① 내부 시스템 로그인 → 선박 목록 표 조회 → 센터별 상세에서 승선인원 파싱
         ② 결과를 외부망으로 클라우드 백엔드 POST /pax 에 주기적으로 전송
       하면, 백엔드가 저장하고 보고서에서 (신선하면) MTIS 출항전 점검표보다 우선 사용한다.

[보안] 내부 시스템 ID/PW는 이 PC의 `pax_agent.env` 에만 둔다(클라우드에 절대 안 올림).
       백엔드로 가는 건 결과 숫자 + PAX_TOKEN 뿐.

[실행] (회사 내부망 PC에서)
    pip install requests python-dotenv
    # pax_agent.env 를 채운 뒤
    python pax_agent.py            # 주기 실행(기본 3분)
    python pax_agent.py --once     # 1회만 실행(테스트)

[맞춤 필요 — 2곳] 화면마다 HTML이 달라 아래 두 함수만 실제 화면에 맞춰 채운다:
    parse_vessel_list() / parse_pax_detail()
  ('사이트별 맞춤' 표시. 실제 HTML을 확보하면 정규식/선택자를 확정한다.)
"""
import argparse
import logging
import os
import re
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv("pax_agent.env")           # 이 파일에 내부 자격증명·설정 보관(.gitignore)

# ── 설정(모두 pax_agent.env 에서) ───────────────────────────────────
INTERNAL_BASE = os.environ.get("INTERNAL_BASE", "").rstrip("/")        # 예: https://내부시스템.회사.local
INTERNAL_ID = os.environ.get("INTERNAL_ID", "")
INTERNAL_PW = os.environ.get("INTERNAL_PW", "")
LOGIN_PATH = os.environ.get("INTERNAL_LOGIN_PATH", "/login")           # 로그인 폼 action 경로
ID_FIELD = os.environ.get("INTERNAL_ID_FIELD", "userId")              # 로그인 폼의 아이디 input name
PW_FIELD = os.environ.get("INTERNAL_PW_FIELD", "password")            # 로그인 폼의 비밀번호 input name
LIST_PATH = os.environ.get("INTERNAL_LIST_PATH", "")                  # 선박 목록 표 화면 경로
DETAIL_PATH = os.environ.get("INTERNAL_DETAIL_PATH", "")             # 센터별 상세 화면 경로(템플릿)

BACKEND_PAX_URL = os.environ.get("BACKEND_PAX_URL", "")              # 예: https://sarchatbot.duckdns.org/pax
PAX_TOKEN = os.environ.get("PAX_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 180))            # 조회·전송 주기(초). 가볍게 — 사람 손 속도
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", 15))
VERIFY_TLS = os.environ.get("VERIFY_TLS", "1") != "0"               # 사내 사설 인증서면 0

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [pax_agent] %(levelname)s %(message)s")
log = logging.getLogger("pax_agent")


# ── 유틸 ────────────────────────────────────────────────────────────
def _strip_tags(html: str) -> str:
    """태그 제거 + 공백 정리(간단 파싱용)."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def _to_int(s):
    """'12명', '1,234', '' → 정수 또는 None."""
    if s is None:
        return None
    m = re.search(r"-?\d[\d,]*", str(s))
    return int(m.group(0).replace(",", "")) if m else None


def _to_num(s):
    """'12.5 M/T', '1,234.0', '' → 실수 또는 None (화물 M/T용, 소수 보존)."""
    if s is None:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(s))
    if not m:
        return None
    v = float(m.group(0).replace(",", ""))
    return int(v) if v == int(v) else v


def _looks_like_login(html: str) -> bool:
    """응답이 로그인 화면이면 True(세션 만료 감지용) — 화면에 맞게 키워드 조정."""
    h = html or ""
    return (PW_FIELD in h and "</form>" in h) or "로그인" in h[:2000]


# ── 로그인 ──────────────────────────────────────────────────────────
def login(s: requests.Session) -> bool:
    if not (INTERNAL_BASE and INTERNAL_ID and INTERNAL_PW):
        log.error("INTERNAL_BASE/ID/PW 미설정 — pax_agent.env 를 확인하세요")
        return False
    url = INTERNAL_BASE + LOGIN_PATH
    try:
        # 일부 사이트는 GET으로 폼/CSRF 토큰을 먼저 받아야 한다.
        # CSRF가 있으면 여기서 추출해 payload에 추가한다(사이트별 — 필요 시 확정).
        s.get(url, timeout=HTTP_TIMEOUT, verify=VERIFY_TLS)
        payload = {ID_FIELD: INTERNAL_ID, PW_FIELD: INTERNAL_PW}
        r = s.post(url, data=payload, timeout=HTTP_TIMEOUT, verify=VERIFY_TLS,
                   allow_redirects=True)
        ok = r.status_code < 400 and not _looks_like_login(r.text)
        log.info("로그인 %s (status=%s)", "성공" if ok else "실패", r.status_code)
        return ok
    except requests.RequestException as e:
        log.error("로그인 오류: %s", e)
        return False


# ── 사이트별 맞춤 ① 선박 목록 표 파싱 ════════════════════════════════
# 목록 화면 HTML에서 (선박명, 상세조회에 필요한 키)를 뽑는다.
# 실제 표 구조(컬럼·링크 파라미터)를 확인하면 아래 정규식을 확정한다.
def parse_vessel_list(html: str):
    """→ [{"선박명": str, "선박코드": str, "detail_params": {..}}] 목록.

    예시(가정): <tr> 안에 선박명과 상세 링크(센터/항차 키)가 있는 표.
    실제 HTML을 받으면 컬럼 인덱스/파라미터명을 맞춘다.
    """
    vessels = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = [_strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
        if len(cells) < 2:
            continue
        # TODO(사이트별): 선박명이 들어있는 컬럼 위치를 확정. 헤더행은 건너뜀.
        name = cells[0]
        if not name or name in ("선박명", "선명"):
            continue
        # TODO(사이트별): 상세 조회 링크의 파라미터(센터코드·선박코드·항차 등) 추출
        params = {}
        m = re.search(r'href="[^"]*?[?&]([^"]+)"', row)
        if m:
            for kv in m.group(1).split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    params[k] = v
        vessels.append({"선박명": name, "선박코드": params.get("psnshpCd", ""),
                        "detail_params": params})
    return vessels


# ── 사이트별 맞춤 ② 센터별 상세에서 승선인원 파싱 ═══════════════════════
def parse_pax_detail(html: str):
    """상세 화면 HTML → {"여객":int|None, "승무원":.., "차량":.., "화물":float|int|None, "대인":.., "소인":.., "유아":..}.

    실제 라벨(예: '승선인원', '여객', '선원', '차량', '화물')에 맞춰 정규식을 확정한다.
    화물은 실적재중량(M/T)이라 소수일 수 있어 _to_num 으로 뽑는다.
    """
    text = _strip_tags(html)

    def near(label):
        # '여객 31명' / '여객: 31' 등 라벨 뒤 첫 숫자(정수)
        m = re.search(label + r"\s*[:：]?\s*(-?\d[\d,]*)", text)
        return _to_int(m.group(1)) if m else None

    def near_num(label):
        # '화물 12.5 M/T' / '적재중량: 12.5' 등 라벨 뒤 첫 숫자(소수 허용)
        m = re.search(label + r"\s*[:：]?\s*(-?\d[\d,]*\.?\d*)", text)
        return _to_num(m.group(1)) if m else None

    return {
        "여객": near("여객") or near("승객"),
        "승무원": near("선원") or near("승무원"),
        "차량": near("차량"),
        "화물": near_num("화물") or near_num("적재중량") or near_num("실적재"),
        "대인": near("대인") or near("성인"),
        "소인": near("소인") or near("소아"),
        "유아": near("유아"),
    }
# ════════════════════════════════════════════════════════════════════


def fetch_vessels(s: requests.Session):
    r = s.get(INTERNAL_BASE + LIST_PATH, timeout=HTTP_TIMEOUT, verify=VERIFY_TLS)
    if _looks_like_login(r.text):
        raise PermissionError("세션 만료(목록)")
    return parse_vessel_list(r.text)


def fetch_pax(s: requests.Session, vessel: dict):
    path = DETAIL_PATH or LIST_PATH
    r = s.get(INTERNAL_BASE + path, params=vessel.get("detail_params") or {},
              timeout=HTTP_TIMEOUT, verify=VERIFY_TLS)
    if _looks_like_login(r.text):
        raise PermissionError("세션 만료(상세)")
    return parse_pax_detail(r.text)


def push(name: str, code: str, fields: dict) -> bool:
    """결과를 백엔드 /pax 로 전송. 값이 하나도 없으면 건너뜀."""
    body = {"name": name, "선박코드": code or "", "token": PAX_TOKEN,
            **{k: v for k, v in fields.items() if v is not None}}
    if all(body.get(k) is None for k in ("여객", "승무원", "차량", "화물", "대인", "소인", "유아")):
        return False
    try:
        r = requests.post(BACKEND_PAX_URL, json=body, timeout=HTTP_TIMEOUT,
                          headers={"X-Pax-Token": PAX_TOKEN})
        if r.status_code == 200:
            return True
        log.warning("전송 실패 %s: %s %s", name, r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.warning("전송 오류 %s: %s", name, e)
    return False


def run_once(s: requests.Session) -> int:
    vessels = fetch_vessels(s)
    log.info("선박 %d척 조회", len(vessels))
    sent = 0
    for v in vessels:
        try:
            fields = fetch_pax(s, v)
        except PermissionError:
            raise
        except Exception as e:
            log.warning("상세 조회 실패 %s: %s", v.get("선박명"), e)
            continue
        if push(v.get("선박명", ""), v.get("선박코드", ""), fields):
            sent += 1
    log.info("전송 완료 %d척", sent)
    return sent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="1회만 실행(테스트)")
    args = ap.parse_args()

    if not BACKEND_PAX_URL:
        log.error("BACKEND_PAX_URL 미설정 — pax_agent.env 를 확인하세요")
        sys.exit(1)

    s = requests.Session()
    s.headers.update({"User-Agent": "pax-agent/1.0"})
    if not login(s):
        sys.exit(2)

    while True:
        try:
            run_once(s)
        except PermissionError:
            log.info("세션 만료 — 재로그인")
            if not login(s):
                log.error("재로그인 실패 — 다음 주기에 재시도")
        except Exception as e:
            log.exception("주기 실행 오류: %s", e)
        if args.once:
            break
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
