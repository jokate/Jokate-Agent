@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Agent 카태 - 새 머신 설치

rem  새 머신에서 이 파일 하나만 받아 실행: 저장소를 받고 setup.bat 을 이어서 실행합니다.
rem  사용법: install.bat [설치 폴더]    기본: 이 파일을 실행한 현재 폴더 아래 Jokate-Agent

set "REPO_URL=https://github.com/jokate/Jokate-Agent.git"
set "TARGET=%~1"
rem 이미 받은 저장소 안에서 실행했다면 그 자리에서 설치만 진행
if "%TARGET%"=="" if exist "%~dp0pyproject.toml" if exist "%~dp0setup.bat" (
    call "%~dp0setup.bat"
    exit /b
)
if "%TARGET%"=="" set "TARGET=%CD%\Jokate-Agent"

where git >nul 2>&1 || (
    echo  [X] git 이 없습니다. https://git-scm.com/download/win 에서 설치한 뒤 다시 실행하세요.
    if not "%KATAE_NONINTERACTIVE%"=="1" pause
    exit /b 1
)

if exist "%TARGET%\pyproject.toml" (
    echo  이미 설치되어 있습니다: %TARGET%  ^(최신으로 업데이트합니다^)
    call "%TARGET%\update.bat"
    exit /b
)

rem  (주의: echo 문구에 리다이렉트 기호를 쓰면 설치 폴더 이름의 파일이 생겨 clone 이 실패한다)
echo  저장소 받는 중: %REPO_URL%  →  %TARGET%
git clone "%REPO_URL%" "%TARGET%"
if errorlevel 1 (
    echo  [X] clone 실패
    if not "%KATAE_NONINTERACTIVE%"=="1" pause
    exit /b 1
)
call "%TARGET%\setup.bat"
