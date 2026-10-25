@echo off
cd /d "%~dp0"
chcp 65001 >nul
title 메이플 채팅 번역기
set "APPDIR=%~dp0"
rem --- 진짜 파이썬 찾기 (윈도우 스토어 가짜 python 은 건너뜀) ---
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY for %%V in (313 312 311 310) do if not defined PY if exist "%LocalAppData%\Programs\Python\Python%%V\python.exe" set "PY="%LocalAppData%\Programs\Python\Python%%V\python.exe""
if not defined PY for %%V in (313 312 311 310) do if not defined PY if exist "%ProgramFiles%\Python%%V\python.exe" set "PY="%ProgramFiles%\Python%%V\python.exe""
if not defined PY (
  echo 파이썬을 찾을 수 없어요. 설치하기.bat 를 먼저 실행하세요.
  pause
  exit /b
)
%PY% "%APPDIR%maple_translator.py"
if errorlevel 1 pause
