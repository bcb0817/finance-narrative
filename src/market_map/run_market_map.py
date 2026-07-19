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


def main():
    try:
        from common.runtime import output_dir
        out = str(output_dir("market_map") / "market_map.png")
    except ImportError:
        out = "outputs/market_map/market_map.png"

    dry_run = not _post_enabled()
    post = generate_market_map_post(out_path=out)
    move = post.get("total_change")
    cap_change = post.get("total_change")  # S&P500時価総額の増減（USD）
    total_pct = post.get("total_pct", 0.0)
    skew = post.get("sector_skew", 0.0)
    top_sector = post.get("top_sector", "")
    headline = post.get("headline", "")

    # #13 投稿ゲート: 大きな市場変化があるときだけ投稿する。
    #   - 時価総額変化 ±3000億ドル以上（MARKET_MAP_MIN_ABS_CHANGE_USD）
    #   - または 指数近似の変化率 ±0.5%以上（MARKET_MAP_MIN_INDEX_PCT）
    #   - または セクター偏り（|変化|の最大セクター占有率）が閾値以上
    #   FORCE_POST=true のときはゲートを無視（手動テスト用）。
    min_abs = _env_float("MARKET_MAP_MIN_ABS_CHANGE_USD", 300e9)
    min_pct = _env_float("MARKET_MAP_MIN_INDEX_PCT", 0.5)
    min_skew = _env_float("MARKET_MAP_SECTOR_SKEW", 0.6)

    gate_abs = abs(move or 0.0) >= min_abs
    gate_pct = abs(total_pct) >= min_pct
    gate_skew = skew >= min_skew
    gate_pass = gate_abs or gate_pct or gate_skew or _force()

    print(f"[GATE] |Δmcap|=${abs(move or 0)/1e9:.0f}B(>= {min_abs/1e9:.0f}B:{gate_abs}) "
          f"| idx≈{total_pct:+.2f}%(>= {min_pct}%:{gate_pct}) "
          f"| skew={skew:.2f}({top_sector})(>= {min_skew}:{gate_skew}) "
          f"| force={_force()} -> pass={gate_pass}")

    if not gate_pass:
        _decision_log(
            market_move=move, market_cap_change=cap_change,
            threshold=f"abs>={min_abs/1e9:.0f}B or pct>={min_pct}% or skew>={min_skew}",
            should_post=False, skip_reason="market_gate_not_met",
            post_enabled=_post_enabled(), dry_run=dry_run,
            actual_post_attempted=False, tweet_id="-",
        )
        print("市場変化が小さいため投稿スキップ（market_gate_not_met）")
        return

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
            extra={
                "market_move": move,
                "market_cap_change": cap_change,
                "market_scope": "market_map",
            },
        )

    if dry_run and not tweet_id:
        skip_reason = "dry_run_not_posted"

    _decision_log(
        market_move=move, market_cap_change=cap_change,
        threshold=f"abs>={min_abs/1e9:.0f}B or pct>={min_pct}% or skew>={min_skew}",
        should_post=should_post, skip_reason=skip_reason,
        post_enabled=_post_enabled(), dry_run=dry_run,
        actual_post_attempted=_post_enabled(), tweet_id=tweet_id or "-",
    )


if __name__ == "__main__":
    main()
