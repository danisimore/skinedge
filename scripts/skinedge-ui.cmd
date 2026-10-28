@echo off
rem SkinEdge: interface in the browser (scanner settings and trade journal).
rem Works only on this computer (127.0.0.1). The scanner keeps running separately.
rem Stop: Ctrl+C or close the window.
cd /d "%~dp0.."
set "SKINEDGE_EXE=skinedge"
if exist ".venv\Scripts\skinedge.exe" set "SKINEDGE_EXE=.venv\Scripts\skinedge.exe"
"%SKINEDGE_EXE%" ui %*
if errorlevel 1 pause
