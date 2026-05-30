@echo off
REM ============================================================
REM  Kasona Weekly Digest — Friday Morning Scheduler
REM  
REM  This script registers a Windows Task Scheduler job that
REM  runs generate_weekly_digest.py every Friday at 08:00 AM.
REM
REM  Usage (run once as Administrator):
REM    schedule_friday.bat
REM
REM  To remove the scheduled task:
REM    schtasks /Delete /TN "KasonaWeeklyDigest" /F
REM ============================================================

SET SCRIPT_DIR=%~dp0
SET PYTHON_SCRIPT=%SCRIPT_DIR%scripts\generate_weekly_digest.py

REM Find python in PATH
where python >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found in PATH. Please install Python or add it to your PATH.
    pause
    exit /b 1
)

REM Get the full path to python.exe
FOR /F "tokens=*" %%i IN ('where python') DO SET PYTHON_EXE=%%i

echo.
echo ============================================
echo   Kasona Weekly Digest Scheduler
echo ============================================
echo.
echo Python:  %PYTHON_EXE%
echo Script:  %PYTHON_SCRIPT%
echo Schedule: Every Friday at 08:00 AM
echo Task Name: KasonaWeeklyDigest
echo.

REM Create the scheduled task
schtasks /Create ^
    /TN "KasonaWeeklyDigest" ^
    /TR "\"%PYTHON_EXE%\" \"%PYTHON_SCRIPT%\" --days 7" ^
    /SC WEEKLY ^
    /D FRI ^
    /ST 08:00 ^
    /F ^
    /RL HIGHEST

IF %ERRORLEVEL% EQU 0 (
    echo.
    echo [SUCCESS] Scheduled task "KasonaWeeklyDigest" created!
    echo The digest will run every Friday at 08:00 AM.
    echo.
    echo To verify:   schtasks /Query /TN "KasonaWeeklyDigest"
    echo To delete:   schtasks /Delete /TN "KasonaWeeklyDigest" /F
    echo To run now:  schtasks /Run /TN "KasonaWeeklyDigest"
) ELSE (
    echo.
    echo [ERROR] Failed to create scheduled task.
    echo Try running this script as Administrator.
)

echo.
pause
