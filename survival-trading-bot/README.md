# Survival Trading Bot

> Open-source, experimental **trend-following** bot for Binance USDT-M perpetuals (crypto, and stock / gold / index / FX perpetuals when Binance lists them).

> ⚠️ **This is NOT financial advice.** Experimental project, still being improved. Leveraged trading can wipe out your capital. Always run the backtest and paper mode for weeks before risking real money.

## Why trend following

Earlier versions scalped 1-minute candles with high leverage. Live paper tests showed it does not work: on 1-minute data, fees and noise eat any edge, and "measured edges" were mostly statistical noise (one session: 62 trades, 18 % winners, -$776). Those versions are kept in `legacy/` for reference.

v2.0 uses a classic, well-documented approach used by CTA funds: trade **with** the higher-timeframe trend, enter on **breakouts**, cut losers fast and let winners run. Expect a win rate around 35-50 % with wins much larger than losses. It only makes money when markets actually trend; in choppy markets it loses small amounts.

## How it works

| Step | Rule |
|---|---|
| Direction | 4h trend: EMA50 above EMA200 and price above EMA200 = long only (mirror for short) |
| Entry | 1h close breaks the highest high (long) / lowest low (short) of the previous 20 hours, with volume > 1.2x average |
| Size | **Risk 0.5 % of equity per trade**: size = risk / stop distance. Leverage (max x5) only fits the position into the margin |
| Stop | Initial stop at 2 ATR(1h) |
| Exit | Chandelier trailing stop 3 ATR behind the best price, or 4h trend reversal |
| Portfolio | Max 6 positions, max 4 in the same direction (avoids one big correlated bet) |
| Markets | All liquid USDT perpetuals (volume filter per category), pre-market excluded |

## Quick start

```bash
pip install -r requirements.txt
python bot/trading_bot.py --backtest   # ALWAYS first: 12 months of real 1h data, 60 markets, fees + slippage
python bot/trading_bot.py              # paper trading
python dashboard/dashboard.py          # http://localhost:8000
```
Windows: `install.bat`, then `backtest.bat`, then `start.bat`.

The backtest downloads 12 months of history, **tunes** a small parameter grid (breakout length, trailing stop, initial stop) on the first 8 months, then **tests** the chosen settings on the last 4 months, which were never seen during tuning (walk-forward / out-of-sample validation). It prints trades, win rate, average win/loss, **profit factor** (> 1.3 interesting, < 1 losing), max drawdown and return for each phase, plus a verdict. If the out-of-sample profit factor is ≥ 1, the settings are saved to `params_optimises.json` and used by the live bot. **A negative out-of-sample result means: do not run it.** A positive one guarantees nothing.

## Settings (`CONFIG` at the top of `bot/trading_bot.py`)

| Key | Default | Meaning |
|---|---|---|
| `risque_pct` | `0.005` | Equity risked per trade (0.5 %) |
| `donchian` | `20` | Breakout lookback (1h candles) |
| `stop_atr` / `trailing_atr` | `2.0` / `3.0` | Initial stop / trailing stop distance in ATR |
| `max_positions` / `max_meme_sens` | `6` / `4` | Portfolio limits |
| `levier_max` | `5` | Max leverage (used only to fit positions in margin) |
| `capital_initial` / `seuil_mort` | `10000` / `10` | Paper capital / the bot dies at $10 |
| `live` | `False` | `True` sends real orders (isolated margin, exchange-side stop orders) |

## Project structure

```
bot/trading_bot.py        Trend-following bot (v2.0) + backtest
dashboard/dashboard.py    Real-time web dashboard
legacy/                   Previous scalping versions (kept for reference, not recommended)
install.bat / start.bat / backtest.bat   Windows helpers
```

## License

MIT


## Slow trend bot (stocks, gold, forex) — `bot/tendance_lente.py`

Daily trend following on 20 US stocks, gold/silver/copper, 7 major FX pairs, 2 indices and 12 major cryptos.
Few trades held for days or weeks, total leverage capped at 5x, fees and funding included.

1. `backtest_tendance.bat` downloads ~20 years of daily data (Yahoo Finance), tunes on 2005-2016
   and tests on 2017-today (out of sample). It prints a POSITIVE / NEGATIVE verdict.
2. `paper_tendance.bat` runs the paper simulation with the validated settings and updates the dashboard.
   Run it once a day after the US close. Before every NEW position it reads Google News, Reddit and
   Yahoo Finance headlines (plus the crypto Fear & Greed index), scores them with VADER and skips the
   trade when the news clearly go against it. X/Twitter is not included (paid API).

Simulation only. Not financial advice.

## World stocks + FX bot — `bot/tendance_monde.py`

Same engine as the slow trend bot, restricted to 70 large stocks (US, Europe, Asia, Americas) and
24 currency pairs (majors, crosses, emerging), total leverage up to 6x.
`backtest_monde.bat` first, then `paper_monde.bat` once a day if the verdict is POSITIVE.
These markets trade at CFD / FX brokers, not on Binance. Simulation only.
