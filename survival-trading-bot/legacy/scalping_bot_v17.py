"""
Bot de trading "survie" v15 — tout est calculé : taille par Kelly, levier/stop/zone optimisés pour la croissance du capital — Futures USDT-M, levier jusqu'à x100, scan de TOUTES les paires,
analyse approfondie (1h/4h, funding, open interest, carnet d'ordres) + sentiment X/web.
Capital initial 1000 $, 5 positions de 200 $ (marge isolée), SANS stop-loss :
chaque position reste ouverte jusqu'à son objectif (+50 %) ou la perte de ses 200 $.
Avant chaque mise, le bot teste chaque levier : probabilité estimée, durée estimée, frais, espérance.
Le bot meurt quand son équité tombe à 10 $.
Backtest : technique uniquement, confiance fixée à 50 % (pas d'historique carnet/funding).
Mode PAPER par défaut.

pip install ccxt pandas requests
Variables d'environnement (optionnelles) :
  BINANCE_KEY / BINANCE_SECRET   -> ordres réels (live)
  X_BEARER_TOKEN                 -> news/sentiment X (API X payante, plan Basic minimum)
  CRYPTOPANIC_TOKEN              -> news crypto agrégées (gratuit)

python bot/trading_bot.py --backtest
python bot/trading_bot.py
"""
import argparse, json, os, threading, time
from collections import deque
from datetime import datetime

LOGS = deque(maxlen=300)   # journal lu par le dashboard
import ccxt, requests
import numpy as np
import pandas as pd

CONFIG = {
    "mode": "agressif",          # <<< UN SEUL RÉGLAGE : "prudent", "equilibre" ou "agressif" (voir MODES plus bas)
    "timeframe": "1m",           # SCALPING : bougies 1 minute
    "capital_initial": 10000.0,
    "seuil_mort": 10.0,          # équité <= 10 $ => toutes les positions sont fermées et le bot meurt
    # --- MINI-TRADES ---
    "mise_pct": 0.01,            # mise plancher = 1 % de l'équité (100 $ au départ)
    # --- TAILLE ET RISQUE : 100 % CALCULÉS (pas de plafond arbitraire) ---
    "fraction_kelly": 0.25,      # quart de Kelly (standard pro quand l'avantage est incertain)
    "z_prudence": 1.0,
    "liquidite_max": 0.05,       # position <= 5 % de la valeur échangée sur ce marché en 10 min (sinon glissement)
    "correlation": 0.7,          # corrélation moyenne entre marchés de même catégorie (crypto entre eux...)           # avantage utilisé = mesuré - 2 erreurs-types (95 % de confiance qu'il est au moins là)       # demi-Kelly : la taille "optimale" calculée, divisée par 2 (l'avantage mesuré est imprécis)
    "stops_atr": [1.5, 2.5, 4.0],  # distances de stop testées (en ATR 1m) ; le calcul choisit
    "zone_x_risque": [1.0, 5.0], # zone d'encaissement = 1 à 5 fois le risque pris
    "marge_par_trade": 100.0,
    "max_positions": 1000,       # pas de limite : seul le capital disponible limite
    "exposition_max": 1.0,
    "leviers_testes": [10, 15, 20, 25, 30, 40, 50],
    "levier_max": 50,
    # --- SORTIES GÉRÉES PAR LE BOT (pas d'objectif fixe) ---
    # --- ZONE DE GAIN : le bot encaisse entre +20 $ et +100 $, au meilleur moment ---
    "zone_gain_min": 20.0,       # sous +20 $ on n'encaisse pas (pas rentable après frais/risque)
    "zone_gain_max": 100.0,      # à +100 $ on encaisse toujours
    "garde_pct": 0.70,           # dans la zone : on garde au moins 70 % du meilleur gain atteint (jamais < 20 $)
    "signaux_encaisse_zone": 2,  # dans la zone, 2 signaux de faiblesse suffisent pour encaisser
    "rsi_epuisement": 78,        # RSI(7) > 78 (long) / < 22 (short) dans la zone = mouvement essoufflé -> on encaisse
    "signaux_retournement_min": 3,  # hors zone : sortie seulement si >= 3 signaux sur 6 dont cassure de structure
    # --- DISCIPLINE DE RISQUE ---
    "perte_max_usd": 20.0,       # STOP DUR : une perte ne dépasse jamais -20 $ (= le plus petit gain visé)
    "perte_max_pct": 0.60,       # (sécurité secondaire si la mise est petite : -60 % de la mise)
    "coupe_perte_usd": 12.0,     # retournement confirmé ET perte >= 12 $ -> on coupe avant le stop
    "cooldown_perte_min": 45,    # après une perte sur un marché : interdit d'y revenir pendant 45 min
    "pertes_consecutives_max": 4,  # 4 pertes d'affilée -> pause des nouvelles entrées...
    "pause_min": 20,             # ... pendant 20 min
    "filtre_tendance_btc": True, # pas de short crypto si BTC monte (15m), pas de long si BTC baisse
    "anciennete_min_jours": 30,  # marchés listés depuis moins de 30 jours exclus (trop nerveux)
    "duree_vie_max_min": 30,     # filet de sécurité : 30 min maximum
    "simulations": 2000,         # trajectoires simulées par levier (avec ces règles de sortie)
    # --- AVANTAGE MESURÉ ---
    "historique_mesure": 900,    # bougies 1m utilisées pour mesurer l'avantage (~15 h)
    "echantillons_min": 30,
    "retrecissement": 50,
    "proba_liq_max": 0.03,
    "esperance_min": 0.10,       # espérance minimale en $ par mini-trade (après frais)
    "score_min_15m": 5,
    "atr_min": 0.0002,           # volatilité minimale (bas pour inclure actions/or/devises ; les frais sont vérifiés au calcul)
    "confiance_min": 0.6,
    "mmr": 0.005,
    "frais": 0.0005,
    "min_notionnel": 5.0,
    # --- MARCHÉS & VITESSE ---
    "top_paires": None,          # TOUS les perpétuels USDT : crypto, actions, or/argent, indices, devises
    "volume_min_24h": 20_000_000,   # crypto : marchés liquides uniquement
    "volume_min_par_type": {     # seuil de volume adapté à chaque catégorie
        "crypto": 20_000_000, "actions": 500_000, "matières premières": 500_000,
        "indices": 500_000, "devises": 500_000},
    "analyse_approfondie_top": 40,
    "tf_approfondie": ["5m", "15m"],
    "poids_sentiment": 0.15,
    "surveillance_s": 2,         # chaque position relue toutes les 2 s (bougies 1m incluses)
    "file_validite_s": 180,      # un candidat pré-calculé reste utilisable 3 min
    "tendance_obligatoire": ["5m", "15m"],   # unités de temps qui doivent aller dans le sens du trade
    "live": False,
    "etat_fichier": "bot_state_v2.json",
}

# Niveaux de risque : chaque mode remplace les réglages ci-dessous. Tout le reste du calcul est identique.
MODES = {
    "prudent": dict(z_prudence=2.0, fraction_kelly=0.25, confiance_min=0.65, filtre_tendance_btc=True,
                    tendance_obligatoire=["5m", "15m"], cooldown_perte_min=45, score_min_15m=5, esperance_min=0.5),
    "equilibre": dict(z_prudence=1.0, fraction_kelly=0.25, confiance_min=0.6, filtre_tendance_btc=True,
                      tendance_obligatoire=["15m"], cooldown_perte_min=30, score_min_15m=5, esperance_min=0.1),
    # agressif : mise sur l'avantage MESURÉ tel quel (s'il est positif), demi-Kelly, longs ET shorts quel que
    # soit BTC, une seule unité de temps doit confirmer, moins de signaux requis, revient vite sur un marché.
    "agressif": dict(z_prudence=0.0, fraction_kelly=0.5, confiance_min=0.5, filtre_tendance_btc=False,
                     tendance_obligatoire=["5m"], cooldown_perte_min=10, score_min_15m=4, esperance_min=0.05,
                     pertes_consecutives_max=6, pause_min=10),
}
CONFIG.update(MODES.get(CONFIG["mode"], {}))

MOTS_POS = ["surge", "rally", "bullish", "etf approved", "partnership", "listing", "breakout",
            "adoption", "record high", "pump", "upgrade", "hausse", "approval"]
MOTS_NEG = ["hack", "exploit", "lawsuit", "sec sues", "ban", "crash", "bearish", "delist",
            "scam", "rug", "liquidation", "fraud", "baisse", "investigation", "outage"]

def log(msg):
    ligne = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    LOGS.append(ligne)
    print(ligne, flush=True)

# ================= INDICATEURS =================
def indicateurs(df):
    c = df["close"]
    df["ema50"] = c.ewm(span=50, adjust=False).mean()
    df["ema200"] = c.ewm(span=200, adjust=False).mean()
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    p = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + g / p)
    df["macd"] = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    m, s = c.rolling(20).mean(), c.rolling(20).std()
    df["bb_haut"], df["bb_bas"] = m + 2 * s, m - 2 * s
    tr = pd.concat([df.high - df.low, (df.high - c.shift()).abs(), (df.low - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["vol_moy"] = df["volume"].rolling(20).mean()
    return df

def verifier(row, prev, sens):
    """Vérifie chaque indicateur pour un LONG ou un SHORT.
    Signal si la tendance (EMA50/EMA200) est dans le bon sens ET au moins score_min_15m indicateurs sur 7 valident."""
    if sens == "long":
        c = {
            "EMA50 > EMA200": row.ema50 > row.ema200,
            "Prix > EMA50": row.close > row.ema50,
            "RSI 45-65": 45 < row.rsi < 65,
            "MACD au-dessus du signal": row.macd > row.macd_signal,
            "Prix < Bollinger haute": row.close < row.bb_haut,
        }
    else:
        c = {
            "EMA50 < EMA200": row.ema50 < row.ema200,
            "Prix < EMA50": row.close < row.ema50,
            "RSI 35-55": 35 < row.rsi < 55,
            "MACD sous le signal": row.macd < row.macd_signal,
            "Prix > Bollinger basse": row.close > row.bb_bas,
        }
    c["Volume > moyenne"] = row.volume > row.vol_moy
    c["ATR exploitable"] = row.atr / row.close > CONFIG["atr_min"]
    tendance_ok = list(c.values())[0]
    return bool(tendance_ok and sum(c.values()) >= CONFIG["score_min_15m"]), c

def avantage_mesure(df, sens, horizon, cfg):
    """Mesure RÉELLE : rendement moyen du prix `horizon` bougies après chaque bougie où le même signal
    était présent, dans l'historique de la paire. Renvoie (dérive rétrécie, nb d'échantillons, taux de hausse)."""
    s = 1 if sens == "long" else -1
    horizon = max(1, int(round(horizon)))
    c = df.close
    if sens == "long":
        conds = [df.ema50 > df.ema200, c > df.ema50, (df.rsi > 45) & (df.rsi < 65), df.macd > df.macd_signal, c < df.bb_haut]
    else:
        conds = [df.ema50 < df.ema200, c < df.ema50, (df.rsi > 35) & (df.rsi < 55), df.macd < df.macd_signal, c > df.bb_bas]
    conds += [df.volume > df.vol_moy, df.atr / c > cfg["atr_min"]]
    nb = sum(x.astype(int) for x in conds)
    signal = conds[0] & (nb >= cfg["score_min_15m"])
    fut = (c.shift(-horizon) / c - 1) * s
    r = fut[signal].dropna()
    n = len(r)
    if n < cfg["echantillons_min"]:
        return 0.0, n, None, 1.0
    n_eff = max(1.0, n / horizon)                       # échantillons qui se chevauchent : n effectif réduit
    se = float(r.std()) / np.sqrt(n_eff)                # marge d'erreur statistique de l'avantage
    return float(r.mean()) * n / (n + cfg["retrecissement"]), n, float((r > 0).mean()), se

def retournement(o, sens):
    """Analyse les bougies 1m CLÔTURÉES d'une position (la bougie en cours est ignorée).
    6 signaux de retournement contre la position ; renvoie (nb_signaux, liste, structure_cassée, atr_relatif).
    Un vrai retournement = au moins 3 signaux DONT une cassure de structure (plus bas/haut ou EMA50)."""
    o = o[:-1]                                         # on ne juge que des bougies terminées
    if len(o) < 52:
        return 0, [], False, None, None
    c = pd.Series([x[4] for x in o]); h = pd.Series([x[2] for x in o]); l = pd.Series([x[3] for x in o])
    e9, e21, e50 = (c.ewm(span=k, adjust=False).mean() for k in (9, 21, 50))
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean(); sigl = macd.ewm(span=9, adjust=False).mean()
    d = c.diff(); g = d.clip(lower=0).ewm(alpha=1/7, adjust=False).mean(); pe = (-d.clip(upper=0)).ewm(alpha=1/7, adjust=False).mean()
    rsi = 100 - 100 / (1 + g / pe)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1] / c.iloc[-1])
    if sens == "long":
        sig = {"EMA9 sous EMA21": e9.iloc[-1] < e21.iloc[-1] and (e9.iloc[-4:-1] >= e21.iloc[-4:-1]).any(),
               "clôture sous EMA21": c.iloc[-1] < e21.iloc[-1],
               "MACD sous son signal": macd.iloc[-1] < sigl.iloc[-1],
               "RSI retombé (<45 après >60)": rsi.iloc[-1] < 45 and rsi.iloc[-10:].max() > 60,
               "plus bas des 5 bougies cassé": c.iloc[-1] < l.iloc[-6:-1].min(),
               "tendance EMA50 cassée": c.iloc[-1] < e50.iloc[-1]}
    else:
        sig = {"EMA9 au-dessus EMA21": e9.iloc[-1] > e21.iloc[-1] and (e9.iloc[-4:-1] <= e21.iloc[-4:-1]).any(),
               "clôture au-dessus EMA21": c.iloc[-1] > e21.iloc[-1],
               "MACD au-dessus de son signal": macd.iloc[-1] > sigl.iloc[-1],
               "RSI remonté (>55 après <40)": rsi.iloc[-1] > 55 and rsi.iloc[-10:].min() < 40,
               "plus haut des 5 bougies cassé": c.iloc[-1] > h.iloc[-6:-1].max(),
               "tendance EMA50 cassée": c.iloc[-1] > e50.iloc[-1]}
    actifs = [k for k, v in sig.items() if v]
    structure = any(k.startswith(("plus ", "tendance")) for k in actifs)
    return len(actifs), actifs, structure, atr, float(rsi.iloc[-1])

def score_technique(df):
    """Score de rentabilité potentielle : force de tendance x volatilité x momentum."""
    r = df.iloc[-1]
    tendance = (r.ema50 - r.ema200) / r.ema200
    momentum = df.close.pct_change(16).iloc[-1]          # ~4h
    volat = r.atr / r.close
    return tendance * 50 + momentum * 20 + volat * 30

# ================= SENTIMENT =================
class Sentiment:
    def __init__(self):
        self.cache, self.t_cache = {}, {}

    def _score_textes(self, textes):
        s = 0
        for t in textes:
            t = t.lower()
            s += sum(m in t for m in MOTS_POS) - sum(m in t for m in MOTS_NEG)
        return max(-1, min(1, s / max(len(textes), 1)))

    def x_news(self, coin):
        tok = os.getenv("X_BEARER_TOKEN")
        if not tok: return None
        try:
            r = requests.get("https://api.twitter.com/2/tweets/search/recent",
                             headers={"Authorization": f"Bearer {tok}"},
                             params={"query": f"(${coin} OR #{coin}) lang:en -is:retweet", "max_results": 50},
                             timeout=10)
            return [t["text"] for t in r.json().get("data", [])]
        except Exception as e:
            log(f"X indisponible: {e}"); return None

    def cryptopanic(self, coin):
        tok = os.getenv("CRYPTOPANIC_TOKEN")
        if not tok: return None
        try:
            r = requests.get("https://cryptopanic.com/api/v1/posts/",
                             params={"auth_token": tok, "currencies": coin, "kind": "news"}, timeout=10)
            return [p["title"] for p in r.json().get("results", [])]
        except Exception:
            return None

    def fear_greed(self):
        """Indice Fear & Greed global (0-100) -> [-1, 1]."""
        try:
            v = int(requests.get("https://api.alternative.me/fng/", timeout=10).json()["data"][0]["value"])
            return (v - 50) / 50
        except Exception:
            return 0

    def tendances_web(self):
        """Coins en tendance sur CoinGecko (recherches web)."""
        try:
            j = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10).json()
            return {c["item"]["symbol"].upper() for c in j.get("coins", [])}
        except Exception:
            return set()

    def score(self, coin, trending, fg):
        if coin in self.cache and time.time() - self.t_cache[coin] < 900:
            return self.cache[coin]
        textes = (self.x_news(coin) or []) + (self.cryptopanic(coin) or [])
        s = self._score_textes(textes) if textes else 0
        s = 0.6 * s + 0.2 * fg + (0.2 if coin in trending else 0)
        self.cache[coin], self.t_cache[coin] = s, time.time()
        return s

# ================= BOT =================
class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ex = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 15000,
                                    "apiKey": os.getenv("BINANCE_KEY") or None,
                                    "secret": os.getenv("BINANCE_SECRET") or None})
        # 2e connexion dédiée à la surveillance : les positions restent suivies pendant les recherches
        self.ex_surv = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 10000,
                                         "apiKey": os.getenv("BINANCE_KEY") or None,
                                         "secret": os.getenv("BINANCE_SECRET") or None})
        self.ex_ord = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 10000,
                                        "apiKey": os.getenv("BINANCE_KEY") or None,
                                        "secret": os.getenv("BINANCE_SECRET") or None})
        self.verrou = threading.RLock()
        self.file = []                                  # candidats pré-calculés, prêts à être ouverts
        self.t_file = 0
        self.sent = Sentiment()
        self.capital = cfg["capital_initial"]
        self.positions = {}
        self.vivant = True
        self.historique = []
        self.trades, self.analyse = [], {}
        self.relancer = True
        self.cooldown = {}                              # marché -> interdit jusqu'à (après une perte)
        self.pertes_suite, self.pause_jusqua = 0, 0
        self.courbe = [(time.time(), self.capital)]
        log("Démarrage du bot — connexion à Binance Futures...")
        self.sauver()
        self.connexion()

    def connexion(self):
        """Teste l'accès à Binance avec un message clair en cas d'échec."""
        for essai in range(1, 4):
            try:
                t0 = time.time()
                self.ex.fetch_time()
                log(f"✔ Binance joignable ({(time.time()-t0)*1000:.0f} ms). Chargement des marchés...")
                self.ex.load_markets()
                for e in (self.ex_surv, self.ex_ord):
                    e.markets, e.markets_by_id, e.symbols, e.ids = self.ex.markets, self.ex.markets_by_id, self.ex.symbols, self.ex.ids
                types = {}
                for m in self.ex.markets.values():
                    if m.get("swap") and m.get("quote") == "USDT":
                        t = self.type_marche(m["symbol"]); types[t] = types.get(t, 0) + 1
                log(f"✔ {len(self.ex.markets)} marchés chargés. Perpétuels USDT par catégorie : "
                    + ", ".join(f"{k} {v}" for k, v in sorted(types.items(), key=lambda x: -x[1])))
                return
            except Exception as e:
                log(f"✖ Connexion Binance impossible (essai {essai}/3) : {type(e).__name__}: {str(e)[:200]}")
                time.sleep(3)
        log("ARRÊT : Binance Futures (fapi.binance.com) est inaccessible depuis ce réseau. "
            "Causes fréquentes : réseau d'entreprise/proxy, pare-feu, VPN, ou restriction géographique.")
        self.sauver()
        raise SystemExit(1)

    def type_marche(self, sym):
        """Catégorie du sous-jacent selon Binance (crypto, or, action, indice...)."""
        try:
            info = self.ex.market(sym).get("info", {}) or {}
            t = (info.get("underlyingType") or "").upper()
            sub = [x.upper() for x in (info.get("underlyingSubType") or [])]
            if t in ("COMMODITY",) or any("GOLD" in x or "METAL" in x for x in sub): return "matières premières"
            if t in ("EQUITY", "STOCK") or any("STOCK" in x or "EQUITY" in x for x in sub): return "actions"
            if t == "INDEX": return "indices"
            base = sym.split("/")[0]
            if base in ("EUR", "GBP", "JPY", "AUD", "CHF", "CAD", "NZD", "CNH", "TRY", "BRL", "MXN", "ZAR", "SGD", "HKD", "KRW", "INR"):
                return "devises"
            if t == "PREMARKET": return "pré-marché"
            return "crypto"
        except Exception:
            return "crypto"

    # ---- sélection de la paire ----
    def marche_sain(self, sym):
        """Exclut les marchés à risque d'exécution : pré-marché, listés récemment, noms non standards,
        et ceux sous interdiction temporaire après une perte."""
        if not sym.split("/")[0].isascii() or not sym.split("/")[0].replace("1000", "").isalnum():
            return False
        if self.cooldown.get(sym, 0) > time.time():
            return False
        try:
            info = self.ex.market(sym).get("info", {}) or {}
            if (info.get("underlyingType") or "").upper() == "PREMARKET": return False
            if (info.get("contractType") or "PERPETUAL").upper() != "PERPETUAL": return False
            onboard = float(info.get("onboardDate") or 0) / 1000
            if (onboard and time.time() - onboard < self.cfg["anciennete_min_jours"] * 86400
                    and self.type_marche(sym) == "crypto"):
                return False                            # l'ancienneté ne compte que pour les cryptos (tokens récents)
        except Exception:
            pass
        return True

    def regime_btc(self):
        """Tendance de fond du marché crypto (BTC 15m) : 'haussier', 'baissier' ou 'neutre'."""
        try:
            o = self.ex.fetch_ohlcv("BTC/USDT:USDT", "15m", limit=250)
            c = pd.Series([x[4] for x in o])
            e50, e200 = c.ewm(span=50, adjust=False).mean(), c.ewm(span=200, adjust=False).mean()
            pente = e50.iloc[-1] / e50.iloc[-5] - 1
            if e50.iloc[-1] > e200.iloc[-1] and pente > 0: return "haussier"
            if e50.iloc[-1] < e200.iloc[-1] and pente < 0: return "baissier"
        except Exception:
            pass
        return "neutre"

    def paires(self):
        log("Récupération des volumes 24h...")
        t = self.ex.fetch_tickers()
        seuils = self.cfg["volume_min_par_type"]
        ok = [(s, v["quoteVolume"] or 0) for s, v in t.items()
              if s.endswith("/USDT:USDT") and (v["quoteVolume"] or 0) > seuils.get(self.type_marche(s), self.cfg["volume_min_24h"])
              and self.marche_sain(s)]
        cats = {}
        for s_, _ in ok: cats[self.type_marche(s_)] = cats.get(self.type_marche(s_), 0) + 1
        log("Marchés retenus : " + ", ".join(f"{k} {v}" for k, v in sorted(cats.items(), key=lambda x: -x[1])))
        return [s for s, _ in sorted(ok, key=lambda x: -x[1])[: self.cfg["top_paires"]]]

    def ohlcv(self, sym, limit=300):
        o = self.ex.fetch_ohlcv(sym, self.cfg["timeframe"], limit=limit)
        df = pd.DataFrame(o, columns=["ts", "open", "high", "low", "close", "volume"])
        return indicateurs(df).dropna()

    def analyse_approfondie(self, sym, sens):
        """Contrôles supplémentaires : unités de temps supérieures, funding, open interest, carnet d'ordres."""
        s = 1 if sens == "long" else -1
        c = {}
        for tf in self.cfg["tf_approfondie"]:
            try:
                o = self.ex.fetch_ohlcv(sym, tf, limit=250)
                d = indicateurs(pd.DataFrame(o, columns=["ts", "open", "high", "low", "close", "volume"])).dropna()
                r = d.iloc[-2]
                c[f"Tendance {tf} alignée"] = bool((r.ema50 - r.ema200) * s > 0)
                c[f"MACD {tf} aligné"] = bool((r.macd - r.macd_signal) * s > 0)
                c[f"RSI {tf} pas extrême"] = bool(25 < r.rsi < 75)
            except Exception as e:
                log(f"      {sym} {tf}: {e}")
        try:
            f = self.ex.fetch_funding_rate(sym).get("fundingRate") or 0
            # funding trop élevé dans notre sens = marché déjà surchargé de positions identiques
            c["Funding non surchargé"] = bool(f * s < 0.0005)
        except Exception:
            pass
        try:
            hist = self.ex.fetch_open_interest_history(sym, "1h", limit=6)
            oi = [h.get("openInterestAmount") or h.get("openInterestValue") or 0 for h in hist]
            c["Open interest en hausse"] = bool(len(oi) >= 2 and oi[-1] > oi[0])
        except Exception:
            pass
        try:
            ob = self.ex.fetch_order_book(sym, limit=50)
            bids = sum(q for _, q in ob["bids"][:50]); asks = sum(q for _, q in ob["asks"][:50])
            desequilibre = (bids - asks) / (bids + asks + 1e-12)
            c["Carnet d'ordres favorable"] = bool(desequilibre * s > 0.05)
            bid, ask = ob["bids"][0][0], ob["asks"][0][0]
            c["Spread serré (<0,05 %)"] = bool((ask - bid) / bid < 0.0005)
        except Exception:
            pass
        confiance = sum(c.values()) / len(c) if c else 0
        return confiance, c

    def candidats(self):
        """Scan de toutes les paires, puis analyse approfondie des meilleurs signaux."""
        fg, trending = self.sent.fear_greed(), self.sent.tendances_web()
        log(f"Fear&Greed {fg:+.2f} | tendances web: {', '.join(sorted(trending)) or '-'}")
        with self.verrou:
            deja = {p["sym"].split("/")[0] for p in self.positions.values()}
        candidats = []
        regime = self.regime_btc() if self.cfg["filtre_tendance_btc"] else "neutre"
        liste = self.paires()
        log(f"Analyse de {len(liste)} marchés sains | tendance BTC 15m : {regime}")
        for k, sym in enumerate(liste, 1):
            if k % 25 == 0: log(f"   ... {k}/{len(liste)}")
            if sym.split("/")[0] in deja: continue
            try:
                df = self.ohlcv(sym, self.cfg["historique_mesure"])
                row, prev = df.iloc[-2], df.iloc[-3]
                for sens in ("long", "short"):
                    if self.type_marche(sym) == "crypto" and ((regime == "haussier" and sens == "short") or
                                                              (regime == "baissier" and sens == "long")):
                        continue                                # on ne trade pas contre le marché
                    ok, checks = verifier(row, prev, sens)
                    if not ok: continue
                    coin = sym.split("/")[0]
                    tech = score_technique(df) * (1 if sens == "long" else -1)
                    sent = self.sent.score(coin, trending, fg) * (1 if sens == "long" else -1)
                    if sent < -0.2:
                        log(f"   ⛔ {sym} {sens} bloqué par le sentiment ({sent:+.2f})"); continue
                    total = (1 - self.cfg["poids_sentiment"]) * tech + self.cfg["poids_sentiment"] * sent
                    edge, n_ech, taux, se = avantage_mesure(df.iloc[:-1], sens, self.cfg["duree_vie_max_min"], self.cfg)
                    candidats.append(dict(score=total, sym=sym, sens=sens, row=row, checks=checks, sent=sent,
                                          edge=edge, n_ech=n_ech, taux=taux, se=se,
                                          prix=float(df.iloc[-1].close)))   # entrée au prix actuel
            except Exception as e:
                log(f"   {sym}: {e}")
        candidats.sort(key=lambda x: -x["score"])
        top = candidats[: self.cfg["analyse_approfondie_top"]]
        log(f"{len(candidats)} signal(aux) technique(s). Analyse approfondie des {len(top)} meilleurs...")
        for c in top:
            deep_conf, c["deep"] = self.analyse_approfondie(c["sym"], c["sens"])
            frac15 = sum(c["checks"].values()) / len(c["checks"])
            c["conf"] = 0.5 * deep_conf + 0.5 * frac15
            contraires = [tf for tf in self.cfg["tendance_obligatoire"] if not c["deep"].get(f"Tendance {tf} alignée", True)]
            if contraires:
                c["plan"] = {"ok": False, "raison": f"tendance {'/'.join(contraires)} contraire"}
            else:
                c["plan"] = self.planifier(c)
        top.sort(key=lambda c: -(c["plan"]["esperance"] if c["plan"]["ok"] else -1e9))
        refus = {}
        for c in top:
            if not c["plan"]["ok"]:
                cle = c["plan"]["raison"].split("(")[0].split(":")[0].strip()[:60]
                refus[cle] = refus.get(cle, 0) + 1
        if refus:
            log("   Refus : " + " | ".join(f"{v}x {k}" for k, v in sorted(refus.items(), key=lambda x: -x[1])))
        self.analyse = {"heure": datetime.now().isoformat(timespec="seconds"),
                        "fear_greed": fg, "tendances": sorted(trending), "nb_paires": len(liste),
                        "candidats": [{"paire": c["sym"], "sens": c["sens"], "score": round(c["score"], 4),
                                       "sentiment": round(c["sent"], 2), "confiance": round(c["conf"], 2),
                                       "levier": c["plan"].get("lev"), "esperance": c["plan"].get("esperance"),
                                       "decision": "OK" if c["plan"]["ok"] else c["plan"]["raison"]} for c in top],
                        "checks": {}}
        if top:
            b = top[0]
            ch = {k: bool(v) for k, v in b["checks"].items()}
            ch.update({f"[Approfondi] {k}": bool(v) for k, v in b["deep"].items()})
            self.analyse["checks"] = ch
            self.analyse["checks_paire"] = f'{b["sym"]} {b["sens"]}'
        return [c for c in top if c["plan"]["ok"]]

    # ---- calcul avant trade ----
    def marge_disponible(self):
        eq = self.equite()
        engagee = sum(p["marge"] for p in self.positions.values())
        plafond = eq * self.cfg["exposition_max"] - engagee
        mise = eq * self.cfg.get("mise_pct", 0) or self.cfg["marge_par_trade"]   # réinvestit les gains
        return max(0.0, min(plafond, self.capital))     # capital libre (la taille est décidée par la conviction)

    def conviction(self, c):
        """Conviction de 0 à 1 : confiance de l'analyse + taux de réussite MESURÉ du signal + nombre d'observations.
        Pas d'avantage mesuré -> conviction nulle."""
        if c.get("edge", 0) <= 0 or not c.get("taux"):
            return 0.0
        cl = lambda x: max(0.0, min(1.0, x))
        return round(0.35 * cl((c["conf"] - 0.6) / 0.35) + 0.45 * cl((c["taux"] - 0.52) / 0.18)
                     + 0.20 * cl(c.get("n_ech", 0) / 300), 3)

    def planifier(self, c):
        """RÉFLEXION DE TRADER, 100 % calculée, avant chaque trade :
        1. Pas d'avantage MESURÉ sur l'historique -> pas de trade.
        2. Pour chaque levier x distance de stop (en ATR), simule des milliers de trajectoires avec les règles de
           sortie réelles (stop, zone d'encaissement 1R à 5R, plancher 70 %, liquidation, durée max).
        3. En tire la distribution des résultats par dollar misé : moyenne, variance, probabilités.
        4. Taille par le critère de KELLY (fraction qui maximise la croissance du capital), divisée par 2.
        5. Garde la combinaison qui fait croître le capital le plus vite. Seule limite : l'argent disponible."""
        import numpy as np
        row, sym, conf = c["row"], c["sym"], c["conf"]
        f, mmr, T = self.cfg["frais"], self.cfg["mmr"], self.cfg["duree_vie_max_min"]
        if conf < self.cfg["confiance_min"]:
            return {"ok": False, "raison": f"confiance {conf:.0%} < {self.cfg['confiance_min']:.0%}"}
        if c.get("edge", 0) <= 0:
            return {"ok": False, "raison": "aucun avantage mesuré sur l'historique : pas de pari"}
        # Avantage MINIMAL PLAUSIBLE = mesuré - 2 erreurs-types : on ne mise que sur ce qui est statistiquement solide
        edge_prudent = c["edge"] - self.cfg["z_prudence"] * c.get("se", c["edge"])
        if edge_prudent <= 0:
            return {"ok": False, "raison": f"avantage {c['edge']*100:+.2f} % non significatif (marge d'erreur "
                                           f"±{self.cfg['z_prudence'] * c.get('se', 0) * 100:.2f} %)"}
        with self.verrou:
            libre, eq, cap = self.marge_disponible(), self.equite(), self.capital
        if libre < 1:
            return {"ok": False, "raison": "plus de capital libre"}
        lev_max = self.cfg["levier_max"]
        try:
            m = (self.ex.market(sym).get("limits", {}).get("leverage", {}) or {}).get("max")
            if m: lev_max = min(lev_max, m)
        except Exception:
            pass
        pas = max(1, int(round(T * 6)))
        atr = row.atr / row.close
        sig = atr / 1.25 / np.sqrt(6)
        derive = edge_prudent / max(1, T) / 6           # la simulation utilise l'avantage PRUDENT, pas l'optimiste
        rng = np.random.default_rng(42)
        bruit = rng.standard_t(4, size=(self.cfg["simulations"], pas)) / np.sqrt(2) * sig
        ch_avec, ch_hasard = np.cumsum(bruit + derive, axis=1), np.cumsum(bruit, axis=1)
        garde = self.cfg["garde_pct"]
        zx_min, zx_max = self.cfg["zone_x_risque"]

        def simuler(ch, N, mise, P, b_liq, zmin, zmax):
            n = ch.shape[0]
            res = np.zeros(n); vivant = np.ones(n, bool); pic = np.full(n, -1e9); motif = np.zeros(n, int)
            for t in range(ch.shape[1]):
                net = N * ch[:, t] - 2 * N * f
                pic = np.maximum(pic, net)
                liq = vivant & (ch[:, t] <= -b_liq)
                filet = vivant & ~liq & (net <= -P)
                plafond = vivant & ~liq & ~filet & (net >= zmax)
                zone = vivant & ~liq & ~filet & ~plafond & (pic >= zmin) & (net <= np.maximum(zmin, pic * garde))
                res = np.where(liq, -mise - N * f, np.where(filet, -P, np.where(plafond, zmax,
                      np.where(zone, np.maximum(net, 0), res))))
                motif = np.where(liq, 3, np.where(filet, 2, np.where(plafond | zone, 1, motif)))
                vivant &= ~(liq | filet | plafond | zone)
            net = N * ch[:, -1] - 2 * N * f
            return np.where(vivant, net, res), np.where(vivant, 4, motif)

        base = 100.0                                    # calcul pour 100 $ de mise, puis mise à l'échelle
        meilleur = None
        for lev in self.cfg["leviers_testes"]:
            if lev > lev_max: continue
            N = base * lev
            b_liq = 1 / lev - mmr
            for k in self.cfg["stops_atr"]:
                d = k * atr                                     # stop structurel : k ATR sous l'entrée
                if d >= b_liq * 0.9: continue                   # le stop doit passer avant la liquidation
                P = N * d + 2 * N * f                           # perte au stop, frais compris
                zmin, zmax = zx_min * P, zx_max * P
                pnl, motif = simuler(ch_avec, N, base, P, b_liq, zmin, zmax)
                pnl0, _ = simuler(ch_hasard, N, base, P, b_liq, zmin, zmax)
                r = pnl / base                                  # rendement par dollar misé
                moy, var = float(r.mean()), float(r.var()) + 1e-12
                if moy <= 0: continue
                kelly = moy / var                               # fraction de l'équité à miser (Kelly continu)
                fr = self.cfg["fraction_kelly"] * kelly
                croissance = fr * moy - 0.5 * fr * fr * var     # croissance attendue du capital par trade
                p_liq = float((motif == 3).mean())
                if p_liq > self.cfg["proba_liq_max"]: continue
                e = dict(lev=lev, k=k, P=P, zmin=zmin, zmax=zmax, b_liq=b_liq, fr=fr, kelly=kelly, croissance=croissance,
                         moy=moy, moy0=float(pnl0.mean()) / base, p_gain=float((motif == 1).mean()),
                         p_filet=float((motif == 2).mean()), p_liq=p_liq, p_temps=float((motif == 4).mean()),
                         gain_moyen=float(pnl[motif == 1].mean()) / base if (motif == 1).any() else 0.0)
                if meilleur is None or e["croissance"] > meilleur["croissance"]:
                    meilleur = e
        if meilleur is None:
            return {"ok": False, "raison": "aucune combinaison levier/stop à espérance positive après frais"}
        m = meilleur
        # Corrélation : n positions déjà ouvertes de même catégorie = n paris presque identiques
        typ = self.type_marche(sym)
        with self.verrou:
            n_corr = sum(1 for p in self.positions.values() if p.get("type") == typ)
        fr = m["fr"] / (1 + self.cfg["correlation"] * n_corr)
        # Liquidité : position plafonnée à x % de la valeur échangée en 10 min sur ce marché
        valeur_10min = float(getattr(row, "vol_moy", row.volume)) * row.close * 10
        mise_liq = self.cfg["liquidite_max"] * valeur_10min / m["lev"] if valeur_10min > 0 else float("inf")
        mise = min(fr * eq, libre, cap / (1 + m["lev"] * f), mise_liq)     # Kelly ajusté, puis argent et liquidité
        limite = ("liquidité" if mise == mise_liq else "capital libre" if mise in (libre, cap / (1 + m["lev"] * f))
                  else f"Kelly ajusté ({n_corr} positions corrélées)" if n_corr else "Kelly")
        if mise * m["lev"] < self.cfg["min_notionnel"] or mise < 1:
            return {"ok": False, "raison": "mise calculée trop faible"}
        x = mise / base
        r2 = lambda v: round(v, 2)
        plan = dict(ok=True, lev=m["lev"], marge=r2(mise), proba=r2(m["p_gain"]), proba_stop=r2(m["p_filet"]),
                    proba_liq=r2(m["p_liq"]), proba_temps=r2(m["p_temps"]), esperance=r2(m["moy"] * mise),
                    esperance_hasard=r2(m["moy0"] * mise), gain_moyen=r2(m["gain_moyen"] * mise), perte=r2(m["P"] * x),
                    frais=r2(2 * mise * m["lev"] * f), mvt_liq=m["b_liq"], duree_min=T, stop_atr=m["k"],
                    edge=edge_prudent, n_ech=c.get("n_ech", 0), taux=c.get("taux"),
                    conviction=r2(mise / eq), kelly=r2(m["kelly"]), limite=limite, zone_min=r2(m["zmin"] * x), zone_max=r2(m["zmax"] * x))
        if plan["esperance"] < self.cfg["esperance_min"]:
            plan.update(ok=False, raison=f"espérance {plan['esperance']:+.2f} $ trop faible")
        return plan

    # ---- exécution ----
    def pnl_latent(self, p, prix=None):
        prix = prix or p.get("prix") or p["entree"]
        return (prix - p["entree"]) * p["qty"] * (1 if p["sens"] == "long" else -1)

    def pnl_net(self, p, prix=None):
        """PnL si on fermait maintenant, frais d'entrée et de sortie compris."""
        prix = prix or p.get("prix") or p["entree"]
        return self.pnl_latent(p, prix) - p["qty"] * (p["entree"] + prix) * self.cfg["frais"]

    def equite(self):
        return self.capital + sum(max(0, p["marge"] + self.pnl_latent(p)) for p in self.positions.values())

    def ouvrir(self, c, plan, px):
        sym, sens = c["sym"], c["sens"]
        lev, marge = plan["lev"], plan["marge"]
        notionnel = marge * lev
        qty = notionnel / px
        s = 1 if sens == "long" else -1
        P = plan["perte"]
        stop = px * (1 - s * (P - 2 * notionnel * self.cfg["frais"]) / notionnel)
        liq = px * (1 - s * plan["mvt_liq"])
        typ = self.type_marche(sym)
        if self.cfg["live"]:
            try: self.ex_ord.set_margin_mode("isolated", sym)
            except Exception as e: log(f"   marge isolée: {e}")
            self.ex_ord.set_leverage(lev, sym)
            self.ex_ord.create_market_order(sym, "buy" if sens == "long" else "sell", qty)
            self.ex_ord.create_order(sym, "STOP_MARKET", "sell" if sens == "long" else "buy", qty,
                                     params={"stopPrice": stop, "reduceOnly": True})   # filet côté Binance
        self.capital -= marge + notionnel * self.cfg["frais"]
        self.positions[sym] = dict(sym=sym, sens=sens, qty=qty, entree=px, prix=px, stop=stop, type=typ,
                                   marge=marge, lev=lev, liq=liq, conf=round(c.get("conf", 0.5), 2),
                                   proba=plan["proba"], proba_liq=plan["proba_liq"], duree_min=plan["duree_min"],
                                   perte_prevue=P, gain_prevu=plan["gain_moyen"], pic=0.0, cible=None,
                                   pic_px=px, partiel=False, encaisse=0.0, atr_rel=c["row"].atr / c["row"].close,
                                   gain_min=plan.get("zone_min", self.cfg["zone_gain_min"]),
                                   zone_min=plan.get("zone_min", self.cfg["zone_gain_min"]),
                                   zone_max=plan.get("zone_max", self.cfg["zone_gain_max"]),
                                   conviction=plan.get("conviction", 0),
                                   ouverture=datetime.now().isoformat(timespec="seconds"),
                                   t_ouv=c.get("t") or time.time())
        log(f"🟢 {sens.upper()} {sym} [{typ}] x{lev} | Kelly {plan.get('kelly', 0):.0%} -> mise {plan.get('conviction', 0):.1%} "
            f"de l'équité = {marge:.0f} $ (limite : {plan.get('limite', '-')}) | stop {plan.get('stop_atr', 0)} ATR | "
            f"position {notionnel:.0f} $ | zone de gain +{plan.get('zone_min', 0):.0f}/+{plan.get('zone_max', 0):.0f} $ | "
            f"filet -{P:.0f} $ à {stop:.5g} | liquidation {liq:.5g} | simulation : gain encaissé {plan['proba']:.0%} "
            f"(moy. +{plan['gain_moyen']:.2f} $) / filet {plan['proba_stop']:.0%} / liquidation {plan['proba_liq']:.0%} / "
            f"au temps {plan['proba_temps']:.0%} | frais {plan['frais']:.2f} $ | espérance {plan['esperance']:+.2f} $ "
            f"(hasard pur : {plan['esperance_hasard']:+.2f} $) | avantage mesuré {plan['edge']*100:+.3f} % sur "
            f"{plan['n_ech']} signaux" + (f" ({plan['taux']:.0%} gagnants)" if plan['taux'] is not None else ""))

    def fermer(self, sym, prix, raison):
        p = self.positions.pop(sym)
        if self.cfg["live"] and not raison.startswith("LIQUIDATION"):
            try:
                self.ex_surv.create_market_order(sym, "sell" if p["sens"] == "long" else "buy", p["qty"],
                                                 params={"reduceOnly": True})
            except Exception as e: log(f"   ordre de clôture {sym}: {e}")
        if self.cfg["live"]:
            try: self.ex_surv.cancel_all_orders(sym)
            except Exception: pass
        frais_entree = p["qty"] * p["entree"] * self.cfg["frais"]
        if raison.startswith("LIQUIDATION"):
            retour = 0.0
        else:
            retour = max(0, p["marge"] + self.pnl_latent(p, prix) - p["qty"] * prix * self.cfg["frais"])
        self.capital += retour
        pnl = retour - p["marge"] - frais_entree + p.get("encaisse", 0.0)   # inclut la prise partielle
        self.historique.append(pnl)
        if pnl < 0:
            self.cooldown[sym] = time.time() + self.cfg["cooldown_perte_min"] * 60
            self.pertes_suite += 1
            if self.pertes_suite >= self.cfg["pertes_consecutives_max"]:
                self.pause_jusqua = time.time() + self.cfg["pause_min"] * 60
                log(f"⏸️ {self.pertes_suite} pertes d'affilée : pause des nouvelles entrées pendant {self.cfg['pause_min']} min "
                    f"(les positions ouvertes restent gérées)")
                self.pertes_suite = 0
        else:
            self.pertes_suite = 0
        self.relancer = True
        self.trades.append({"paire": sym, "sens": p["sens"], "levier": p["lev"], "entree": p["entree"],
                            "sortie": prix, "pnl": round(pnl, 4), "raison": raison,
                            "heure": datetime.now().isoformat(timespec="seconds")})
        duree = time.time() - p["t_ouv"] if p["t_ouv"] > 1e9 and p["t_ouv"] <= time.time() else 0
        log(f"{'🔴' if pnl < 0 else '💰'} CLÔTURE {sym} x{p['lev']} @ {prix:.5g} ({raison}) | PnL {pnl:+.2f} $"
            + (f" en {duree:.0f} s" if duree else "") + f" | équité {self.equite():.2f} $")

    def verifier_mort(self):
        eq = self.equite()
        if eq <= self.cfg["seuil_mort"]:
            log(f"💀 Équité {eq:.2f} $ <= {self.cfg['seuil_mort']} $ : fermeture de toutes les positions.")
            for sym in list(self.positions):
                p = self.positions[sym]; self.fermer(sym, p["prix"], "MORT DU BOT")
            self.vivant = False
            log(f"💀 LE BOT EST MORT. Équité finale {self.equite():.2f} $")

    def fermer_partiel(self, sym, prix, frac, raison):
        """Encaisse une fraction de la position (prise de gain partielle)."""
        p = self.positions[sym]
        q = p["qty"] * frac; m = p["marge"] * frac
        if self.cfg["live"]:
            try:
                self.ex_surv.create_market_order(sym, "sell" if p["sens"] == "long" else "buy", q, params={"reduceOnly": True})
            except Exception as e: log(f"   prise partielle {sym}: {e}")
        s = 1 if p["sens"] == "long" else -1
        brut = (prix - p["entree"]) * q * s
        net = brut - q * (p["entree"] + prix) * self.cfg["frais"]
        self.capital += m + brut - q * prix * self.cfg["frais"]
        p["qty"] -= q; p["marge"] -= m; p["encaisse"] += net; p["partiel"] = True
        p["stop"] = p["entree"] * (1 + s * 2 * self.cfg["frais"])      # le reste ne peut plus perdre (frais couverts)
        p["pic"] = self.pnl_net(p, prix)
        log(f"💵 PRISE DE GAIN PARTIELLE {sym} : {frac:.0%} encaissés @ {prix:.5g} = {net:+.2f} $ ({raison}) | "
            f"stop du reste remonté au prix d'entrée, on laisse courir")

    def decider_sortie(self, sym, low, high, last, retourne=(0, [], False, None, None), ouv=None, maintenant=None):
        """Réflexion de trader sous levier, à chaque passage (toutes les 2 s, bougies 1m clôturées) :
        SÉCURITÉS : liquidation, filet -60 % de la mise, durée max.
        PLAFOND   : +100 $ net -> encaissé immédiatement.
        ZONE 20-100 $ : on laisse courir tant que l'élan tient, et on encaisse dès que :
            - l'élan faiblit (>= 2 signaux de retournement), ou
            - le mouvement s'essouffle (RSI extrême), ou
            - on a rendu 30 % du meilleur gain (plancher = max(20 $, 70 % du max)).
        SOUS 20 $ : on n'encaisse pas ; sortie seulement sur retournement CONFIRMÉ (>= 3 signaux + structure)."""
        p = self.positions[sym]; s = 1 if p["sens"] == "long" else -1
        p["prix"] = float(last)
        ouv = last if ouv is None else ouv
        nb, signaux, structure, atr, rsi = (tuple(retourne) + (None,) * 5)[:5]
        if atr: p["atr_rel"] = atr
        zmin, zmax = p.get("zone_min", self.cfg["zone_gain_min"]), p.get("zone_max", self.cfg["zone_gain_max"])
        if ((ouv <= p["liq"]) if s == 1 else (ouv >= p["liq"])) or ((low <= p["liq"]) if s == 1 else (high >= p["liq"])):
            return self.fermer(sym, p["liq"], "LIQUIDATION")
        if (low <= p["stop"]) if s == 1 else (high >= p["stop"]):
            return self.fermer(sym, p["stop"], f"STOP -{p['perte_prevue']:.0f} $")
        meilleur_px = high if s == 1 else low
        net, net_max_bougie = self.pnl_net(p, last), self.pnl_net(p, meilleur_px)
        p["pic"] = max(p["pic"], net_max_bougie)
        # PLAFOND : +100 $ atteint -> on encaisse (ordre limite au niveau +100 $)
        if net_max_bougie >= zmax:
            q, e, f = p["qty"], p["entree"], self.cfg["frais"]
            px_cible = (zmax / q + e * (1 + f)) / (1 - f) if s == 1 else (e * (1 - f) - zmax / q) / (1 + f)
            return self.fermer(sym, last if net >= zmax else px_cible, f"GAIN ENCAISSÉ +{zmax:.0f} $ (plafond)")
        nb = nb or 0
        if p["pic"] >= zmin:                                   # on est (ou on a été) dans la zone de gain
            plancher = max(zmin, p["pic"] * self.cfg["garde_pct"])
            if net >= zmin and nb >= self.cfg["signaux_encaisse_zone"]:
                return self.fermer(sym, last, f"GAIN ENCAISSÉ +{net:.0f} $ : l'élan faiblit ({nb} signaux : {', '.join(signaux[:2])})")
            if net >= zmin and rsi is not None and (rsi > self.cfg["rsi_epuisement"] if s == 1 else rsi < 100 - self.cfg["rsi_epuisement"]):
                return self.fermer(sym, last, f"GAIN ENCAISSÉ +{net:.0f} $ : mouvement essoufflé (RSI {rsi:.0f})")
            if net <= plancher:
                q, e, f = p["qty"], p["entree"], self.cfg["frais"]
                px_pl = (plancher / q + e * (1 + f)) / (1 - f) if s == 1 else (e * (1 - f) - plancher / q) / (1 + f)
                gap = (ouv - px_pl) * s < 0                     # ouverture déjà sous le plancher : on sort au prix réel
                return self.fermer(sym, last if gap else px_pl,
                                   f"GAIN ENCAISSÉ : plancher protégé +{plancher:.0f} $ (max atteint +{p['pic']:.0f} $)")
            return                                             # dans la zone et l'élan tient : on laisse courir
        confirme = nb >= self.cfg["signaux_retournement_min"] and structure
        if confirme and net < 0 and -net >= max(self.cfg["coupe_perte_usd"], 0.6 * p["perte_prevue"]):
            return self.fermer(sym, last, f"PERTE COUPÉE : retournement confirmé ({nb}/6 : {', '.join(signaux[:3])})")
        if (maintenant or time.time()) - p["t_ouv"] >= self.cfg["duree_vie_max_min"] * 60:
            return self.fermer(sym, last, f"FIN DES {self.cfg['duree_vie_max_min']} MIN")

    def sauver(self):
        with self.verrou:
            self._sauver()

    def _sauver(self):
        eq = self.equite()
        self.courbe.append((time.time(), eq))
        self.courbe = self.courbe[-2000:]
        pos = [dict(p, pnl_latent=round(self.pnl_net(p), 2)) for p in self.positions.values()]
        etat = {"maj": datetime.now().isoformat(timespec="seconds"),
                "mode": "LIVE" if self.cfg["live"] else "PAPER",
                "capital_initial": self.cfg["capital_initial"], "seuil_mort": self.cfg["seuil_mort"],
                "capital": self.capital, "equite": eq, "positions": pos, "file_candidats": len(self.file),
                "max_positions": self.cfg["max_positions"], "vivant": self.vivant,
                "historique": self.historique, "trades": self.trades[-300:],
                "courbe": self.courbe, "analyse": self.analyse, "logs": list(LOGS)[-200:]}
        tmp = self.cfg["etat_fichier"] + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(etat, fh, default=str)
        os.replace(tmp, self.cfg["etat_fichier"])

    def resume(self):
        g = sum(1 for x in self.historique if x > 0)
        log(f"Trades {len(self.historique)} | gagnants {g} | équité {self.equite():.2f} $ | "
            f"{'VIVANT' if self.vivant else 'MORT'}")

    # ---- les 3 fils : surveillance / recherche / ouverture ----
    def surveiller(self):
        """Relit les 30 dernières bougies 1m de chaque position et décide de sa sortie."""
        with self.verrou:
            syms = list(self.positions)
        for sym in syms:
            try:
                o = self.ex_surv.fetch_ohlcv(sym, "1m", limit=60)
                with self.verrou:
                    if sym not in self.positions: continue
                    t_ouv = self.positions[sym]["t_ouv"] * 1000
                    depuis = [x for x in o if x[0] + 60000 > t_ouv] or o[-1:]
                    low = min(x[3] for x in depuis[-2:]); high = max(x[2] for x in depuis[-2:])
                    ret = retournement(o, self.positions[sym]["sens"])
                    self.decider_sortie(sym, low, high, o[-1][4], ret)
            except Exception as e:
                log(f"   surveillance {sym}: {e}")

    def boucle_surveillance(self):
        dernier_statut = time.time()
        while self.vivant:
            try:
                self.surveiller()
                with self.verrou:
                    self.verifier_mort()
                    if time.time() - dernier_statut >= 300:
                        g = [x for x in self.historique if x > 0]
                        log(f"📊 {len(self.positions)} position(s) | {len(self.historique)} trades clôturés "
                            f"({len(g)} gagnants) | file {len(self.file)} candidats | équité {self.equite():.2f} $")
                        dernier_statut = time.time()
                self.sauver()
            except Exception as e:
                log(f"Erreur surveillance: {e}")
            time.sleep(self.cfg["surveillance_s"])

    def boucle_recherche(self):
        """Fil de recherche permanent : prépare une file de candidats déjà analysés et calculés."""
        while self.vivant:
            try:
                t0 = time.time()
                cands = self.candidats()
                with self.verrou:
                    self.file = sorted(cands, key=lambda c: -c["plan"]["esperance"])
                    self.t_file = time.time()
                log(f"🔎 File de candidats mise à jour : {len(cands)} prêt(s) en {time.time() - t0:.0f} s")
            except Exception as e:
                log(f"Erreur recherche: {e}")
                time.sleep(5)

    def remplir(self):
        """Ouvre IMMÉDIATEMENT les meilleurs candidats de la file dès qu'une place est libre."""
        with self.verrou:
            if time.time() < self.pause_jusqua: return
            if time.time() - self.t_file > self.cfg["file_validite_s"]: return
            places = self.cfg["max_positions"] - len(self.positions)
            file = [c for c in self.file if c["sym"] not in self.positions and self.cooldown.get(c["sym"], 0) <= time.time()]
        for c in file[:max(0, places)]:
            try:
                px = float(self.ex_ord.fetch_ticker(c["sym"])["last"])        # prix de l'instant
            except Exception:
                px = c.get("prix")
            if not px: continue
            ecart = abs(px / c["prix"] - 1) if c.get("prix") else 0
            if ecart > 2 * c["row"].atr / c["row"].close:
                with self.verrou:
                    self.file = [x for x in self.file if x is not c]
                continue                                    # le prix a trop bougé depuis l'analyse
            plan = self.planifier(c)                        # recalcul express avec le capital du moment
            with self.verrou:
                self.file = [x for x in self.file if x is not c]
                if not self.vivant or len(self.positions) >= self.cfg["max_positions"] or c["sym"] in self.positions:
                    continue
                if plan["ok"]: self.ouvrir(c, plan, px)

    def live_loop(self):
        log(f"Démarrage {'LIVE ⚠️' if self.cfg['live'] else 'PAPER'} | MODE {self.cfg['mode'].upper()} | capital {self.capital:.0f} $ | taille calculée "
            f"(Kelly x{self.cfg['fraction_kelly']}) | levier x{min(self.cfg['leviers_testes'])}"
            f"-x{self.cfg['levier_max']} | sorties décidées par le bot à chaque bougie | filet -{self.cfg['perte_max_pct']:.0%} "
            f"de la mise | {self.cfg['duree_vie_max_min']} min max | mort à {self.cfg['seuil_mort']:.0f} $")
        threading.Thread(target=self.boucle_surveillance, daemon=True).start()
        threading.Thread(target=self.boucle_recherche, daemon=True).start()
        while self.vivant:
            try:
                self.remplir()
            except Exception as e:
                log(f"Erreur ouverture: {e}")
            time.sleep(1)
        self.sauver()
        self.resume()

    def backtest(self):
        """Backtest technique sur l'historique 1m (avantage mesuré sur le passé de chaque bougie)."""
        syms = self.paires()[:20]
        data = {}
        for k, s_ in enumerate(syms, 1):
            log(f"Téléchargement {k}/{len(syms)} : {s_}")
            data[s_] = self.ohlcv(s_, 1500)
        n = min(len(d) for d in data.values())
        debut = self.cfg["historique_mesure"]
        log(f"Backtest sur {n - debut} bougies x {len(data)} paires...")
        for i in range(debut, n):
            if not self.vivant: break
            for sym in list(self.positions):
                df = data[sym]; r = df.iloc[i]
                o = df.iloc[max(0, i - 59): i + 1][["ts", "open", "high", "low", "close"]].values.tolist()
                self.decider_sortie(sym, r.low, r.high, r.close, retournement(o, self.positions[sym]["sens"]),
                                    r.open, r.ts / 1000)
            self.verifier_mort()
            if not self.vivant or len(self.positions) >= self.cfg["max_positions"]: continue
            for sym, df in data.items():
                if sym in self.positions or len(self.positions) >= self.cfg["max_positions"]: continue
                for sens in ("long", "short"):
                    ok, ch = verifier(df.iloc[i], df.iloc[i - 1], sens)
                    if not ok: continue
                    edge, n_ech, taux, se = avantage_mesure(df.iloc[i - debut: i], sens, self.cfg["duree_vie_max_min"], self.cfg)
                    c = dict(sym=sym, sens=sens, row=df.iloc[i], t=df.iloc[i].ts / 1000, conf=sum(ch.values()) / len(ch),
                             edge=edge, n_ech=n_ech, taux=taux, se=se, prix=float(df.iloc[i].close))
                    plan = self.planifier(c)
                    if plan["ok"]: self.ouvrir(c, plan, c["prix"]); break
        for sym in list(self.positions):
            self.fermer(sym, data[sym].iloc[n - 1].close, "fin du backtest")
        self.sauver()
        self.resume()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    bot = Bot(CONFIG)
    bot.backtest() if ap.parse_args().backtest else bot.live_loop()
