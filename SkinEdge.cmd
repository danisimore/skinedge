@echo off
rem SkinEdge: opens the app in the browser; the scanner starts with a button there.
rem No console window stays open, the log is in logs\skinedge.log.
rem Quit with the power-off button in the app. Options: --port 8766, --scan (start the scanner at once),
rem --skinport (also look for deals buying or selling on Skinport).
rem If SkinEdge is already running, this only opens it in the browser again.
cd /d "%~dp0"
set "SKINEDGE_PYW=pythonw"
if exist ".venv\Scripts\pythonw.exe" set "SKINEDGE_PYW=.venv\Scripts\pythonw.exe"
start "" "%SKINEDGE_PYW%" -m skinedge app %*
