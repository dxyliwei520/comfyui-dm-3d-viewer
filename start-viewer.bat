@echo off
chcp 65001 >nul
title DM 3D Viewer
REM ===========================================================================
REM  Portable launcher (Windows). Needs Python 3.9+ on PATH ("python" or "py").
REM  three.js is bundled locally, so no internet access is required.
REM
REM  Usage:
REM    start-viewer.bat                       scan the default folders
REM    start-viewer.bat --dir "E:\my\models"  add a folder
REM
REM  KEEP PURE ASCII (cmd.exe + chcp mis-parses CJK characters).
REM ===========================================================================

setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY ( where py >nul 2>&1 && set "PY=py" )
if not defined PY (
    echo [ERROR] Python not found. Install Python 3.9+ and make sure "python" is on PATH.
    pause
    exit /b 1
)

%PY% -X utf8 server.py %*
echo.
echo [server stopped]
pause
