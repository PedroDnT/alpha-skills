# Data Adapters 数据适配器

Ready-to-use data adapters for different markets. Just set `DATA_MODULE` in your config.

开箱即用的市场数据适配器。只需在配置中设置 `DATA_MODULE` 即可。

## Available Adapters 可用适配器

| Market 市场 | File 文件 | Data Source 数据源 | Cost 成本 | Benchmark 基准 |
|-------------|-----------|-------------------|-----------|----------------|
| **A-share 中国A股** | Default (Tushare) | Tushare Pro API | 0.3% | 000300.SH |
| **US 美股** | `us_data_yfinance.py` | Yahoo Finance | 0.1% | ^GSPC |
| **HK 港股** | `hk_data_yfinance.py` | Yahoo Finance | 0.2% | ^HSI |
| **Brazil 巴西股市** | `br_data_yfinance.py` | Yahoo Finance | 0.2% | ^BVSP |

## Quick Setup 快速配置

### US Stocks 美股

```bash
pip install yfinance
```

In `.claude/alpha-agent.config.md`:
```markdown
MARKET: US
DATA_MODULE: examples.us_data_yfinance
```

### HK Stocks 港股

```bash
pip install yfinance
```

In `.claude/alpha-agent.config.md`:
```markdown
MARKET: HK
DATA_MODULE: examples.hk_data_yfinance
```

### Brazil Stocks 巴西股市

```bash
pip install yfinance
```

In `.claude/alpha-agent.config.md`:
```markdown
MARKET: BR
DATA_MODULE: examples.br_data_yfinance
```

### A-Share A股 (Default 默认)

Install tushare and configure token. No `DATA_MODULE` needed.
安装tushare并配置token，无需设置 `DATA_MODULE`。

```bash
pip install tushare
```

## Brazil adapter flags 巴西适配器选项

| Flag 选项 | Default 默认 | Effect 作用 |
|-----------|-------------|-------------|
| `USE_STATIC_SHARES_FOR_TURNOVER` | `False` | Off: `turnover_rate_f` is NaN and turnover factors report no coverage — use `vol` / `amount` for volume signals. On: turnover ≈ `vol / sharesOutstanding × 100` from a **current** share count applied to all history, which is itself mild look-ahead. |

Regenerate the IBrX 100 universe after B3's quadrimestral rebalance (first Monday of
January, May, September) / B3每四个月调整一次成分股，调整后重新生成universe：

```bash
python -c "import examples.br_data_yfinance as m; print(m.refresh_universe())"
```

### First run: verify the data path 首次运行：验证数据链路

The adapter was built and unit-tested in an environment Yahoo rate-limited, so the live
data path has never been exercised. Run these three checks once, in order, from the repo
root, before trusting any Brazilian factor output.

适配器在被 Yahoo 限流的环境中开发，实盘数据链路尚未验证。首次使用前请在仓库根目录按顺序执行以下三步检查。

**1. Contract tests — offline, no network:**

```bash
python tests/test_br_data_yfinance.py
```

Expect `All contract assertions passed.` and exit code 0. This proves the output schemas,
date convention and split adjustment are correct. It does **not** touch Yahoo.

**2. Two-ticker probe — confirms Yahoo answers at all (~30s):**

```bash
python -c "
import examples.br_data_yfinance as br
print(br.load_prices('20240102','20240131',['PETR4.SA','VALE3.SA']).head())
"
```

Run this *before* the full pull. If Yahoo is throttling you it fails here in about 30
seconds; the same failure across all 99 symbols takes several minutes before it tells you
anything. Expect ~21 rows per ticker with PETR4 in the tens of BRL.

先跑这一步。如果 Yahoo 限流，30秒内即可发现；跑全量99个代码则要等几分钟才报错。

**3. Full universe pull — the check that actually matters:**

```bash
python -c "
import examples.br_data_yfinance as br
df = br.load_prices('20240102', '20240331')
print(f'rows={len(df)} tickers={df.ts_code.nunique()} dates={df.trade_date.nunique()}')
"
```

Expect roughly **6,000 rows across ~99 tickers and ~60 trading days**. Then confirm the
skills see it end to end — set `MARKET: BR` in your config and ask your assistant to
`evaluate reversal_5`.

**Troubleshooting 故障排查**

| Symptom 现象 | Meaning 含义 |
|---|---|
| `429 Too Many Requests`, or `curl (35) Recv failure: Connection reset by peer` | Yahoo is throttling your IP, not an adapter bug. Wait, or run from a different network. Shared cloud/CI IPs are throttled most often. |
| `rows=0` plus a warning naming *every* symbol | Nothing reached the adapter — network, not data. The real error prints above the result. |
| Full pull hangs for minutes | Same throttling, ×99 symbols. Cancel and run the two-ticker probe to see the error quickly. |
| Warning naming *a few* symbols | Normal. Those tickers have no Yahoo history (recent renames or IPOs); the universe shrinks and the run continues. |
| `ModuleNotFoundError: yfinance` | `pip install yfinance` |
| `ModuleNotFoundError: examples` | Run from the repo root so `examples/` is importable. |

**Caveats 注意事项** — full detail in the module docstring:

- **No fundamentals.** `load_financial` is empty by design and `load_daily_basic` returns
  NaN valuations rather than backdating a current snapshot. Price/volume factors only.
- **Static index membership** → survivorship bias; B3 index turnover is high.
- **Nominal BRL against a zero risk-free rate** — with the Selic in double digits this
  flatters Sharpe. Prefer long-short spreads.
- **~99 names is a thin cross-section** — about 20 per quintile. Read IC and long-short
  spreads rather than leaning on single-quintile results.

## Writing Your Own Adapter 编写自定义适配器

Create a Python module with these functions (all returning pandas DataFrames):

创建一个Python模块，实现以下函数（全部返回pandas DataFrame）：

```python
MARKET_CONFIG = {
    "market": "YourMarket",
    "currency": "USD",
    "benchmark": "INDEX_SYMBOL",
    "cost_rate": 0.001,
    "price_limit": None,      # or 0.1 for markets with price limits
    "min_trade_unit": 1,
    "t_plus": 0,
}

def load_prices(start_date, end_date, ts_code_list=None): ...
def load_adj_factor(start_date, end_date, ts_code_list=None): ...
def load_daily_basic(start_date, end_date, ts_code_list=None): ...
def load_financial(start_date, end_date, ts_code_list=None): ...
def load_index(ts_code, start_date, end_date): ...
def load_stock_pool(date): ...
def load_trade_cal(start_date, end_date): ...
```

See `us_data_yfinance.py` or `hk_data_yfinance.py` for reference implementations.

参考 `us_data_yfinance.py` 或 `hk_data_yfinance.py` 的实现。
