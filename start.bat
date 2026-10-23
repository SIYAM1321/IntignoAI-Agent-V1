@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  set "PY=python"
)

if not exist "myenv\Scripts\python.exe" (
  echo [IntignoAI] Creating virtual environment...
  %PY% -m venv myenv
  if errorlevel 1 goto :fail
)

"myenv\Scripts\python.exe" -c "import flask, dotenv, psutil, greenlet; from playwright.sync_api import sync_playwright" >nul 2>nul
if errorlevel 1 (
  echo [IntignoAI] Installing required Python packages, including Playwright...
  echo [IntignoAI] Python 3.9 uses a compatible greenlet wheel to avoid compiling C++ code.
  "myenv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 goto :installfail
)

echo [IntignoAI] Checking Playwright Chromium browser (first run downloads it automatically)...
"myenv\Scripts\python.exe" tools\ensure_playwright.py
if errorlevel 1 goto :fail

echo.
echo [IntignoAI] Starting on http://127.0.0.1:2000 ...
"myenv\Scripts\python.exe" app.py
exit /b %errorlevel%

:installfail
echo.
echo [IntignoAI] Python package installation failed.
echo [IntignoAI] Confirm internet access and check the pip error above.
echo [IntignoAI] The Python 3.9 greenlet pin is in requirements.txt; Visual C++ Build Tools should not be needed for supported Windows wheels.
goto :fail

:fail
echo.
echo [IntignoAI] Startup failed. Check Python 3.9+ and internet access for first-time dependency/browser installation.
pause
exit /b 1
