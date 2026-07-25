#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
local_finance_bot.py
GitHub Actions に依存せず、ローカルPC/サーバーで金融Bot4系統を運用するCLI。

  python local_finance_bot.py status
  python local_finance_bot.py init-state
  python local_finance_bot.py once news --mode image
  python local_finance_bot.py once news --mode diagram
  python local_finance_bot.py once narrative
  python local_finance_bot.py once market-map
  python local_finance_bot.py once weekly
  python local_finance_bot.py force news --mode image
  python local_finance_bot.py force narrative
  python local_finance_bot.py force market-map
  python local_finance_bot.py force weekly
  python local_finance_bot.py daemon

安全設計:
- POST_ENABLED=true でない限り、X への実投稿は行われない（x_client / market_map 側で遮断）
- force はスケジュール条件のみ無視。投稿価値ゲート・安全審査・OpenAIレビューは維持
- init-state で過去スロットの暴発を防止（CATCH_UP_ENABLED=false が既定）
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone, date, time as dtime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(SRC_DIR / "common"))

# Windows pipes otherwise default to CP932 while this parent decodes UTF-8.
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from common.runtime import (  # noqa: E402
    load_env, state_dir, output_dir, log_dir, post_enabled,
    log_run, log_error, JST,
)
from common.calendar_utils import is_us_market_business_day  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone(timedelta(hours=-4))  # 近似フォールバック

BOTS = ("news", "narrative", "market-map", "weekly")
SCHED_BOTS = BOTS + ("metrics", "report", "radar", "fx-monitor", "market-data")

DEFAULT_SCHEDULE = {
    "news": {"enabled": True, "type": "interval_minutes", "every_minutes": 30,
             "default_mode": "image"},
    "narrative": {"enabled": True, "type": "et_times_business_days",
                  "times": ["08:30", "09:35", "16:05"]},
    "market-map": {"enabled": True, "type": "et_times_business_days",
                   "times": ["09:35", "15:50"]},
    "weekly": {"enabled": True, "type": "weekly_jst",
               "weekday": 6, "time": "21:00"},  # weekday: 0=月 ... 6=日
    # 投稿実績レポート（毎日22:00 JST。X APIから実績を取得して集計）
    "report": {"enabled": True, "type": "daily_jst", "time": "22:00"},
    "metrics": {"enabled": True, "type": "interval_minutes", "every_minutes": 30},
    "radar": {"enabled": False, "type": "daily_jst_times", "times": ["21:00","22:30"]},
    "fx-monitor": {"enabled": True, "type": "interval_minutes", "every_minutes": 5},
    "market-data": {"enabled": True, "type": "interval_minutes", "every_minutes": 15},
}


# ---------------------------------------------------------------------------
# 設定・状態
# ---------------------------------------------------------------------------

def load_schedule() -> dict:
    path = REPO_ROOT / "config" / "schedule.json"
    sched = {k: dict(v) for k, v in DEFAULT_SCHEDULE.items()}
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
            for k, v in user.items():
                if k in sched and isinstance(v, dict):
                    sched[k].update(v)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] config/schedule.json の読み込みに失敗（既定値で続行）: {e}")
    # .env の NEWS_RUN_EVERY_MINUTES / NEWS_DEFAULT_MODE を上書き反映
    em = os.environ.get("NEWS_RUN_EVERY_MINUTES", "").strip()
    if em.isdigit() and int(em) > 0:
        sched["news"]["every_minutes"] = int(em)
    dm = os.environ.get("NEWS_DEFAULT_MODE", "").strip().lower()
    if dm in ("image", "diagram", "random"):
        sched["news"]["default_mode"] = dm
    sched["metrics"]["enabled"] = os.environ.get("X_METRICS_ENABLED", "true").lower() in ("1", "true", "yes")
    interval = os.environ.get("X_METRICS_INTERVAL_MINUTES", "60")
    if interval.isdigit() and int(interval) > 0: sched["metrics"]["every_minutes"] = int(interval)
    # schedule.enabled is authoritative; feature flags are an additional AND condition.
    sched["radar"]["enabled"] = bool(sched["radar"].get("enabled", False)) and (
        os.environ.get("XAI_ENABLED", "false").lower() in ("1", "true", "yes")) and (
        os.environ.get("XAI_X_SEARCH_ENABLED", "true").lower() in ("1", "true", "yes"))
    radar_interval = os.environ.get("XAI_SEARCH_INTERVAL_MINUTES", "60")
    if radar_interval.isdigit() and int(radar_interval) > 0: sched["radar"]["every_minutes"] = int(radar_interval)
    sched["fx-monitor"]["enabled"] = bool(sched["fx-monitor"].get("enabled", True)) and (
        os.environ.get("FX_ENABLED", "true").lower() in ("1", "true", "yes"))
    fx_interval = os.environ.get("FX_POLL_INTERVAL_MINUTES", "5")
    if fx_interval.isdigit() and int(fx_interval) > 0:
        sched["fx-monitor"]["every_minutes"] = int(fx_interval)
    sched["market-data"]["enabled"] = bool(sched["market-data"].get("enabled", True)) and (
        os.environ.get("MARKET_DATA_ENABLED", "true").lower() in ("1", "true", "yes"))
    market_interval = os.environ.get("MARKET_DATA_POLL_INTERVAL_MINUTES", "15")
    if market_interval.isdigit() and int(market_interval) > 0:
        sched["market-data"]["every_minutes"] = int(market_interval)
    return sched


def _state_path() -> Path:
    return state_dir() / "local_state.json"


def load_state() -> dict:
    p = _state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[WARN] 状態保存に失敗（Bot処理は継続）: {type(exc).__name__}")
        try: tmp.unlink(missing_ok=True)
        except OSError: pass


# ---------------------------------------------------------------------------
# スケジュール計算
# ---------------------------------------------------------------------------

def _next_interval(now: datetime, every_min: int) -> datetime:
    """次の every_min 境界（毎時00/30 など）。"""
    minute = (now.minute // every_min + 1) * every_min
    base = now.replace(second=0, microsecond=0, minute=0)
    return base + timedelta(minutes=minute)


def _next_et_times(now_utc: datetime, times: list[str]) -> datetime:
    """次の 米国営業日 x 指定ET時刻。"""
    now_et = now_utc.astimezone(ET)
    for add_days in range(0, 14):
        d = (now_et + timedelta(days=add_days)).date()
        if not is_us_market_business_day(d):
            continue
        for hhmm in sorted(times):
            h, m = map(int, hhmm.split(":"))
            cand = datetime.combine(d, dtime(h, m), tzinfo=ET)
            if cand > now_et:
                return cand.astimezone(timezone.utc)
    return (now_et + timedelta(days=14)).astimezone(timezone.utc)


def _next_weekly_jst(now_utc: datetime, weekday: int, hhmm: str) -> datetime:
    now_jst = now_utc.astimezone(JST)
    h, m = map(int, hhmm.split(":"))
    for add_days in range(0, 8):
        d = (now_jst + timedelta(days=add_days)).date()
        if d.weekday() != weekday:
            continue
        cand = datetime.combine(d, dtime(h, m), tzinfo=JST)
        if cand > now_jst:
            return cand.astimezone(timezone.utc)
    return (now_jst + timedelta(days=8)).astimezone(timezone.utc)


def next_run_utc(bot: str, sched: dict, now_utc: datetime | None = None) -> datetime | None:
    now_utc = now_utc or datetime.now(timezone.utc)
    conf = sched.get(bot, {})
    if not conf.get("enabled", True):
        return None
    t = conf.get("type")
    if t == "interval_minutes":
        return _next_interval(now_utc.astimezone(JST), int(conf.get("every_minutes", 30))
                              ).astimezone(timezone.utc)
    if t == "et_times_business_days":
        return _next_et_times(now_utc, list(conf.get("times", [])))
    if t == "weekly_jst":
        return _next_weekly_jst(now_utc, int(conf.get("weekday", 6)), conf.get("time", "21:00"))
    if t == "daily_jst":
        return _next_daily_jst(now_utc, conf.get("time", "22:00"))
    if t == "daily_jst_times":
        return _next_jst_times(now_utc, list(conf.get("times", [])))
    return None


def _next_daily_jst(now_utc: datetime, hhmm: str) -> datetime:
    """毎日 指定JST時刻の次回。"""
    now_jst = now_utc.astimezone(JST)
    h, m = map(int, hhmm.split(":"))
    cand = now_jst.replace(hour=h, minute=m, second=0, microsecond=0)
    if cand <= now_jst:
        cand = cand + timedelta(days=1)
    return cand.astimezone(timezone.utc)


def _next_jst_times(now_utc: datetime, times: list[str]) -> datetime:
    now_jst = now_utc.astimezone(JST)
    for add_days in (0, 1):
        day = (now_jst + timedelta(days=add_days)).date()
        for hhmm in sorted(times):
            h, m = map(int, hhmm.split(":"))
            candidate = datetime.combine(day, dtime(h, m), tzinfo=JST)
            if candidate > now_jst:
                return candidate.astimezone(timezone.utc)
    return (now_jst + timedelta(days=1)).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 実行（subprocess で既存 entry を起動）
# ---------------------------------------------------------------------------

def _decode_child_output(data: bytes | None) -> str:
    """子プロセス出力をWindowsの既定コードページに依存せず文字列化する。"""
    if not data:
        return ""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # 外部ツール等がCP932を返した場合だけ互換フォールバックする。
        return data.decode("cp932", errors="replace")


def _failure_summary(stderr: str, limit: int = 500) -> str:
    """Return a compact, JSONL-safe reason for a failed child process."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    return (lines[-1] if lines else "child process failed without stderr")[:limit]


def _news_mode(sched: dict, cli_mode: str | None) -> str:
    if cli_mode in ("image", "diagram"):
        return cli_mode
    dm = sched["news"].get("default_mode", "image")
    if dm == "random":
        return random.choice(["image", "diagram"])
    return dm if dm in ("image", "diagram") else "image"


def run_bot(bot: str, *, mode: str | None = None, force: bool = False,
            sched: dict | None = None) -> dict:
    """既存 entry を subprocess で実行し、結果 dict を返す（run_history.jsonl にも記録）。"""
    sched = sched or load_schedule()
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONLEGACYWINDOWSSTDIO"] = "0"
    env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + str(SRC_DIR / "common") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if force:
        env["FORCE_POST"] = "true"  # スケジュール条件のみ無視。安全審査はバイパスしない。

    if bot in ("report", "metrics", "radar", "fx-monitor", "market-data"):
        started = datetime.now(JST)
        print(f"[RUN] bot={bot} started={started:%Y-%m-%d %H:%M:%S}")
        try:
            if bot == "report":
                report_result=cmd_report(days=1)
                rc=int(report_result.get("exit_code",0))
            elif bot == "metrics":
                from common.metrics_collector import collect_metrics
                metrics_result = collect_metrics()
                print(json.dumps(metrics_result, ensure_ascii=False))
                if metrics_result.get("status") == "error":
                    raise RuntimeError(
                        f"metrics collection failed: {metrics_result.get('status_code') or metrics_result.get('error_type') or 'unknown'}")
            elif bot == "radar":
                from common.xai_radar import refresh
                radar_result = refresh()
                print(json.dumps(radar_result, ensure_ascii=False))
                # disabled/key missing/budget超過は既存Bot継続のため正常終了。
                if radar_result.get("status") == "error":
                    raise RuntimeError(f"radar failed: {radar_result.get('reason','unknown')}")
            elif bot == "fx-monitor":
                from fx_alert.monitor import run_monitor
                fx_result = run_monitor()
                print(json.dumps(fx_result, ensure_ascii=False))
                if fx_result.get("status") not in {
                    "posted", "disabled", "provider_unavailable", "quality_blocked",
                    "no_material_movement", "gate_blocked", "not_posted",
                    "review_blocked", "content_blocked", "image_blocked",
                }:
                    raise RuntimeError(f"fx-alert failed: {fx_result.get('status','unknown')}")
            else:
                from market_data.monitor import run_market_monitor
                market_result = run_market_monitor()
                print(json.dumps(market_result, ensure_ascii=False))
                if market_result.get("status") not in {
                    "completed", "disabled", "waiting_for_market_session",
                    "provider_unavailable",
                }:
                    raise RuntimeError(
                        f"market-data failed: {market_result.get('status','unknown')}")
            if bot != "report": rc=0
            err = "" if rc in (0,2) else "report failed"
        except Exception as e:  # noqa: BLE001
            rc, err = 1, f"{type(e).__name__}: {e}"
            print(f"[RUN] {bot} 失敗: {err}")
        result = {"bot": bot, "mode": bot, "force": force,
                  "started_at": started.isoformat(),
                  "finished_at": datetime.now(JST).isoformat(),
                  "returncode": rc, "error": err, "post_enabled": post_enabled()}
        log_run(result)
        state = load_state(); state.setdefault(bot, {})
        state[bot]["last_run_at"] = started.isoformat()
        state[bot]["last_result"] = {"returncode": rc, "error": err}
        save_state(state)
        try:
            from common.operations_alerts import flush_discord_logs
            flush_discord_logs()
        except Exception as exc:
            print(f"[WARN] Discordログ送信失敗（daemonは継続）: {type(exc).__name__}")
        return result

    if bot == "news":
        m = _news_mode(sched, mode)
        cmd = [sys.executable, "-X", "utf8", "news_bot/post.py", m]
        cwd = SRC_DIR
        run_mode = m
    elif bot == "narrative":
        cmd = [sys.executable, "-X", "utf8", "narrative_bot/narrative_post.py", "post"]
        cwd = SRC_DIR
        run_mode = "post"
    elif bot == "market-map":
        cmd = [sys.executable, "-X", "utf8", "-m", "market_map.run_market_map"]
        cwd = REPO_ROOT
        env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env["PYTHONPATH"]
        run_mode = "post"
    elif bot == "weekly":
        cmd = [sys.executable, "-X", "utf8", "weekly_bot/weekly_post.py", "post"]
        cwd = SRC_DIR
        run_mode = "post"
    else:
        raise SystemExit(f"unknown bot: {bot}")

    # 共通投稿履歴にBot種別・モードを残すため、子プロセスへ実行コンテキストを渡す
    env["FINANCE_BOT_NAME"] = bot
    env["FINANCE_BOT_MODE"] = run_mode

    started = datetime.now(JST)
    print(f"[RUN] bot={bot} mode={run_mode} force={force} "
          f"POST_ENABLED={post_enabled()} started={started:%Y-%m-%d %H:%M:%S}")
    log_name = bot.replace("-", "_")
    out_log = log_dir() / f"{log_name}_stdout.log"
    err_log = log_dir() / f"{log_name}_stderr.log"
    try:
        # Windowsのロケールに復号を任せるとUTF-8がCP932として壊れるため、
        # bytesで受け取り、ここで必ずUTF-8として復号する。
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, timeout=900,
                              capture_output=True, text=False)
        rc = proc.returncode
        err = ""
        child_stdout = _decode_child_output(proc.stdout)
        child_stderr = _decode_child_output(proc.stderr)
        if rc != 0:
            err = _failure_summary(child_stderr)
        # コンソールへも出しつつ、Bot別ログに追記保存
        if child_stdout:
            print(child_stdout, end="")
        if child_stderr:
            print(child_stderr, end="")
        try:
            from common.operations_alerts import queue_discord_log
            if child_stdout:
                queue_discord_log(f"{bot}.stdout",child_stdout,
                                  level="INFO")
            if child_stderr:
                queue_discord_log(f"{bot}.stderr",child_stderr,
                                  level="ERROR" if rc else "INFO")
        except Exception:
            pass
        header = f"\n===== {started:%Y-%m-%d %H:%M:%S} bot={bot} mode={run_mode} rc={rc} =====\n"
        try:
            with open(out_log, "a", encoding="utf-8") as f:
                f.write(header + child_stdout)
            with open(err_log, "a", encoding="utf-8") as f:
                f.write(header + child_stderr)
        except OSError:
            pass
    except subprocess.TimeoutExpired:
        rc, err = -1, "timeout(900s)"
    except Exception as e:  # noqa: BLE001
        rc, err = -1, f"{type(e).__name__}: {e}"

    result = {
        "bot": bot, "mode": run_mode, "force": force,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(JST).isoformat(),
        "returncode": rc, "error": err,
        "post_enabled": post_enabled(),
    }
    log_run(result)
    if rc != 0:
        log_error(result)
        print(f"[RUN] bot={bot} 終了コード={rc} error={err or '-'}（詳細は logs/ を確認）")
    else:
        print(f"[RUN] bot={bot} 正常終了")
        if bot == "weekly":
            try:
                from common.media_intelligence import write_weekly_media_plan
                from common.metrics_collector import load_snapshots
                history = json.loads((state_dir() / "posted_history.json").read_text(encoding="utf-8"))
                write_weekly_media_plan(history, load_snapshots())
            except Exception as exc:
                print(f"[WARN] 週次メディア企画の生成失敗（weekly投稿は維持）: {exc}")

    # 状態更新
    state = load_state()
    state.setdefault(bot, {})
    state[bot]["last_run_at"] = started.isoformat()
    state[bot]["last_result"] = {"returncode": rc, "error": err}
    save_state(state)
    try:
        from common.operations_alerts import flush_discord_logs
        flush_discord_logs()
    except Exception as exc:
        print(f"[WARN] Discordログ送信失敗（daemonは継続）: {type(exc).__name__}")
    return result


# ---------------------------------------------------------------------------
# コマンド
# ---------------------------------------------------------------------------

def cmd_status() -> None:
    sched = load_schedule()
    state = load_state()
    now_utc = datetime.now(timezone.utc)
    print("=== local_finance_bot status ===")
    print(f"JST now : {now_utc.astimezone(JST):%Y-%m-%d %H:%M:%S}")
    print(f"ET  now : {now_utc.astimezone(ET):%Y-%m-%d %H:%M:%S}")
    print(f"POST_ENABLED : {post_enabled()}"
          + ("  ※falseの間は絶対にXへ投稿されません" if not post_enabled() else ""))
    print(f"STATE_DIR  : {state_dir()}")
    print(f"OUTPUT_DIR : {output_dir()}")
    print(f"LOG_DIR    : {log_dir()}")
    try:
        from common.posting_policy import policy_status
        policy = policy_status()
        print(
            "POST LIMIT  : "
            f"today={policy['today_count']}/{policy['daily_limit']} "
            f"hour={policy['hour_count']}/{policy['hourly_limit']}"
        )
        print(
            "X WRITE COST: "
            f"${policy['estimated_x_write_usd']:.2f}/"
            f"${policy['monthly_write_budget_usd']:.2f} this month"
        )
        from common.api_costs import monthly_openai_cost
        openai_limit = float(os.getenv("OPENAI_MONTHLY_BUDGET_USD", "5.0") or 5.0)
        print(f"OPENAI COST : ${monthly_openai_cost():.2f}/${openai_limit:.2f} this month")
        from common.xai_radar import usage_summary as xai_usage_summary
        xai=xai_usage_summary()
        print(f"xAI COST    : ${xai['spent_usd']:.4f}/${xai['budget_usd']:.2f} "
              f"remaining=${xai['remaining_usd']:.4f} calls(today/month)={xai['daily_calls']}/{xai['monthly_calls']}")
        from fx_alert.providers import get_provider
        fx_provider = get_provider().status(probe=False)
        print(
            "FX ALERT    : "
            f"enabled={os.getenv('FX_ENABLED','true')} "
            f"post_enabled={os.getenv('FX_POST_ENABLED','false')} "
            f"provider={fx_provider.name} ready={fx_provider.available} mode={fx_provider.mode}"
        )
        from market_data.monitor import market_status
        market = market_status()
        print(
            "MARKET DATA : "
            f"enabled={market['enabled']} post_enabled={market['post_enabled']} "
            f"external_display={market['external_display_approved']} "
            f"plan={market['plan']} credits={market['usage']['daily_credits']}/"
            f"{market['usage']['daily_limit']}"
        )
    except Exception as exc:
        print(f"POST LIMIT  : unavailable ({exc})")

    hist_file = state_dir() / "posted_history.json"
    entries = []
    if hist_file.exists():
        try:
            entries = json.loads(hist_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            entries = []
    print(f"posted_history.json : {len(entries)}件")
    for e in entries[-3:]:
        print(f"  - {e.get('posted_at','?')} [{e.get('mode','?')}] {str(e.get('title',''))[:48]}")

    print("--- 次回実行予定 ---")
    for bot in SCHED_BOTS:
        nxt = next_run_utc(bot, sched, now_utc)
        s = f"{nxt.astimezone(JST):%Y-%m-%d %H:%M} JST" if nxt else "disabled"
        last = state.get(bot, {})
        lr = last.get("last_run_at", "-")
        res = last.get("last_result", {})
        print(f"  {bot:11s} next={s}  last_run={lr}  last_rc={res.get('returncode','-')}")

    lock = Path(os.environ.get("LOCK_FILE", "") or (state_dir() / "finance_bot.lock"))
    if lock.exists():
        pid, metadata = _read_lock(lock)
        lock_status = "running" if _pid_is_running(pid) else "stale"
        print(f"lock file : {lock} -> {lock_status} (pid={pid})")
        if metadata.get("started_at"):
            print(f"daemon started : {metadata['started_at']}")
    else:
        print(f"lock file : {lock} -> absent")
    heartbeat = _heartbeat_path()
    if heartbeat.exists():
        try:
            hb = json.loads(heartbeat.read_text(encoding="utf-8"))
            print(f"last heartbeat : {hb.get('updated_at', '-')} status={hb.get('status', '-')}")
        except (json.JSONDecodeError, OSError):
            print(f"last heartbeat : unreadable ({heartbeat})")
    try:
        from common.runtime_manifest import runtime_status
        from common.data_governance import license_status
        from common.external_heartbeat import status as external_heartbeat_status
        from common.metrics_quality import stage_status
        from market_data.shadow import report as shadow_report
        from common.json_utils import make_json_safe
        print("RUNTIME      : " + json.dumps(make_json_safe(runtime_status()), ensure_ascii=False))
        print("TD LICENSE   : " + json.dumps(make_json_safe(license_status()), ensure_ascii=False))
        print("METRICS 7D   : " + json.dumps(make_json_safe(stage_status(days=7)), ensure_ascii=False))
        print("SHADOW 7D    : " + json.dumps(make_json_safe(shadow_report(days=7)), ensure_ascii=False))
        print("EXT HEARTBEAT: " + json.dumps(make_json_safe(external_heartbeat_status()), ensure_ascii=False))
    except Exception as exc:
        print(f"EXTENDED STATUS: unavailable ({type(exc).__name__})")


def cmd_init_state() -> None:
    """ローカル移行初回の暴発防止。過去スケジュールを追いかけない状態にする。"""
    now = datetime.now(JST).isoformat()
    state = load_state()
    for bot in BOTS:
        state.setdefault(bot, {})
        state[bot]["last_run_at"] = now
        state[bot]["initialized_at"] = now
    save_state(state)
    # ディレクトリも作成
    state_dir(); output_dir(); log_dir()
    print("[init-state] 実投稿は行っていません。")
    print(f"[init-state] 各Botの last_run_at を現在時刻で初期化しました: {now}")
    print(f"[init-state] 保存先: {_state_path()}")
    print("[init-state] daemon 起動後は未来のスケジュールから通常運用します"
          "（CATCH_UP_ENABLED=false のため過去スロットは追いかけません）。")


def cmd_ai_status() -> None:
    from common.openai_service import configuration_status, usage_path, embeddings_path
    status = configuration_status()
    print("=== OpenAI role status ===")
    print(f"API key configured : {'yes' if status['api_key_configured'] else 'no'}")
    print(f"allowed validation : {'ok' if status['valid'] else 'ERROR'}")
    print(f"Responses API      : {status['responses_api']}")
    for role, row in status["roles"].items():
        print(f"  {role:14s} model={row['model']:24s} enabled={row['enabled']} calls_today={status['counts'].get(role, 0)}")
    for error in status["errors"]: print(f"  ERROR: {error}")
    print(f"usage log          : {usage_path()}")
    print(f"embedding store    : {embeddings_path()}")


def cmd_ai_smoke(dry_run: bool) -> None:
    from common.openai_service import configuration_status
    status = configuration_status()
    if not dry_run:
        raise SystemExit("ai-smokeは --dry-run のみ許可しています")
    print("[ai-smoke] config-only dry-run（API・X投稿は呼びません）")
    print(f"[ai-smoke] model validation={'ok' if status['valid'] else 'error'}")
    if not status["valid"]: raise SystemExit(2)


def cmd_ai_deep() -> None:
    from common.openai_config import OpenAIRole, env_bool
    from common.openai_service import OpenAIService
    if not env_bool("OPENAI_DEEP_ANALYSIS_ENABLED", False):
        print("[ai-deep] disabled: OPENAI_DEEP_ANALYSIS_ENABLED=false")
        return
    history_path = state_dir() / "posted_history.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else []
    prompt = "以下の過去投稿だけを根拠に、金融メディアの月次改善計画を作成。最新市況を推測しない。\n" + json.dumps(history[-100:], ensure_ascii=False)
    result = OpenAIService().text(prompt, role=OpenAIRole.DEEP_ANALYZE,
                                  max_tokens=4000, operation="manual_deep_analysis", reasoning="medium")
    out = output_dir("deep_analysis") / f"strategy_{datetime.now(JST):%Y%m%d}.md"
    out.write_text(result, encoding="utf-8")
    print(f"[ai-deep] saved: {out}")


def cmd_ai_batch_status(batch_id: str = "", collect: bool = False) -> None:
    from common.openai_batch import collect as collect_batch, config_status, latest_batches, refresh
    if batch_id:
        result = collect_batch(batch_id) if collect else refresh(batch_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(json.dumps({**config_status(), "batches": list(latest_batches().values())[-10:]},
                     ensure_ascii=False, indent=2))


def cmd_ai_batch_smoke() -> None:
    from common.openai_batch import build_request_file, validate_request_file
    with tempfile.TemporaryDirectory() as temp:
        path = build_request_file([{"custom_id": "smoke-1", "input": "Batch API dry-run",
                                    "max_output_tokens": 32}], operation="smoke",
                                  output_path=Path(temp) / "smoke.jsonl")
        count = validate_request_file(path)
    print(f"[ai-batch-smoke] OK requests={count} API upload/submission=none")


def cmd_ai_batch_submit(path: str, operation: str) -> None:
    from common.openai_batch import submit
    print(json.dumps(submit(Path(path).resolve(), operation=operation), ensure_ascii=False, indent=2))


def cmd_ai_batch_cancel(batch_id: str) -> None:
    from common.openai_batch import cancel
    print(json.dumps(cancel(batch_id), ensure_ascii=False, indent=2))


def cmd_xai_status() -> None:
    from common.xai_radar import status
    row=status()
    print("=== xAI radar status ===")
    print(f"enabled={row['enabled']} ready={row['ready']}")
    print(f"model={row['model']} API key configured={'yes' if row['api_key_configured'] else 'no'}")
    print(f"reason={row['reason']} cached_topics={row['cache']['topic_count']} hit_rate={row['cache']['hit_rate']}")
    print(f"calls today={row['usage']['daily_calls']}/{row['usage']['daily_limit']} total={row['usage']['calls']}")
    print(
        f"effective_cost=${row['usage']['total_effective_cost_usd']:.4f}"
        f"/${row['usage']['budget_usd']:.2f}"
        f" remaining=${row['usage']['remaining_usd']:.4f}"
    )


def cmd_config_status() -> None:
    from common.growth_config import effective_radar_status
    schedule=load_schedule(); result={"radar":effective_radar_status(schedule.get("radar",{})),
        "post_enabled":post_enabled(),"experiments_enabled":os.getenv("EXPERIMENTS_ENABLED","true").lower() in ("1","true","yes"),
        "alerts_enabled":os.getenv("ALERTS_ENABLED","true").lower() in ("1","true","yes"),
        "batch_enabled":os.getenv("OPENAI_BATCH_ENABLED","false").lower() in ("1","true","yes")}
    print(json.dumps(result,ensure_ascii=False,indent=2))


def cmd_radar_plan() -> None:
    from common.xai_radar import radar_plan
    print(json.dumps(radar_plan(),ensure_ascii=False,indent=2))


def cmd_metrics_status() -> None:
    from common.metrics_collector import metrics_status
    print(json.dumps(metrics_status(),ensure_ascii=False,indent=2))


def cmd_fx_status() -> None:
    from fx_alert.monitor import configured_pairs, enabled
    from fx_alert.providers import get_provider
    from fx_alert.storage import load_state, read_jsonl
    provider = get_provider().status(probe=False)
    result = {
        "enabled": enabled(),
        "post_enabled": os.getenv("FX_POST_ENABLED", "false").lower() in ("1", "true", "yes"),
        "pairs": configured_pairs(),
        "configured_mode": os.getenv("FX_MONITOR_MODE", "websocket"),
        "effective_mode": provider.mode,
        "poll_interval_minutes": int(os.getenv("FX_POLL_INTERVAL_MINUTES", "5") or 5),
        "provider": provider.to_dict(),
        "movement_count": len(read_jsonl("movements.jsonl")),
        "alert_count": len(read_jsonl("alerts.jsonl")),
        "state_updated_at": load_state().get("updated_at"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_fx_provider_status(probe: bool = False) -> None:
    from fx_alert.providers import get_provider
    print(json.dumps(get_provider().status(probe=probe).to_dict(), ensure_ascii=False, indent=2))


def cmd_fx_monitor(dry_run: bool) -> None:
    from fx_alert.monitor import run_monitor
    print(json.dumps(run_monitor(dry_run=dry_run), ensure_ascii=False, indent=2))


def cmd_fx_check(pair: str, fixture: bool = False) -> None:
    from fx_alert.monitor import run_monitor
    print(json.dumps(run_monitor(dry_run=True, fixture=fixture, pair=pair), ensure_ascii=False, indent=2))


def cmd_fx_chart(pair: str, period: str) -> None:
    from fx_alert.chart import create_chart
    from fx_alert.detector import strongest_movement, detect_movements
    from fx_alert.fixture import movement_fixture
    hours = {"1h": 1, "4h": 4, "24h": 24}.get(period, 24)
    bars = movement_fixture(pair, points=max(180, hours * 60))
    movement = strongest_movement(detect_movements(bars))
    if movement is None:
        raise SystemExit("fixture did not produce a chartable movement")
    image, metadata = create_chart(bars, movement)
    print(json.dumps({"status": "created", "chart": str(image), "metadata": str(metadata)}, ensure_ascii=False, indent=2))


def cmd_fx_history(limit: int) -> None:
    from fx_alert.storage import read_jsonl
    print(json.dumps({
        "movements": read_jsonl("movements.jsonl", limit=limit),
        "alerts": read_jsonl("alerts.jsonl", limit=limit),
    }, ensure_ascii=False, indent=2))


def cmd_td_capabilities(refresh: bool = False) -> None:
    from market_data.capabilities import check_capabilities
    print(json.dumps(check_capabilities(refresh=refresh), ensure_ascii=False, indent=2))


def cmd_td_provider_status(probe: bool = False) -> None:
    from market_data.provider import TwelveDataMarketProvider, provider_status
    result = provider_status()
    if probe:
        try:
            usage = TwelveDataMarketProvider().api_usage(cache_seconds=0)
            result["probe"] = {
                "success": True,
                "plan_limit": usage.get("plan_limit"),
                "daily_usage": usage.get("daily_usage"),
            }
        except Exception as exc:
            result["probe"] = {"success": False, "error_type": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_market_data_status() -> None:
    from market_data.monitor import market_status
    print(json.dumps(market_status(), ensure_ascii=False, indent=2))


def cmd_market_watchlist() -> None:
    from market_data.symbols import load_watchlist
    print(json.dumps(load_watchlist(), ensure_ascii=False, indent=2))


def cmd_market_check(symbol: str) -> None:
    from market_data.monitor import check_symbol
    print(json.dumps(check_symbol(symbol), ensure_ascii=False, indent=2))


def cmd_market_chart(symbol: str, period: str) -> None:
    from market_data.monitor import chart_symbol
    print(json.dumps(chart_symbol(symbol, period=period), ensure_ascii=False, indent=2))


def cmd_market_fixture(kind: str) -> None:
    from market_data.monitor import run_fixture
    print(json.dumps(run_fixture(kind, send_preview=True), ensure_ascii=False, indent=2))


def cmd_market_usage() -> None:
    from market_data.storage import usage_summary
    print(json.dumps(usage_summary(), ensure_ascii=False, indent=2))


def cmd_alerts(clear_resolved: bool=False) -> None:
    from common.operations_alerts import write_alerts
    path,rows=write_alerts()
    print(json.dumps({"active":len(rows),"path":str(path),"alerts":rows},ensure_ascii=False,indent=2))


def cmd_health() -> None:
    from common.ops_quality import health_check
    print(json.dumps(health_check(),ensure_ascii=False,indent=2))


def cmd_xai_cost(days: int=30) -> None:
    from common.xai_radar import cost_report
    print(json.dumps(cost_report(days),ensure_ascii=False,indent=2))


def cmd_xai_roi(days: int=30) -> None:
    from common.ops_quality import xai_roi_report
    print(json.dumps(xai_roi_report(days),ensure_ascii=False,indent=2))


def cmd_alert_self_test() -> None:
    from common.operations_alerts import self_test
    print(json.dumps(self_test(),ensure_ascii=False,indent=2))


def cmd_radar(refresh_now: bool=False) -> None:
    from common.xai_radar import load_cache, refresh
    result=refresh() if refresh_now else {"status":"cache","topics":load_cache()}
    print(f"radar status={result.get('status')} topics={len(result.get('topics',[]))}")
    for row in result.get("topics",[]): print(f"- {row.get('topic')} acceleration={row.get('acceleration_score')} confirmation={row.get('news_confirmation_status')}")


def cmd_quote_queue(today=False,pending=False) -> None:
    from common.quote_queue import list_queue
    rows=list_queue(today=today,pending=pending)
    print(f"quote queue: {len(rows)}件（自動投稿なし）")
    for row in rows:
        print(f"- [{row.get('status')}] {row.get('detected_topic')} @{row.get('source_username')} {row.get('source_post_url')}")


def cmd_experiments(weekly=False) -> None:
    from common.experiments import variant_summary
    from common.metrics_collector import load_snapshots
    path=state_dir()/"posted_history.json"
    posts=json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    rows=variant_summary(posts,load_snapshots())
    print(f"experiments: {len(rows)} variants")
    for row in rows: print(f"- {row['variant']} n={row['sample_size']} iph={row['mean_impressions_per_hour']} confidence={row['confidence']}")


def cmd_series_drafts(series_id="") -> None:
    from common.series_drafts import create_draft,load_series
    if not series_id:
        for row in load_series(): print(f"- {row['series_id']} enabled={row['enabled']} title={row['title']}")
        return
    path=state_dir()/"posted_history.json"; sources=json.loads(path.read_text(encoding="utf-8"))[-10:] if path.exists() else []
    print(f"series draft saved: {create_draft(series_id,sources)}")


def _acquire_lock() -> Path | None:
    lock = Path(os.environ.get("LOCK_FILE", "") or (state_dir() / "finance_bot.lock"))
    lock.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "pid": os.getpid(),
        "started_at": datetime.now(JST).isoformat(),
        "repo_root": str(REPO_ROOT),
    }, ensure_ascii=False)

    for _attempt in range(2):
        try:
            # O_EXCL makes the check-and-create operation atomic.
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload + "\n")
            return lock
        except FileExistsError:
            pid, _metadata = _read_lock(lock)
            if _pid_is_running(pid):
                print(f"[daemon] 既に起動しています（pid={pid}）: {lock}")
                return None
            print(f"[daemon] 古いロックを自動削除します（pid={pid}）: {lock}")
            try:
                lock.unlink()
            except FileNotFoundError:
                continue
            except OSError as e:
                print(f"[daemon] 古いロックを削除できません: {e}")
                return None
    return None


def _read_lock(lock: Path) -> tuple[int, dict]:
    """Read both the legacy integer lock and the current JSON lock format."""
    try:
        raw = lock.read_text(encoding="utf-8").strip()
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return int(data.get("pid", 0) or 0), data
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return int(raw or "0"), {}
    except (OSError, ValueError):
        return 0, {}


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except (AttributeError, OSError):
            return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True


def _heartbeat_path() -> Path:
    return state_dir() / "daemon_heartbeat.json"


def _write_heartbeat(*, status: str, next_bot: str | None = None,
                     next_run: datetime | None = None) -> None:
    record = {
        "pid": os.getpid(),
        "status": status,
        "updated_at": datetime.now(JST).isoformat(),
        "next_bot": next_bot,
        "next_run_at": next_run.astimezone(JST).isoformat() if next_run else None,
    }
    path = _heartbeat_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def cmd_report_legacy(days: int = 1) -> None:
    """投稿実績レポート（インプレ/いいね/RT + テーマ別分析）を表示し、logs にも保存する。"""
    try:
        from common.report import build_report
    except ImportError:
        sys.path.insert(0, str(SRC_DIR / "common"))
        from report import build_report

    try:
        from common.daily_log_analysis import analyze_daily_logs
        log_analysis=analyze_daily_logs()
        print(f"[log-analysis] status={log_analysis['status']} errors={log_analysis['summary']['errors']}")
    except Exception as exc:
        print(f"[WARN] daily log analysis failed; report continues: {type(exc).__name__}")
    text = build_report(days=days)
    print(text)
    try:
        out = log_dir() / "reports"
        out.mkdir(parents=True, exist_ok=True)
        f = out / f"report_{datetime.now(JST):%Y%m%d}.txt"
        f.write_text(text + "\n", encoding="utf-8")
        print(f"\n（保存しました: {f}）")
    except OSError as e:
        print(f"[WARN] レポートの保存に失敗: {e}")


def cmd_report(days: int = 1) -> dict:
    from common.report_orchestrator import run_daily_report
    result=run_daily_report(days)
    report_task=next((task for task in result["tasks"] if task["name"]=="performance_report"),{})
    if report_task.get("status")=="success":
        print(report_task.get("result",""))
    print(json.dumps({
        "status":result["status"],"exit_code":result["exit_code"],"status_file":result["status_file"],
        "tasks":[{"name":task["name"],"status":task["status"],"error_type":task.get("error_type")}
                 for task in result["tasks"]],
    },ensure_ascii=False,indent=2))
    return result


def _print_json(value) -> None:
    from common.json_utils import make_json_safe
    print(json.dumps(make_json_safe(value), ensure_ascii=False, indent=2))


def cmd_td_license_status() -> None:
    from common.data_governance import license_status
    _print_json(license_status())


def cmd_td_license_checklist() -> None:
    from common.data_governance import license_checklist
    _print_json(license_checklist())


def cmd_market_publication_status() -> None:
    from common.data_governance import market_publication_status
    _print_json(market_publication_status())


def cmd_rss_status() -> None:
    from news_bot.news import rss_status
    _print_json(rss_status())


def cmd_metrics_quality(command: str, *, days: int = 7) -> None:
    from common.metrics_quality import missed_items, stage_status
    value = missed_items() if command == "metrics-missed" else stage_status(days=days)
    if command == "metrics-next-due":
        value = {"next_due": value["next_due"], "oldest_pending": value["oldest_pending"]}
    _print_json(value)


def cmd_xai_quality(command: str, *, days: int = 30) -> None:
    from common.xai_quality import cost_breakdown, funnel
    from common.xai_radar_v2 import cache_status
    if command == "xai-funnel":
        value = funnel(days)
    elif command == "xai-cache-status":
        value = cache_status(days)
    else:
        value = cost_breakdown(days)
    _print_json(value)


def cmd_shadow(command: str, candidate_id: str = "", reason: str = "", days: int = 7) -> None:
    from market_data.shadow import list_candidates, report, review, show
    if command == "shadow-list":
        value = list_candidates(days=days)
    elif command == "shadow-show":
        value = show(candidate_id) or {"status": "not_found", "candidate_id": candidate_id}
    elif command == "shadow-approve":
        value = review(candidate_id, "approved", reason)
    elif command == "shadow-reject":
        value = review(candidate_id, "rejected", reason)
    else:
        value = report(days=days)
    _print_json(value)


def cmd_external_heartbeat(command: str) -> None:
    from common.external_heartbeat import publish, status
    _print_json(publish(dry_run=True) if command == "heartbeat-test" else status())


def cmd_runtime_manifest(write: bool = False) -> None:
    from common.runtime_manifest import runtime_status, write_manifest
    _print_json(write_manifest() if write else runtime_status())


def cmd_daemon() -> None:
    sched = load_schedule()
    window = int(os.environ.get("RUN_WINDOW_MINUTES", "10") or 10)
    catch_up = os.environ.get("CATCH_UP_ENABLED", "false").strip().lower() in ("true", "1", "yes")

    lock = _acquire_lock()
    if lock is None:
        return

    stop = {"flag": False}

    def _sigint(_sig, _frm):
        print("\n[daemon] 停止要求を受信。安全に終了します。")
        stop["flag"] = True

    signal.signal(signal.SIGINT, _sigint)
    try:
        signal.signal(signal.SIGTERM, _sigint)
    except (ValueError, AttributeError):
        pass

    print(f"[daemon] 起動 POST_ENABLED={post_enabled()} window={window}min catch_up={catch_up}")
    try:
        from common.runtime_manifest import write_manifest
        write_manifest()
    except Exception as exc:
        print(f"[WARN] runtime manifest unavailable: {type(exc).__name__}")
    _write_heartbeat(status="started")
    try:
        while not stop["flag"]:
            now_utc = datetime.now(timezone.utc)
            plans = []
            for bot in SCHED_BOTS:
                nxt = next_run_utc(bot, sched, now_utc)
                if nxt:
                    plans.append((nxt, bot))
            if not plans:
                print("[daemon] 有効なスケジュールがありません。終了します。")
                break
            plans.sort()
            nxt, bot = plans[0]
            due_bots = [name for planned_at, name in plans
                        if abs((planned_at - nxt).total_seconds()) < 1]
            next_label = ",".join(due_bots)
            _write_heartbeat(status="waiting", next_bot=next_label, next_run=nxt)
            print(f"[daemon] 次回: {next_label} @ {nxt.astimezone(JST):%Y-%m-%d %H:%M} JST")

            # sleepは分割して Ctrl+C に応答
            while not stop["flag"]:
                remain = (nxt - datetime.now(timezone.utc)).total_seconds()
                if remain <= 0:
                    break
                time.sleep(min(remain, 30))
                _write_heartbeat(status="waiting", next_bot=next_label, next_run=nxt)
                try:
                    from common.external_heartbeat import publish
                    publish()
                except Exception as exc:
                    print(f"[WARN] external heartbeat continued after {type(exc).__name__}")
            if stop["flag"]:
                break

            for due_bot in due_bots:
                delay_min = (datetime.now(timezone.utc) - nxt).total_seconds() / 60.0
                if delay_min > window and not catch_up:
                    print(f"[daemon] {due_bot}: 予定より{delay_min:.0f}分遅延（window={window}分超）のためスキップ")
                    log_run({"bot": due_bot, "skipped": True,
                             "reason": f"delayed {delay_min:.0f}min > window {window}min"})
                    continue
                _write_heartbeat(status="running", next_bot=due_bot, next_run=nxt)
                run_bot(due_bot, sched=sched)
    finally:
        try:
            _write_heartbeat(status="stopped")
        except OSError:
            pass
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass
        print("[daemon] 終了しました（lock解除済み）。")


def main() -> None:
    load_env()
    try:
        from common.openai_config import validate_models
        model_errors = validate_models()
        if model_errors:
            print("[ERROR] OpenAIモデル設定が許可リスト外です。OpenAI機能を安全停止します。")
            for error in model_errors: print(f"[ERROR] {error}")
    except Exception as exc:
        print(f"[WARN] OpenAIモデル設定の検証に失敗: {type(exc).__name__}")
    try:
        from common.runtime import setup_file_logging
        setup_file_logging()
    except Exception:
        pass
    if not (REPO_ROOT / ".env").exists():
        print("[WARN] .env が見つかりません。`cp .env.example .env` で作成し、APIキーを設定してください。")
        print("[WARN] POST_ENABLED=false のままなら実投稿はされません（動作確認は可能な範囲で進みます）。")

    ap = argparse.ArgumentParser(description="ローカル金融Bot CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("init-state")
    rp = sub.add_parser("report", help="投稿実績（インプレ/いいね/RT）を集計して表示")
    rp.add_argument("--days", type=int, default=1, help="直近何日分をサマリするか（既定1）")
    for name in ("once", "force"):
        sp = sub.add_parser(name)
        sp.add_argument("bot", choices=SCHED_BOTS)
        sp.add_argument("--mode", choices=["image", "diagram"], default=None,
                        help="newsのみ有効")
    sub.add_parser("daemon")
    sub.add_parser("ai-status", help="OpenAIの役割・モデル・当日利用回数を表示")
    smoke = sub.add_parser("ai-smoke", help="OpenAI設定をAPI呼び出しなしで検証")
    smoke.add_argument("--dry-run", action="store_true", required=True)
    sub.add_parser("ai-deep", help="有効時のみSolで手動戦略分析（1日1回）")
    sub.add_parser("xai-status", help="xAI X Searchレーダーの状態と予算")
    xsmoke=sub.add_parser("xai-smoke", help="xAI設定を呼び出しなしで確認")
    xsmoke.add_argument("--dry-run",action="store_true",required=True)
    radar=sub.add_parser("radar",help="X話題レーダーを表示")
    batch_status = sub.add_parser("ai-batch-status", help="Batch API configuration and job status")
    batch_status.add_argument("--batch-id", default="")
    batch_status.add_argument("--collect", action="store_true")
    sub.add_parser("ai-batch-smoke", help="Validate Batch JSONL without an API call")
    batch_submit = sub.add_parser("ai-batch-submit", help="Submit an analysis JSONL file")
    batch_submit.add_argument("input")
    batch_submit.add_argument("--operation", default="manual_analysis")
    batch_cancel = sub.add_parser("ai-batch-cancel", help="Cancel a Batch job")
    batch_cancel.add_argument("batch_id")
    sub.add_parser("config-status", help="Feature flags and effective configuration")
    sub.add_parser("radar-plan", help="Today's xAI priority-window allocation")
    sub.add_parser("rss-status", help="Per-feed RSS health without fetching or posting")
    sub.add_parser("metrics-status", help="Metrics collection status")
    sub.add_parser("metrics-stage-status", help="Stage-level metrics status")
    sub.add_parser("metrics-missed", help="Classified missed metrics")
    sub.add_parser("metrics-next-due", help="Next metrics deadlines")
    for command in ("metrics-rolling",):
        metrics_rolling = sub.add_parser(command, help="Rolling metrics success")
        metrics_rolling.add_argument("--days", type=int, choices=[7, 30], default=7)
    sub.add_parser("health-check", help="Windows local daemon and data-quality health")
    sub.add_parser("fx-status", help="FX Alertの設定・実行状態")
    fx_provider = sub.add_parser("fx-provider-status", help="FXデータプロバイダー状態")
    fx_provider.add_argument("--probe", action="store_true")
    fx_monitor = sub.add_parser("fx-monitor", help="FX監視を1回実行")
    fx_monitor.add_argument("--dry-run", action="store_true", required=True)
    fx_check = sub.add_parser("fx-check", help="指定通貨ペアを安全に判定")
    fx_check.add_argument("pair")
    fx_check.add_argument("--fixture", action="store_true")
    fx_chart = sub.add_parser("fx-chart", help="FXチャートを生成（投稿なし）")
    fx_chart.add_argument("pair")
    fx_chart.add_argument("--period", choices=["1h", "4h", "24h"], default="24h")
    fx_test = sub.add_parser("fx-alert-test", help="fixtureによるエンドツーエンド試験")
    fx_test.add_argument("--fixture", action="store_true", required=True)
    fx_history = sub.add_parser("fx-history", help="FX検知・通知履歴")
    fx_history.add_argument("--limit", type=int, default=20)
    sub.add_parser("fx-enable-status", help="FX投稿フラグを変更せず表示")
    td_capabilities = sub.add_parser("td-capabilities", help="Twelve Data capability and plan audit")
    td_capabilities.add_argument("--refresh", action="store_true")
    td_provider = sub.add_parser("td-provider-status", help="Twelve Data provider status")
    td_provider.add_argument("--probe", action="store_true")
    sub.add_parser("td-license-status", help="Unified Twelve Data display rights")
    sub.add_parser("td-license-checklist", help="Human contract review checklist")
    sub.add_parser("market-publication-status", help="Publication gates by surface")
    sub.add_parser("market-data-status", help="Multi-asset monitor status")
    sub.add_parser("market-watchlist", help="Configured multi-asset watchlist")
    market_check = sub.add_parser("market-check", help="Safely inspect one market symbol")
    market_check.add_argument("symbol")
    market_chart = sub.add_parser("market-chart", help="Create a local market chart without posting")
    market_chart.add_argument("symbol")
    market_chart.add_argument("--period", choices=["1h", "4h", "24h"], default="24h")
    for command in ("mega-alert-test", "etf-alert-test", "cross-asset-test", "earnings-reaction-test"):
        fixture_parser = sub.add_parser(command, help="Run a clearly labelled local fixture test")
        fixture_parser.add_argument("--fixture", action="store_true", required=True)
    sub.add_parser("market-usage", help="Twelve Data credit and cache usage")
    sub.add_parser("market-data-enable-status", help="Display market-data flags without changing them")
    xcost=sub.add_parser("xai-cost-report", help="xAI exclusive cost report")
    xcost.add_argument("--days",type=int,default=30)
    xroi=sub.add_parser("xai-roi-report", help="xAI influence and ROI report")
    xroi.add_argument("--days",type=int,default=30)
    for command in ("xai-roi", "xai-funnel", "xai-cost-breakdown", "xai-cache-status"):
        parser = sub.add_parser(command)
        parser.add_argument("--days", type=int, default=30)
    shadow_list = sub.add_parser("shadow-list")
    shadow_list.add_argument("--days", type=int, default=30)
    shadow_show = sub.add_parser("shadow-show")
    shadow_show.add_argument("candidate_id")
    shadow_approve = sub.add_parser("shadow-approve")
    shadow_approve.add_argument("candidate_id")
    shadow_reject = sub.add_parser("shadow-reject")
    shadow_reject.add_argument("candidate_id")
    shadow_reject.add_argument("--reason", required=True)
    shadow_report_parser = sub.add_parser("shadow-report")
    shadow_report_parser.add_argument("--days", type=int, default=7)
    sub.add_parser("heartbeat-status")
    heartbeat_test = sub.add_parser("heartbeat-test")
    heartbeat_test.add_argument("--dry-run", action="store_true", required=True)
    runtime_manifest = sub.add_parser("runtime-manifest")
    runtime_manifest.add_argument("--write", action="store_true")
    sub.add_parser("alerts-self-test",help="Test alert writes without touching production files")
    alerts=sub.add_parser("alerts", help="Local operational alerts")
    alerts.add_argument("--clear-resolved",action="store_true")
    radar.add_argument("--refresh",action="store_true")
    quote=sub.add_parser("quote-queue",help="手動引用候補を表示（自動投稿なし）")
    quote.add_argument("--today",action="store_true"); quote.add_argument("--pending",action="store_true")
    experiments=sub.add_parser("experiments",help="投稿実験のvariant集計")
    experiments.add_argument("--weekly",action="store_true")
    series=sub.add_parser("series-drafts",help="定番シリーズの草案生成（自動投稿なし）")
    series.add_argument("--series-id",default="")
    args = ap.parse_args()

    if args.cmd == "status":
        cmd_status()
    elif args.cmd == "init-state":
        cmd_init_state()
    elif args.cmd == "report":
        result=cmd_report(days=args.days)
        if result.get("exit_code") == 1: raise SystemExit(1)
    elif args.cmd in ("once", "force"):
        result = run_bot(args.bot, mode=getattr(args, "mode", None), force=(args.cmd == "force"))
        if result.get("returncode", 0) != 0:
            raise SystemExit(1)
    elif args.cmd == "daemon":
        cmd_daemon()
    elif args.cmd == "ai-status":
        cmd_ai_status()
    elif args.cmd == "ai-smoke":
        cmd_ai_smoke(args.dry_run)
    elif args.cmd == "ai-deep":
        cmd_ai_deep()
    elif args.cmd == "ai-batch-status": cmd_ai_batch_status(args.batch_id, args.collect)
    elif args.cmd == "ai-batch-smoke": cmd_ai_batch_smoke()
    elif args.cmd == "ai-batch-submit": cmd_ai_batch_submit(args.input, args.operation)
    elif args.cmd == "ai-batch-cancel": cmd_ai_batch_cancel(args.batch_id)
    elif args.cmd == "xai-status": cmd_xai_status()
    elif args.cmd == "rss-status": cmd_rss_status()
    elif args.cmd == "xai-smoke":
        print("[xai-smoke] config-only dry-run（検索・投稿なし）"); cmd_xai_status()
    elif args.cmd == "config-status": cmd_config_status()
    elif args.cmd == "radar-plan": cmd_radar_plan()
    elif args.cmd == "metrics-status": cmd_metrics_status()
    elif args.cmd in ("metrics-stage-status", "metrics-missed", "metrics-next-due"):
        cmd_metrics_quality(args.cmd)
    elif args.cmd == "metrics-rolling": cmd_metrics_quality(args.cmd, days=args.days)
    elif args.cmd == "health-check": cmd_health()
    elif args.cmd == "fx-status": cmd_fx_status()
    elif args.cmd == "fx-provider-status": cmd_fx_provider_status(args.probe)
    elif args.cmd == "fx-monitor": cmd_fx_monitor(args.dry_run)
    elif args.cmd == "fx-check": cmd_fx_check(args.pair, args.fixture)
    elif args.cmd == "fx-chart": cmd_fx_chart(args.pair, args.period)
    elif args.cmd == "fx-alert-test": cmd_fx_check("USDJPY", True)
    elif args.cmd == "fx-history": cmd_fx_history(args.limit)
    elif args.cmd == "fx-enable-status": cmd_fx_status()
    elif args.cmd == "td-capabilities": cmd_td_capabilities(args.refresh)
    elif args.cmd == "td-provider-status": cmd_td_provider_status(args.probe)
    elif args.cmd == "td-license-status": cmd_td_license_status()
    elif args.cmd == "td-license-checklist": cmd_td_license_checklist()
    elif args.cmd == "market-publication-status": cmd_market_publication_status()
    elif args.cmd in ("market-data-status", "market-data-enable-status"): cmd_market_data_status()
    elif args.cmd == "market-watchlist": cmd_market_watchlist()
    elif args.cmd == "market-check": cmd_market_check(args.symbol)
    elif args.cmd == "market-chart": cmd_market_chart(args.symbol, args.period)
    elif args.cmd == "mega-alert-test": cmd_market_fixture("mega")
    elif args.cmd == "etf-alert-test": cmd_market_fixture("etf")
    elif args.cmd == "cross-asset-test": cmd_market_fixture("cross_asset")
    elif args.cmd == "earnings-reaction-test": cmd_market_fixture("earnings")
    elif args.cmd == "market-usage": cmd_market_usage()
    elif args.cmd == "xai-cost-report": cmd_xai_cost(args.days)
    elif args.cmd == "xai-roi-report": cmd_xai_roi(args.days)
    elif args.cmd in ("xai-roi", "xai-funnel", "xai-cost-breakdown", "xai-cache-status"):
        cmd_xai_quality(args.cmd, days=args.days)
    elif args.cmd in ("shadow-list", "shadow-show", "shadow-approve", "shadow-reject", "shadow-report"):
        cmd_shadow(
            args.cmd,
            candidate_id=getattr(args, "candidate_id", ""),
            reason=getattr(args, "reason", ""),
            days=getattr(args, "days", 7),
        )
    elif args.cmd in ("heartbeat-status", "heartbeat-test"):
        cmd_external_heartbeat(args.cmd)
    elif args.cmd == "runtime-manifest":
        cmd_runtime_manifest(args.write)
    elif args.cmd == "alerts-self-test": cmd_alert_self_test()
    elif args.cmd == "alerts": cmd_alerts(args.clear_resolved)
    elif args.cmd == "radar": cmd_radar(args.refresh)
    elif args.cmd == "quote-queue": cmd_quote_queue(args.today,args.pending)
    elif args.cmd == "experiments": cmd_experiments(args.weekly)
    elif args.cmd == "series-drafts": cmd_series_drafts(args.series_id)


if __name__ == "__main__":
    main()
