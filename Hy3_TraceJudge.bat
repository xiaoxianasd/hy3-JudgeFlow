@echo off
setlocal EnableExtensions
title Hy3 TraceJudge Launcher

cd /d "%~dp0"

set "APP_COMMAND=%~1"
set "APP_EXIT_CODE=0"
set "APP_PYTHON=%~dp0.venv\Scripts\python.exe"
set "APP_CLI=%~dp0.venv\Scripts\tracejudge.exe"
set "APP_URL=http://127.0.0.1:8765"

if /I "%APP_COMMAND%"=="install" goto install
if /I "%APP_COMMAND%"=="start" goto start_app
if /I "%APP_COMMAND%"=="doctor" goto doctor
if /I "%APP_COMMAND%"=="fixtures" goto fixtures
if /I "%APP_COMMAND%"=="benchmark" goto benchmark
if /I "%APP_COMMAND%"=="test" goto tests

:menu
cls
echo ============================================================
echo                  Hy3 TraceJudge Launcher
echo ============================================================
echo.
echo   1. Start Web app ^(frontend + backend^)
echo   2. Check Hy3 TokenHub connection
echo   3. Run offline evaluator validation
echo   4. Run real Hy3 benchmark
echo   5. Run project tests
echo   6. Install or repair Python environment
echo   0. Exit
echo.
choice /C 1234560 /N /M "Select: "
if errorlevel 7 goto end
if errorlevel 6 goto install
if errorlevel 5 goto tests
if errorlevel 4 goto benchmark
if errorlevel 3 goto fixtures
if errorlevel 2 goto doctor
if errorlevel 1 goto start_app
goto menu

:ensure_environment
if exist "%APP_PYTHON%" exit /b 0
echo [Setup] Python environment is missing.
call :install_environment
exit /b %errorlevel%

:install
call :install_environment
if errorlevel 1 goto failed
echo.
echo [Done] Python environment and dependencies are installed.
goto succeeded

:install_environment
where py >nul 2>nul
if errorlevel 1 goto install_with_python
echo [1/3] Creating .venv with Python launcher ...
py -3 -m venv ".venv"
if errorlevel 1 exit /b 1
goto install_dependencies

:install_with_python
where python >nul 2>nul
if errorlevel 1 (
    echo [Error] Python 3 was not found. Install Python 3.10 or newer first.
    exit /b 1
)
echo [1/3] Creating .venv with Python ...
python -m venv ".venv"
if errorlevel 1 exit /b 1

:install_dependencies
echo [2/3] Updating pip ...
"%APP_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
echo [3/3] Installing Hy3 TraceJudge and Hypothesis ...
"%APP_PYTHON%" -m pip install -e "."
exit /b %errorlevel%

:check_config
if exist ".env" exit /b 0
echo [Config] .env is missing. Creating it from .env.example.
copy /Y ".env.example" ".env" >nul
echo.
echo Fill in these TokenHub settings in the file that opens:
echo   HY3_BASE_URL=https://tokenhub.tencentmaas.com/v1
echo   HY3_API_KEY=YOUR_NEW_API_KEY
echo   HY3_MODEL=hy3
echo.
start "" notepad.exe "%~dp0.env"
echo Save .env and run this launcher again.
exit /b 1

:start_app
call :ensure_environment
if errorlevel 1 goto failed
call :check_config
if errorlevel 1 goto failed
echo [Check] Connecting to Hy3 TokenHub ...
"%APP_CLI%" doctor
if not errorlevel 1 goto launch_web
echo.
echo [Warning] Hy3 is unavailable. The Web UI can start, but model evaluation will fail.
choice /C YN /N /M "Start the Web app anyway? [Y/N] "
if errorlevel 2 goto end

:launch_web
echo.
echo [Start] Web URL: %APP_URL%
echo [Info] Keep this backend window open. Press Ctrl+C to stop.
start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process '%APP_URL%'"
"%APP_PYTHON%" -m hy3_tracejudge serve --host 127.0.0.1 --port 8765
goto end

:doctor
call :ensure_environment
if errorlevel 1 goto failed
call :check_config
if errorlevel 1 goto failed
"%APP_CLI%" doctor
if errorlevel 1 goto failed
goto succeeded

:fixtures
call :ensure_environment
if errorlevel 1 goto failed
echo [Run] Controlled evaluator fixtures. Hy3 API is not called.
"%APP_CLI%" benchmark --source fixtures --hypothesis-examples 20
if errorlevel 1 goto failed
echo [Output] reports\fixtures_benchmark.json
goto succeeded

:benchmark
call :ensure_environment
if errorlevel 1 goto failed
call :check_config
if errorlevel 1 goto failed
echo [Check] Connecting to Hy3 TokenHub ...
"%APP_CLI%" doctor
if errorlevel 1 goto failed
echo [Run] This real Hy3 benchmark consumes API tokens.
choice /C YN /N /M "Continue? [Y/N] "
if errorlevel 2 goto end
"%APP_CLI%" benchmark --source hy3 --review-mode supervisor --hypothesis-examples 60 --output "reports\hy3_benchmark.json"
if errorlevel 1 goto failed
echo [Output] reports\hy3_benchmark.json
goto succeeded

:tests
call :ensure_environment
if errorlevel 1 goto failed
"%APP_PYTHON%" -m unittest discover -s tests -v
if errorlevel 1 goto failed
goto succeeded

:succeeded
echo.
echo Operation completed successfully.
if defined APP_COMMAND goto end
pause
goto menu

:failed
set "APP_EXIT_CODE=1"
echo.
echo Operation failed. Review the error above.
if defined APP_COMMAND goto end
pause
set "APP_EXIT_CODE=0"
goto menu

:end
endlocal & exit /b %APP_EXIT_CODE%
