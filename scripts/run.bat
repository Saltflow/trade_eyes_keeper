@echo off
REM 股票量化系统启动脚本（Windows）
REM 用法: run.bat [--once]
echo 股票量化系统启动中...
echo.
REM 检查Python
python --version >nul 2>&1
if errorlevel 1 (
    echo 错误: 未找到Python，请先安装Python 3.8+
    pause
    exit /b 1
)
REM 检查依赖
if not exist "..\requirements.txt" (
    echo 错误: 未找到requirements.txt
    pause
    exit /b 1
)
echo 检查Python依赖包...
pip install -r ..\requirements.txt >nul 2>&1
if errorlevel 1 (
    echo 警告: 依赖包安装失败，尝试继续运行...
)
REM 创建必要目录
if not exist "..\logs" mkdir ..\logs
if not exist "..\data" mkdir ..\data
if not exist "..\config" mkdir ..\config
REM 检查配置文件
if not exist "..\config\config.yaml" (
    echo 错误: 配置文件 config\config.yaml 不存在
    echo 请复制 config\config.yaml.example 并修改配置
    pause
    exit /b 1
)
REM 运行主程序
REM 设置UTF-8编码模式，确保正确处理中文字符
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if "%1"=="--once" (
    echo 单次运行模式...
    python ..\main.py --once
) else (
    echo 定时运行模式（按Ctrl+C退出）...
    python ..\main.py
)
pause