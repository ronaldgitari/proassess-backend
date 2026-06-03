@echo off
setlocal
REM ===================================================================
REM  ProAssess service launcher  (adaptive to this script's location)
REM  Place this file in the BACKEND folder (the one with docker-compose.yml).
REM  The frontend is expected as a sibling folder: ..\proassess-frontend
REM
REM  Usage:
REM    proassess.bat            start all services (default)
REM    proassess.bat start      start backend (docker) + frontend (npm run dev)
REM    proassess.bat reload     force-recreate the API (picks up .env) + restart frontend
REM    proassess.bat stop       stop backend (docker compose down) + frontend
REM ===================================================================

REM --- Resolve folders relative to THIS script ----------------------
set "BACKEND=%~dp0"

set "FRONTEND="
pushd "%~dp0..\proassess-frontend" 2>nul
if not errorlevel 1 (
  set "FRONTEND=%CD%"
  popd
)

REM --- Validate layout ----------------------------------------------
if not exist "%BACKEND%docker-compose.yml" (
  echo [ERROR] docker-compose.yml not found in "%BACKEND%".
  echo         Put proassess.bat in the backend folder ^(next to docker-compose.yml^).
  exit /b 1
)
if not defined FRONTEND (
  echo [ERROR] Frontend folder not found at "%~dp0..\proassess-frontend".
  exit /b 1
)
if not exist "%FRONTEND%\package.json" (
  echo [ERROR] No package.json in "%FRONTEND%".
  exit /b 1
)

REM --- Dispatch -----------------------------------------------------
set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"

if /I "%ACTION%"=="start"  goto start
if /I "%ACTION%"=="reload" goto reload
if /I "%ACTION%"=="stop"   goto stop

echo Unknown action "%ACTION%".  Use: start ^| reload ^| stop
exit /b 1

REM ------------------------------------------------------------------
:start
echo === Backend: docker compose up -d ===
pushd "%BACKEND%"
docker compose up -d
popd
echo.
echo === Frontend: npm run dev ^(new window^) ===
start "ProAssess Frontend" /D "%FRONTEND%" cmd /k "npm run dev"
echo.
echo ProAssess starting...  Backend: http://localhost:8000   Frontend: http://localhost:3000
goto end

REM ------------------------------------------------------------------
:reload
echo === Reloading API ^(force-recreate so .env changes take effect^) ===
pushd "%BACKEND%"
docker compose up -d --force-recreate api
popd
echo.
echo === Restarting frontend dev server ===
call :killport 3000
start "ProAssess Frontend" /D "%FRONTEND%" cmd /k "npm run dev"
echo.
echo Reload complete.
goto end

REM ------------------------------------------------------------------
:stop
echo === Backend: docker compose down ===
pushd "%BACKEND%"
docker compose down
popd
echo === Stopping frontend dev server ===
call :killport 3000
echo Stopped.
goto end

REM --- Helper: kill whatever is LISTENING on a given port -----------
:killport
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%~1 " ^| findstr LISTENING') do (
  taskkill /F /PID %%P >nul 2>&1
)
exit /b 0

:end
endlocal
