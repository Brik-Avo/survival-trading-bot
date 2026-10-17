# Installation Guide

This guide takes you from zero to a running backtest and a live paper-trading dashboard in about 10 minutes.

> **Not financial advice.** Every bot in this repository runs in **paper trading (simulation)** by default. No real order is sent.

---

## 1. Requirements

| Requirement | Version | Check |
|---|---|---|
| Python | 3.11, 3.12 or 3.13 (3.14 not supported yet) | `python --version` |
| Git | any recent version | `git --version` |
| Internet access | Yahoo Finance, Google News, Reddit, Binance public API | — |
| Disk space | ~300 MB (virtual environment + market data) | — |

No API key is required to run the backtests, the paper trading or the dashboard.

---

## 2. Get the code

```bash
git clone https://github.com/Brik-Avo/survival-trading-bot.git
cd survival-trading-bot
```

Or download the ZIP from GitHub (**Code → Download ZIP**) and extract it.

---

## 3. Install

### Windows (one click)

Double-click **`install.bat`**. It:
1. finds Python 3.12, 3.11 or 3.13 on your machine,
2. creates a virtual environment in `.venv`,
3. installs the dependencies from `requirements.txt`.

<details>
<summary>Manual install on Windows (PowerShell)</summary>

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```
</details>

> **Corporate PC?** If `pip.exe` is blocked (Device Guard / AppLocker), always use `python -m pip ...` instead of `pip ...`.

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 4. Check the installation

```bash
pip install pytest
python -m pytest -q
```

Expected output:

```
4 passed
```

The tests run offline on synthetic prices. They check that the engine has no look-ahead bias, that it profits only when trends exist, and that the news scorer works.

---

## 5. Run the backtest (always first)

| | Windows | Linux / macOS |
|---|---|---|
| Slow trend bot (stocks, metals, FX, indices, crypto) | `backtest_tendance.bat` | `python bot/tendance_lente.py backtest` |
| World trend bot (world stocks + FX) | `backtest_monde.bat` | `python bot/tendance_monde.py backtest` |

The backtest downloads about 20 years of daily data (a few minutes the first time), tunes on 2005-2016, tests on 2017-today and ends with:

```
VERDICT : POSITIF   ->  the system won on years it had never seen
VERDICT : NÉGATIF   ->  do not use it, not even for long paper trading
```

The chosen settings are saved to `params_tendance.json` (or `params_monde.json`).

---

## 6. Start paper trading + dashboard

Only if the verdict is **POSITIVE**.

| | Windows | Linux / macOS |
|---|---|---|
| Slow trend bot | `paper_tendance.bat` | `python bot/tendance_lente.py paper` then `python dashboard/dashboard.py` |
| World trend bot | `paper_monde.bat` | `python bot/tendance_monde.py paper` then `python dashboard/dashboard.py` |

Open **http://localhost:8000** to see equity, open positions, closed trades and the news analysis.

Run it **once a day, after the US market close (22:30 Paris time / 4:30 pm New York)**. Each run replays the simulation from its start date, reads the news for any new signal and updates the dashboard.

> **Tip:** schedule it with Windows Task Scheduler (action: `paper_tendance.bat`, trigger: daily 22:45) or a cron job on Linux: `45 22 * * 1-5 cd /path/to/survival-trading-bot && .venv/bin/python bot/tendance_lente.py paper`

---

## 7. Configuration

All settings are at the top of each bot file. The main ones:

| Setting | File | Default | Meaning |
|---|---|---|---|
| `capital_initial` | `bot/tendance_lente.py` (`CFG`) | `10000` | Simulated starting capital ($) |
| `risque` | same | `0.005` | Loss if a stop is hit: 0.5 % of equity |
| `levier_total_max` | same | `5.0` | Sum of all positions ≤ 5× equity (6× in the world bot) |
| `frais`, `funding_jour` | same | `0.0005`, `0.0002` | Fees per order, daily financing cost |
| `seuil_sentiment` | same | `0.15` | How negative the news must be to skip a buy |
| `UNIVERS` | `bot/tendance_lente.py` | 44 markets | Yahoo Finance tickers traded |
| `ACTIONS`, `DEVISES` | `bot/tendance_monde.py` | 93 markets | World stocks and FX pairs |
| `PORT` | `.env` | `8000` | Dashboard port |

To add a market, add its Yahoo Finance ticker to the right list (examples: `AIR.PA` for Airbus, `7203.T` for Toyota, `EURUSD=X` for EUR/USD, `BTC-USD` for Bitcoin), then **run the backtest again**.

### Optional `.env`

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Only needed to change the dashboard port, or for the Binance trend bot in live mode (not recommended). If you ever add Binance keys: **futures only, withdrawal disabled, IP restricted**, and never commit `.env`.

---

## 8. Other bots (research only)

| Bot | Windows | Status |
|---|---|---|
| Scalper, up to 100 positions on Binance, 3-second cycle | `start.bat` | Simulation only. Lost ~34 % in one hour of paper trading |
| Binance 1h/4h trend bot | `backtest.bat`, `start_tendance.bat` | Negative out of sample (PF 0.91) |

They are kept to document what does not work. See [`CHANGELOG.md`](CHANGELOG.md).

---

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| `error: Microsoft Visual C++ 14.0 is required` / `cffi` build error | You are on Python 3.14. Install Python 3.12 and run `install.bat` again |
| `pip.exe` blocked by policy | Use `python -m pip install -r requirements.txt` |
| `0 marchés avec assez d'historique` | Internet / proxy blocks Yahoo Finance. Check that https://finance.yahoo.com opens in your browser |
| `possibly delisted; no timezone found` for one ticker | Yahoo no longer knows this ticker. Remove it from the list, the others still work |
| Dashboard empty or showing old positions | Close all bot windows, delete `bot_state_v2.json`, start again |
| `Address already in use` on port 8000 | Another dashboard is running. Close it or set `PORT=8001` in `.env` |
| Reddit returns nothing | Reddit limits anonymous requests. The bot continues with press headlines only |

---

## 10. Uninstall

Delete the project folder. Everything (virtual environment, data, state files) lives inside it.
