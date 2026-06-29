@echo off
REM Start the BarBoards backend. Double-click this, then open mlb-betting\play.html.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First-time setup: creating venv and installing dependencies...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
echo.
echo Backend running at http://localhost:8000  (Ctrl+C to stop)
echo Open mlb-betting\play.html in your browser.
echo.
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
