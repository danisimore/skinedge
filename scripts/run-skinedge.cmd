@echo off
rem SkinEdge: background scan on a schedule. Analysis only, it never buys or sells.
rem Usage: scripts\run-skinedge.cmd [options of "skinedge run"], for example --every 10
rem Stop: Ctrl+C or close the window.
cd /d "%~dp0.."
set "SKINEDGE_EXE=skinedge"
if exist ".venv\Scripts\skinedge.exe" set "SKINEDGE_EXE=.venv\Scripts\skinedge.exe"
"%SKINEDGE_EXE%" run %*
if errorlevel 1 pause
