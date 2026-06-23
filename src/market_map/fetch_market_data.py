"""S&P500の寄り付きデータを取得するモジュール。

- ユニバース(ticker / company_name / sector)は Wikipedia の S&P500 一覧から取得
- 価格・時価総額は yfinance の fast_info から取得(info より高速・軽量でレート制限に強い)

取得列:
    ticker, company_name, sector, current_price, prev_close, market_cap, logo_url
"""
from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO

import pandas as pd
import requests
import yfinance as yf

logger = logging.getLogger(__name__)

WIKI_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# WikipediaはUser-Agentなしのリクエストを403で弾くため、ブラウザ相当のUAを付ける
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def get_sp500_universe() -> pd.DataFrame:
    """Wikipedia から ticker / company_name / sector を取得する。

    Wikipedia のシンボルは "BRK.B" のように "." を使うが、
    yfinance は "BRK-B" を使うので置換する。
    """
    resp = requests.get(WIKI_SP500_URL, headers=_HEADERS, timeout=30)
    resp.raise_for_status()

    tables = pd.read_html(StringIO(resp.text))
    raw = tables[0]
    df = pd.DataFrame(
        {
            "ticker": raw["Symbol"].astype(str).str.replace(".", "-", regex=False),
            "company_name": raw["Security"].astype(str),
            "sector": raw["GICS Sector"].astype(str),
        }
    )
    df = df.drop_duplicates(subset="ticker").reset_index(drop=True)
    return df


def _is_valid(v) -> bool:
    if v is None:
        return False
    if isinstance(v, float) and math.isnan(v):
        return False
    return v != 0


def _safe(fast_info, *names):
    """fast_info から属性 or キーアクセスで最初に取れた有効値を返す。"""
    for n in names:
        v = None
        try:
            v = getattr(fast_info, n)
        except Exception:  # noqa: BLE001
            try:
                v = fast_info[n]
            except Exception:  # noqa: BLE001
                v = None
        if _is_valid(v):
            return v
    return None


def _fetch_one(ticker: str) -> dict | None:
    """1銘柄の current_price / prev_close / market_cap を取得。

    current_price は寄り付き値(open)を優先し、無ければ直近値(last_price)。
    """
    try:
        fi = yf.Ticker(ticker).fast_info
        current_price = _safe(fi, "open", "last_price")
        prev_close = _safe(fi, "previous_close", "regular_market_previous_close")
        market_cap = _safe(fi, "market_cap")
        if not (_is_valid(current_price) and _is_valid(prev_close) and _is_valid(market_cap)):
            return None
        return {
            "ticker": ticker,
            "current_price": float(current_price),
            "prev_close": float(prev_close),
            "market_cap": float(market_cap),
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("fetch failed for %s: %s", ticker, e)
        return None


def fetch_market_data(max_workers: int = 8) -> pd.DataFrame:
    """S&P500 全銘柄の寄り付きデータを取得して結合する。

    Returns:
        ticker, company_name, sector, current_price, prev_close,
        market_cap, logo_url を持つ DataFrame。
    """
    universe = get_sp500_universe()

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_one, t): t for t in universe["ticker"]}
        for fut in as_completed(futures):
            r = fut.result()
            if r:
                rows.append(r)

    prices = pd.DataFrame(rows)
    if prices.empty:
        raise RuntimeError(
            "価格データを取得できませんでした(yfinance のレート制限の可能性)"
        )

    df = universe.merge(prices, on="ticker", how="inner")
    # 後からロゴを追加しやすいよう logo_url 列を空で用意しておく
    df["logo_url"] = ""

    logger.info("取得成功: %d / %d 銘柄", len(df), len(universe))
    return df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = fetch_market_data()
    print(out.head())
    print("rows:", len(out))
