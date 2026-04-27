"""
定时任务调度管理器
使用APScheduler管理定时任务
"""

import logging
import functools
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

logger = logging.getLogger(__name__)


class SchedulerManager:
    """定时任务调度管理器"""

    def __init__(self, config, task_function=None):
        """
        初始化调度管理器

        Args:
            config: 配置字典
            task_function: 定时执行的任务函数，如果为None则从main导入run_daily_task
        """
        self.config = config
        self.scheduler_config = config.get("scheduler", {})
        self.scheduler = None
        self.health_server = None
        self.task_function = task_function

    def start(self):
        """启动调度器"""
        try:
            # 创建调度器
            self.scheduler = BlockingScheduler()

            # 启动健康服务器（如果启用）
            self._start_health_server()

            # 获取运行时间配置
            run_time = self.scheduler_config.get("run_time", "15:30")
            timezone_str = self.scheduler_config.get("timezone", "Asia/Shanghai")

            # 解析运行时间
            hour, minute = self._parse_run_time(run_time)

            # 设置时区
            try:
                timezone = pytz.timezone(timezone_str)
            except pytz.exceptions.UnknownTimeZoneError:
                logger.warning(f"未知时区: {timezone_str}，使用默认时区Asia/Shanghai")
                timezone = pytz.timezone("Asia/Shanghai")

            # 添加每日任务
            trigger = CronTrigger(hour=hour, minute=minute, timezone=timezone)

            # 确定要执行的任务函数
            if self.task_function is None:
                # 导入主任务函数（避免循环导入）
                from main import run_daily_task

                task_func = run_daily_task
            else:
                task_func = self.task_function

            self.scheduler.add_job(
                func=task_func,
                trigger=trigger,
                id="daily_stock_task",
                name="每日股票数据获取和分析任务",
                replace_existing=True,
            )

            # ── 注册简报任务（config scheduler.brief_reports）──
            brief_reports = self.scheduler_config.get("brief_reports", [])
            for br in brief_reports:
                if not br.get("enabled", True):
                    logger.info(f"简报已禁用: {br.get('label', br.get('id', '?'))}")
                    continue

                br_time = br.get("run_time", "09:50")
                br_hour, br_minute = self._parse_run_time(br_time)
                br_id = br.get("id", "brief_unknown")
                br_label = br.get("label", br_id)

                try:
                    from main import run_brief_report

                    br_trigger = CronTrigger(
                        hour=br_hour, minute=br_minute, timezone=timezone
                    )
                    br_task = functools.partial(
                        run_brief_report, report_id=br_id
                    )
                    self.scheduler.add_job(
                        func=br_task,
                        trigger=br_trigger,
                        id=f"brief_{br_id}",
                        name=br_label,
                        replace_existing=True,
                    )
                    logger.info(
                        f"简报已注册: {br_label} "
                        f"({br_hour:02d}:{br_minute:02d})"
                    )
                except Exception as e:
                    logger.error(f"注册简报失败 ({br_label}): {e}")

            # 添加启动时的立即执行任务（可选）
            if self.scheduler_config.get("run_on_startup", False):
                self.scheduler.add_job(
                    func=task_func,
                    trigger="date",
                    run_date=datetime.now(timezone),
                    id="startup_task",
                    name="启动时立即执行任务",
                )

            logger.info(
                f"定时任务已配置，将在每天 {hour:02d}:{minute:02d} ({timezone_str}) 执行"
            )
            logger.info("调度器已启动，按 Ctrl+C 退出")

            # 启动调度器
            self.scheduler.start()

        except KeyboardInterrupt:
            logger.info("收到中断信号，正在停止调度器...")
            self.stop()
        except Exception as e:
            logger.error(f"启动调度器失败: {e}")
            raise

    def _parse_run_time(self, run_time_str):
        """
        解析运行时间字符串

        Args:
            run_time_str: 时间字符串，格式为"HH:MM"或"HH.MM"

        Returns:
            tuple: (小时, 分钟)
        """
        try:
            # 支持多种分隔符
            if ":" in run_time_str:
                hour_str, minute_str = run_time_str.split(":")
            elif "." in run_time_str:
                hour_str, minute_str = run_time_str.split(".")
            else:
                raise ValueError(f"无效的时间格式: {run_time_str}")

            hour = int(hour_str.strip())
            minute = int(minute_str.strip())

            # 验证时间范围
            if not (0 <= hour <= 23):
                raise ValueError(f"小时必须在0-23之间: {hour}")
            if not (0 <= minute <= 59):
                raise ValueError(f"分钟必须在0-59之间: {minute}")

            return hour, minute

        except Exception as e:
            logger.warning(f"解析运行时间失败 {run_time_str}: {e}，使用默认时间15:30")
            return 15, 30

    def _start_health_server(self):
        """启动健康检查服务器"""
        try:
            health_config = self.config.get("health_server", {})
            if not health_config.get("enabled", True):
                logger.info("健康服务器已禁用")
                return

            # 导入健康服务器（避免循环导入）
            from .health_server import HealthServer

            host = health_config.get("host", "0.0.0.0")
            port = health_config.get("port", 1933)

            self.health_server = HealthServer(self.config, host=host, port=port)

            if self.health_server.start(daemon=True):
                logger.info(f"健康服务器已启动在 {host}:{port}")
            else:
                logger.warning("启动健康服务器失败")

        except ImportError as e:
            logger.warning(f"无法导入健康服务器模块: {e}")
        except Exception as e:
            logger.error(f"启动健康服务器时出错: {e}")

    def stop(self):
        """停止调度器"""
        # 停止健康服务器
        if self.health_server:
            try:
                self.health_server.stop()
                logger.info("健康服务器已停止")
            except Exception as e:
                logger.error(f"停止健康服务器时发生错误: {e}")

        # 停止调度器
        if self.scheduler:
            try:
                self.scheduler.shutdown()
                logger.info("调度器已停止")
            except Exception as e:
                logger.error(f"停止调度器时发生错误: {e}")
