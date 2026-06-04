@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"

rem Stop the Vivado Lab persistent server with the bundled Python runtime.
set "PYTHON_EXE=%~dp0runtime\python\python.exe"
set "PYTHON_SCRIPT=%~dp0scripts\stop_vivado_server.py"

if not exist "%PYTHON_EXE%" (
    echo ERROR: Python runtime was not found.
    echo Missing file: %PYTHON_EXE%
    pause
    popd
    exit /b 1
)

if not exist "%PYTHON_SCRIPT%" (
    echo ERROR: Vivado stop script was not found.
    echo Missing file: %PYTHON_SCRIPT%
    pause
    popd
    exit /b 1
)

"%PYTHON_EXE%" -B -S "%PYTHON_SCRIPT%"
set "PROGRAM_RESULT=%ERRORLEVEL%"

pause
popd
exit /b %PROGRAM_RESULT%
