@echo off
cd /d "%~dp0"
chcp 65001 >nul
title 메이플 채팅 번역기
set "APPDIR=%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% "%APPDIR%maple_translator.py"
if errorlevel 1 pause
