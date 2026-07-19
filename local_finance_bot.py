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
SCHED_BOTS = BOTS + ("report",)   # daemonで回す対象（reportは日次）

DEFAULT_SCHEDULE = {
    "news": {"enabled": True, "type": "interval_minutes", "every_minutes": 30,
             "default_mode": "image"},
    "narrative": {"enabled": True, "type": "et_times_business_days",
                  "times": ["08:30", "09:35", "16:05"]},
    "market-map": {"enabled": True, "type": "et_times_business_days",
                   "times": ["09:35"]},
    "weekly": {"enabled": True, "type": "weekly_jst",
               "weekday": 6, "time": "21:00"},  # weekday: 0=月 ... 6=日
    # 投稿実績レポート（毎日22:00 JST。X APIから実績を取得して集計）
    "report": {"enabled": True, "type": "daily_jst", "time": "22:00"},
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
    _state_path().write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    return None


def _next_daily_jst(now_utc: datetime, hhmm: str) -> datetime:
    """毎日 指定JST時刻の次回。"""
    now_jst = now_utc.astimezone(JST)
    h, m = map(int, hhmm.split(":"))
    cand = now_jst.replace(hour=h, minute=m, second=0, microsecond=0)
    if cand <= now_jst:
        cand = cand + timedelta(days=1)
    return cand.astimezone(timezone.utc)


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

    if bot == "report":
        started = datetime.now(JST)
        print(f"[RUN] bot=report started={started:%Y-%m-%d %H:%M:%S}")
        try:
            cmd_report(days=1)
            rc, err = 0, ""
        except Exception as e:  # noqa: BLE001
            rc, err = 1, f"{type(e).__name__}: {e}"
            print(f"[RUN] report 失敗: {err}")
        result = {"bot": "report", "mode": "report", "force": force,
                  "started_at": started.isoformat(),
                  "finished_at": datetime.now(JST).isoformat(),
                  "returncode": rc, "error": err, "post_enabled": post_enabled()}
        log_run(result)
        state = load_state(); state.setdefault("report", {})
        state["report"]["last_run_at"] = started.isoformat()
        state["report"]["last_result"] = {"returncode": rc, "error": err}
        save_state(state)
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
        # コンソールへも出しつつ、Bot別ログに追記保存
        if child_stdout:
            print(child_stdout, end="")
        if child_stderr:
            print(child_stderr, end="")
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

    # 状態更新
    state = load_state()
    state.setdefault(bot, {})
    state[bot]["last_run_at"] = started.isoformat()
    state[bot]["last_result"] = {"returncode": rc, "error": err}
    save_state(state)
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


def cmd_report(days: int = 1) -> None:
    """投稿実績レポート（インプレ/いいね/RT + テーマ別分析）を表示し、logs にも保存する。"""
    try:
        from common.report import build_report
    except ImportError:
        sys.path.insert(0, str(SRC_DIR / "common"))
        from report import build_report

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
            _write_heartbeat(status="waiting", next_bot=bot, next_run=nxt)
            print(f"[daemon] 次回: {bot} @ {nxt.astimezone(JST):%Y-%m-%d %H:%M} JST")

            # sleepは分割して Ctrl+C に応答
            while not stop["flag"]:
                remain = (nxt - datetime.now(timezone.utc)).total_seconds()
                if remain <= 0:
                    break
                time.sleep(min(remain, 30))
                _write_heartbeat(status="waiting", next_bot=bot, next_run=nxt)
            if stop["flag"]:
                break

            delay_min = (datetime.now(timezone.utc) - nxt).total_seconds() / 60.0
            if delay_min > window and not catch_up:
                print(f"[daemon] {bot}: 予定より{delay_min:.0f}分遅延（window={window}分超）のためスキップ")
                log_run({"bot": bot, "skipped": True,
                         "reason": f"delayed {delay_min:.0f}min > window {window}min"})
                continue
            _write_heartbeat(status="running", next_bot=bot, next_run=nxt)
            run_bot(bot, sched=sched)
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
    args = ap.parse_args()

    if args.cmd == "status":
        cmd_status()
    elif args.cmd == "init-state":
        cmd_init_state()
    elif args.cmd == "report":
        cmd_report(days=args.days)
    elif args.cmd in ("once", "force"):
        run_bot(args.bot, mode=getattr(args, "mode", None), force=(args.cmd == "force"))
    elif args.cmd == "daemon":
        cmd_daemon()


if __name__ == "__main__":
    main()
