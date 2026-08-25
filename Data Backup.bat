@echo off
REM ===========================================================================
REM  Launch the floating C: -> S: data-backup status widget (datasync package).
REM
REM  Version-controlled here so a clone / install of datasync has a double-click
REM  launcher. Copy it to the Desktop if you like.
REM
REM  Design notes (two are not obvious):
REM  * pythonw.exe, NOT python.exe: pythonw is the console-less build, so the
REM    widget runs with no black console window sitting behind it.
REM  * The import is checked FIRST, synchronously, with python.exe -- pythonw has
REM    no stdout/stderr, so a failure to import (wrong env, missing tkinter) would
REM    otherwise be COMPLETELY SILENT: double-click, nothing happens, no clue why.
REM    Checking first means loud on failure, silent on success.
REM
REM  Environment: `python` / `pythonw` are taken from PATH, i.e. whichever
REM  environment datasync is installed in and currently activated. If you launch
REM  this from a shell that does NOT have that environment on PATH (e.g. a bare
REM  double-click and datasync lives in a conda/venv), uncomment ENVDIR below and
REM  point it at that environment.
REM ===========================================================================
setlocal

REM -- optional: pin to a specific environment ---------------------------------
REM set "ENVDIR=C:\Users\slab\miniconda3\envs\tprocv2_365"
REM set "PY=%ENVDIR%\python.exe"
REM set "PYW=%ENVDIR%\pythonw.exe"

if not defined PY  set "PY=python"
if not defined PYW set "PYW=pythonw"
set "LOG=%TEMP%\datasync_widget_launch.log"

REM -- fail loudly here rather than silently under console-less pythonw ---------
"%PY%" -c "import datasync.data_sync_ui" 2>"%LOG%"
if errorlevel 1 (
    echo.
    echo Could not load the datasync backup widget. Details:
    echo -----------------------------------------------------------------
    type "%LOG%"
    echo -----------------------------------------------------------------
    echo.
    echo If datasync lives in a conda/venv, activate it first, or uncomment and
    echo edit ENVDIR at the top of this file to that environment.
    pause
    exit /b 1
)

REM `start ""` returns immediately, so this script exits and its cmd window
REM closes instead of sitting open as the widget's parent.
start "" "%PYW%" -m datasync.data_sync_ui
endlocal
