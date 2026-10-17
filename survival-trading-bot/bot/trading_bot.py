"""
Survival Trading Bot — v2.0 « SUIVI DE TENDANCE »
=================================================
Stratégie de suivi de tendance (style CTA) sur les perpétuels USDT de Binance :
  - Direction : tendance 4h (EMA50 vs EMA200 + prix au-dessus/au-dessous de l'EMA200).
  - Entrée    : cassure du plus haut (long) / plus bas (short) des 20 dernières bougies 1h,
                dans le sens de la tendance 4h, avec un volume supérieur à la moyenne.
  - Risque    : 1 % du capital par trade. Stop initial à 2 ATR(1h). Taille = risque / distance du stop.
  - Sortie    : stop suiveur « chandelier » à 3 ATR du meilleur prix (laisse courir les gagnants),
                ou inversion de la tendance 4h.
  - Portefeuille : 6 positions max, 4 max dans le même sens (évite le pari unique corrélé).

Mode PAPER par défaut. Toujours lancer le backtest d'abord :
    python bot/trading_bot.py --backtest
    python bot/trading_bot.py
"""
import argparse, json, math, os, threading, time
from collections import deque
from datetime import datetime

import ccxt
import numpy as np
import pandas as pd

CONFIG = {
    "capital_initial": 10000.0,
    "seuil_mort": 10.0,
    # --- stratégie ---
    "tf_entree": "1h",
    "tf_tendance": "4h",
    "donchian": 20,              # cassure du plus haut/bas des 20 dernières bougies 1h
    "atr_periode": 14,
    "stop_atr": 2.0,             # stop initial à 2 ATR
    "trailing_atr": 3.0,         # stop suiveur à 3 ATR du meilleur prix
    "volume_x": 1.2,             # bougie de cassure : volume > 1,2 x moyenne 20
    "atr_max_pct": 0.06,         # ignore les marchés trop fous (ATR 1h > 6 % du prix)
    # --- risque & portefeuille ---
    "risque_pct": 0.005,         # 0,5 % du capital risqué par trade (drawdown ~divisé par 2)
    "max_positions": 6,
    "max_meme_sens": 4,
    "levier_max": 5,             # le levier sert juste à loger la position dans la marge
    "frais": 0.0005,             # taker 0,05 %
    "glissement": 0.0005,        # glissement estimé (backtest)
    "min_notionnel": 5.0,
    # --- marchés ---
    "volume_min_par_type": {"crypto": 50_000_000, "actions": 1_000_000, "matières premières": 1_000_000,
                            "indices": 1_000_000, "devises": 1_000_000},
    "max_marches": 2500,         # analyse jusqu'à 2500 marchés (tous ceux qui passent le filtre de volume)
    # --- boucle ---
    "surveillance_s": 10,        # stops vérifiés toutes les 10 s
    "scan_min": 5,               # recherche de cassures toutes les 5 min
    "backtest_marches": 60,
    "backtest_jours": 365,       # 12 mois d'historique 1h
    "part_optimisation": 0.67,   # 8 premiers mois : réglage ; 4 derniers mois : test sur données jamais vues
    "grille": {"donchian": [20, 40, 55], "trailing_atr": [3.0, 4.0], "stop_atr": [2.0, 3.0]},
    "params_fichier": "params_optimises.json",
    "live": False,
    "etat_fichier": "bot_state_v2.json",
}

LOGS = deque(maxlen=300)


def log(msg):
    ligne = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    LOGS.append(ligne)
    print(ligne, flush=True)


# ================= INDICATEURS =================
def atr(df, n):
    tr = pd.concat([df.high - df.low, (df.high - df.close.shift()).abs(), (df.low - df.close.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def preparer(h1, h4, cfg):
    """Ajoute à la série 1h : canal de Donchian, ATR, volume moyen, et la tendance 4h (sans regarder le futur)."""
    n = cfg["donchian"]
    h1 = h1.copy()
    h1["haut_n"] = h1.high.shift(1).rolling(n).max()      # plus haut des n bougies PRÉCÉDENTES
    h1["bas_n"] = h1.low.shift(1).rolling(n).min()
    h1["atr"] = atr(h1, cfg["atr_periode"])
    h1["vol_moy"] = h1.volume.shift(1).rolling(20).mean()
    h4 = h4.copy()
    h4["e50"] = h4.close.ewm(span=50, adjust=False).mean()
    h4["e200"] = h4.close.ewm(span=200, adjust=False).mean()
    h4["tendance"] = np.where((h4.e50 > h4.e200) & (h4.close > h4.e200), 1,
                              np.where((h4.e50 < h4.e200) & (h4.close < h4.e200), -1, 0))
    h4["ts_dispo"] = h4.ts + 4 * 3600 * 1000              # une bougie 4h n'est connue qu'une fois clôturée
    h1["ts_fin"] = h1.ts + 3600 * 1000
    m = pd.merge_asof(h1.sort_values("ts_fin"), h4[["ts_dispo", "tendance"]].sort_values("ts_dispo"),
                      left_on="ts_fin", right_on="ts_dispo", direction="backward")
    m["tendance"] = m["tendance"].fillna(0)
    return m


def signal(r, cfg):
    """Signal sur une bougie 1h CLÔTURÉE : +1 long, -1 short, 0 rien."""
    if any(pd.isna(r[k]) for k in ("haut_n", "bas_n", "atr", "vol_moy")) or r.atr <= 0:
        return 0
    if r.atr / r.close > cfg["atr_max_pct"]:
        return 0
    vol_ok = r.volume > cfg["volume_x"] * r.vol_moy
    if r.tendance == 1 and r.close > r.haut_n and vol_ok:
        return 1
    if r.tendance == -1 and r.close < r.bas_n and vol_ok:
        return -1
    return 0


# ================= MOTEUR =================
class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        cle = {"apiKey": os.getenv("BINANCE_KEY") or None, "secret": os.getenv("BINANCE_SECRET") or None}
        self.ex = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 15000, **cle})
        self.ex_surv = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 10000, **cle})
        self.verrou = threading.RLock()
        self.capital = cfg["capital_initial"]
        self.positions, self.trades, self.historique = {}, [], []
        self.courbe = [(time.time(), self.capital)]
        self.analyse, self.vivant = {}, True
        log("Démarrage — connexion à Binance Futures...")
        self.sauver()
        self.connexion()

    # ---------- connexion & marchés ----------
    def connexion(self):
        for essai in range(1, 4):
            try:
                t0 = time.time(); self.ex.fetch_time()
                log(f"✔ Binance joignable ({(time.time() - t0) * 1000:.0f} ms). Chargement des marchés...")
                self.ex.load_markets()
                e = self.ex_surv
                e.markets, e.markets_by_id, e.symbols, e.ids = self.ex.markets, self.ex.markets_by_id, self.ex.symbols, self.ex.ids
                log(f"✔ {len(self.ex.markets)} marchés chargés.")
                return
            except Exception as ex:
                log(f"✖ Connexion impossible (essai {essai}/3) : {type(ex).__name__}: {str(ex)[:150]}")
                time.sleep(3)
        log("ARRÊT : Binance Futures inaccessible depuis ce réseau.")
        self.sauver()
        raise SystemExit(1)

    def type_marche(self, sym):
        try:
            info = self.ex.market(sym).get("info", {}) or {}
            t = (info.get("underlyingType") or "").upper()
            sub = [x.upper() for x in (info.get("underlyingSubType") or [])]
            if t == "COMMODITY" or any("METAL" in x or "GOLD" in x for x in sub): return "matières premières"
            if t in ("EQUITY", "STOCK") or any("STOCK" in x for x in sub): return "actions"
            if t == "INDEX": return "indices"
            if t == "PREMARKET": return "pré-marché"
            if sym.split("/")[0] in ("EUR", "GBP", "JPY", "AUD", "CHF", "CAD", "NZD"): return "devises"
        except Exception:
            pass
        return "crypto"

    def marches(self):
        t = self.ex.fetch_tickers()
        seuils = self.cfg["volume_min_par_type"]
        ok = []
        for s, v in t.items():
            if not s.endswith("/USDT:USDT"): continue
            base = s.split("/")[0]
            if not base.isascii() or not base.replace("1000", "").isalnum(): continue
            typ = self.type_marche(s)
            if typ == "pré-marché": continue
            if (v.get("quoteVolume") or 0) >= seuils.get(typ, 50_000_000):
                ok.append((s, v["quoteVolume"]))
        ok.sort(key=lambda x: -x[1])
        return [s for s, _ in ok[: self.cfg["max_marches"]]]

    def bougies(self, ex, sym, tf, limit):
        o = ex.fetch_ohlcv(sym, tf, limit=limit)
        return pd.DataFrame(o, columns=["ts", "open", "high", "low", "close", "volume"])

    def donnees(self, sym, limit1h=300):
        h1 = self.bougies(self.ex, sym, self.cfg["tf_entree"], limit1h)
        h4 = self.bougies(self.ex, sym, self.cfg["tf_tendance"], 400)
        return preparer(h1, h4, self.cfg)

    # ---------- comptabilité ----------
    def pnl(self, p, prix):
        return (prix - p["entree"]) * p["qty"] * p["s"]

    def equite(self):
        return self.capital + sum(p["marge"] + self.pnl(p, p["prix"]) for p in self.positions.values())

    def taille(self, prix, atr_v, eq):
        """Taille par le risque : on perd risque_pct du capital si le stop initial est touché.
        La marge est répartie entre les emplacements libres ; le levier (<= levier_max) loge la position."""
        risque = eq * self.cfg["risque_pct"]
        dist = self.cfg["stop_atr"] * atr_v
        qty = risque / dist
        notionnel = qty * prix
        places = max(1, self.cfg["max_positions"] - len(self.positions))
        marge_dispo = self.capital * 0.95 / places
        lev = max(1, min(self.cfg["levier_max"], math.ceil(notionnel / max(1e-9, marge_dispo))))
        marge = notionnel / lev
        if marge > marge_dispo:                             # même au levier max ça ne rentre pas : on réduit
            marge = marge_dispo; notionnel = marge * lev; qty = notionnel / prix
            risque = qty * dist                             # risque RÉEL, annoncé tel quel
        return qty, notionnel, marge, lev, risque

    # ---------- ouverture / clôture ----------
    def ouvrir(self, sym, s, prix, atr_v, t=None, glisse=0.0):
        prix = prix * (1 + s * glisse)
        qty, notionnel, marge, lev, risque = self.taille(prix, atr_v, self.equite())
        if notionnel < self.cfg["min_notionnel"]:
            return
        stop = prix - s * self.cfg["stop_atr"] * atr_v
        if self.cfg["live"]:
            try: self.ex.set_margin_mode("isolated", sym)
            except Exception: pass
            self.ex.set_leverage(lev, sym)
            self.ex.create_market_order(sym, "buy" if s == 1 else "sell", qty)
            self.ex.create_order(sym, "STOP_MARKET", "sell" if s == 1 else "buy", qty,
                                 params={"stopPrice": stop, "reduceOnly": True})
        self.capital -= marge + notionnel * self.cfg["frais"]
        self.positions[sym] = dict(sym=sym, s=s, sens="long" if s == 1 else "short", qty=qty, entree=prix, prix=prix,
                                   stop=stop, meilleur=prix, atr=atr_v, marge=marge, lev=lev, risque=risque,
                                   type=self.type_marche(sym) if not t else "crypto", t_ouv=t or time.time(),
                                   ouverture=datetime.fromtimestamp(t or time.time()).isoformat(timespec="seconds"),
                                   liq=prix * (1 - s * (1 / lev - 0.005)), pic=0.0, perte_prevue=risque, gain_prevu=None,
                                   proba=None, duree_min=None, cible=None)
        if not t:
            log(f"🟢 {'LONG' if s == 1 else 'SHORT'} {sym} — cassure {self.cfg['donchian']}h dans le sens de la tendance 4h | "
                f"entrée {prix:.6g} | stop {stop:.6g} ({self.cfg['stop_atr']:g} ATR) | risque {risque:.0f} $ ({risque / max(1, self.equite()):.1%} du capital) | "
                f"position {notionnel:.0f} $ x{lev}")

    def fermer(self, sym, prix, raison, t=None, glisse=0.0):
        p = self.positions.pop(sym)
        prix = prix * (1 - p["s"] * glisse)
        if self.cfg["live"]:
            try:
                self.ex_surv.create_market_order(sym, "sell" if p["s"] == 1 else "buy", p["qty"], params={"reduceOnly": True})
                self.ex_surv.cancel_all_orders(sym)
            except Exception as e:
                log(f"   clôture {sym}: {e}")
        brut = self.pnl(p, prix)
        frais = p["qty"] * prix * self.cfg["frais"]
        self.capital += p["marge"] + brut - frais
        net = brut - frais - p["qty"] * p["entree"] * self.cfg["frais"]
        self.historique.append(net)
        r_mult = net / p["risque"] if p["risque"] else 0
        self.trades.append({"paire": sym, "sens": p["sens"], "levier": p["lev"], "entree": p["entree"], "sortie": prix,
                            "pnl": round(net, 2), "R": round(r_mult, 2), "raison": raison,
                            "heure": datetime.fromtimestamp(t or time.time()).isoformat(timespec="seconds")})
        if not t:
            log(f"{'💰' if net > 0 else '🔴'} CLÔTURE {sym} @ {prix:.6g} ({raison}) | {net:+.2f} $ ({r_mult:+.1f} R) | "
                f"équité {self.equite():.2f} $")

    def gerer(self, sym, haut, bas, dernier, atr_v=None, tendance=None, t=None, glisse=0.0):
        """Stop (touché en cours de bougie), stop suiveur chandelier mis à jour, inversion de tendance 4h."""
        p = self.positions[sym]; s = p["s"]
        p["prix"] = dernier
        if (bas <= p["stop"]) if s == 1 else (haut >= p["stop"]):
            gain = (p["stop"] - p["entree"]) * s > 0
            return self.fermer(sym, p["stop"], "STOP SUIVEUR (gain sécurisé)" if gain else "STOP INITIAL", t, glisse)
        if atr_v:                                            # mise à jour sur bougie clôturée
            p["atr"] = atr_v
            p["meilleur"] = max(p["meilleur"], haut) if s == 1 else min(p["meilleur"], bas)
            suiveur = p["meilleur"] - s * self.cfg["trailing_atr"] * atr_v
            if (suiveur - p["stop"]) * s > 0:
                p["stop"] = suiveur
                if self.cfg["live"] and not t:
                    try:
                        self.ex_surv.cancel_all_orders(sym)
                        self.ex_surv.create_order(sym, "STOP_MARKET", "sell" if s == 1 else "buy", p["qty"],
                                                  params={"stopPrice": p["stop"], "reduceOnly": True})
                    except Exception as e:
                        log(f"   stop suiveur {sym}: {e}")
        if tendance is not None and tendance == -s:
            return self.fermer(sym, dernier, "TENDANCE 4H INVERSÉE", t, glisse)
        p["pic"] = max(p["pic"], self.pnl(p, dernier))

    def peut_ouvrir(self, s):
        if len(self.positions) >= self.cfg["max_positions"]: return False
        return sum(1 for p in self.positions.values() if p["s"] == s) < self.cfg["max_meme_sens"]

    def verifier_mort(self):
        if self.equite() <= self.cfg["seuil_mort"]:
            for sym in list(self.positions):
                self.fermer(sym, self.positions[sym]["prix"], "MORT DU BOT")
            self.vivant = False
            log("💀 LE BOT EST MORT.")

    # ---------- état pour le tableau de bord ----------
    def sauver(self):
        with self.verrou:
            eq = self.equite()
            self.courbe.append((time.time(), eq)); self.courbe = self.courbe[-3000:]
            pos = [dict(p, pnl_latent=round(self.pnl(p, p["prix"]), 2)) for p in self.positions.values()]
            etat = {"maj": datetime.now().isoformat(timespec="seconds"), "mode": "LIVE" if self.cfg["live"] else "PAPER",
                    "capital_initial": self.cfg["capital_initial"], "seuil_mort": self.cfg["seuil_mort"],
                    "capital": self.capital, "equite": eq, "positions": pos, "max_positions": self.cfg["max_positions"],
                    "vivant": self.vivant, "historique": self.historique, "trades": self.trades[-300:],
                    "courbe": self.courbe, "analyse": self.analyse, "logs": list(LOGS)[-200:], "file_candidats": 0}
            tmp = self.cfg["etat_fichier"] + ".tmp"
            with open(tmp, "w") as f:
                json.dump(etat, f, default=str)
            os.replace(tmp, self.cfg["etat_fichier"])

    # ---------- BACKTEST sur les vraies données ----------
    # ---------- BACKTEST 12 MOIS + VALIDATION HORS ÉCHANTILLON ----------
    def telecharger(self, sym, tf, jours):
        """Télécharge tout l'historique par morceaux de 1500 bougies."""
        pas = {"1h": 3600, "4h": 14400}[tf] * 1000
        depuis = int((time.time() - jours * 86400) * 1000)
        lignes = []
        while True:
            o = self.ex.fetch_ohlcv(sym, tf, since=depuis, limit=1500)
            if not o: break
            lignes += o
            if len(o) < 1500 or o[-1][0] + pas >= time.time() * 1000: break
            depuis = o[-1][0] + pas
        df = pd.DataFrame(lignes, columns=["ts", "open", "high", "low", "close", "volume"])
        return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)

    def charger(self):
        jours = self.cfg["backtest_jours"]
        syms = self.marches()[: self.cfg["backtest_marches"]]
        log(f"Backtest : téléchargement de {jours} jours de bougies 1h et 4h x {len(syms)} marchés "
            f"(plusieurs minutes, une seule fois)...")
        brut = {}
        for k, sym in enumerate(syms, 1):
            try:
                h1 = self.telecharger(sym, "1h", jours)
                h4 = self.telecharger(sym, "4h", jours + 60)        # marge pour l'EMA200 4h
                if len(h1) > 500: brut[sym] = (h1, h4)
            except Exception as e:
                log(f"   {sym}: {e}")
            if k % 10 == 0: log(f"   {k}/{len(syms)} marchés téléchargés")
        index = sorted(set().union(*[set(h1.ts) for h1, _ in brut.values()]))
        return brut, np.array(index)

    def matrices(self, brut, index, donchian):
        """Aligne tous les marchés sur un même axe horaire -> matrices numpy (temps x marchés)."""
        cfg = dict(self.cfg, donchian=donchian)
        cols = ["high", "low", "close", "atr", "volume", "vol_moy", "tendance", "haut_n", "bas_n"]
        M = {c: np.full((len(index), len(brut)), np.nan) for c in cols}
        pos = {t: i for i, t in enumerate(index)}
        for j, (sym, (h1, h4)) in enumerate(brut.items()):
            d = preparer(h1, h4, cfg)
            rows = [pos[t] for t in d.ts]
            for c in cols: M[c][rows, j] = d[c].values
        return M

    def simuler(self, M, syms, t0, t1, donchian, trailing, stop_x):
        """Simulation rapide du portefeuille (mêmes règles que le bot en direct), frais + glissement inclus."""
        cfg = self.cfg; f, g = cfg["frais"], cfg["glissement"]
        eq, eq_max, dd = cfg["capital_initial"], cfg["capital_initial"], 0.0
        pos, res = {}, []
        H, L, C, A, V, VM, TD, HN, BN = (M[k] for k in ("high", "low", "close", "atr", "volume", "vol_moy", "tendance", "haut_n", "bas_n"))
        for t in range(t0, t1):
            for j in list(pos):                                 # gestion des positions
                p = pos[j]; s = p["s"]
                if np.isnan(C[t, j]): continue
                sortie = None
                if (L[t, j] <= p["stop"]) if s == 1 else (H[t, j] >= p["stop"]):
                    sortie = p["stop"]
                elif TD[t, j] == -s:
                    sortie = C[t, j]
                else:
                    p["best"] = max(p["best"], H[t, j]) if s == 1 else min(p["best"], L[t, j])
                    nv = p["best"] - s * trailing * A[t, j]
                    if (nv - p["stop"]) * s > 0: p["stop"] = nv
                if sortie is not None:
                    px = sortie * (1 - s * g)
                    pnl = (px - p["e"]) * p["q"] * s - p["q"] * (px + p["e"]) * f
                    eq += pnl; res.append(pnl); del pos[j]
            for j in range(len(syms)):                          # nouvelles entrées
                if j in pos or len(pos) >= cfg["max_positions"]: continue
                c, a = C[t, j], A[t, j]
                if np.isnan(c) or np.isnan(HN[t, j]) or np.isnan(VM[t, j]) or not a > 0 or a / c > cfg["atr_max_pct"]: continue
                if not V[t, j] > cfg["volume_x"] * VM[t, j]: continue
                s = 1 if (TD[t, j] == 1 and c > HN[t, j]) else -1 if (TD[t, j] == -1 and c < BN[t, j]) else 0
                if not s or sum(1 for p in pos.values() if p["s"] == s) >= cfg["max_meme_sens"]: continue
                e = c * (1 + s * g)
                q = eq * cfg["risque_pct"] / (stop_x * a)
                expo = sum(p["q"] * p["e"] for p in pos.values())
                q = min(q, max(0.0, cfg["levier_max"] * eq - expo) / e)    # le levier total reste <= x5
                if q * e < cfg["min_notionnel"]: continue
                pos[j] = dict(s=s, e=e, q=q, stop=e - s * stop_x * a, best=e)
            latent = sum((C[t, j] - p["e"]) * p["q"] * p["s"] for j, p in pos.items() if not np.isnan(C[t, j]))
            eq_max = max(eq_max, eq + latent); dd = max(dd, 1 - (eq + latent) / eq_max)
        for j, p in pos.items():                                # clôture en fin de période
            c = C[t1 - 1, j]
            if not np.isnan(c): res.append((c - p["e"]) * p["q"] * p["s"] - p["q"] * (c + p["e"]) * f); eq += res[-1]
        gains = [x for x in res if x > 0]; pertes = [x for x in res if x <= 0]
        return dict(trades=len(res), win=len(gains) / max(1, len(res)), gain_moy=np.mean(gains) if gains else 0,
                    perte_moy=np.mean(pertes) if pertes else 0,
                    pf=(sum(gains) / -sum(pertes)) if pertes and sum(pertes) else float("inf"),
                    dd=dd, ret=eq / cfg["capital_initial"] - 1)

    def ligne(self, nom, r):
        return (f"  {nom:<34} trades {r['trades']:>4} | gagnants {r['win']:.0%} | gain moy {r['gain_moy']:+7.1f} $ | "
                f"perte moy {r['perte_moy']:+6.1f} $ | PF {r['pf']:.2f} | drawdown -{r['dd']:.0%} | résultat {r['ret']:+.1%}")

    def backtest(self):
        brut, index = self.charger()
        syms = list(brut)
        if len(syms) < 3 or len(index) < 1000:
            log("ARRÊT : pas assez de données téléchargées pour un backtest fiable."); return
        coupe = int(len(index) * self.cfg["part_optimisation"])
        jours = lambda a, b: (index[b - 1] - index[a]) / 86400000
        log(f"Données : {len(syms)} marchés, {len(index)} heures. Réglage sur {jours(0, coupe):.0f} jours, "
            f"test sur les {jours(coupe, len(index)):.0f} derniers jours (jamais vus pendant le réglage).")
        grille = self.cfg["grille"]
        cache, essais = {}, []
        for dc in grille["donchian"]:
            cache[dc] = self.matrices(brut, index, dc)
            for tr in grille["trailing_atr"]:
                for sx in grille["stop_atr"]:
                    r = self.simuler(cache[dc], syms, 0, coupe, dc, tr, sx)
                    essais.append(((dc, tr, sx), r))
                    log(f"   réglage donchian {dc} / suiveur {tr} ATR / stop {sx} ATR -> PF {r['pf']:.2f}, "
                        f"{r['trades']} trades, {r['ret']:+.1%}")
        valides = [e for e in essais if e[1]["trades"] >= 30]
        (dc, tr, sx), r_is = max(valides or essais, key=lambda e: e[1]["pf"])
        d0 = (self.cfg["donchian"], self.cfg["trailing_atr"], self.cfg["stop_atr"])
        r_oos = self.simuler(cache[dc], syms, coupe, len(index), dc, tr, sx)
        r_def = self.simuler(cache.get(d0[0]) or self.matrices(brut, index, d0[0]), syms, coupe, len(index), *d0)
        r_all = self.simuler(cache[dc], syms, 0, len(index), dc, tr, sx)
        log("=" * 100)
        log(f"RÉSULTAT — risque {self.cfg['risque_pct']:.1%} par trade, frais + glissement inclus")
        log(f"Meilleurs réglages trouvés sur les 8 premiers mois : donchian {dc}, suiveur {tr} ATR, stop {sx} ATR")
        log(self.ligne("1. Réglage (8 mois, données vues)", r_is))
        log(self.ligne("2. TEST (4 mois, données JAMAIS vues)", r_oos))
        log(self.ligne("   réglages par défaut, même test", r_def))
        log(self.ligne("3. Sur les 12 mois", r_all))
        log("=" * 100)
        if r_oos["pf"] >= 1.3 and r_oos["ret"] > 0:
            verdict = "SOLIDE sur les données jamais vues : simulation en direct recommandée avant tout argent réel."
        elif r_oos["pf"] >= 1.0:
            verdict = "FRAGILE : légèrement positif hors échantillon, avantage faible. Pas d'argent réel."
        else:
            verdict = "NÉGATIF sur les données jamais vues : la stratégie ne tient pas. NE PAS lancer."
        log(f"VERDICT : {verdict}")
        if r_oos["pf"] >= 1.0:
            with open(self.cfg["params_fichier"], "w") as fh:
                json.dump({"donchian": dc, "trailing_atr": tr, "stop_atr": sx, "pf_test": round(r_oos["pf"], 3),
                           "date": datetime.now().isoformat(timespec="seconds")}, fh)
            log(f"Réglages enregistrés dans {self.cfg['params_fichier']} : le bot en direct les utilisera.")

    # ---------- BOUCLE EN DIRECT ----------
    def surveillance(self):
        while self.vivant:
            try:
                with self.verrou:
                    syms = list(self.positions)
                for sym in syms:
                    o = self.ex_surv.fetch_ohlcv(sym, "1m", limit=2)
                    with self.verrou:
                        if sym in self.positions:
                            self.gerer(sym, max(x[2] for x in o), min(x[3] for x in o), o[-1][4])
                with self.verrou:
                    self.verifier_mort()
                self.sauver()
            except Exception as e:
                log(f"Erreur surveillance: {e}")
            time.sleep(self.cfg["surveillance_s"])

    def scan(self):
        liste = self.marches()
        signaux, refus = [], {}
        for sym in liste:
            try:
                d = self.donnees(sym)
                r = d.iloc[-2]                                   # dernière bougie 1h CLÔTURÉE
                with self.verrou:
                    if sym in self.positions:
                        self.gerer(sym, r.high, r.low, d.iloc[-1].close, r.atr, r.tendance)
                        continue
                s = signal(r, self.cfg)
                if s:
                    signaux.append((sym, s, float(d.iloc[-1].close), float(r.atr)))
                else:
                    cle = "pas de tendance 4h" if r.tendance == 0 else "pas de cassure"
                    refus[cle] = refus.get(cle, 0) + 1
            except Exception as e:
                refus["erreur données"] = refus.get("erreur données", 0) + 1
        ouverts = 0
        for sym, s, prix, a in signaux:
            with self.verrou:
                if self.peut_ouvrir(s):
                    self.ouvrir(sym, s, prix, a); ouverts += 1
        self.analyse = {"heure": datetime.now().isoformat(timespec="seconds"), "nb_paires": len(liste),
                        "candidats": [{"paire": x[0], "sens": "long" if x[1] == 1 else "short", "score": 0, "sentiment": 0,
                                       "decision": "OK"} for x in signaux], "checks": {}}
        log(f"🔎 {len(liste)} marchés analysés | {len(signaux)} cassure(s) | {ouverts} ouverture(s) | "
            + " | ".join(f"{v} {k}" for k, v in refus.items()))

    def charger_params(self):
        try:
            with open(self.cfg["params_fichier"]) as fh:
                p = json.load(fh)
            for k in ("donchian", "trailing_atr", "stop_atr"): self.cfg[k] = p[k]
            log(f"Réglages issus du backtest : donchian {p['donchian']}, suiveur {p['trailing_atr']} ATR, "
                f"stop {p['stop_atr']} ATR (PF hors échantillon {p['pf_test']})")
        except FileNotFoundError:
            log("Aucun réglage de backtest trouvé : réglages par défaut. Lance d'abord le backtest.")
        except Exception as e:
            log(f"Réglages de backtest illisibles ({e}) : réglages par défaut.")

    def live_loop(self):
        self.charger_params()
        log(f"Démarrage {'LIVE ⚠️' if self.cfg['live'] else 'PAPER'} | SUIVI DE TENDANCE 1h/4h | capital {self.capital:.0f} $ | "
            f"risque {self.cfg['risque_pct']:.0%} par trade | {self.cfg['max_positions']} positions max")
        threading.Thread(target=self.surveillance, daemon=True).start()
        while self.vivant:
            try:
                self.scan()
            except Exception as e:
                log(f"Erreur scan: {e}")
            time.sleep(self.cfg["scan_min"] * 60)
        self.sauver()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()
    bot = Bot(CONFIG)
    bot.backtest() if a.backtest else bot.live_loop()
