"""
TENDANCE LENTE — actions, or/métaux, devises, indices, crypto
=============================================
Suivi de tendance sur bougies JOURNALIÈRES : peu de trades, tenus des jours ou des semaines,
levier total limité à x5, frais faibles. C'est la famille de stratégies utilisée par les fonds
"managed futures" depuis des décennies.

  python bot/tendance_lente.py backtest   -> 20 ans de données, réglage 2005-2016, TEST 2017-aujourd'hui
  python bot/tendance_lente.py paper      -> simulation quotidienne avec les réglages validés

Règles :
  ENTRÉE  achat si clôture > plus haut des N derniers jours ET > moyenne 200 jours (vente : l'inverse)
  STOP    suiveur à k x ATR(20) sous le meilleur cours depuis l'entrée (au-dessus pour une vente)
  TAILLE  perte au stop = 0,5 % du capital ; position <= 100 % du capital ; total des positions <= x5
  COÛTS   0,05 % par ordre + 0,02 %/jour de financement (funding) sur chaque position

SIMULATION UNIQUEMENT : aucun ordre réel n'est envoyé. Ceci n'est pas un conseil financier.
"""
import json, os, sys, itertools
from datetime import datetime
import numpy as np
import pandas as pd

UNIVERS = {
    "actions": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "XOM", "JNJ",
                "WMT", "KO", "INTC", "AMD", "NFLX", "BA", "CAT", "PG", "V", "UNH"],
    "or & métaux": ["GC=F", "SI=F", "HG=F"],
    "devises": ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCHF=X", "USDCAD=X", "NZDUSD=X"],
    "indices": ["^GSPC", "^NDX"],
    "crypto": ["BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "XRP-USD", "ADA-USD", "DOGE-USD",
               "AVAX-USD", "LINK-USD", "LTC-USD", "DOT-USD", "TRX-USD"],
}
NOMS = {"GC=F": "OR", "SI=F": "ARGENT", "HG=F": "CUIVRE", "^GSPC": "S&P500", "^NDX": "NASDAQ100"}

CFG = {
    "capital_initial": 10000.0, "risque": 0.005, "max_position": 1.0, "levier_total_max": 5.0,
    "frais": 0.0005, "funding_jour": 0.0002, "funding_crypto_jour": 0.0003, "ma_longue": 200, "atr_n": 20,
    "grille_entree": [20, 55, 100], "grille_k": [2.0, 3.0, 4.0],
    "debut": "2005-01-01", "fin_reglage": "2016-12-31",
    "seuil_sentiment": 0.15,          # news nettement contraires (score <= -0,15 pour un achat) = on n'entre pas
    "params_fichier": "params_tendance.json", "etat_fichier": "bot_state_v2.json",
    "paper_fichier": "paper_tendance.json",
}


def nom(t): return NOMS.get(t, t.replace("=X", "").replace("-USD", ""))
def categorie(t): return next(c for c, l in UNIVERS.items() if t in l)


# ---------------- données ----------------
def telecharger(debut):
    import yfinance as yf
    tick = [t for l in UNIVERS.values() for t in l]
    print(f"Téléchargement de {len(tick)} marchés depuis {debut} (Yahoo Finance)...", flush=True)
    d = yf.download(tick, start=debut, auto_adjust=True, group_by="ticker", progress=False, threads=True)
    out = {}
    for t in tick:
        try:
            x = d[t][["Open", "High", "Low", "Close"]].dropna()
            if len(x) > 230: out[t] = x
        except Exception:
            pass
    print(f"{len(out)} marchés avec assez d'historique.", flush=True)
    return out


def indicateurs(df, n_in, cfg):
    o, h, l, c = (df[k].values for k in ("Open", "High", "Low", "Close"))
    tr = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)), abs(l - np.roll(c, 1)))); tr[0] = h[0] - l[0]
    atr = pd.Series(tr).rolling(cfg["atr_n"]).mean().values
    ma = pd.Series(c).rolling(cfg["ma_longue"]).mean().values
    hh = pd.Series(c).rolling(n_in).max().shift(1).values
    ll = pd.Series(c).rolling(n_in).min().shift(1).values
    return o, c, atr, ma, hh, ll


# ---------------- simulation portefeuille ----------------
def simuler(data, n_in, k, cfg, debut=None, fin=None, journal=False, vetos=None):
    """Signal sur la clôture du jour J, exécution à l'ouverture de J+1 (pas de triche sur le futur)."""
    dates = sorted(set().union(*[set(df.index) for df in data.values()]))
    dates = [d for d in dates if (debut is None or d >= pd.Timestamp(debut)) and (fin is None or d <= pd.Timestamp(fin))]
    ind = {t: dict(zip(("o", "c", "atr", "ma", "hh", "ll"), indicateurs(df, n_in, cfg)), idx={d: i for i, d in enumerate(df.index)})
           for t, df in data.items()}
    eq, pos, trades, courbe, ordres = cfg["capital_initial"], {}, [], [], {}
    for d in dates:
        # 1) exécuter les ordres décidés hier, à l'ouverture
        for t, (act, sens, val) in list(ordres.items()):
            I = ind[t]; i = I["idx"].get(d)
            if i is None: continue
            o, cprev = I["o"][i], I["c"][i - 1]
            if act == "sortie" and t in pos:
                p = pos.pop(t); r = p["s"] * (o / cprev - 1); g = p["V"] * r - p["V"] * cfg["frais"]
                eq += g; p["pnl"] += g
                trades.append(dict(paire=nom(t), cat=categorie(t), sens="long" if p["s"] > 0 else "short", entree=p["e"],
                                   sortie=o, pnl=p["pnl"], jours=(d - p["d"]).days, ouverture=p["d"], heure=d))
            elif act == "entree" and t not in pos:
                eq -= val * cfg["frais"]
                pos[t] = dict(s=sens, V=val, e=o, d=d, pic=o, pnl=-val * cfg["frais"], ref=o, cry=categorie(t) == "crypto")
            del ordres[t]
        # 2) valoriser les positions à la clôture
        for t, p in pos.items():
            I = ind[t]; i = I["idx"].get(d)
            if i is None: continue
            c = I["c"][i]; r = p["s"] * (c / p["ref"] - 1); g = p["V"] * r - p["V"] * (cfg["funding_crypto_jour"] if p.get("cry") else cfg["funding_jour"])
            eq += g; p["pnl"] += g; p["V"] *= (1 + r); p["ref"] = c
            p["pic"] = max(p["pic"], c) if p["s"] > 0 else min(p["pic"], c)
        courbe.append((d, eq))
        if eq <= 0: break
        # 3) décider pour demain
        brut = sum(p["V"] for p in pos.values())
        for t, I in ind.items():
            i = I["idx"].get(d)
            if i is None or np.isnan(I["ma"][i]) or np.isnan(I["atr"][i]): continue
            c, atr = I["c"][i], I["atr"][i]
            if t in pos:
                p = pos[t]; stop = p["pic"] - p["s"] * k * atr
                if (c - stop) * p["s"] <= 0: ordres[t] = ("sortie", 0, 0)
                continue
            sens = 1 if (c > I["hh"][i] and c > I["ma"][i]) else -1 if (c < I["ll"][i] and c < I["ma"][i]) else 0
            if not sens: continue
            val = min(eq * cfg["risque"] / (k * atr / c), eq * cfg["max_position"])
            if brut + val > eq * cfg["levier_total_max"]: continue
            if vetos and vetos.get(str(d.date()), {}).get(t) == sens: continue   # refusé par l'analyse des news
            ordres[t] = ("entree", sens, val); brut += val
    return courbe, trades, pos, ordres


def stats(courbe, trades):
    if len(courbe) < 2: return {}
    s = pd.Series([e for _, e in courbe], index=[d for d, _ in courbe])
    ans = (s.index[-1] - s.index[0]).days / 365.25
    r = s.pct_change().dropna()
    g = sum(t["pnl"] for t in trades if t["pnl"] > 0); p = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    return dict(rendement_annuel=(s.iloc[-1] / s.iloc[0]) ** (1 / max(ans, 0.1)) - 1,
                drawdown_max=(s / s.cummax() - 1).min(),
                sharpe=r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0,
                profit_factor=g / p if p else float("inf"), trades=len(trades),
                gagnants=sum(1 for t in trades if t["pnl"] > 0) / max(1, len(trades)),
                final=s.iloc[-1])


def afficher(titre, st):
    print(f"  {titre:<38} rendement/an {st['rendement_annuel']:+7.1%} | pire baisse {st['drawdown_max']:7.1%} | "
          f"Sharpe {st['sharpe']:5.2f} | PF {st['profit_factor']:4.2f} | {st['trades']} trades, {st['gagnants']:.0%} gagnants")


def backtest(cfg=CFG):
    data = telecharger(cfg["debut"])
    fin_r = cfg["fin_reglage"]; debut_t = (pd.Timestamp(fin_r) + pd.Timedelta(days=1)).date().isoformat()
    print(f"\n=== 1) RÉGLAGE sur {cfg['debut'][:4]}-{fin_r[:4]} (le bot choisit ses paramètres ici) ===")
    res = []
    for n_in, k in itertools.product(cfg["grille_entree"], cfg["grille_k"]):
        st = stats(*simuler(data, n_in, k, cfg, fin=fin_r)[:2]); res.append((st["sharpe"], n_in, k, st))
        afficher(f"entrée {n_in} j, stop {k} ATR", st)
    _, n_in, k, _ = max(res, key=lambda x: x[0])
    print(f"\n=== 2) TEST sur {debut_t[:4]}-aujourd'hui : données JAMAIS vues pendant le réglage ===")
    print(f"  Réglage retenu : entrée sur plus haut/bas de {n_in} jours, stop suiveur {k} ATR")
    courbe, trades, _, _ = simuler(data, n_in, k, cfg, debut=debut_t)
    st = stats(courbe, trades); afficher("RÉSULTAT HORS ÉCHANTILLON", st)
    print("\n  Robustesse : tous les réglages sur la période de test (un bon système gagne avec la plupart)")
    oos = []
    for n2, k2 in itertools.product(cfg["grille_entree"], cfg["grille_k"]):
        s2 = stats(*simuler(data, n2, k2, cfg, debut=debut_t)[:2]); oos.append(s2["rendement_annuel"] > 0)
        afficher(f"entrée {n2} j, stop {k2} ATR", s2)
    if "^GSPC" in data:
        sp = data["^GSPC"]["Close"]; sp = sp[sp.index >= pd.Timestamp(debut_t)]
        bh = (sp.iloc[-1] / sp.iloc[0]) ** (365.25 / (sp.index[-1] - sp.index[0]).days) - 1
        print(f"\n  Référence : acheter et garder le S&P 500 = {bh:+.1%}/an sur la même période")
    print("\n  Par année :")
    s = pd.Series([e for _, e in courbe], index=[d for d, _ in courbe])
    for an, x in s.groupby(s.index.year): print(f"    {an} : {x.iloc[-1] / x.iloc[0] - 1:+7.1%}")
    print("\n  Par catégorie (test) :")
    for cat in UNIVERS:
        tt = [t for t in trades if t["cat"] == cat]
        if tt: print(f"    {cat:<14} {len(tt):4d} trades | résultat {sum(t['pnl'] for t in tt):+10.0f} $")
    ok = st["rendement_annuel"] > 0 and st["profit_factor"] > 1.1 and sum(oos) >= len(oos) * 2 / 3
    verdict = "POSITIF : le système a gagné sur des années qu'il n'avait jamais vues" if ok else \
              "NÉGATIF : ne pas utiliser, même en simulation prolongée"
    print(f"\n  VERDICT : {verdict}")
    json.dump(dict(n_entree=n_in, k=k, valide=bool(ok), date=datetime.now().isoformat(timespec="seconds"),
                   test=dict((a, float(b)) for a, b in st.items())), open(cfg["params_fichier"], "w"), indent=2)
    print(f"  Réglages sauvegardés dans {cfg['params_fichier']}")


# ---------------- simulation quotidienne (paper) ----------------
def paper(cfg=CFG):
    if not os.path.exists(cfg["params_fichier"]):
        print("Lance d'abord le backtest (backtest_tendance.bat)."); return
    pr = json.load(open(cfg["params_fichier"]))
    if not pr.get("valide"): print("⚠ Le backtest était NÉGATIF : simulation lancée quand même, à titre d'observation.")
    debut = cfg.get("paper_debut") or datetime.now().date().isoformat()
    if os.path.exists(cfg["paper_fichier"]): debut = json.load(open(cfg["paper_fichier"]))["debut"]
    else: json.dump({"debut": debut}, open(cfg["paper_fichier"], "w"))
    pf = json.load(open(cfg["paper_fichier"])); vetos = pf.setdefault("vetos", {}); analyses = []
    data = telecharger((pd.Timestamp(debut) - pd.Timedelta(days=700)).date().isoformat())
    courbe, trades, pos, ordres = simuler(data, pr["n_entree"], pr["k"], cfg, debut=debut, vetos=vetos)
    # ---- avant chaque NOUVELLE position : lecture de la presse, de Reddit et de Yahoo ----
    entrees = {t: o for t, o in ordres.items() if o[0] == "entree"}
    if entrees and courbe:
        import sentiment
        jour = str(pd.Timestamp(courbe[-1][0]).date())
        print(f"\n{len(entrees)} signal(aux) technique(s) aujourd'hui : analyse des news et de Reddit avant d'entrer...")
        for t, (_, sens, val) in entrees.items():
            a = sentiment.analyser(t, nom(t), categorie(t))
            contre = a["score"] * sens <= -cfg["seuil_sentiment"]
            dec = "REFUSÉ (les news vont contre)" if contre else "VALIDÉ"
            print(f"  {'LONG ' if sens > 0 else 'SHORT'} {nom(t):<10} sentiment {a['score']:+.2f} sur {a['n']} textes "
                  f"(presse {a['presse']:+.2f}, reddit {a['reddit']:+.2f}{', peur/avidité ' + str(a['fg']) if a['fg'] is not None else ''}) -> {dec}")
            for ti in a["titres"]: print("       " + ti)
            analyses.append(dict(paire=nom(t), sens="long" if sens > 0 else "short", score=round(a["score"], 2),
                                 textes=a["n"], decision=dec, titres=a["titres"]))
            if contre: vetos.setdefault(jour, {})[t] = sens
        json.dump(pf, open(cfg["paper_fichier"], "w"), indent=1)
        courbe, trades, pos, ordres = simuler(data, pr["n_entree"], pr["k"], cfg, debut=debut, vetos=vetos)
    eq = courbe[-1][1] if courbe else cfg["capital_initial"]
    posl = []
    for t, p in pos.items():
        posl.append(dict(sym=nom(t), type=categorie(t), sens="long" if p["s"] > 0 else "short",
                         lev=round(p["V"] / max(eq, 1), 2), marge=round(p["V"] / cfg["levier_total_max"], 2),
                         entree=p["e"], prix=p["ref"], pnl_latent=round(p["pnl"], 2), pic=p["pic"]))
    etat = {"maj": datetime.now().isoformat(timespec="seconds"), "mode": "PAPER (tendance lente, journalier)",
            "capital_initial": cfg["capital_initial"], "seuil_mort": 10, "capital": eq, "equite": eq,
            "positions": posl, "max_positions": len(data), "vivant": True, "historique": [],
            "trades": [dict(t, heure=str(t["heure"])[:10], levier="-", raison=f"stop suiveur ({t['jours']} j)") for t in trades][-300:],
            "courbe": [(pd.Timestamp(d).timestamp(), e) for d, e in courbe],
            "analyse": {"heure": datetime.now().isoformat(timespec="seconds"), "nb_paires": len(data), "candidats": analyses, "checks": {}},
            "logs": [f"Réglages : entrée {pr['n_entree']} j, stop {pr['k']} ATR | depuis le {debut}",
                     f"Équité {eq:.2f} $ | {len(pos)} positions ouvertes | {len(trades)} trades clos"]
                    + [f"News {x['paire']} {x['sens']} : sentiment {x['score']:+.2f} ({x['textes']} textes) -> {x['decision']}" for x in analyses], "file_candidats": 0}
    json.dump(etat, open(cfg["etat_fichier"], "w"), default=str)
    print(f"\nSIMULATION depuis le {debut} : équité {eq:.2f} $ | {len(pos)} positions | {len(trades)} trades clos")
    for p in posl: print(f"  {p['sens']:>5} {p['sym']:<10} ({p['type']}) entrée {p['entree']:.4f} | PnL {p['pnl_latent']:+.2f} $")
    print("Relance une fois par jour (après la clôture US, 22h30) : le bot rejoue tout depuis le départ et met le tableau de bord à jour.")


if __name__ == "__main__":
    (paper if len(sys.argv) > 1 and sys.argv[1] == "paper" else backtest)()
