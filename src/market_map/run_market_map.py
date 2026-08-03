from market_map import generate_market_map_post, post_to_x


def _post_enabled() -> bool:
    import os
    return os.environ.get("POST_ENABLED", "false").strip().lower() in ("true", "1", "yes")


def _decision_log(**fields):
    """market-map の判断ログ（#5）。標準出力＋ decisions.jsonl。"""
    import logging
    from datetime import datetime, timezone, timedelta
    fields.setdefault("bot", "market-map")
    fields.setdefault("selected_post_type", "market_map")
    fields.setdefault("ts", datetime.now(timezone(timedelta(hours=9))).isoformat())
    logging.getLogger(__name__).info(
        "[MARKET-MAP] bot=%s | selected_post_type=%s | market_move=%s | "
        "market_cap_change=%s | threshold=%s | should_post=%s | skip_reason=%s | "
        "post_enabled=%s | dry_run=%s | actual_post_attempted=%s | tweet_id=%s",
        fields.get("bot"), fields.get("selected_post_type"), fields.get("market_move"),
        fields.get("market_cap_change"), fields.get("threshold"), fields.get("should_post"),
        fields.get("skip_reason", "-"), fields.get("post_enabled"),
        fields.get("dry_run"), fields.get("actual_post_attempted"), fields.get("tweet_id", "-"),
    )
    try:
        from common.runtime import log_decision
        log_decision(fields)
    except Exception:
        try:
            from runtime import log_decision
            log_decision(fields)
        except Exception:
            pass


def _env_float(name: str, default: float) -> float:
    import os
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _force() -> bool:
    import os
    return os.environ.get("FORCE_POST", "").strip().lower() in ("true", "1", "yes")


def _market_move_gate(
    move: float | None,
    total_pct: float,
    skew: float,
    breadth_ratio: float = 0.5,
    max_sector_pct: float = 0.0,
    intraday_reversal_pct: float = 0.0,
    *,
    min_abs: float,
    min_pct: float,
    min_skew: float,
    min_breadth: float = 0.7,
    min_sector_pct: float = 1.5,
    min_reversal_pct: float = 0.75,
    force: bool = False,
) -> dict:
    """Evaluate large moves only; scheduled slots never bypass this gate."""
    abs_move = abs(float(move or 0.0))
    gate_abs = abs_move >= min_abs
    gate_pct = abs(float(total_pct or 0.0)) >= min_pct
    # Concentration alone is noisy on quiet days, so require meaningful size.
    gate_skew = skew >= min_skew and abs_move >= min_abs * 0.5
    breadth_extreme = (
        breadth_ratio >= min_breadth
        or breadth_ratio <= 1.0 - min_breadth
    )
    gate_rotation = breadth_extreme and abs(max_sector_pct) >= min_sector_pct
    gate_reversal = abs(intraday_reversal_pct) >= min_reversal_pct
    return {
        "abs": gate_abs,
        "pct": gate_pct,
        "skew": gate_skew,
        "rotation": gate_rotation,
        "reversal": gate_reversal,
        "force": force,
        "pass": gate_abs or gate_pct or gate_skew or gate_rotation or gate_reversal or force,
    }


def main():
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        et_now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        et_now = datetime.now()
    session = "pre_close" if (et_now.hour, et_now.minute) >= (15, 30) else "open"
    try:
        from common.runtime import output_dir
        out = str(output_dir("market_map") / "market_map.png")
    except ImportError:
        out = "outputs/market_map/market_map.png"

    dry_run = not _post_enabled()
    post = generate_market_map_post(out_path=out, session=session)
    move = post.get("total_change")
    cap_change = post.get("total_change")  # S&P500時価総額の増減（USD）
    total_pct = post.get("total_pct", 0.0)
    skew = post.get("sector_skew", 0.0)
    breadth_ratio = post.get("breadth_ratio", 0.5)
    max_sector_pct = post.get("max_sector_pct", 0.0)
    intraday_reversal_pct = post.get("intraday_reversal_pct", 0.0)
    max_sector_pct_name = post.get("max_sector_pct_name", "")
    top_sector = post.get("top_sector", "")
    headline = post.get("headline", "")

    # 大幅変動時のみ投稿する。定時枠は判定時刻であり、ゲートを迂回しない。
    # セクター集中は、時価総額変化が絶対額基準の半分以上の場合だけ有効。
    # FORCE_POST=true は手動検証用の明示的な例外として維持する。
    min_abs = _env_float("MARKET_MAP_MIN_ABS_CHANGE_USD", 500e9)
    min_pct = _env_float("MARKET_MAP_MIN_INDEX_PCT", 1.0)
    min_skew = _env_float("MARKET_MAP_SECTOR_SKEW", 0.7)
    min_breadth = _env_float("MARKET_MAP_MIN_BREADTH_RATIO", 0.7)
    min_sector_pct = _env_float("MARKET_MAP_MIN_SECTOR_PCT", 1.5)
    min_reversal_pct = _env_float("MARKET_MAP_MIN_INTRADAY_REVERSAL_PCT", 0.75)
    gate = _market_move_gate(
        move,
        total_pct,
        skew,
        breadth_ratio,
        max_sector_pct,
        intraday_reversal_pct,
        min_abs=min_abs,
        min_pct=min_pct,
        min_skew=min_skew,
        min_breadth=min_breadth,
        min_sector_pct=min_sector_pct,
        min_reversal_pct=min_reversal_pct,
        force=_force(),
    )
    gate_abs = gate["abs"]
    gate_pct = gate["pct"]
    gate_skew = gate["skew"]
    gate_rotation = gate["rotation"]
    gate_reversal = gate["reversal"]
    gate_pass = gate["pass"]

    print(f"[GATE] |Δmcap|=${abs(move or 0)/1e9:.0f}B(>= {min_abs/1e9:.0f}B:{gate_abs}) "
          f"| idx≈{total_pct:+.2f}%(>= {min_pct}%:{gate_pct}) "
          f"| skew={skew:.2f}({top_sector})(>= {min_skew} and "
          f"|move|>={min_abs/2/1e9:.0f}B:{gate_skew}) "
          f"| breadth={breadth_ratio:.1%}, sector={max_sector_pct:+.2f}%"
          f"({max_sector_pct_name})(rotation:{gate_rotation}) "
          f"| reversal={intraday_reversal_pct:+.2f}pt"
          f"(>= {min_reversal_pct}pt:{gate_reversal}) "
          f"| force={_force()} -> pass={gate_pass}")

    if not gate_pass:
        _decision_log(
            market_move=move, market_cap_change=cap_change,
            threshold=(
                f"abs>={min_abs/1e9:.0f}B or pct>={min_pct}% or "
                f"(skew>={min_skew} and abs>={min_abs/2/1e9:.0f}B) or "
                f"(breadth>={min_breadth:.0%}/<={1-min_breadth:.0%} and "
                f"sector>={min_sector_pct}%) or reversal>={min_reversal_pct}pt"
            ),
            should_post=False, skip_reason="market_gate_not_met",
            post_enabled=_post_enabled(), dry_run=dry_run,
            actual_post_attempted=False, tweet_id="-",
        )
        print("市場変化が小さいため投稿スキップ（market_gate_not_met）")
        return

    if not dry_run:
        try:
            from common.xai_social_intelligence import enqueue_market_map_event
            enqueue_market_map_event({
                "headline": headline,
                "market_move": move,
                "market_cap_change": cap_change,
                "total_pct": total_pct,
                "sector_skew": skew,
                "breadth_ratio": breadth_ratio,
                "max_sector_pct": max_sector_pct,
                "top_sector": top_sector,
                "intraday_reversal_pct": intraday_reversal_pct,
                "gate": gate,
            })
        except Exception as exc:
            print(f"[WARN] xAI market-map event queue skipped: {type(exc).__name__}")

    should_post = True
    tweet_id = ""
    skip_reason = "-"
    try:
        if post["image_path"]:
            tweet_id = post_to_x(post["caption"], post["image_path"])
            print(f"ヒートマップ投稿完了: {headline}")
        else:
            tweet_id = post_to_x(post["caption"])  # 画像失敗 → テキストのみ
            print("画像なしでテキスト投稿しました")
    except Exception as e:  # noqa: BLE001
        should_post, skip_reason = False, f"post_error:{e}"
        print(f"投稿失敗: {e}")

    if tweet_id:
        try:
            from common.post_registry import record_post
        except ImportError:
            from post_registry import record_post
        record_post(
            tweet_id,
            text=post.get("caption", ""),
            title=headline,
            source="market_map",
            bot="market-map",
            mode="market-map",
            notify_discord=True,
            extra={
                "market_move": move,
                "market_cap_change": cap_change,
                "market_scope": "market_map",
                "market_session": session,
                "large_move_gate": True,
                "breadth_ratio": breadth_ratio,
                "max_sector_pct": max_sector_pct,
                "rotation_detected": gate_rotation,
                "intraday_reversal_pct": intraday_reversal_pct,
                "intraday_reversal_detected": gate_reversal,
            },
        )

    if dry_run and not tweet_id:
        skip_reason = "dry_run_not_posted"

    _decision_log(
        market_move=move, market_cap_change=cap_change,
        threshold=(
            f"abs>={min_abs/1e9:.0f}B or pct>={min_pct}% or "
            f"(skew>={min_skew} and abs>={min_abs/2/1e9:.0f}B) or "
            f"(breadth>={min_breadth:.0%}/<={1-min_breadth:.0%} and "
            f"sector>={min_sector_pct}%) or reversal>={min_reversal_pct}pt"
        ),
        should_post=should_post, skip_reason=skip_reason,
        post_enabled=_post_enabled(), dry_run=dry_run,
        actual_post_attempted=_post_enabled(), tweet_id=tweet_id or "-",
    )


if __name__ == "__main__":
    main()
