"""
Survival Trading Bot — SCALPER (simulation uniquement)
======================================================
Trades très courts (quelques secondes à 3 minutes) sur les marchés les plus liquides de Binance Futures :
  - Lit TOUS les prix toutes les 3 secondes (un seul appel API).
  - Entre quand le prix accélère fortement dans un sens (mouvement sur 15 s ET sur 60 s dans le même sens).
  - Positions de 5000 $ (x10 -> 500 $ de marge), plusieurs en même temps.
  - Sort dès qu'un gain "sympa" est atteint, ou s'il redonne trop de son meilleur gain, ou au stop, ou après 3 min.
  - Recommence en boucle toute la journée.

AVERTISSEMENT : le backtest de cette stratégie n'a PAS été fait. Les tests de scalping du 15/10 ont été
perdants (62 trades, 18 % de gagnants, -776 $). Chaque aller-retour de 5000 $ coûte ~5 $ de frais.
Ce module est verrouillé en SIMULATION.

    python bot/scalper.py
"""
import json, os, time
from collections import defaultdict, deque
from datetime import datetime, timezone

import ccxt

CONFIG = {
    "capital_initial": 10000.0,
    "seuil_mort": 10.0,
    # --- TAILLE PAR LE RISQUE (comme un trader pro) ---
    "risque_min_pct": 0.001,         # confiance faible  -> 0,1 % du capital si le stop est touché
    "risque_max_pct": 0.004,         # confiance maximale -> 0,4 % (100 positions x 0,4 % = 40 % du capital en jeu)
    "stop_bruit_x": 3.0,             # stop placé à 3x le bruit normal du marché (mesuré sur ses 3 dernières minutes)
    "horizon_s": 300,                # bruit mesuré sur l'horizon du trade (5 min)
    "stop_min_pct": 0.004,           # stop jamais plus serré que 0,4 % du prix
    "levier_max": 125,               # VRAI levier : le maximum autorisé par Binance sur chaque marché (jusqu'à x125)
    "z_min": 0.5,                    # tout marché qui bouge clairement dans un sens est candidat ; les meilleurs passent en premier
    "z_max": 5.0,
    "max_positions": 100,            # jusqu'à 100 trades en même temps
    "max_par_categorie": 100,
    "max_meme_sens": 70,
    "ouvertures_par_cycle": 4,        # il ouvre progressivement (4 toutes les 3 s), pas tout d'un coup
    "mise_min_x": 0.4, "mise_max_x": 4.0,   # mise = 0,4x à 4x une part (capital/50) selon la confiance
    "btc_choc_pct": 0.006,           # BTC bouge de 0,6 % en 5 min = nouvelle importante : on ne va pas contre
    "tendance_maj_s": 600,           # tendances 1h recalculées toutes les 10 min             # au plus 70 positions dans le même sens : amortisseur si le marché se retourne          # au plus 3 positions dans la même catégorie (crypto, actions...) : pas de pari unique
    "echauffement_s": 60,           # aucun trade avant 3 min de données sur un marché (bruit mal mesuré sinon)
    "actions_heures_us": True,       # actions uniquement quand la Bourse US est ouverte (15h30-22h heure de Paris)
    "volume_min_24h": 5_000_000,
    "intervalle_s": 3,
    # --- GESTION DE LA POSITION (en multiples du risque R) ---
    "objectif_R": 1.5,               # vise 2,5 fois le risque
    "protection_des_R": 0.5,         # rien n'est protégé avant +1R ; ensuite on garde au moins 50 % du meilleur gain
    "garde_pct": 0.5,
    "retournement_z": 3.0,           # en perte, on coupe avant le stop seulement si le marché se retourne vraiment
    "duree_max_s": 0,                # plus de limite de temps : il sort quand son analyse le dit
    "cooldown_s": 30,
    "frais": 0.0005,                 # taker 0,05 % par ordre (entrée + sortie)
    "etat_fichier": "bot_state_v2.json",
}

LOGS = deque(maxlen=300)


def log(m):
    l = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {m}"
    LOGS.append(l); print(l, flush=True)


class Scalper:
    def __init__(self, cfg, ex=None):
        self.cfg = cfg
        self.ex = ex or ccxt.binanceusdm({"enableRateLimit": True, "timeout": 10000})
        self.capital = cfg["capital_initial"]
        self.positions, self.trades, self.historique = {}, [], []
        self.prix = defaultdict(lambda: deque(maxlen=120))     # (t, prix) des ~3 dernières minutes
        self.cooldown, self.frais_payes = {}, 0.0
        self.courbe = [(time.time(), self.capital)]
        self.vivant, self.t_bilan, self.liste = True, time.time(), []
        self.tendance = {}                                      # +1 haussier / -1 baissier / 0 indécis (bougies 1h)
        self.t_tendance = 0

    # ---------- marchés & prix ----------
    def choisir_marches(self):
        """Tout le marché : chaque perpétuel USDT de Binance encore actif."""
        try:
            if not getattr(self.ex, "markets", None): self.ex.load_markets()
        except Exception:
            pass
        t = self.ex.fetch_tickers()
        self.liste = [s for s, v in t.items() if s.endswith("/USDT:USDT") and s.split("/")[0].isascii()
                      and (v.get("quoteVolume") or 0) >= self.cfg["volume_min_24h"]]
        log(f"Lecture de TOUT le marché : {len(self.liste)} contrats suivis toutes les {self.cfg['intervalle_s']} s")

    def lire_prix(self):
        t, now = self.ex.fetch_tickers(), time.time()          # un seul appel = tout le marché
        self.tickers = t
        for s, v in t.items():
            if s in self.liste and v.get("last"): self.prix[s].append((now, float(v["last"])))

    def categorie(self, s):
        try:
            info = self.ex.market(s).get("info", {}) or {}
            t = (info.get("underlyingType") or "").upper()
            if t in ("EQUITY", "STOCK"): return "actions"
            if t == "COMMODITY": return "matières premières"
            if t == "INDEX": return "indices"
        except Exception:
            pass
        return "crypto"

    def bourse_us_ouverte(self):
        """Lun-ven, 13h30-20h UTC (15h30-22h à Paris l'été)."""
        u = datetime.now(timezone.utc)
        return u.weekday() < 5 and (13 * 60 + 30) <= u.hour * 60 + u.minute < 20 * 60

    def calc_tendance(self, closes):
        """Tendance de fond sur bougies 1h : prix > EMA20 > EMA50 = haussier, l'inverse = baissier."""
        def ema(v, n):
            k, e = 2 / (n + 1), v[0]
            for x in v[1:]: e = x * k + e * (1 - k)
            return e
        if len(closes) < 50: return 0
        e20, e50, px = ema(closes, 20), ema(closes, 50), closes[-1]
        if px > e20 > e50: return 1
        if px < e20 < e50: return -1
        return 0

    def maj_tendances(self, ex=None):
        """Lit 60 bougies 1h de chaque marché (thread séparé, ne bloque pas le trading)."""
        ex = ex or ccxt.binanceusdm({"enableRateLimit": True, "timeout": 10000})
        n = 0
        for s in list(self.liste):
            try:
                o = ex.fetch_ohlcv(s, "1h", limit=60)
                self.tendance[s] = self.calc_tendance([float(b[4]) for b in o]); n += 1
            except Exception:
                pass
        h = sum(1 for v in self.tendance.values() if v == 1); b = sum(1 for v in self.tendance.values() if v == -1)
        log(f"📈 Tendances 1h à jour : {h} marchés haussiers, {b} baissiers, {n - h - b} sans tendance (ignorés)")

    def choc_btc(self):
        """Proxy des news : un mouvement brutal du BTC traduit une annonce. Renvoie +1 / -1 / 0."""
        v = self.variation("BTC/USDT:USDT", 300)
        if v is None: return 0
        return 1 if v > self.cfg["btc_choc_pct"] else -1 if v < -self.cfg["btc_choc_pct"] else 0

    def levier_marche(self, s):
        try:
            m = (self.ex.market(s).get("limits", {}).get("leverage", {}) or {}).get("max")
            return min(self.cfg["levier_max"], int(m)) if m else self.cfg["levier_max"]
        except Exception:
            return self.cfg["levier_max"]

    def classer(self):
        """Compare chaque marché à TOUS les autres au même instant et calcule une confiance de 0 à 1."""
        import math
        v15 = {s: self.variation(s, 15) for s in self.liste}
        v60 = {s: self.variation(s, 60) for s in self.liste}
        ok = [s for s in self.liste if v15[s] is not None and v60[s] is not None]
        if len(ok) < 20: return []
        def ecart(vals):
            m = sum(vals) / len(vals); return math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals)) or 1e-9
        e15, e60 = ecart([v15[s] for s in ok]), ecart([v60[s] for s in ok])
        vols = sorted((self.tickers.get(s, {}).get("quoteVolume") or 0) for s in ok)
        c = self.cfg; res = []
        self.zmap = {s: (v15[s] / e15, v60[s] / e60) for s in ok}
        for s in ok:
            z15, z60 = v15[s] / e15, v60[s] / e60
            if z15 * z60 <= 0: continue                         # les deux horizons doivent aller dans le même sens
            z = min(abs(z15), abs(z60))
            if z < c["z_min"]: continue
            sens = 1 if z60 > 0 else -1
            var24 = (self.tickers.get(s, {}).get("percentage") or 0) / 100
            bonus_tendance = (0.1 if var24 * sens > 0 else -0.1) + (0.2 if self.tendance.get(s) == sens else 0)
            rang_vol = vols.index(self.tickers.get(s, {}).get("quoteVolume") or 0) / max(1, len(vols) - 1)
            conf = (z - c["z_min"]) / (c["z_max"] - c["z_min"]) * 0.75 + 0.25 * rang_vol + bonus_tendance
            res.append((max(0.0, min(1.0, conf)), s, sens, v15[s], v60[s], z))
        return sorted(res, reverse=True)

    def bruit(self, s):
        """Bruit normal du marché sur l'horizon du trade : écart-type des variations de 3 s, mis à l'échelle."""
        import math
        h = [p for _, p in self.prix[s]]
        if len(h) < 20: return None
        r = [h[i] / h[i - 1] - 1 for i in range(1, len(h))]
        m = sum(r) / len(r); sd = math.sqrt(sum((x - m) ** 2 for x in r) / len(r))
        return sd * math.sqrt(self.cfg["horizon_s"] / self.cfg["intervalle_s"])

    def variation(self, s, secondes):
        h = self.prix[s]
        if len(h) < 2: return None
        now, dernier = h[-1]
        anciens = [p for t, p in h if now - t >= secondes]
        return dernier / anciens[-1] - 1 if anciens else None

    # ---------- comptabilité ----------
    def pnl_net(self, p, prix):
        brut = (prix - p["entree"]) * p["qty"] * p["s"]
        return brut - p["qty"] * (p["entree"] + prix) * self.cfg["frais"]

    def equite(self):
        return self.capital + sum(p["marge"] + self.pnl_net(p, p["prix"]) + p["qty"] * p["entree"] * self.cfg["frais"]
                                  for p in self.positions.values())

    # ---------- trading ----------
    def ouvrir(self, s, sens, prix, v15, v60, conf=0.0, z=0.0):
        """Taille par le risque : 1) combien j'accepte de perdre (selon la confiance), 2) où le stop doit être pour
        ne pas sauter sur le bruit normal, 3) j'en déduis la taille de la position, puis le levier nécessaire."""
        import math
        c = self.cfg
        b = self.bruit(s)
        if not b: return
        stop_pct = max(c["stop_min_pct"], c["stop_bruit_x"] * b)
        eq = self.equite()
        # VRAI levier : le max du marché, réduit seulement si la liquidation tomberait avant le stop
        lev = max(1, min(self.levier_marche(s), int(1 / (1.3 * stop_pct + 0.005))))   # liquidation toujours AU-DELÀ du stop
        # marge selon la confiance : 0,5x à 1,5x une part égale (capital / 100 positions)
        part = eq * 0.95 / 50
        marge = min(part * (c["mise_min_x"] + (c["mise_max_x"] - c["mise_min_x"]) * conf ** 1.5), self.capital * 0.95)
        N = marge * lev
        risque = N * (stop_pct + 2 * c["frais"])           # perte si le stop est touché
        if marge < 5 or N < 20: return
        qty = N / prix
        stop = prix * (1 - sens * stop_pct)
        self.capital -= marge + N * c["frais"]; self.frais_payes += N * c["frais"]
        self.positions[s] = dict(sym=s, s=sens, sens="long" if sens == 1 else "short", qty=qty, entree=prix, prix=prix,
                                 marge=marge, lev=lev, N=N, R=risque, stop=stop, conf=round(conf, 2), proba=conf,
                                 categorie=self.categorie(s),
                                 t_ouv=time.time(), pic=0.0, cible=prix * (1 + sens * stop_pct * c["objectif_R"]),
                                 liq=prix * (1 - sens * (1 / lev - 0.005)), type=self.categorie(s),
                                 ouverture=datetime.now().isoformat(timespec="seconds"), perte_prevue=round(risque, 2),
                                 gain_prevu=round(risque * c["objectif_R"], 2), duree_min=None)
        log(f"🟢 {'LONG' if sens == 1 else 'SHORT'} {s.split('/')[0]} | confiance {conf:.0%} (z={z:.1f}) | bruit {b:.2%} "
            f"-> stop à {stop_pct:.2%} | risque {risque:.0f} $ -> position {N:.0f} $ x{lev} (marge {marge:.0f} $) "
            f"| vise +{risque * c['objectif_R']:.0f} $")

    def fermer(self, s, prix, raison):
        p = self.positions.pop(s)
        frais_sortie = p["qty"] * prix * self.cfg["frais"]
        brut = (prix - p["entree"]) * p["qty"] * p["s"]
        self.capital += p["marge"] + brut - frais_sortie
        self.frais_payes += frais_sortie
        net = self.pnl_net(p, prix)
        self.historique.append(net)
        self.cooldown[s] = time.time() + self.cfg["cooldown_s"]
        duree = time.time() - p["t_ouv"]
        self.trades.append({"paire": s, "sens": p["sens"], "levier": p["lev"], "entree": p["entree"], "sortie": prix,
                            "pnl": round(net, 2), "raison": f"{raison} ({duree:.0f} s)",
                            "heure": datetime.now().isoformat(timespec="seconds")})
        log(f"{'💰' if net > 0 else '🔴'} {s.split('/')[0]} {net:+.2f} $ en {duree:.0f} s ({raison}) | équité {self.equite():.2f} $")

    def cycle(self):
        self.lire_prix()
        c = self.cfg
        candidats = self.classer()                           # met aussi à jour zmap pour les positions ouvertes
        for s in list(self.positions):                       # gestion des positions
            p = self.positions[s]
            if not self.prix[s]: continue
            p["prix"] = self.prix[s][-1][1]
            net = self.pnl_net(p, p["prix"]); p["pic"] = max(p["pic"], net); R_ = p["R"]
            zp = getattr(self, "zmap", {}).get(s)
            contre = zp is not None and min(-zp[0] * p["s"], -zp[1] * p["s"]) >= c["retournement_z"]
            if (p["prix"] - p["liq"]) * p["s"] <= 0:
                self.fermer(s, p["liq"], "LIQUIDÉ"); continue
            if (p["prix"] - p["stop"]) * p["s"] <= 0:
                self.fermer(s, p["prix"], f"stop ({net / R_:+.1f} R)")
            elif net >= R_ * c["objectif_R"]:
                self.fermer(s, p["prix"], f"objectif atteint ({net / R_:+.1f} R)")
            elif p["pic"] >= R_ * c["protection_des_R"] and net <= max(0.3 * R_, p["pic"] * c["garde_pct"]):
                self.fermer(s, p["prix"], f"gain protégé (max {p['pic']:+.0f} $ = {p['pic'] / R_:.1f} R)")
            elif net <= -0.6 * R_ and contre:
                self.fermer(s, p["prix"], f"retournement confirmé sur tout le marché ({net / R_:+.1f} R)")
            elif net >= 0.3 * R_ and zp is not None and zp[0] * p["s"] < -1.0 and zp[1] * p["s"] < 0:
                self.fermer(s, p["prix"], f"élan épuisé, gain encaissé ({net / R_:+.1f} R)")
            elif self.tendance.get(s) == -p["s"]:
                self.fermer(s, p["prix"], f"tendance 1h retournée ({net / R_:+.1f} R)")
            elif (time.time() - p["t_ouv"] >= 300 and abs(net) < 0.1 * R_ and zp is not None
                  and abs(zp[1]) < 0.2):
                self.fermer(s, p["prix"], f"plus aucun mouvement, place libérée ({net / R_:+.1f} R)")
        us = self.bourse_us_ouverte()
        diag = {"candidats": len(candidats), "en démarrage": 0,
                "action hors séance US": 0, "déjà en position / pause": 0,
                "contre la tendance 1h": 0, "contre un choc BTC (news)": 0, "ouverts": 0}
        choc = self.choc_btc()
        if choc: diag["⚡ choc BTC"] = "hausse" if choc > 0 else "baisse"
        for conf, s, sens, v15, v60, z in candidats:     # meilleurs candidats de TOUT le marché d'abord
            if len(self.positions) >= c["max_positions"]: break
            if s in self.positions or self.cooldown.get(s, 0) > time.time(): diag["déjà en position / pause"] += 1; continue
            h = self.prix[s]
            if not h or h[-1][0] - h[0][0] < c["echauffement_s"]: diag["en démarrage"] += 1; continue
            cat = self.categorie(s)
            if cat == "actions" and c["actions_heures_us"] and not us: diag["action hors séance US"] += 1; continue
            if sum(1 for p in self.positions.values() if p.get("type") == cat) >= c["max_par_categorie"]: continue
            if sum(1 for p in self.positions.values() if p["s"] == sens) >= c["max_meme_sens"]: continue  # pas 100 paris identiques
            if self.tendance and self.tendance.get(s, 0) != sens: diag["contre la tendance 1h"] += 1; continue
            if choc == -sens: diag["contre un choc BTC (news)"] += 1; continue
            if diag["ouverts"] >= c["ouvertures_par_cycle"]: break
            n0 = len(self.positions)
            self.ouvrir(s, sens, self.prix[s][-1][1], v15, v60, conf, z)
            if len(self.positions) > n0: diag["ouverts"] += 1
        if self.equite() <= c["seuil_mort"]:
            for s in list(self.positions): self.fermer(s, self.positions[s]["prix"], "MORT")
            self.vivant = False; log("💀 LE BOT EST MORT.")
        if time.time() - getattr(self, "t_diag", 0) >= 30:
            self.t_diag = time.time()
            age = max((h[-1][0] - h[0][0]) for h in self.prix.values() if h) if self.prix else 0
            reste = max(0, c["echauffement_s"] - age)
            log(("⏳ Démarrage : encore " + f"{reste:.0f} s de mesure avant le premier trade | " if reste > 0 else "🔍 ")
                + f"{len(self.positions)} position(s) ouverte(s) | " + " | ".join(f"{k} : {v}" for k, v in diag.items()))
        if time.time() - self.t_bilan >= 300:
            self.bilan(); self.t_bilan = time.time()

    def bilan(self):
        h = self.historique; g = [x for x in h if x > 0]
        log(f"📊 BILAN : {len(h)} trades | {len(g)} gagnants ({len(g) / max(1, len(h)):.0%}) | résultat {sum(h):+.2f} $ | "
            f"FRAIS PAYÉS {self.frais_payes:.2f} $ | équité {self.equite():.2f} $ | {len(self.positions)} ouverte(s)")

    def sauver(self):
        eq = self.equite(); self.courbe.append((time.time(), eq)); self.courbe = self.courbe[-3000:]
        pos = [dict(p, pnl_latent=round(self.pnl_net(p, p["prix"]), 2)) for p in self.positions.values()]
        etat = {"maj": datetime.now().isoformat(timespec="seconds"), "mode": "PAPER (scalper)",
                "capital_initial": self.cfg["capital_initial"], "seuil_mort": self.cfg["seuil_mort"],
                "capital": self.capital, "equite": eq, "positions": pos, "max_positions": self.cfg["max_positions"],
                "vivant": self.vivant, "historique": self.historique, "trades": self.trades[-300:], "courbe": self.courbe,
                "analyse": {"heure": datetime.now().isoformat(timespec="seconds"), "nb_paires": len(self.liste),
                            "candidats": [], "checks": {}}, "logs": list(LOGS)[-200:], "file_candidats": 0}
        tmp = self.cfg["etat_fichier"] + ".tmp"
        with open(tmp, "w") as f: json.dump(etat, f, default=str)
        os.replace(tmp, self.cfg["etat_fichier"])

    def boucle(self):
        c = self.cfg
        log(f"SCALPER v7 (100 positions, sans limite de temps, tendance 1h + chocs BTC, mises selon confiance) — SIMULATION UNIQUEMENT | tout le marché Binance | jusqu'à {c['max_positions']} "
            f"positions simultanées | levier jusqu'à x{c['levier_max']} | marge par trade selon la confiance "
            f"| actions seulement pendant la séance US (15h30-22h)")
        self.choisir_marches()
        if "BTC/USDT:USDT" not in self.liste: self.liste.append("BTC/USDT:USDT")
        import threading
        log("Lecture des tendances 1h de chaque marché (1 à 3 min)... aucun trade avant la fin.")
        self.maj_tendances(); self.t_tendance = time.time()
        t_liste = time.time()
        while self.vivant:
            try:
                if time.time() - self.t_tendance > c["tendance_maj_s"]:
                    self.t_tendance = time.time(); threading.Thread(target=self.maj_tendances, daemon=True).start()
                if time.time() - t_liste > 1800: self.choisir_marches(); t_liste = time.time()
                self.cycle(); self.sauver()
            except Exception as e:
                log(f"Erreur : {type(e).__name__}: {str(e)[:150]}")
            time.sleep(self.cfg["intervalle_s"])
        self.bilan(); self.sauver()


if __name__ == "__main__":
    Scalper(CONFIG).boucle()
