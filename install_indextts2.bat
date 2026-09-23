@echo off
chcp 65001 >nul
setlocal
REM ============================================================
REM  IndexTTS2 自動安裝腳本 v2
REM  改用 Anaconda 建立 Python 3.11 環境（避免 Windows 應用程式控制封鎖）
REM  詳細紀錄寫入 logs\install_log.txt
REM ============================================================
set "BASE=%~dp0"
set "LOG=%BASE%logs\install_log.txt"
set "REPO=%USERPROFILE%\index-tts"
set "ENV=%USERPROFILE%\indextts-py311"
set "PY=%ENV%\python.exe"
set "CONDA=C:\ProgramData\anaconda3\Scripts\conda.exe"
if not exist "%CONDA%" set "CONDA=%USERPROFILE%\anaconda3\Scripts\conda.exe"
set "CONDA_PLUGINS_AUTO_ACCEPT_TOS=yes"
set "PYTHONUTF8=1"
set "UV_LINK_MODE=copy"
set "UV_PYTHON_DOWNLOADS=never"
if not exist "%BASE%logs" mkdir "%BASE%logs"
echo ==== START v2 %DATE% %TIME% ==== > "%LOG%"

echo [1/7] 檢查顯示卡 ...
echo ### STEP1 GPU >> "%LOG%"
powershell -NoProfile -Command "Get-CimInstance Win32_VideoController | ForEach-Object { 'GPU: ' + $_.Name }; 'RAM_GB: ' + [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1); 'CPU: ' + (Get-CimInstance Win32_Processor | Select-Object -First 1).Name" >> "%LOG%" 2>&1
set "TORCH_INDEX=https://download.pytorch.org/whl/cpu"
where nvidia-smi >nul 2>&1 && set "TORCH_INDEX=https://download.pytorch.org/whl/cu128"
if exist "C:\Windows\System32\nvidia-smi.exe" set "TORCH_INDEX=https://download.pytorch.org/whl/cu128"
echo TORCH_INDEX=%TORCH_INDEX% >> "%LOG%"

echo [2/7] 用 Anaconda 建立 Python 3.11 環境 ...
echo ### STEP2 CONDA >> "%LOG%"
if not exist "%CONDA%" (echo NO_CONDA >> "%LOG%" & goto :fail)
if not exist "%PY%" (
  "%CONDA%" create -y -p "%ENV%" python=3.11 >> "%LOG%" 2>&1
)
"%PY%" -c "import sys; print('PYTHON_OK', sys.version)" >> "%LOG%" 2>&1 || (echo PY_BLOCKED >> "%LOG%" & goto :fail)

echo [3/7] 準備套件清單 ...
echo ### STEP3 EXPORT >> "%LOG%"
cd /d "%REPO%" || (echo NO_REPO >> "%LOG%" & goto :fail)
uv export --frozen --no-hashes --extra webui --no-emit-project --no-dev -o "%BASE%logs\req_full.txt" >> "%LOG%" 2>&1 || (echo EXPORT_FAIL >> "%LOG%" & goto :fail)
findstr /v /r /c:"^torch==" /c:"^torchaudio==" /c:"^torchvision==" "%BASE%logs\req_full.txt" > "%BASE%logs\req.txt"

echo [4/7] 安裝 PyTorch（沒有 NVIDIA 顯示卡時裝 CPU 版）...
echo ### STEP4 TORCH >> "%LOG%"
uv pip install --python "%PY%" torch==2.8.0 torchaudio==2.8.0 --index-url %TORCH_INDEX% >> "%LOG%" 2>&1 || (echo TORCH_FAIL >> "%LOG%" & goto :fail)

echo [5/7] 安裝其他套件（需要一段時間）...
echo ### STEP5 DEPS >> "%LOG%"
uv pip install --python "%PY%" -r "%BASE%logs\req.txt" imageio-ffmpeg opencc-python-reimplemented soundfile >> "%LOG%" 2>&1 || (echo DEPS_FAIL >> "%LOG%" & goto :fail)
"%PY%" tools\gpu_check.py >> "%LOG%" 2>&1

echo [6/7] 下載 IndexTTS-2 模型（約 6 GB）...
echo ### STEP6 MODEL >> "%LOG%"
"%PY%" -c "from huggingface_hub import snapshot_download; snapshot_download('IndexTeam/IndexTTS-2', local_dir='checkpoints_2')" >> "%LOG%" 2>&1 || (echo MODEL_FAIL >> "%LOG%" & goto :fail)

echo [7/7] 自我測試（第一次會再下載輔助模型，CPU 會比較久）...
echo ### STEP7 SELFTEST >> "%LOG%"
"%PY%" "%BASE%app\selftest.py" >> "%LOG%" 2>&1 || (echo SELFTEST_FAIL >> "%LOG%" & goto :fail)

echo ==== ALL_DONE %DATE% %TIME% ==== >> "%LOG%"
echo.
echo 安裝完成！接著雙擊 start_server.bat 啟動網站。
pause
exit /b 0

:fail
echo ==== FAILED %DATE% %TIME% ==== >> "%LOG%"
echo.
echo 安裝失敗，請查看 logs\install_log.txt（或告訴 Claude）
pause
exit /b 1
