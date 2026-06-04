@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"

:: 双击入口：固定使用当前 flow 下移植的 Python 环境执行主流程。
set "PYTHON_EXE=%~dp0runtime\python\python.exe"
set "PYTHON_SCRIPT=%~dp0scripts\fpga_flash_download.py"

if not exist "%PYTHON_EXE%" (
    echo 错误：未找到当前目录下的 Python 运行环境。
    echo 缺少文件：%PYTHON_EXE%
    pause
    popd
    exit /b 1
)

if not exist "%PYTHON_SCRIPT%" (
    echo 错误：未找到 Python 主脚本。
    echo 缺少文件：%PYTHON_SCRIPT%
    pause
    popd
    exit /b 1
)

"%PYTHON_EXE%" -B -S "%PYTHON_SCRIPT%"
set "PROGRAM_RESULT=%ERRORLEVEL%"

popd
exit /b %PROGRAM_RESULT%
