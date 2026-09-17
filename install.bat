@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Agent 카태 - 새 머신 설치

rem  새 머신에서 이 파일 하나만 받아 실행: 저장소를 받고 setup.bat 을 이어서 실행합니다.
rem  사용법: install.bat [설치 폴더]    기본 %USERPROFILE%\Projects\Jokate-Agent

set "REPO_URL=https://github.com/jokate/Jokate-Agent.git"
set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=%USERPROFILE%\Projects\Jokate-Agent"

where git >nul 2>&1 || (
    echo  [X] git 이 없습니다. https://git-scm.com/download/win 에서 설치한 뒤 다시 실행하세요.
    pause
    exit /b 1
)

if exist "%TARGET%\pyproject.toml" (
    echo  이미 설치되어 있습니다: %TARGET%  ^(최신으로 업데이트합니다^)
    call "%TARGET%\update.bat"
    exit /b
)

echo  저장소 받는 중: %REPO_URL%  ->  %TARGET%
git clone "%REPO_URL%" "%TARGET%"
if errorlevel 1 (
    echo  [X] clone 실패
    pause
    exit /b 1
)
call "%TARGET%\setup.bat"
