#!/bin/bash
# 股票量化系统启动脚本（Linux）
# 用法: ./run.sh [--once]
echo "股票量化系统启动中..."
echo
# 检查Python
if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到Python3，请先安装Python 3.8+"
    exit 1
fi
# 检查依赖
if [ ! -f "../requirements.txt" ]; then
    echo "错误: 未找到requirements.txt"
    exit 1
fi
echo "检查Python依赖包..."
pip3 install -r ../requirements.txt > /dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "警告: 依赖包安装失败，尝试继续运行..."
fi
# 创建必要目录
mkdir -p ../logs
mkdir -p ../data
mkdir -p ../config
# 检查配置文件
if [ ! -f "../config/config.yaml" ]; then
    echo "错误: 配置文件 config/config.yaml 不存在"
    echo "请复制 config/config.yaml.example 并修改配置"
    exit 1
fi
# 运行主程序
# 设置UTF-8编码模式，确保正确处理中文字符
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
if [ "$1" = "--once" ]; then
    echo "单次运行模式..."
    python3 ../main.py --once
else
    echo "定时运行模式（按Ctrl+C退出）..."
    python3 ../main.py
fi