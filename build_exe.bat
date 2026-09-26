@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    echo 未找到 Python，请先安装 Python 3.10+ 并勾选 Add to PATH。
    pause
    exit /b 1
)
echo 正在安装依赖...
py -m pip install -r requirements-build.txt
if errorlevel 1 goto :error
echo 正在打包单文件 exe...
py -m PyInstaller --noconfirm --clean --workpath output\build --distpath output\dist packaging\bill-analyzer.spec
if errorlevel 1 goto :error
echo.
echo 打包完成：output\dist\个人账单分析工具.exe
pause
exit /b 0
:error
echo.
echo 打包失败，请查看上方错误信息。
pause
exit /b 1
