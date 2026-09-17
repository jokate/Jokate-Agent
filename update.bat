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
echo  ===== 업데이트 완료 — 서버가 켜져 있었다면 창을 닫고 start.bat 으로 다시 실행하세요 =====

:end
if not "%KATAE_NONINTERACTIVE%"=="1" pause
