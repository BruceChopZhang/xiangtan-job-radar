@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (set "RADAR_PYTHON=python") else (set "RADAR_PYTHON=py -3")
%RADAR_PYTHON% -m unittest discover -s tests -v
if errorlevel 1 goto failed
%RADAR_PYTHON% -m radar
set "RADAR_RESULT=%errorlevel%"
if exist site\index.html start "" "site\index.html"
echo Exit code: %RADAR_RESULT% -- 0=complete, 1=errors. Read dashboard logs.
pause
exit /b %RADAR_RESULT%
:failed
echo Tests failed or Python unavailable. See README.md.
pause
exit /b 1
