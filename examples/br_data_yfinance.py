"""Brazilian (B3) market data adapter for Alpha Skills, backed by Yahoo Finance.

Enable it by putting the following in `.claude/alpha-agent.config.md`:

    MARKET: BR
    DATA_MODULE: examples.br_data_yfinance

Requires `pip install yfinance pandas pyarrow`.

The module implements the seven-function adapter interface described in
`examples/README.md`, so every skill (evaluate / mine / backtest / signal /
monitor) works against B3 data without any skill-side change. Instruments are
identified by their yfinance symbol, i.e. the B3 code plus a `.SA` suffix
(`PETR4.SA`, `VALE3.SA`, `BPAC11.SA`), and that symbol is what lands in the
`ts_code` column.

Universe
--------
The default pool is the IBrX 100 (Indice Brasil 100) -- the 100 most tradable
B3 assets, free-float market-cap weighted, rebalanced quadrimestrally on the
first Monday of January, May and September. It admits both ordinary/preferred
share classes of the same issuer (PETR3 / PETR4, ITUB3 / ITUB4) and units
(BPAC11, SANB11, TAEE11). Those are genuinely distinct instruments with their
own liquidity and price behaviour -- keep them all in the cross-section rather
than collapsing them by issuer.

`refresh_universe()` re-fetches the current portfolio straight from B3 so the
hard-coded list below can be regenerated after a rebalance.

Known limitations -- read before trusting any backtest built on this adapter
--------------------------------------------------------------------------
1. SURVIVORSHIP / MEMBERSHIP LOOK-AHEAD. `IBRX100_TICKERS` is a snapshot of
   *today's* index membership applied to *all* history. Names that fell out of
   the index (or delisted, or were absorbed in a merger) are absent, and names
   that only recently entered are present for years before they qualified.
   B3 index turnover is high -- recent portfolios alone carry ticker changes
   from the Eletrobras, Embraer, CCR, Marfrig/BRF and Petz/Cobasi events -- so
   this bias bites harder here than on a US large-cap universe. For research
   you intend to trade, replace the static list with B3's point-in-time
   historical index portfolios.
2. NO FUNDAMENTAL DATA. `load_financial` returns an empty frame and
   `load_daily_basic` returns NaN for pe_ttm / pb / circ_mv / total_mv. See
   `load_daily_basic` for why broadcasting a current snapshot backwards is
   worse than returning nothing. Valuation, quality and growth factors will
   report no coverage; price/volume factors are unaffected.
3. NOMINAL BRL, ZERO RISK-FREE RATE. Returns are nominal BRL and the skills
   compute Sharpe against rf = 0. With the Selic in double digits that
   materially flatters risk-adjusted numbers. Prefer long-short spreads over
   absolute Sharpe, or subtract CDI yourself.
4. THIN YAHOO COVERAGE ON SOME NAMES. The 2020-21 IPO cohort and recently
   renamed tickers have short histories; `load_prices` warns about symbols that
   come back empty instead of dropping them silently.
5. `amount` IS AN APPROXIMATION. Yahoo publishes share volume but not traded
   financial volume, so `amount` is computed as `close * vol`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.request
import warnings
from typing import List, Optional

import pandas as pd

try:
    import yfinance as yf
except ImportError as exc:  # pragma: no cover - import-time guard
    raise ImportError(
        "yfinance is required for examples.br_data_yfinance. "
        "Install it with: pip install yfinance"
    ) from exc


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CACHE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data_cache_br"
)
os.makedirs(CACHE_DIR, exist_ok=True)


# When False (default) `turnover_rate_f` is NaN and turnover-family factors
# report no coverage; use `vol` / `amount` from load_prices for volume signals.
# When True, turnover is approximated as vol / sharesOutstanding * 100 using a
# *current* share count applied to all history -- itself a mild look-ahead, and
# a real one in Brazil where follow-on issuance is common. Opt in knowingly.
USE_STATIC_SHARES_FOR_TURNOVER = False


# IBrX 100 theoretical portfolio, fetched from B3's official index API
# (carteira do dia 07/08/26; 99 assets -- B3 publishes fewer than 100 whenever
# the negotiability buffer leaves a slot unfilled). Regenerate after a
# rebalance with `refresh_universe()`.
IBRX100_CODES: List[str] = [
    "ALOS3", "ABEV3", "ANIM3", "ASAI3", "AURE3", "AXIA3", "AZZA3", "B3SA3",
    "BBSE3", "BBDC3", "BBDC4", "BRAP4", "SAUD3", "BBAS3", "BRKM5", "BRAV3",
    "BPAC11", "CXSE3", "CBAV3", "CEAB3", "CMIG4", "COGN3", "CSMG3", "CPLE3",
    "CSAN3", "CPFE3", "CMIN3", "CURY3", "CVCB3", "CYRE3", "DIRR3", "ECOR3",
    "EMBJ3", "ENGI11", "ENEV3", "EGIE3", "EQTL3", "EZTC3", "FLRY3", "GGBR4",
    "GOAU4", "GGPS3", "GMAT3", "HAPV3", "HYPE3", "IGTI11", "INTB3", "IRBR3",
    "ISAE4", "ITSA4", "ITUB3", "ITUB4", "JHSF3", "KLBN11", "RENT3", "LREN3",
    "MGLU3", "POMO4", "MBRF3", "BEEF3", "MOTV3", "MDNE3", "MOVI3", "MRVE3",
    "MULT3", "NATU3", "ORVR3", "PETR3", "PETR4", "RECV3", "AUAU3", "PSSA3",
    "PRIO3", "RADL3", "RAPT4", "RDOR3", "RAIL3", "SBSP3", "SAPR11", "SANB11",
    "SMTO3", "CSNA3", "SIMH3", "SLCE3", "SMFT3", "SUZB3", "TAEE11", "VIVT3",
    "TEND3", "TIMS3", "TOTS3", "UGPA3", "USIM5", "VALE3", "VAMO3", "VBBR3",
    "VIVA3", "WEGE3", "YDUQ3",
]

# yfinance addresses B3 listings with a '.SA' suffix.
IBRX100_TICKERS: List[str] = [f"{code}.SA" for code in IBRX100_CODES]

# Deduplicate while preserving order
IBRX100_TICKERS = list(dict.fromkeys(IBRX100_TICKERS))


MARKET_CONFIG = {
    "market": "BR",
    "currency": "BRL",
    # Ibovespa, not the IBrX index: the benchmark series is what load_trade_cal
    # derives the B3 calendar from, so it needs long gap-free history. ^BVSP
    # has it and tracks IBrX 100 almost identically.
    "benchmark": "^BVSP",
    # ~20bps round-trip. B3 emolumentos ~0.0030% + liquidacao ~0.0250% is only
    # ~0.03%/side, and retail brokerage is commonly zero; the rest is spread
    # and impact. Conservative for IBrX 100 names; override via config.
    "cost_rate": 0.002,
    # B3 has no fixed daily price band, so the skills' limit-up filter is
    # correctly skipped. It does halt individual names into a leilao when they
    # breach an intraday tunnel, and trips market-wide circuit breakers at
    # -10% (30 min) and -15% (1 h), so fills on gap days are not guaranteed.
    "price_limit": None,
    # IRRF withheld at source on sale proceeds ("dedo-duro"). The 15% swing /
    # 20% day-trade capital gains tax is an investor-level charge settled
    # monthly and is deliberately not modelled here.
    "tax_sell": 0.00005,
    "min_trade_unit": 100,      # round lot; the fractional market allows 1-99
    "t_plus": 0,                # day trade permitted (settlement is D+2)
    "trade_calendar_source": "B3",
}


_B3_INDEX_API = (
    "https://sistemaswebb3-listados.b3.com.br/indexProxy/indexCall/GetPortfolioDay/"
)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_key(name: str, **kwargs) -> str:
    """Build a deterministic cache filename from function name + kwargs."""
    payload = name + repr(sorted(kwargs.items()))
    digest = hashlib.md5(payload.encode("utf-8")).hexdigest()[:12]
    return f"{name}_{digest}.parquet"


def _load_cache(name: str, **kwargs) -> Optional[pd.DataFrame]:
    path = os.path.join(CACHE_DIR, _cache_key(name, **kwargs))
    if os.path.exists(path):
        try:
            return pd.read_parquet(path)
        except Exception:
            # Corrupted cache file -- ignore and refetch.
            return None
    return None


def _is_open_ended(end_date: str) -> bool:
    """True when `end_date` reaches today, i.e. the last bar is still forming."""
    try:
        return pd.Timestamp(_to_yf_date(end_date)) >= pd.Timestamp.today().normalize()
    except Exception:
        return True


def _save_cache(name: str, df: Optional[pd.DataFrame], **kwargs) -> None:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return
    # The cache has no TTL, so a window ending today would pin a partial final
    # bar forever. Skip the write and refetch instead.
    if _is_open_ended(str(kwargs.get("end", ""))):
        return
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, _cache_key(name, **kwargs))
    try:
        df.to_parquet(path)
    except Exception:
        # Cache failure should not break the data path.
        pass


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _to_yf_date(date_str: str) -> str:
    """Convert 'YYYYMMDD' -> 'YYYY-MM-DD' for yfinance."""
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"


def _from_ts_date(ts) -> str:
    """Convert pandas Timestamp -> 'YYYYMMDD' string."""
    return pd.Timestamp(ts).strftime("%Y%m%d")


def _resolve_tickers(ts_code_list: Optional[List[str]]) -> List[str]:
    if ts_code_list is None or len(ts_code_list) == 0:
        return list(IBRX100_TICKERS)
    return list(ts_code_list)


# ---------------------------------------------------------------------------
# Download / reshape
# ---------------------------------------------------------------------------

def _download(tickers: List[str], start_date: str, end_date: str) -> pd.DataFrame:
    """Wrap yf.download to always return a (ticker, field) MultiIndex frame.

    yfinance returns a single-level column DataFrame when only one ticker is
    requested; we normalise that to a MultiIndex grouped by ticker so the
    downstream parsing is uniform.
    """
    start = _to_yf_date(start_date)
    end = _to_yf_date(end_date)

    data = yf.download(
        tickers,
        start=start,
        end=end,
        auto_adjust=False,   # keep raw OHLC *and* 'Adj Close' so we can derive adj_factor
        progress=False,
        threads=True,
        group_by="ticker",
    )

    if data is None or data.empty:
        return pd.DataFrame()

    # Single-ticker case: columns are flat (Open, High, ...). Promote to
    # MultiIndex (ticker, field) so the rest of the code is uniform.
    if not isinstance(data.columns, pd.MultiIndex):
        only = tickers[0] if len(tickers) == 1 else "TICKER"
        data.columns = pd.MultiIndex.from_product([[only], data.columns])
    return data


def _warn_missing(requested: List[str], data: pd.DataFrame) -> None:
    """Report symbols that came back with no data instead of dropping silently."""
    if data.empty:
        missing = list(requested)
    else:
        available = set(data.columns.get_level_values(0).unique())
        missing = [t for t in requested if t not in available]
    if missing:
        warnings.warn(
            f"[br_data_yfinance] no data for {len(missing)} symbol(s), dropped from "
            f"the universe: {', '.join(sorted(missing))}",
            RuntimeWarning,
            stacklevel=3,
        )


def _wide_to_long(data: pd.DataFrame) -> pd.DataFrame:
    """Reshape a (ticker, field) MultiIndex frame to a long frame.

    Returns a frame with `ts_code` + `trade_date` columns alongside the raw
    yfinance field columns (Open/High/Low/Close/Adj Close/Volume).

    Vectorised on purpose: the US and HK adapters build these frames with
    `for ts, row in df.iterrows()`, which is ~375k Python-level iterations for
    100 tickers over 15 years. `stack` does the same work in one pass.
    """
    if data.empty:
        return pd.DataFrame()

    long = data.stack(level=0, future_stack=True)
    long.index = long.index.set_names(["trade_date", "ts_code"])
    long = long.reset_index()

    if "Close" not in long.columns:
        return pd.DataFrame()

    # Rows for days a given ticker did not trade come back all-NaN.
    long = long.dropna(subset=["Close"])
    if long.empty:
        return long

    long["trade_date"] = pd.to_datetime(long["trade_date"]).dt.strftime("%Y%m%d")
    return long


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_stock_pool(date: str) -> pd.DataFrame:
    """Return the IBrX 100 stock pool.

    yfinance exposes no historical constituency feed, so this is the static
    `IBRX100_TICKERS` snapshot -- see the survivorship caveat in the module
    docstring. Columns mirror the Tushare schema:
    `ts_code, symbol, name, list_date`.
    """
    cached = _load_cache("stock_pool", date=date)
    if cached is not None:
        return cached

    rows = []
    for tk in IBRX100_TICKERS:
        rows.append(
            {
                "ts_code": tk,
                "symbol": tk.replace(".SA", ""),   # bare B3 code, e.g. PETR4
                "name": tk.replace(".SA", ""),     # full name needs a per-ticker call
                "list_date": "",                   # unknown without info()
            }
        )
    result = pd.DataFrame(rows)
    _save_cache("stock_pool", result, date=date)
    return result


def load_prices(
    start_date: str,
    end_date: str,
    ts_code_list: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Daily OHLCV bars for B3 equities.

    Returns columns: ts_code, trade_date, open, high, low, close, vol, amount.
    Prices are raw (unadjusted) BRL -- pair with `load_adj_factor` to build a
    forward-adjusted series. `vol` is in shares; `amount` is approximated as
    close * vol (see limitation 5 in the module docstring).
    """
    tickers = _resolve_tickers(ts_code_list)
    empty_cols = ["ts_code", "trade_date", "open", "high", "low",
                  "close", "vol", "amount"]

    cached = _load_cache(
        "prices", start=start_date, end=end_date, codes=tuple(tickers)
    )
    if cached is not None:
        return cached

    data = _download(tickers, start_date, end_date)
    _warn_missing(tickers, data)
    if data.empty:
        return pd.DataFrame(columns=empty_cols)

    long = _wide_to_long(data)
    if long.empty:
        return pd.DataFrame(columns=empty_cols)

    result = pd.DataFrame(
        {
            "ts_code": long["ts_code"],
            "trade_date": long["trade_date"],
            "open": long.get("Open"),
            "high": long.get("High"),
            "low": long.get("Low"),
            "close": long["Close"],
            "vol": long.get("Volume"),
        }
    )
    result["vol"] = result["vol"].fillna(0.0)
    result["amount"] = result["close"].astype(float) * result["vol"].astype(float)
    result = result[empty_cols].reset_index(drop=True)

    _save_cache(
        "prices", result, start=start_date, end=end_date, codes=tuple(tickers)
    )
    return result


def load_adj_factor(
    start_date: str,
    end_date: str,
    ts_code_list: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Adjustment factor = Adj Close / Close (forward-adjusted ratio).

    On B3 this absorbs splits (desdobramentos), bonus issues (bonificacoes),
    cash dividends and JCP (juros sobre capital proprio), which for many
    Brazilian names is the larger part of total return.

    Returns columns: ts_code, trade_date, adj_factor.
    """
    tickers = _resolve_tickers(ts_code_list)
    empty_cols = ["ts_code", "trade_date", "adj_factor"]

    cached = _load_cache(
        "adj_factor", start=start_date, end=end_date, codes=tuple(tickers)
    )
    if cached is not None:
        return cached

    data = _download(tickers, start_date, end_date)
    if data.empty:
        return pd.DataFrame(columns=empty_cols)

    long = _wide_to_long(data)
    if long.empty or "Adj Close" not in long.columns:
        return pd.DataFrame(columns=empty_cols)

    close = long["Close"].astype(float)
    result = pd.DataFrame(
        {
            "ts_code": long["ts_code"],
            "trade_date": long["trade_date"],
            "adj_factor": long["Adj Close"].astype(float) / close.where(close != 0),
        }
    )
    result = result.dropna(subset=["adj_factor"]).reset_index(drop=True)

    _save_cache(
        "adj_factor", result, start=start_date, end=end_date, codes=tuple(tickers)
    )
    return result


def load_daily_basic(
    start_date: str,
    end_date: str,
    ts_code_list: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Per-day valuation snapshot -- intentionally empty of fundamentals.

    yfinance only exposes the *latest* PE/PB/market cap via `Ticker.info`. The
    US and HK adapters broadcast that single snapshot across every historical
    trading day, which stamps today's valuation onto years of past dates: any
    factor built on those columns is look-ahead biased and its backtest is
    fiction. This adapter returns NaN instead, so valuation factors honestly
    report no coverage. Point `DATA_MODULE` at a CVM-backed source if you need
    real Brazilian fundamentals.

    `turnover_rate_f` is NaN unless `USE_STATIC_SHARES_FOR_TURNOVER` is on --
    read the caveat next to that flag before enabling it.

    Returns columns: ts_code, trade_date, pe_ttm, pb, turnover_rate_f,
    circ_mv, total_mv.
    """
    tickers = _resolve_tickers(ts_code_list)
    empty_cols = ["ts_code", "trade_date", "pe_ttm", "pb",
                  "turnover_rate_f", "circ_mv", "total_mv"]

    cached = _load_cache(
        "daily_basic", start=start_date, end=end_date, codes=tuple(tickers)
    )
    if cached is not None:
        return cached

    cal = load_trade_cal(start_date, end_date)
    if cal.empty:
        return pd.DataFrame(columns=empty_cols)
    trade_dates = cal[cal["is_open"] == 1]["cal_date"].tolist()
    if not trade_dates:
        return pd.DataFrame(columns=empty_cols)

    # Cartesian product of tickers x trading days, built without a Python loop.
    result = pd.MultiIndex.from_product(
        [tickers, trade_dates], names=["ts_code", "trade_date"]
    ).to_frame(index=False)
    for col in ("pe_ttm", "pb", "turnover_rate_f", "circ_mv", "total_mv"):
        result[col] = pd.NA

    if USE_STATIC_SHARES_FOR_TURNOVER:
        prices = load_prices(start_date, end_date, tickers)
        shares = {}
        for ticker in tickers:
            try:
                shares[ticker] = (yf.Ticker(ticker).info or {}).get("sharesOutstanding")
            except Exception:
                shares[ticker] = None
        if not prices.empty:
            vol = prices[["ts_code", "trade_date", "vol"]].copy()
            vol["shares"] = vol["ts_code"].map(shares)
            vol["turnover_rate_f"] = (
                vol["vol"].astype(float) / vol["shares"].astype(float) * 100.0
            )
            result = result.drop(columns=["turnover_rate_f"]).merge(
                vol[["ts_code", "trade_date", "turnover_rate_f"]],
                on=["ts_code", "trade_date"],
                how="left",
            )

    result = result[empty_cols]
    _save_cache(
        "daily_basic", result, start=start_date, end=end_date, codes=tuple(tickers)
    )
    return result


def load_financial(
    start_date: str,
    end_date: str,
    ts_code_list: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Fundamental statements -- not implemented for this adapter.

    Returns an empty, schema-correct frame so callers degrade cleanly rather
    than raising. Yahoo's quarterly statements for B3 issuers are sparse,
    silently restated and often misaligned to the fiscal period, which makes
    them worse than nothing for factor research. The authoritative source is
    CVM open data (dados.cvm.gov.br: DFP annual and ITR quarterly filings) --
    wire that in here if you need ROE, margins or growth factors.

    Returns columns: ts_code, ann_date, end_date, roe, roa, gross_margin,
    revenue, net_profit.
    """
    return pd.DataFrame(
        columns=["ts_code", "ann_date", "end_date", "roe", "roa",
                 "gross_margin", "revenue", "net_profit"]
    )


def load_index(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Index OHLCV. `ts_code` is a yfinance symbol such as '^BVSP'.

    Returns columns: ts_code, trade_date, close, open, high, low, vol.
    """
    empty_cols = ["ts_code", "trade_date", "close", "open", "high", "low", "vol"]

    cached = _load_cache("index", code=ts_code, start=start_date, end=end_date)
    if cached is not None:
        return cached

    data = _download([ts_code], start_date, end_date)
    if data.empty:
        return pd.DataFrame(columns=empty_cols)

    long = _wide_to_long(data)
    if long.empty:
        return pd.DataFrame(columns=empty_cols)

    result = pd.DataFrame(
        {
            "ts_code": ts_code,          # report the requested symbol, not the label
            "trade_date": long["trade_date"],
            "close": long["Close"],
            "open": long.get("Open"),
            "high": long.get("High"),
            "low": long.get("Low"),
            "vol": long.get("Volume"),
        }
    )[empty_cols].reset_index(drop=True)

    _save_cache("index", result, code=ts_code, start=start_date, end=end_date)
    return result


def load_trade_cal(start_date: str, end_date: str) -> pd.DataFrame:
    """Inferred B3 trading calendar.

    Derived from Ibovespa bar timestamps rather than a hard-coded holiday
    table, so B3-specific closures (Carnaval, Sexta-feira Santa, Corpus
    Christi, Consciencia Negra, Nossa Senhora Aparecida, Finados) are picked
    up automatically, including the years they moved.

    Returns columns: cal_date, is_open.
    """
    cached = _load_cache("trade_cal", start=start_date, end=end_date)
    if cached is not None:
        return cached

    idx = load_index(MARKET_CONFIG["benchmark"], start_date, end_date)
    if idx.empty:
        return pd.DataFrame(columns=["cal_date", "is_open"])

    open_days = set(idx["trade_date"].astype(str).tolist())

    all_days = pd.date_range(
        pd.Timestamp(_to_yf_date(start_date)),
        pd.Timestamp(_to_yf_date(end_date)),
        freq="D",
    )
    cal_dates = all_days.strftime("%Y%m%d")
    result = pd.DataFrame(
        {
            "cal_date": cal_dates,
            "is_open": [1 if d in open_days else 0 for d in cal_dates],
        }
    )

    _save_cache("trade_cal", result, start=start_date, end=end_date)
    return result


# ---------------------------------------------------------------------------
# Universe maintenance
# ---------------------------------------------------------------------------

def refresh_universe(index_code: str = "IBXX", page_size: int = 130) -> List[str]:
    """Fetch the current index portfolio from B3 and return `.SA` tickers.

    B3 rebalances the IBrX 100 quadrimestrally (first Monday of January, May
    and September), so `IBRX100_CODES` goes stale roughly three times a year.
    Run this after a rebalance and paste the result back into this module:

        python -c "import examples.br_data_yfinance as m; print(m.refresh_universe())"

    `index_code` accepts any B3 index symbol -- 'IBXX' (IBrX 100), 'IBOV'
    (Ibovespa), 'IBXL' (IBrX 50), 'SMLL' (Small Cap). Uses only the standard
    library so the module keeps its yfinance-plus-pandas dependency set.
    """
    payload = {
        "language": "pt-br",
        "pageNumber": 1,
        "pageSize": page_size,
        "index": index_code,
        "segment": "1",
    }
    token = base64.b64encode(json.dumps(payload).encode()).decode()
    request = urllib.request.Request(
        _B3_INDEX_API + token, headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))

    codes = [row["cod"] for row in body.get("results", []) if row.get("cod")]
    if not codes:
        raise RuntimeError(
            f"B3 returned no constituents for index '{index_code}'"
        )
    print(
        f"[br_data_yfinance] {index_code} portfolio dated "
        f"{body.get('header', {}).get('date', '?')}: {len(codes)} assets"
    )
    return [f"{code}.SA" for code in codes]


# ---------------------------------------------------------------------------
# Self-test (no network) -- run with: python examples/br_data_yfinance.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    print("BR data adapter loaded.")
    print(f"  market         : {MARKET_CONFIG['market']}")
    print(f"  currency       : {MARKET_CONFIG['currency']}")
    print(f"  benchmark      : {MARKET_CONFIG['benchmark']}")
    print(f"  pool size      : {len(IBRX100_TICKERS)} tickers")
    print(f"  cache dir      : {CACHE_DIR}")
    print("Public functions:")
    for fn in (
        load_prices, load_adj_factor, load_daily_basic, load_financial,
        load_index, load_stock_pool, load_trade_cal,
    ):
        print(f"  - {fn.__name__}")
