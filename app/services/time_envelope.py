"""Time envelope — stamp every user message with the current time.

Copied from akashic-agent /prompts/agent.py so the LLM always has a
reliable time anchor.  Without this the model has no way to answer
"今天星期几" / "现在几点" / relative-time questions.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def _weekday_cn(ts: datetime) -> str:
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][ts.weekday()]


def build_current_message_time_envelope(
    *, message_timestamp: datetime | None = None,
) -> str:
    """Return a short block that anchors the LLM to the real current time.

    Prepended to the user message content before sending to the LLM.
    """
    ts = message_timestamp
    if ts is None:
        ts = datetime.now().astimezone()
    elif ts.tzinfo is None:
        ts = ts.astimezone()

    yesterday = ts - timedelta(days=1)
    tomorrow = ts + timedelta(days=1)
    day_after_tomorrow = ts + timedelta(days=2)

    return (
        f"[当前消息时间: {ts.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
        f"request_time={ts.isoformat()} | "
        f"今天={ts.strftime('%Y-%m-%d')}（{_weekday_cn(ts)}） | "
        f"昨天={yesterday.strftime('%Y-%m-%d')}（{_weekday_cn(yesterday)}） | "
        f"明天={tomorrow.strftime('%Y-%m-%d')}（{_weekday_cn(tomorrow)}） | "
        f"后天={day_after_tomorrow.strftime('%Y-%m-%d')}（{_weekday_cn(day_after_tomorrow)}） | "
        f"weekday={ts.strftime('%A')} | "
        f"相对时间以此为准]"
    )
