"""
reddit_signals.py
主要な投資系サブレディットから「今議論されている話題」を取得する。
Redditの公開JSONエンドポイント（/r/<sub>/top.json）を使うためAPIキーは不要。
ただし適切な User-Agent と低頻度アクセスが前提。失敗しても [] を返し全体は止めない。

返すシグナル形式（dict）:
  source     : "Reddit"
  subreddit  : "r/stocks" など
  title      : 投稿タイトル
  score      : upvote数（話題性の目安）
  comments   : コメント数
  url        : パーマリンク
  flair      : フレア（あれば）
"""

import json
import logging
import urllib.parse
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

# 市場全体・主要セクターの議論が集まるサブのみ（ローカル/雑談系は入れない）
SUBREDDITS = ["stocks", "investing", "wallstreetbets", "StockMarket", "options", "economy"]

# 低品質・運営系を除外するためのタイトル接頭辞/語
_SKIP_TITLE = [
    "daily discussion", "daily thread", "weekend discussion", "rate my portfolio",
    "moves tomorrow", "what are your moves", "megathread", "ban bet",
]

_USER_AGENT = "example-market-narrative/1.0 (by /u/example_user)"


def _fetch_subreddit(sub: str, period: str = "day", limit: int = 15) -> list[dict]:
    url = f"https://www.reddit.com/r/{sub}/top.json?" + urllib.parse.urlencode(
        {"t": period, "limit": limit}
    )
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        logger.warning(f"Reddit HTTPエラー r/{sub}: {e.code} {e.reason}")
        return []
    except Exception as e:
        logger.warning(f"Reddit取得失敗 r/{sub}: {e}")
        return []

    out = []
    for child in (data.get("data", {}) or {}).get("children", []) or []:
        d = child.get("data", {}) or {}
        title = (d.get("title", "") or "").strip()
        if not title:
            continue
        if d.get("stickied") or d.get("over_18"):
            continue
        tl = title.lower()
        if any(s in tl for s in _SKIP_TITLE):
            continue
        out.append({
            "source": "Reddit",
            "subreddit": f"r/{sub}",
            "title": title,
            "score": int(d.get("score", 0) or 0),
            "comments": int(d.get("num_comments", 0) or 0),
            "url": "https://www.reddit.com" + (d.get("permalink", "") or ""),
            "flair": (d.get("link_flair_text", "") or "").strip(),
        })
    return out


def fetch_reddit_signals(min_score: int = 200, limit_total: int = 20) -> list[dict]:
    """主要サブから話題を集約。スコア降順で上位だけ返す。失敗時は[]。"""
    all_posts: list[dict] = []
    for sub in SUBREDDITS:
        all_posts.extend(_fetch_subreddit(sub))

    # 話題性フィルタ（一定のupvoteがあるものだけ＝拡散している議論）
    filtered = [p for p in all_posts if p["score"] >= min_score]
    filtered.sort(key=lambda p: (p["score"] + p["comments"] * 2), reverse=True)
    top = filtered[:limit_total]
    logger.info(f"Redditシグナル: {len(all_posts)}件取得 → 話題性で{len(top)}件採用")
    return top


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    for s in fetch_reddit_signals():
        print(f"[{s['subreddit']}] ↑{s['score']} 💬{s['comments']} {s['title']}")
