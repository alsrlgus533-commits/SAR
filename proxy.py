# -*- coding: utf-8 -*-
"""
해양사고 신속 보고 프로토타입용 로컬 CORS 프록시
- 브라우저는 보안 정책(CORS) 때문에 공공데이터포털·기상청 API를 직접 호출하지 못할 수 있습니다.
- 이 스크립트를 실행하면 http://localhost:8000 프록시가 열리고,
  프로토타입 ⚙설정의 '프록시 주소'에 http://localhost:8000 을 입력하면 우회됩니다.

실행 방법 (Python 3만 있으면 됩니다. 추가 설치 불필요):
    python proxy.py
"""
import json
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

# 보안: 허용된 API 호스트로만 중계
ALLOWED_HOSTS = (
    "apis.data.go.kr",
    "apihub.kma.go.kr",
)

PORT = 8000


class ProxyHandler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/fetch":
            self._reply(404, {"error": "사용법: /fetch?url=<인코딩된 대상 URL>"})
            return
        qs = urllib.parse.parse_qs(parsed.query)
        target = (qs.get("url") or [""])[0]
        host = urllib.parse.urlparse(target).hostname or ""
        if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
            self._reply(403, {"error": f"허용되지 않은 호스트: {host}"})
            return
        try:
            req = urllib.request.Request(target, headers={"User-Agent": "rapid-report-prototype/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read()
                ctype = r.headers.get("Content-Type", "text/plain; charset=utf-8")
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._reply(502, {"error": str(e)})

    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("[proxy]", fmt % args)


if __name__ == "__main__":
    print(f"프록시 실행 중: http://localhost:{PORT}  (중지: Ctrl+C)")
    print("프로토타입 ⚙설정 → 프록시 주소에 http://localhost:8000 입력")
    HTTPServer(("0.0.0.0", PORT), ProxyHandler).serve_forever()
