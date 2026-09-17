@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Agent 카태 - 서버

rem  사용법: start.bat [포트]      기본 8020
rem  KATAE_NO_BROWSER=1 이면 브라우저를 열지 않음

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8020"
set "URL=http://127.0.0.1:%PORT%"

where uv >nul 2>&1 || (
    echo  [X] uv 가 없습니다. setup.bat 을 먼저 실행하세요.
    pause
    exit /b 1
)

rem 이미 켜져 있으면 대시보드만 연다
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo  서버가 이미 %URL% 에서 실행 중입니다.
    if not "%KATAE_NO_BROWSER%"=="1" start "" "%URL%"
    exit /b 0
)

echo  Agent 카태 서버 시작: %URL%   ^(끄려면 이 창에서 Ctrl+C^)
if not "%KATAE_NO_BROWSER%"=="1" (
    start "" /b cmd /c "timeout /t 4 /nobreak >nul & start "" "%URL%""
)
uv run relay serve --port %PORT%
