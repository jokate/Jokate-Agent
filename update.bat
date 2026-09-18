@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Agent 카태 - 업데이트

echo.
echo  ===== Agent 카태 업데이트 =====

git diff --quiet
if errorlevel 1 (
    echo  [!] 이 폴더에 커밋하지 않은 수정이 있어 업데이트를 멈춥니다.
    echo      git status 로 확인한 뒤 커밋하거나 되돌리고 다시 실행하세요.
    goto :end
)

git pull --ff-only
if errorlevel 1 (
    echo  [X] git pull 실패 ^(네트워크 또는 이 머신에만 있는 커밋^)
    goto :end
)

uv sync
if errorlevel 1 (
    echo  [X] uv sync 실패
    goto :end
)

uv run relay doctor
echo.

rem 켜져 있는 서버는 새 코드로 다시 시작한다 (진행 중인 실행이 있으면 서버가 거절하고 그대로 둔다)
netstat -ano | findstr /r /c:":8020 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    curl -s -f -X POST http://127.0.0.1:8020/admin/restart >nul 2>&1
    if errorlevel 1 (
        echo  [!] 서버를 자동으로 다시 시작하지 못했습니다 ^(진행 중인 실행이 있거나 예전 서버^).
        echo      실행이 끝나면 대시보드의 '서버 재시작' 버튼을 누르거나, 서버 창을 닫고 start.bat 을 실행하세요.
    ) else (
        echo  서버를 새 코드로 다시 시작했습니다.
    )
)
echo  ===== 업데이트 완료 =====

:end
if not "%KATAE_NONINTERACTIVE%"=="1" pause
