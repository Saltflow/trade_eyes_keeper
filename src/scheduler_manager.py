"""
定时任务调度管理器
使用APScheduler管理定时任务
"""

import logging
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

logger = logging.getLogger(__name__)

class SchedulerManager:
    """定时任务调度管理器"""
    
    def __init__(self, config):
        """
        初始化调度管理器
        
        Args:
            config: 配置字典
        """
        self.config = config
        self.scheduler_config = config.get('scheduler', {})
        self.scheduler = None
    
    def start(self):
        """启动调度器"""
        try:
            # 创建调度器
            self.scheduler = BlockingScheduler()
            
            # 获取运行时间配置
            run_time = self.scheduler_config.get('run_time', '15:30')
            timezone_str = self.scheduler_config.get('timezone', 'Asia/Shanghai')
            
            # 解析运行时间
            hour, minute = self._parse_run_time(run_time)
            
            # 设置时区
            try:
                timezone = pytz.timezone(timezone_str)
            except pytz.exceptions.UnknownTimeZoneError:
                logger.warning(f"未知时区: {timezone_str}，使用默认时区Asia/Shanghai")
                timezone = pytz.timezone('Asia/Shanghai')
            
            # 添加每日任务
            trigger = CronTrigger(
                hour=hour,
                minute=minute,
                timezone=timezone
            )
            
            # 导入主任务函数（避免循环导入）
            from main import run_daily_task
            
            self.scheduler.add_job(
                func=run_daily_task,
                trigger=trigger,
                id='daily_stock_task',
                name='每日股票数据获取和分析任务',
                replace_existing=True
            )
            
            # 添加启动时的立即执行任务（可选）
            if self.scheduler_config.get('run_on_startup', False):
                self.scheduler.add_job(
                    func=run_daily_task,
                    trigger='date',
                    run_date=datetime.now(timezone),
                    id='startup_task',
                    name='启动时立即执行任务'
                )
            
            logger.info(f"定时任务已配置，将在每天 {hour:02d}:{minute:02d} ({timezone_str}) 执行")
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
            if ':' in run_time_str:
                hour_str, minute_str = run_time_str.split(':')
            elif '.' in run_time_str:
                hour_str, minute_str = run_time_str.split('.')
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
    
    def stop(self):
        """停止调度器"""
        if self.scheduler:
            try:
                self.scheduler.shutdown()
                logger.info("调度器已停止")
            except Exception as e:
                logger.error(f"停止调度器时发生错误: {e}")

    
