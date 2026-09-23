@echo off
chcp 65001 >nul
setlocal
REM ============================================================
REM  （選配）安裝台語引擎：MERaLiON OmniVoice Hokkien（實驗性）
REM  獨立環境 hokkien_env，不影響 IndexTTS2。約需 5~6 GB。
REM ============================================================
set "BASE=%~dp0"
REM 模型快取放在 E 槽（%USERPROFILE%\.cache 權限被鎖住）
set "HF_HOME=%~dp0hf_home"
set "LOG=%BASE%logs\hokkien_install_log.txt"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
set "UV_LINK_MODE=copy"
set "HENV=%USERPROFILE%\hokkien-py311"
set "CONDA=C:\ProgramData\anaconda3\Scripts\conda.exe"
set "CONDA_PLUGINS_AUTO_ACCEPT_TOS=yes"
set "TORCH_INDEX=https://download.pytorch.org/whl/cpu"
if exist "C:\Windows\System32\nvidia-smi.exe" set "TORCH_INDEX=https://download.pytorch.org/whl/cu128"
if not exist "%BASE%logs" mkdir "%BASE%logs"
echo ==== START %DATE% %TIME% ==== > "%LOG%"
cd /d "%BASE%"

echo [1/3] 建立台語引擎專用 Python 環境 ...
if not exist "%HENV%\python.exe" "%CONDA%" create -y -p "%HENV%" python=3.11 >> "%LOG%" 2>&1
"%HENV%\python.exe" -c "print(1)" >> "%LOG%" 2>&1 || goto :fail
echo [2/3] 安裝 PyTorch 與 OmniVoice（需要一段時間）...
uv pip install --python "%HENV%\python.exe" torch==2.8.0 torchaudio==2.8.0 --index-url %TORCH_INDEX% >> "%LOG%" 2>&1 || goto :fail
uv pip install --python "%HENV%\python.exe" omnivoice soundfile >> "%LOG%" 2>&1 || goto :fail
echo [3/3] 下載台語模型並測試 ...
"%HENV%\python.exe" "%BASE%app\hokkien_worker.py" --selftest "%BASE%logs\hokkien_test.wav" >> "%LOG%" 2>&1 || goto :fail
echo ==== ALL_DONE ==== >> "%LOG%"
echo 台語引擎安裝完成！重新執行 start_server.bat 即可使用。
pause
exit /b 0
:fail
echo ==== FAILED ==== >> "%LOG%"
echo 安裝失敗，請查看 logs\hokkien_install_log.txt
pause
exit /b 1
