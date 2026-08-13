"""Contract tests for examples/br_data_yfinance.py.

Run with: python tests/test_br_data_yfinance.py

No network and no test framework required. `_download` is stubbed with a frame
shaped exactly like yfinance's real output -- a DatetimeIndex against
(ticker, field) MultiIndex columns -- so these assertions pin the adapter's
output contract: the column sets the skills expect, the YYYYMMDD date
convention, uniqueness, and that raw prices and adjustment factors recombine
into a continuous forward-adjusted series across a corporate action.

What this does NOT cover: whether Yahoo actually serves B3 symbols. Run a real
`load_prices("20240101", "20240331")` for that.
"""

import atexit
import os
import shutil
import sys
import tempfile
import warnings

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import examples.br_data_yfinance as br  # noqa: E402

FIELDS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
FAILURES = []


def check(name, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          f"{'  -- ' + detail if detail and not condition else ''}")
    if not condition:
        FAILURES.append(name)


def make_frame(tickers, dates):
    """Build a yfinance-shaped frame containing a 2:1 split at the midpoint."""
    columns = pd.MultiIndex.from_product([tickers, FIELDS])
    data = pd.DataFrame(index=pd.DatetimeIndex(dates, name="Date"),
                        columns=columns, dtype=float)
    for i, ticker in enumerate(tickers):
        n = len(dates)
        # Economic (split-free) path -- what Adj Close tracks.
        econ = np.linspace(10 + i, 20 + i, n)
        # A 2:1 split halves the *raw* quoted price from the midpoint on.
        raw = np.where(np.arange(n) < n // 2, econ * 2.0, econ)
        data[(ticker, "Open")] = raw * 0.99
        data[(ticker, "High")] = raw * 1.02
        data[(ticker, "Low")] = raw * 0.97
        data[(ticker, "Close")] = raw
        data[(ticker, "Adj Close")] = econ          # continuous across the split
        data[(ticker, "Volume")] = np.arange(n) * 1000.0 + 5000.0
    return data


# Business days with Carnaval (12-13 Feb 2024) removed, so calendar inference
# has a real B3 holiday to discover.
BUSINESS_DAYS = pd.bdate_range("2024-02-05", "2024-02-23")
CARNAVAL = {pd.Timestamp("2024-02-12"), pd.Timestamp("2024-02-13")}
OPEN_DAYS = [d for d in BUSINESS_DAYS if d not in CARNAVAL]

TICKERS = ["PETR4.SA", "VALE3.SA", "BPAC11.SA"]
DELISTED = "ZZZZ3.SA"
START, END = "20240205", "20240223"


def stub_download(tickers, start_date, end_date):
    if tickers and str(tickers[0]).startswith("^"):
        return make_frame(["^BVSP"], OPEN_DAYS)
    live = [t for t in tickers if t != DELISTED]
    return make_frame(live, OPEN_DAYS) if live else pd.DataFrame()


br._download = stub_download
# Throwaway cache so a test run never touches data_cache_br/ or leaves artifacts.
br.CACHE_DIR = tempfile.mkdtemp(prefix="br_adapter_test_")
atexit.register(shutil.rmtree, br.CACHE_DIR, True)


print("\n== load_prices ==")
prices = br.load_prices(START, END, TICKERS)
check("column set", list(prices.columns) == ["ts_code", "trade_date", "open", "high",
                                             "low", "close", "vol", "amount"])
check("row count", len(prices) == len(TICKERS) * len(OPEN_DAYS))
check("trade_date is YYYYMMDD",
      prices["trade_date"].astype(str).str.fullmatch(r"\d{8}").all())
check("no duplicate (ts_code, trade_date)",
      not prices.duplicated(["ts_code", "trade_date"]).any())
check("amount == close * vol",
      np.allclose(prices["amount"], prices["close"] * prices["vol"]))
check("holiday absent", "20240212" not in set(prices["trade_date"]))

print("\n== delisted symbols warn rather than vanish ==")
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    br.load_prices(START, END, TICKERS + [DELISTED])
    messages = [str(w.message) for w in caught]
check("warns naming the symbol", any(DELISTED in m for m in messages), str(messages))

print("\n== load_adj_factor ==")
adj = br.load_adj_factor(START, END, TICKERS)
check("column set", list(adj.columns) == ["ts_code", "trade_date", "adj_factor"])
check("no NaN", adj["adj_factor"].notna().all())
series = adj[adj["ts_code"] == "PETR4.SA"].sort_values("trade_date")["adj_factor"]
check("split reads 0.5 -> 1.0",
      np.isclose(series.iloc[0], 0.5) and np.isclose(series.iloc[-1], 1.0),
      f"{series.iloc[0]} .. {series.iloc[-1]}")

print("\n== forward-adjusted continuity (the skills' own pipeline) ==")
close_wide = prices.assign(d=pd.to_datetime(prices["trade_date"])).pivot_table(
    index="d", columns="ts_code", values="close")
adj_wide = adj.assign(d=pd.to_datetime(adj["trade_date"])).pivot_table(
    index="d", columns="ts_code", values="adj_factor")
adjusted = close_wide * (adj_wide / adj_wide.iloc[-1])
returns = adjusted.pct_change().dropna()
check("split leaves no artificial jump", (returns.abs() < 0.4).all().all(),
      str(returns.abs().max().max()))

print("\n== load_trade_cal ==")
cal = br.load_trade_cal(START, END)
check("column set", list(cal.columns) == ["cal_date", "is_open"])
check("covers every calendar day", len(cal) == 19, str(len(cal)))
check("open-day count", int(cal["is_open"].sum()) == len(OPEN_DAYS))
check("holiday closed", int(cal.loc[cal["cal_date"] == "20240212", "is_open"].iloc[0]) == 0)
check("weekend closed", int(cal.loc[cal["cal_date"] == "20240210", "is_open"].iloc[0]) == 0)

print("\n== load_daily_basic (no backdated valuations) ==")
basic = br.load_daily_basic(START, END, TICKERS)
check("column set", list(basic.columns) == ["ts_code", "trade_date", "pe_ttm", "pb",
                                            "turnover_rate_f", "circ_mv", "total_mv"])
check("trading days only", len(basic) == len(TICKERS) * len(OPEN_DAYS))
check("no weekend rows", not basic["trade_date"].isin(["20240210", "20240211"]).any())
check("pe_ttm is NA", basic["pe_ttm"].isna().all())
check("total_mv is NA", basic["total_mv"].isna().all())

print("\n== load_financial ==")
fin = br.load_financial(START, END, TICKERS)
check("empty, does not raise", fin.empty)
check("column set", list(fin.columns) == ["ts_code", "ann_date", "end_date", "roe",
                                          "roa", "gross_margin", "revenue", "net_profit"])

print("\n== load_index ==")
index = br.load_index("^BVSP", START, END)
check("column set", list(index.columns) == ["ts_code", "trade_date", "close", "open",
                                            "high", "low", "vol"])
check("reports requested symbol", (index["ts_code"] == "^BVSP").all())
check("row count", len(index) == len(OPEN_DAYS))

print("\n== load_stock_pool ==")
pool = br.load_stock_pool("20240223")
check("column set", list(pool.columns) == ["ts_code", "symbol", "name", "list_date"])
check("matches universe size", len(pool) == len(br.IBRX100_TICKERS))
check("symbol strips .SA", (~pool["symbol"].str.contains(".SA", regex=False)).all())

print("\n== adapter contract parity ==")
for name in ("load_prices", "load_adj_factor", "load_daily_basic", "load_financial",
             "load_index", "load_stock_pool", "load_trade_cal"):
    check(f"{name} present", callable(getattr(br, name, None)))
expected_keys = {"market", "currency", "benchmark", "cost_rate", "price_limit",
                 "tax_sell", "min_trade_unit", "t_plus", "trade_calendar_source"}
check("MARKET_CONFIG keys match US/HK", set(br.MARKET_CONFIG) == expected_keys,
      str(set(br.MARKET_CONFIG) ^ expected_keys))

print("\n== universe ==")
check("no duplicates", len(br.IBRX100_TICKERS) == len(set(br.IBRX100_TICKERS)))
check("all .SA suffixed", all(t.endswith(".SA") for t in br.IBRX100_TICKERS))
check("both share classes kept",
      {"PETR3.SA", "PETR4.SA"} <= set(br.IBRX100_TICKERS))
check("units kept",
      {"BPAC11.SA", "SANB11.SA", "TAEE11.SA"} <= set(br.IBRX100_TICKERS))

print("\n" + "=" * 60)
if FAILURES:
    print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
    sys.exit(1)
print("All contract assertions passed.")
