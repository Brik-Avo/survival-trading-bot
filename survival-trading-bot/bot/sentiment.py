"""
Analyse des news et des réseaux avant d'ouvrir une position (sources GRATUITES, sans clé API) :
  - Google News (RSS)                      : articles de presse des dernières 48 h
  - Reddit (r/stocks, r/wallstreetbets, r/CryptoCurrency, r/Forex, r/Gold...) : titres de la dernière semaine
  - Yahoo Finance                          : news liées au ticker
  - Fear & Greed Index (crypto uniquement) : humeur globale du marché crypto
Chaque titre reçoit un score de -1 (très négatif) à +1 (très positif) avec VADER, un outil d'analyse
de sentiment conçu pour les textes courts des réseaux sociaux.

X / Twitter n'est PAS inclus : leur API est payante. Un compte API X peut être branché plus tard.
"""
import re, time, xml.etree.ElementTree as ET
from urllib.parse import quote
import requests

UA = {"User-Agent": "Mozilla/5.0 (survival-trading-bot; open-source research)"}
SUBS = {"actions Europe": ["eupersonalfinance", "stocks", "investing"], "actions Asie": ["stocks", "investing", "IndiaInvestments"],
        "actions": ["stocks", "wallstreetbets", "investing"], "crypto": ["CryptoCurrency", "CryptoMarkets"],
        "devises": ["Forex"], "or & métaux": ["Gold", "Silverbugs", "investing"], "indices": ["stocks", "investing"]}
MOTS = {"actions": "stock", "crypto": "crypto", "devises": "forex", "or & métaux": "price", "indices": "index"}
NOMS_LONGS = {"GC=F": "gold", "SI=F": "silver", "HG=F": "copper", "^GSPC": "S&P 500", "^NDX": "Nasdaq 100",
              "EURUSD=X": "EUR/USD euro dollar", "GBPUSD=X": "GBP/USD pound", "USDJPY=X": "USD/JPY yen",
              "AUDUSD=X": "AUD/USD", "USDCHF=X": "USD/CHF franc", "USDCAD=X": "USD/CAD", "NZDUSD=X": "NZD/USD",
              "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana", "BNB-USD": "BNB", "XRP-USD": "XRP",
              "ADA-USD": "Cardano", "DOGE-USD": "Dogecoin", "AVAX-USD": "Avalanche", "LINK-USD": "Chainlink",
              "LTC-USD": "Litecoin", "DOT-USD": "Polkadot", "TRX-USD": "Tron"}

_vader = None
def score_texte(t):
    global _vader
    if _vader is None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        _vader = SentimentIntensityAnalyzer()
    return _vader.polarity_scores(t)["compound"]


def google_news(q):
    try:
        r = requests.get(f"https://news.google.com/rss/search?q={quote(q)}+when:2d&hl=en-US&gl=US&ceid=US:en", headers=UA, timeout=10)
        return [i.findtext("title") or "" for i in ET.fromstring(r.content).iter("item")][:30]
    except Exception:
        return []


def reddit(q, subs):
    out = []
    for s in subs:
        try:
            r = requests.get(f"https://www.reddit.com/r/{s}/search.json?q={quote(q)}&restrict_sr=1&sort=new&t=week&limit=25",
                             headers=UA, timeout=10)
            for c in r.json().get("data", {}).get("children", []):
                d = c["data"]; out.append((d.get("title", ""), max(1, d.get("score", 1))))
            time.sleep(1.2)                          # Reddit limite les requêtes anonymes
        except Exception:
            pass
    return out


def yahoo(ticker):
    try:
        import yfinance as yf
        return [(n.get("content") or n).get("title", "") for n in (yf.Ticker(ticker).news or [])][:20]
    except Exception:
        return []


def fear_greed():
    try:
        v = int(requests.get("https://api.alternative.me/fng/", timeout=10).json()["data"][0]["value"])
        return v                                     # 0 = peur extrême, 100 = avidité extrême
    except Exception:
        return None


def analyser(ticker, nom, categorie):
    """Renvoie un score global (-1..+1), le nombre de textes lus et les titres les plus marquants."""
    q = NOMS_LONGS.get(ticker, nom)
    presse = [(t, 1) for t in google_news(f"{q} {MOTS.get(categorie) or MOTS.get(categorie.split()[0], '')}")] + [(t, 1) for t in yahoo(ticker)]
    red = reddit(q, SUBS.get(categorie) or SUBS.get(categorie.split()[0], ["investing"]))
    textes = [(t, w, "presse") for t, w in presse if t] + [(t, w, "reddit") for t, w in red if t]
    if not textes: return dict(score=0.0, n=0, presse=0.0, reddit=0.0, fg=None, titres=[])
    notes = [(score_texte(re.sub(r"\s+-\s+[^-]+$", "", t)), w, src, t) for t, w, src in textes]
    def moy(src):
        x = [(s, min(w, 500) ** 0.5) for s, w, so, _ in notes if so == src and s != 0]
        return sum(s * w for s, w in x) / sum(w for _, w in x) if x else 0.0
    sp, sr = moy("presse"), moy("reddit")
    score = 0.6 * sp + 0.4 * sr                      # la presse pèse plus que les forums
    fg = fear_greed() if categorie == "crypto" else None
    if fg is not None: score += (fg - 50) / 250      # humeur crypto : -0,2 à +0,2
    marquants = sorted(notes, key=lambda x: -abs(x[0]))[:3]
    return dict(score=max(-1, min(1, score)), n=len(notes), presse=sp, reddit=sr, fg=fg,
                titres=[f"{s:+.2f} [{src}] {t[:110]}" for s, _, src, t in marquants])
