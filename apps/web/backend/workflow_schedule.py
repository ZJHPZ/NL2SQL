"""工作流 cron 解析（一期原生调度）。

不自己写 cron 解析器：APScheduler 在本项目 Web 依赖里已经存在，
直接用它的 `CronTrigger.from_crontab` 校验 5 段表达式并算下一次触发时间，
与调度平台（Taier 用 quartz cron）保持同一套标准 5 段语义。
"""
from __future__ import annotations

from datetime import datetime

from apscheduler.triggers.cron import CronTrigger

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def validate_cron(expr: str) -> str:
    """校验并返回 5 段 cron 表达式（去首尾空白）。"""
    text = (expr or "").strip()
    if not text:
        raise ValueError("cron 表达式不能为空")
    if len(text.split()) != 5:
        raise ValueError("cron 需要 5 段（分 时 日 月 周），例如 0 2 * * *")
    try:
        CronTrigger.from_crontab(text)
    except ValueError as exc:
        raise ValueError(f"cron 表达式不合法：{exc}") from exc
    return text


def next_fire_time(expr: str, *, after: datetime | None = None) -> datetime:
    """下一次触发时间（严格晚于 after；默认现在）。"""
    trigger = CronTrigger.from_crontab(validate_cron(expr))
    base = after or datetime.now()
    nxt = trigger.get_next_fire_time(None, base)
    if nxt is None:
        raise ValueError("cron 表达式没有可预见的触发时间")
    return nxt


def fmt_time(moment: datetime) -> str:
    return moment.strftime(_TS_FORMAT)


def next_fire_text(expr: str, *, after: datetime | None = None) -> str:
    return fmt_time(next_fire_time(expr, after=after))
