@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title Agent 카태 - 설치

rem  KATAE_NONINTERACTIVE=1 : 질문 없이 진행 (저장소 등록·서버 실행 건너뜀)

echo.
echo  ===== Agent 카태 설치 =====
echo.

rem ---------------------------------------------------------------- 1. 필수 도구
where git >nul 2>&1
if errorlevel 1 (
    echo  [X] git 이 없습니다. https://git-scm.com/download/win 에서 설치한 뒤 다시 실행하세요.
    goto :fail
)
echo  [OK] git

where uv >nul 2>&1
if errorlevel 1 (
    echo  [!] uv ^(파이썬 패키지 관리자^)가 없습니다.
    call :ask "uv 를 지금 설치할까요? (winget 사용)" || goto :fail
    winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo  [X] uv 설치 실패. https://docs.astral.sh/uv/ 안내대로 설치한 뒤 다시 실행하세요.
        goto :fail
    )
    set "PATH=%USERPROFILE%\.local\bin;%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
    where uv >nul 2>&1 || (
        echo  [!] uv 는 설치됐지만 이 창에서 아직 인식되지 않습니다. 창을 닫고 setup.bat 을 다시 실행하세요.
        goto :fail
    )
)
echo  [OK] uv

where claude >nul 2>&1
if errorlevel 1 (
    echo  [!] Claude Code CLI 가 없습니다. 릴레이 실행에 필요합니다.
    echo      설치: https://docs.claude.com/claude-code  설치 후 터미널에서 claude 를 한 번 실행해 로그인하세요.
) else (
    echo  [OK] Claude Code CLI
)

rem ---------------------------------------------------------------- 2. 의존성
echo.
echo  의존성 설치 중 (uv sync)...
uv sync
if errorlevel 1 (
    echo  [X] uv sync 실패
    goto :fail
)

rem ---------------------------------------------------------------- 3. 이 머신 전용 설정
if not exist "relay.config.local.yaml" (
    > "relay.config.local.yaml" (
        echo # This machine only ^(git-ignored^). Paths and secrets for this PC.
        echo # docs_root: C:/path/to/docs
        echo # extra_allowed_tools: ["Bash(dotnet test:*)"]
    )
    echo  [OK] relay.config.local.yaml 생성
)

rem ---------------------------------------------------------------- 4. 점검
echo.
uv run relay doctor

if "%KATAE_NONINTERACTIVE%"=="1" goto :done

rem ---------------------------------------------------------------- 5. 작업 저장소 등록
echo.
echo  작업할 저장소를 등록합니다. 이름을 비워 두고 Enter 를 누르면 넘어갑니다.
:repo_loop
set "REPO_NAME="
set "REPO_PATH="
set "REPO_VERIFY="
set "REPO_MODE="
echo.
set /p "REPO_NAME=  저장소 이름 (예: mnys): "
if "!REPO_NAME!"=="" goto :repos_done
set /p "REPO_PATH=  이 머신의 경로: "
if not exist "!REPO_PATH!\" (
    echo  [X] 폴더가 없습니다: !REPO_PATH!
    goto :repo_loop
)
set /p "REPO_VERIFY=  검증 명령 (예: uv run pytest -q, 없으면 Enter): "
set /p "REPO_MODE=  작업 방식 copy/inplace (Enter=copy, UE 처럼 무거우면 inplace): "
if "!REPO_MODE!"=="" set "REPO_MODE=copy"
set "ARGS=repo add "!REPO_NAME!" "!REPO_PATH!" --workspace !REPO_MODE!"
if not "!REPO_VERIFY!"=="" set "ARGS=!ARGS! --verify "!REPO_VERIFY!""
uv run relay !ARGS!
goto :repo_loop
:repos_done
uv run relay repos

rem ---------------------------------------------------------------- 6. 원격 접속
echo.
call :ask_no "다른 기기(휴대폰·다른 PC)에서도 이 서버에 접속하게 할까요? 토큰을 만들어 줍니다" && uv run relay remote on

rem ---------------------------------------------------------------- 7. 실행
echo.
call :ask "지금 서버를 실행하고 대시보드를 열까요?" && call "%~dp0start.bat"

:done
echo.
echo  ===== 설치 완료 =====
echo  실행: start.bat   업데이트: update.bat
if not "%KATAE_NONINTERACTIVE%"=="1" pause
exit /b 0

:fail
echo.
echo  ===== 설치 중단 =====
if not "%KATAE_NONINTERACTIVE%"=="1" pause
exit /b 1

rem ---------------------------------------------------------------- helpers
:ask
if "%KATAE_NONINTERACTIVE%"=="1" exit /b 1
set "ANSWER="
set /p "ANSWER=  %~1 [Y/n] "
if /i "!ANSWER!"=="n" exit /b 1
exit /b 0

:ask_no
if "%KATAE_NONINTERACTIVE%"=="1" exit /b 1
set "ANSWER="
set /p "ANSWER=  %~1 [y/N] "
if /i "!ANSWER!"=="y" exit /b 0
exit /b 1
