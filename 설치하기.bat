@echo off
chcp 65001 >nul
title 메이플 채팅 번역기 - 설치
cd /d "%~dp0"
echo ==========================================
echo   메이플 채팅 번역기 설치 (처음 한 번만)
echo ==========================================
echo.
where py >nul 2>nul && (set PY=py -3) || (
  where python >nul 2>nul && (set PY=python) || (set PY=)
)
if "%PY%"=="" (
  echo [1/3] 파이썬이 없어서 설치합니다. 잠시 기다려 주세요...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  echo.
  echo 파이썬 설치가 끝났어요. 이 창을 닫고 설치하기.bat 를 한 번 더 실행해 주세요.
  echo ^(winget 오류가 나면 https://www.python.org/downloads/ 에서 설치하고,
  echo  설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크하세요.^)
  pause
  exit /b
)
echo [1/3] 파이썬 확인 완료
%PY% --version
echo.
echo [2/3] 필요한 부품 설치 중...
%PY% -m pip install --upgrade pip >nul
%PY% -m pip install mss pillow winocr deep-translator keyboard
if errorlevel 1 (
  echo.
  echo 설치 중 오류가 났어요. 화면을 캡처해서 Claude에게 보내주세요.
  pause
  exit /b
)
echo.
echo [3/3] 영어 OCR 언어팩 확인 중...
powershell -NoProfile -Command "if ((Get-WindowsCapability -Online -Name 'Language.OCR~~~en-US~0.0.1.0').State -ne 'Installed') { Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -Command Add-WindowsCapability -Online -Name Language.OCR~~~en-US~0.0.1.0' } else { Write-Host '이미 설치되어 있어요.' }"
echo.
echo ==========================================
echo   설치 완료! 이제 실행하기.bat 를 실행하세요.
echo ==========================================
pause
