@echo off
chcp 65001 >nul
title 메이플 채팅 번역기 - 설치
cd /d "%~dp0"
echo ==========================================
echo   메이플 채팅 번역기 설치 (처음 한 번만)
echo ==========================================
echo.
rem --- 진짜 파이썬 찾기 (윈도우 스토어 가짜 python 은 건너뜀) ---
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY for %%V in (313 312 311 310) do if not defined PY if exist "%LocalAppData%\Programs\Python\Python%%V\python.exe" set "PY="%LocalAppData%\Programs\Python\Python%%V\python.exe""
if not defined PY for %%V in (313 312 311 310) do if not defined PY if exist "%ProgramFiles%\Python%%V\python.exe" set "PY="%ProgramFiles%\Python%%V\python.exe""
if not defined PY (
  echo [1/3] 파이썬이 없어서 설치합니다. 잠시 기다려 주세요...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY="%LocalAppData%\Programs\Python\Python312\python.exe""
)
if not defined PY (
  echo.
  echo 파이썬 자동 설치가 안 됐어요.
  echo https://www.python.org/downloads/ 에서 직접 설치하고,
  echo 설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크한 뒤 이 파일을 다시 실행하세요.
  pause
  exit /b
)
echo [1/3] 파이썬 확인 완료
%PY% --version
echo.
echo [2/3] 필요한 부품 설치 중...
%PY% -m pip install --upgrade pip >nul 2>nul
%PY% -m pip install mss pillow winocr deep-translator keyboard requests
if errorlevel 1 (
  echo.
  echo 설치 중 오류가 났어요. 화면을 캡처해서 보내주세요.
  pause
  exit /b
)
echo.
echo [3/3] 영어 OCR 언어팩 확인 중...
%PY% -c "from winrt.windows.media.ocr import OcrEngine; from winrt.windows.globalization import Language; import sys; sys.exit(0 if OcrEngine.is_language_supported(Language('en-US')) else 1)" >nul 2>nul
if errorlevel 1 (
  echo 영어 OCR 언어팩을 설치합니다. "디바이스를 변경하도록 허용" 창이 뜨면 [예]를 눌러주세요. 몇 분 걸릴 수 있어요...
  powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -Command Add-WindowsCapability -Online -Name Language.OCR~~~en-US~0.0.1.0'"
  %PY% -c "from winrt.windows.media.ocr import OcrEngine; from winrt.windows.globalization import Language; import sys; sys.exit(0 if OcrEngine.is_language_supported(Language('en-US')) else 1)" >nul 2>nul
  if errorlevel 1 (
    echo 영어 OCR 언어팩 설치가 안 됐어요. [설정 - 시간 및 언어 - 언어 및 지역 - 언어 추가]에서 English ^(United States^) 를 추가해 주세요.
    echo ^(언어팩이 없어도 번역기는 한국어 OCR로 대신 동작해요^)
  ) else (
    echo 영어 OCR 언어팩 설치 완료
  )
) else (
  echo 이미 설치되어 있어요.
)
echo.
echo ==========================================
echo   설치 완료! 이제 실행하기.bat 를 실행하세요.
echo ==========================================
pause
