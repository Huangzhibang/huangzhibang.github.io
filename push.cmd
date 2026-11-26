@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

set "GITEXE=D:\Git\cmd\git.exe"
if not exist "%GITEXE%" set "GITEXE=git"
set "GCM=D:\Git\mingw64\bin\git-credential-manager.exe"

echo [1/3] git: %GITEXE%
"%GITEXE%" config --local credential.helper "\"%GCM%\""

echo [2/3] pushing to GitHub ...
"%GITEXE%" push origin main

if errorlevel 1 (
  echo.
  echo PUSH FAILED - please screenshot this window
) else (
  echo.
  echo PUSH OK
)

echo [3/3] last commit:
"%GITEXE%" log --oneline -1
echo.
echo Check: https://github.com/Huangzhibang/huangzhibang.github.io/actions
pause
