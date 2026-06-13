@echo off
title SAR Prototype Launcher

echo ===================================================
echo   해양사고 신속 보고 시스템 (SAR) 실행
echo ===================================================
echo.

cd /d "%~dp0"

:: --- 1/3 : Python 백엔드 의존성 확인 ---
echo [1/3] Python 백엔드 의존성 확인 중...
where python >nul 2>nul
if errorlevel 1 (
    echo [오류] Python 이 설치되어 있지 않거나 PATH 에 없습니다.
    echo        https://www.python.org 에서 설치 후 다시 실행하세요.
    pause
    exit /b 1
)
python -c "import flask, flask_cors, dotenv" >nul 2>nul
if errorlevel 1 (
    echo     필요한 패키지를 설치합니다...
    python -m pip install -r requirements.txt
)

:: --- 2/3 : 프론트엔드 의존성 확인 ---
echo [2/3] 프론트엔드 의존성 확인 중...
if not exist "node_modules" (
    echo     npm 패키지를 설치합니다...
    call npm install
)

:: --- 3/3 : 서버 실행 ---
echo [3/3] 백엔드와 프론트엔드 서버를 시작합니다...
echo.

:: 백엔드 (Flask, 포트 8000)
start "SAR-Backend" cmd /k "python backend.py"

:: 프론트엔드 (Vite, 포트 5173 ? 자동으로 브라우저 열림)
start "SAR-Frontend" cmd /k "npm run dev"

echo.
echo ===================================================
echo   실행 완료!
echo   - 백엔드  : http://localhost:8000
echo   - 프론트  : http://localhost:5173
echo.
echo   이 창은 닫아도 됩니다. 백엔드/프론트엔드는 각자의
echo   창에서 계속 실행됩니다.
echo ===================================================
timeout /t 5 /nobreak >nul