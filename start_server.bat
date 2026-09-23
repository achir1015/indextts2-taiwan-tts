@echo off
chcp 65001 >nul
setlocal
REM ============================================================
REM  台灣語音 MP3 工具 —— 啟動網站
REM  開啟後瀏覽器會自動打開 http://127.0.0.1:7861
REM ============================================================
set "BASE=%~dp0"
REM 模型快取放在 E 槽（%USERPROFILE%\.cache 權限被鎖住）
set "HF_HOME=%~dp0hf_home"
set "PY=%USERPROFILE%\indextts-py311\python.exe"
set "PYTHONUTF8=1"
set "TTS_PORT=7861"
REM 要開放到網路上時，把下一行的 REM 拿掉並改成你的密碼
REM set "TTS_PASSWORD=請改成你的密碼"

if not exist "%PY%" (
  echo 找不到 IndexTTS2 環境，請先執行 install_indextts2.bat
  pause & exit /b 1
)
if not exist "%USERPROFILE%\index-tts\checkpoints_2\gpt.pth" (
  echo 模型還沒下載完成，請先執行 install_indextts2.bat
  pause & exit /b 1
)
cd /d "%BASE%app"

if not exist "voices\candidates" (
  echo 第一次啟動：下載台灣口音聲音範本（約 3~10 分鐘）...
  "%PY%" prepare_voices.py > "%BASE%logs\prepare_voices_log.txt" 2>&1
  type "%BASE%logs\prepare_voices_log.txt"
)

if exist "%USERPROFILE%\hokkien-py311\python.exe" (
  echo 啟動台語引擎（另一個最小化視窗）...
  start "台語引擎" /min cmd /c ""%USERPROFILE%\hokkien-py311\python.exe" "%BASE%app\hokkien_worker.py" > "%BASE%logs\hokkien_log.txt" 2>&1"
)

start "" cmd /c "timeout /t 8 >nul & start http://127.0.0.1:%TTS_PORT%"
"%PY%" server.py
pause
