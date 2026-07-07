@echo off
setlocal

set PROFILE=%~1
if "%PROFILE%"=="" set PROFILE=local

set PYTHON=python
if exist "%~dp0..\.venv\Scripts\python.exe" set PYTHON=%~dp0..\.venv\Scripts\python.exe

"%PYTHON%" -m visual_agent.cli quality-gate --profile %PROFILE% --workspace-root .agent-workspace --run
exit /b %ERRORLEVEL%
