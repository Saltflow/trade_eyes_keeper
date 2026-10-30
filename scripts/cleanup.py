#!/usr/bin/env python3
"""
清理脚本 - 删除旧的日志文件和邮件存档
用法：
    python cleanup.py [--days 30] [--dry-run] [--logs] [--archives]
参数：
    --days N       删除N天前的文件（默认30）
    --dry-run      只显示将要删除的文件，不实际删除
    --logs         仅清理日志文件
    --archives     仅清理邮件存档文件
    --all          清理所有（默认）
示例：
    python cleanup.py --days 7 --dry-run
    python cleanup.py --days 30 --logs
    python cleanup.py --days 90 --archives
"""
import os
import sys
import glob
import time
import argparse
from datetime import datetime, timedelta

def get_files_older_than(directory, pattern, days):
    """获取目录中超过指定天数的文件列表"""
    cutoff = time.time() - days * 86400
    files = []
    for filepath in glob.glob(os.path.join(directory, pattern)):
        if os.path.isfile(filepath):
            if os.path.getmtime(filepath) < cutoff:
                files.append(filepath)
    return files

def delete_files(file_list, dry_run=False):
    """删除文件列表，如果dry_run为True则只打印"""
    for filepath in file_list:
        if dry_run:
            print(f"[DRY RUN] 将删除: {filepath}")
        else:
            try:
                os.remove(filepath)
                print(f"已删除: {filepath}")
            except Exception as e:
                print(f"删除失败 {filepath}: {e}")

def main():
    parser = argparse.ArgumentParser(description="清理旧的日志文件和邮件存档")
    parser.add_argument("--days", type=int, default=30, help="删除多少天前的文件（默认30）")
    parser.add_argument("--dry-run", action="store_true", help="只显示不实际删除")
    parser.add_argument("--logs", action="store_true", help="仅清理日志文件")
    parser.add_argument("--archives", action="store_true", help="仅清理邮件存档文件")
    parser.add_argument("--all", action="store_true", help="清理所有（默认）")
    args = parser.parse_args()
    # 如果没有指定具体类型，则清理所有
    if not (args.logs or args.archives):
        args.all = True
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files_to_delete = []
    # 清理日志文件
    if args.all or args.logs:
        log_dir = os.path.join(base_dir, "logs")
        if os.path.exists(log_dir):
            log_files = get_files_older_than(log_dir, "*.log", args.days)
            files_to_delete.extend(log_files)
    # 清理邮件存档
    if args.all or args.archives:
        archive_dir = os.path.join(base_dir, "data", "email_archive")
        if os.path.exists(archive_dir):
            archive_files = get_files_older_than(archive_dir, "*.html", args.days)
            files_to_delete.extend(archive_files)
    if not files_to_delete:
        print("没有找到需要清理的文件")
        return
    print(f"找到 {len(files_to_delete)} 个需要清理的文件（超过 {args.days} 天）")
    if args.dry_run:
        print("\n以下文件将被删除：")
        for f in files_to_delete:
            print(f"  {f}")
    else:
        print("开始清理...")
        delete_files(files_to_delete)
        print("清理完成")

if __name__ == "__main__":
    main()
