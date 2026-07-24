"""話題速度に応じた投稿間隔。既存上限を緩和しない。"""
from __future__ import annotations
import os
from datetime import datetime
try: from runtime import JST
except ImportError: from common.runtime import JST
try: from post_registry import hours_since_last_post
except ImportError: from common.post_registry import hours_since_last_post

def posting_window(acceleration: float, now: datetime | None=None) -> dict:
    enabled=os.getenv("DYNAMIC_POSTING_ENABLED","true").lower() in ("1","true","yes")
    minimum=float(os.getenv("X_TOPIC_ACCELERATION_MINIMUM","1.25") or 1.25)
    quiet_min=int(os.getenv("QUIET_MIN_GAP_MINUTES","60") or 60)
    quiet_max=max(quiet_min,int(os.getenv("QUIET_MAX_GAP_MINUTES","120") or 120))
    elapsed=hours_since_last_post(now)
    if not enabled: return {"allow":True,"mode":"disabled","required_gap_minutes":0}
    high=acceleration>=minimum
    current=now or datetime.now(JST)
    # 60〜120分の範囲で時間帯ごとに安定した間隔を選び、毎回の乱数ぶれを避ける。
    span=quiet_max-quiet_min+1
    quiet_gap=quiet_min+((current.date().toordinal()+current.hour)%span)
    required=0 if high else quiet_gap
    elapsed_minutes=float("inf") if elapsed is None else elapsed*60
    return {"allow":elapsed_minutes>=required,"mode":"high_velocity" if high else "quiet",
            "required_gap_minutes":required,"quiet_max_minutes":quiet_max,"elapsed_minutes":elapsed_minutes}
