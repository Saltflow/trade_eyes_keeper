@echo off
REM Git pre-commit钩子 (Windows版本)
REM 运行Session安全测试，防止有问题的代码被提交
REM 零配置，强制启用

echo ============================================================
echo 运行Session数据安全检查...
echo ============================================================

cd /d "%~dp0.."

REM 运行pytest测试
python -m pytest tests/validation/test_session_safety.py -v --tb=short

if %errorlevel% neq 0 (
    echo.
    echo ============================================================
    echo [错误] Session数据安全检查失败！
    echo 发现问题：检测到可能的随机数据写入Session的风险
    echo 请修复问题后再提交代码
    echo ============================================================
    exit /b 1
)

echo.
echo ============================================================
echo [成功] Session数据安全检查通过！
echo ============================================================
echo.
echo 所有pre-commit检查通过，允许提交
exit /b 0
