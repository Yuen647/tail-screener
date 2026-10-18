@echo off
setlocal
set "SCREENER_ROOT=%~dp0"
set "SCREENER_PYTHON="
set "SCREENER_PAUSE="
if "%~1"=="" set "SCREENER_PAUSE=1"
if defined TAIL_SCREENER_PYTHON (
    set "SCREENER_PYTHON=%TAIL_SCREENER_PYTHON%"
    goto local_python
)

rem Prefer the environment already prepared by this project.
for /d %%R in ("%SCREENER_ROOT%.runtime\cpython-*") do if exist "%%~fR\Scripts\python.exe" set "SCREENER_PYTHON=%%~fR\Scripts\python.exe"
if defined SCREENER_PYTHON goto local_python
where py >nul 2>nul
if not errorlevel 1 goto python_launcher
where python >nul 2>nul
if not errorlevel 1 goto path_python
echo Python 3.10+ was not found. Install Python or set TAIL_SCREENER_PYTHON to its executable path.
set "SCREENER_RESULT=4"
goto finish

:local_python
pushd "%SCREENER_ROOT%"
"%SCREENER_PYTHON%" -X utf8 "%SCREENER_ROOT%scripts\screen.py" %*
set "SCREENER_RESULT=%ERRORLEVEL%"
popd
goto finish

:python_launcher
pushd "%SCREENER_ROOT%"
py -3 -X utf8 "%SCREENER_ROOT%scripts\screen.py" %*
set "SCREENER_RESULT=%ERRORLEVEL%"
popd
goto finish

:path_python
pushd "%SCREENER_ROOT%"
python -X utf8 "%SCREENER_ROOT%scripts\screen.py" %*
set "SCREENER_RESULT=%ERRORLEVEL%"
popd

:finish
echo.
echo Exit code: %SCREENER_RESULT%  ^(0=complete, 1=incomplete, 2=failed/arguments, 4=environment^)
if defined SCREENER_PAUSE pause
exit /b %SCREENER_RESULT%
