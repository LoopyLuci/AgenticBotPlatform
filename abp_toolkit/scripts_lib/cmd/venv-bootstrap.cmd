@echo off
rem name: venv-bootstrap
rem description: Create .venv in the current folder with the newest installed Python, upgrade pip, install requirements.txt if present
rem params: [PYTHON_VERSION] - e.g. 3.12 (uses the py launcher); default: newest
rem safety: changes
setlocal EnableExtensions
if exist ".venv\Scripts\python.exe" (
  echo .venv already exists.
  goto :deps
)
if "%~1"=="" (py -3 -m venv .venv || python -m venv .venv) else (py -%~1 -m venv .venv)
if errorlevel 1 (
  echo Could not create the virtual environment.
  exit /b 1
)
:deps
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
if exist requirements.txt ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if exist pyproject.toml ".venv\Scripts\python.exe" -m pip install -q -e .
".venv\Scripts\python.exe" --version
echo Activate with: .venv\Scripts\activate
exit /b 0
