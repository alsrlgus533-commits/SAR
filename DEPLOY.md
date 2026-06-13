# 클라우드 배포 가이드 (Cloudtype)

카카오 챗봇 스킬 서버(`/kakao`)를 항상 켜진 HTTPS 주소로 띄우기 위한 배포 절차.

## 0. 사전 점검
- `.env`는 `.gitignore`에 등록됨 → **키는 GitHub에 올라가지 않음**. 키 값은 Cloudtype 환경변수로 따로 입력한다.
- 운영 서버는 `gunicorn`으로 구동 (로컬 Windows 개발은 `python backend.py` 그대로).

## 1. GitHub에 올리기 (최초 1회)
```powershell
cd D:\SAR
git init
git add .
git commit -m "해양사고 신속보고 백엔드 + 카카오 스킬서버"
git branch -M main
git remote add origin https://github.com/<내계정>/<레포명>.git
git push -u origin main
```
> 푸시 후 GitHub 레포에 `.env`가 **없는지** 반드시 확인.

## 2. Cloudtype 배포
1. https://cloudtype.io 로그인 → **프로젝트 생성**
2. **+ 서비스 → GitHub 저장소 연결** → 위 레포 선택
3. 프레임워크: **Python** 선택 (Python 3.12 권장)
4. **시작 명령어(Start Command)**:
   ```
   gunicorn backend:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120
   ```
   (Procfile에도 동일 명령이 있어 자동 인식될 수 있음)
5. **포트**: `$PORT` 자동 주입 사용. 포트 입력란이 따로 있으면 `8000` 지정 후 명령어의 `$PORT`를 `8000`으로 고정.
6. **환경변수(Variables)** 등록 — `.env` 내용을 그대로:
   - `KOMSA_KEY` (필수)
   - `KMA_KEY` (필수)
   - `GEMINI_KEY` 또는 `ANTHROPIC_KEY` (자연어 파싱용 — 없으면 규칙 파서로 동작)
7. **배포(Deploy)** → 완료되면 `https://<서비스>.cloudtype.app` 형태의 HTTPS 주소 발급

## 3. 카카오 오픈빌더 연결
- 스킬 URL = `https://<서비스>.cloudtype.app/kakao`
- 오픈빌더 → [스킬] 생성에 위 URL 입력 → 블록 봇 응답에 스킬 연결 → **콜백 사용 ON** → 배포
- 자세한 절차는 `CLAUDE.md`의 "카카오톡 챗봇" 절 참조

## 4. 확인
```powershell
curl.exe -X POST "https://<서비스>.cloudtype.app/kakao" -H "Content-Type: application/json" -d "{\"userRequest\":{\"utterance\":\"섬사랑12호 추자도 북동방 2해리 여객 28명 승무원 4명 폐그물 감김\"}}"
```
→ 보고서 JSON이 오면 정상. (콜백 없는 동기 폴백 응답으로 내용 확인 가능)

## 주의
- 무료/슬립 인스턴스는 콜드스타트로 카카오 5초 제한을 넘길 수 있음 → **상시 가동 인스턴스** 사용 권장.
- `/kakao`는 인증이 없으므로, 운영 시 추측 어려운 경로 사용 또는 토큰 검증 추가 권장.
- 코드 수정 후 `git push` 하면 Cloudtype가 자동 재배포(설정 시).
