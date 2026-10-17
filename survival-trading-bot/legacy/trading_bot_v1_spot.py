"""
Bot de trading "survie" — capital initial 50 $, mort si le capital passe sous le seuil.
Mode PAPER par défaut (aucun argent réel). Données : Binance via ccxt (publiques).

pip install ccxt pandas
python trading_bot.py               # paper trading live
python trading_bot.py --backtest    # backtest sur l'historique
"""
import argparse, json, os, time, sys
from datetime import datetime
import ccxt
import pandas as pd

# ---------------- CONFIG ----------------
CONFIG = {
    "symbol": "BTC/USDT",
    "timeframe": "15m",
    "capital_initial": 50.0,
    "seuil_mort": 50.0,        # capital < seuil => le bot meurt (mets 45 pour tolérer -10 %)
    "risque_par_trade": 0.02,  # 2 % du capital risqué par trade
    "frais": 0.001,            # 0,1 % par ordre (Binance spot)
    "min_ordre": 5.0,          # montant minimum d'ordre en $
    "live": False,             # True = ordres réels (clés API requises) — à tes risques
    "etat_fichier": "bot_state.json",
}

# ---------------- INDICATEURS ----------------
def indicateurs(df):
    c = df["close"]
    df["ema50"] = c.ewm(span=50, adjust=False).mean()
    df["ema200"] = c.ewm(span=200, adjust=False).mean()
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    perte = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / perte)
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["bb_mid"] = c.rolling(20).mean()
    std = c.rolling(20).std()
    df["bb_haut"], df["bb_bas"] = df["bb_mid"] + 2 * std, df["bb_mid"] - 2 * std
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - c.shift()).abs(),
                    (df["low"] - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["vol_moy"] = df["volume"].rolling(20).mean()
    return df

def verifier(row, prev):
    """Chaque indicateur est vérifié individuellement. Achat seulement si TOUS valident."""
    checks = {
        "Tendance (EMA50 > EMA200)": row.ema50 > row.ema200,
        "Prix au-dessus EMA50": row.close > row.ema50,
        "RSI entre 45 et 65": 45 < row.rsi < 65,
        "MACD croise au-dessus du signal": prev.macd <= prev.macd_signal and row.macd > row.macd_signal,
        "Prix sous bande haute Bollinger": row.close < row.bb_haut,
        "Volume > moyenne 20": row.volume > row.vol_moy,
        "Volatilité ATR exploitable": row.atr / row.close > 0.002,
    }
    return all(checks.values()), checks

# ---------------- MOTEUR ----------------
class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ex = ccxt.binance({"enableRateLimit": True,
                                "apiKey": os.getenv("BINANCE_KEY"),
                                "secret": os.getenv("BINANCE_SECRET")})
        self.capital = cfg["capital_initial"]
        self.position = None   # dict: qty, entree, stop, cible
        self.vivant = True
        self.historique = []

    def log(self, msg):
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)

    def mourir(self, prix):
        self.vivant = False
        self.log(f"💀 LE BOT EST MORT. Capital {self.capital:.2f} $ < seuil {self.cfg['seuil_mort']} $ (prix {prix:.2f})")

    def ouvrir(self, row):
        risque_usd = self.capital * self.cfg["risque_par_trade"]
        stop = row.close - 1.5 * row.atr
        cible = row.close + 3 * row.atr          # ratio gain/risque 2:1
        qty = risque_usd / (row.close - stop)
        montant = min(qty * row.close, self.capital * 0.95)
        if montant < self.cfg["min_ordre"]:
            self.log("Montant trop faible, trade ignoré"); return
        qty = montant / row.close
        if self.cfg["live"]:
            self.ex.create_market_buy_order(self.cfg["symbol"], qty)
        self.capital -= montant * (1 + self.cfg["frais"])
        self.position = {"qty": qty, "entree": row.close, "stop": stop, "cible": cible, "cout": montant}
        self.log(f"🟢 ACHAT {qty:.6f} @ {row.close:.2f} | stop {stop:.2f} | cible {cible:.2f}")

    def fermer(self, prix, raison):
        p = self.position
        if self.cfg["live"]:
            self.ex.create_market_sell_order(self.cfg["symbol"], p["qty"])
        recette = p["qty"] * prix * (1 - self.cfg["frais"])
        self.capital += recette
        pnl = recette - p["cout"] * (1 + self.cfg["frais"])
        self.historique.append(pnl)
        self.log(f"🔴 VENTE @ {prix:.2f} ({raison}) | PnL {pnl:+.2f} $ | capital {self.capital:.2f} $")
        self.position = None

    def equite(self, prix):
        return self.capital + (self.position["qty"] * prix if self.position else 0)

    def pas(self, row, prev, verbose=True):
        if not self.vivant: return
        if self.position:
            if row.low <= self.position["stop"]:
                self.fermer(self.position["stop"], "stop-loss")
            elif row.high >= self.position["cible"]:
                self.fermer(self.position["cible"], "take-profit")
            elif row.macd < row.macd_signal and row.close < row.ema50:
                self.fermer(row.close, "retournement indicateurs")
        else:
            ok, checks = verifier(row, prev)
            if verbose:
                for nom, v in checks.items():
                    self.log(f"   {'✅' if v else '❌'} {nom}")
            if ok:
                self.ouvrir(row)
        if self.position is None and self.equite(row.close) < self.cfg["seuil_mort"]:
            self.mourir(row.close)

    def donnees(self, limit=500):
        o = self.ex.fetch_ohlcv(self.cfg["symbol"], self.cfg["timeframe"], limit=limit)
        df = pd.DataFrame(o, columns=["ts", "open", "high", "low", "close", "volume"])
        return indicateurs(df).dropna()

    def sauver(self):
        with open(self.cfg["etat_fichier"], "w") as f:
            json.dump({"capital": self.capital, "position": self.position,
                       "vivant": self.vivant, "historique": self.historique}, f)

    def resume(self):
        g = [x for x in self.historique if x > 0]
        self.log(f"Trades: {len(self.historique)} | gagnants: {len(g)} | "
                 f"capital final: {self.capital:.2f} $ | {'vivant' if self.vivant else 'MORT'}")

    # ---- modes ----
    def backtest(self):
        df = self.donnees(1000)
        for i in range(1, len(df)):
            self.pas(df.iloc[i], df.iloc[i-1], verbose=False)
            if not self.vivant: break
        if self.position: self.fermer(df.iloc[-1].close, "fin du backtest")
        self.resume()

    def live_loop(self):
        self.log(f"Démarrage {'LIVE ⚠️' if self.cfg['live'] else 'PAPER'} — capital {self.capital} $")
        while self.vivant:
            try:
                df = self.donnees()
                self.log(f"Prix {df.iloc[-1].close:.2f} | équité {self.equite(df.iloc[-1].close):.2f} $")
                self.pas(df.iloc[-2], df.iloc[-3])  # bougie clôturée uniquement
                self.sauver()
            except Exception as e:
                self.log(f"Erreur: {e}")
            time.sleep(60)
        self.resume()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()
    bot = Bot(CONFIG)
    bot.backtest() if a.backtest else bot.live_loop()
