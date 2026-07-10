@echo off
setlocal
cd /d "%~dp0"

set "APP_NAME=Infinite Agent Work"
set "PORT=3000"
set "LOCAL_URL=http://127.0.0.1:%PORT%/"
set "PYEXE="
set "PYARGS="

echo ============================================
echo    %APP_NAME%
echo ============================================
echo.

call :find_python
if errorlevel 1 goto :no_python

call :check_python_version
if errorlevel 1 goto :bad_python

call :ensure_pip
if errorlevel 1 goto :pip_failed

call :ensure_dependencies
if errorlevel 1 goto :deps_failed

if defined LAUNCHER_CHECK_ONLY (
    echo [OK] Launcher checks passed.
    exit /b 0
)

call :detect_lan_ip
set "APP_URL=http://%LAN_IP%:%PORT%/"

echo.
echo Visit: %APP_URL%
echo Local: %LOCAL_URL%
echo Press Ctrl+C to stop.
echo.

start /b cmd /c "timeout /t 3 /nobreak >nul && start %APP_URL%"
"%PYEXE%" %PYARGS% main.py
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Server stopped. Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%

:find_python
if defined PYEXE_OVERRIDE (
    if exist "%PYEXE_OVERRIDE%" (
        set "PYEXE=%PYEXE_OVERRIDE%"
        set "PYARGS="
        exit /b 0
    )
)

if exist "%~dp0python\python.exe" (
    set "PYEXE=%~dp0python\python.exe"
    set "PYARGS="
    exit /b 0
)

if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYEXE=%~dp0.venv\Scripts\python.exe"
    set "PYARGS="
    exit /b 0
)

py -3 --version >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=py"
    set "PYARGS=-3"
    exit /b 0
)

python --version >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=python"
    set "PYARGS="
    exit /b 0
)

python3 --version >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=python3"
    set "PYARGS="
    exit /b 0
)

exit /b 1

:check_python_version
"%PYEXE%" %PYARGS% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
exit /b %ERRORLEVEL%

:ensure_pip
"%PYEXE%" %PYARGS% -m pip --version >nul 2>nul
if not errorlevel 1 exit /b 0

echo [INFO] pip not found. Trying to enable pip...
"%PYEXE%" %PYARGS% -m ensurepip --upgrade
exit /b %ERRORLEVEL%

:ensure_dependencies
"%PYEXE%" %PYARGS% -c "import fastapi, uvicorn, httpx, PIL, requests, pydantic, multipart, websockets" >nul 2>nul
if not errorlevel 1 exit /b 0

echo [INFO] Installing required Python packages...
if exist "%~dp0packages" (
    "%PYEXE%" %PYARGS% -m pip install --no-index --find-links="%~dp0packages" -r requirements.txt
    if not errorlevel 1 exit /b 0
    echo [WARN] Offline install failed. Trying online install...
)

"%PYEXE%" %PYARGS% -m pip install -r requirements.txt
exit /b %ERRORLEVEL%

:detect_lan_ip
set "LAN_IP=127.0.0.1"
for /f "usebackq delims=" %%I in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$ip=(Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address } | Select-Object -First 1 -ExpandProperty IPv4Address).IPAddress; if(-not $ip){$ip=(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Sort-Object InterfaceMetric | Select-Object -First 1 -ExpandProperty IPAddress)}; if($ip){$ip}else{'127.0.0.1'}" 2^>nul`) do set "LAN_IP=%%I"
exit /b 0

:no_python
echo [ERROR] Python 3.10+ was not found.
echo.
echo Install Python from https://www.python.org/downloads/
echo Or place a bundled runtime at:
echo   %~dp0python\python.exe
echo.
pause
exit /b 1

:bad_python
echo [ERROR] Python 3.10+ is required.
echo Current runtime:
"%PYEXE%" %PYARGS% --version
echo.
pause
exit /b 1

:pip_failed
echo [ERROR] Could not enable pip for this Python runtime.
echo.
pause
exit /b 1

:deps_failed
echo [ERROR] Dependency installation failed.
echo Check your network, or put offline wheels in the packages folder.
echo.
pause
exit /b 1
