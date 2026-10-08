@echo off
chcp 65001 >nul
rem 管理者権限で起動し直す (ドライブを直接読むために必要)
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"
where py >nul 2>&1 && (py -3 undelete.py %*) || (python undelete.py %*)
echo.
pause
