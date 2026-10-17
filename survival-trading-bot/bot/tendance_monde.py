"""
TENDANCE MONDE — actions du monde entier + devises, avec effet de levier
=======================================================================
Même moteur que tendance_lente.py (bougies journalières, entrée sur cassure dans le sens de la
moyenne 200 jours, stop suiveur ATR, analyse presse + Reddit avant chaque position), mais
uniquement sur :
  - 70 grandes actions : États-Unis, Europe, Asie, Amériques
  - 24 paires de devises : majeures, croisées et émergentes

  python bot/tendance_monde.py backtest   -> 20 ans, réglage 2005-2016, TEST 2017-aujourd'hui
  python bot/tendance_monde.py paper      -> simulation quotidienne avec les réglages validés

Levier : perte au stop = 0,5 % du capital par trade ; total des positions jusqu'à x6 le capital
(les devises bougent peu, le levier y est la norme). Ces marchés se traitent chez un courtier
CFD / Forex (IG, Interactive Brokers...), pas sur Binance.
SIMULATION UNIQUEMENT. Ceci n'est pas un conseil financier.
"""
import sys
import tendance_lente as tl

ACTIONS = {
    "actions US": {"AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "AMZN": "Amazon", "GOOGL": "Alphabet Google",
                   "META": "Meta", "TSLA": "Tesla", "JPM": "JPMorgan", "XOM": "Exxon", "JNJ": "Johnson & Johnson",
                   "WMT": "Walmart", "KO": "Coca-Cola", "AMD": "AMD", "NFLX": "Netflix", "BA": "Boeing",
                   "CAT": "Caterpillar", "V": "Visa", "UNH": "UnitedHealth", "LLY": "Eli Lilly", "COST": "Costco"},
    "actions Europe": {"MC.PA": "LVMH", "OR.PA": "L'Oreal", "TTE.PA": "TotalEnergies", "AIR.PA": "Airbus", "SAN.PA": "Sanofi",
                       "BNP.PA": "BNP Paribas", "SAP.DE": "SAP", "SIE.DE": "Siemens", "ALV.DE": "Allianz",
                       "VOW3.DE": "Volkswagen", "ASML.AS": "ASML", "NESN.SW": "Nestle", "NOVN.SW": "Novartis",
                       "SHEL.L": "Shell", "AZN.L": "AstraZeneca", "HSBA.L": "HSBC", "ULVR.L": "Unilever",
                       "BP.L": "BP", "ENI.MI": "Eni", "ISP.MI": "Intesa Sanpaolo", "ITX.MC": "Inditex",
                       "SAN.MC": "Banco Santander", "NOVO-B.CO": "Novo Nordisk"},
    "actions Asie": {"7203.T": "Toyota", "6758.T": "Sony", "9984.T": "SoftBank", "8306.T": "Mitsubishi UFJ",
                     "6861.T": "Keyence", "0700.HK": "Tencent", "9988.HK": "Alibaba", "0941.HK": "China Mobile",
                     "1299.HK": "AIA Group", "005930.KS": "Samsung Electronics", "000660.KS": "SK Hynix",
                     "2330.TW": "TSMC", "RELIANCE.NS": "Reliance Industries", "TCS.NS": "Tata Consultancy",
                     "INFY.NS": "Infosys", "HDFCBANK.NS": "HDFC Bank", "BHP.AX": "BHP", "CBA.AX": "Commonwealth Bank"},
    "actions Amériques": {"SHOP.TO": "Shopify", "RY.TO": "Royal Bank of Canada", "ENB.TO": "Enbridge",
                          "VALE3.SA": "Vale", "PETR4.SA": "Petrobras", "ITUB4.SA": "Itau Unibanco",
                          "WALMEX.MX": "Walmart de Mexico", "AMXB.MX": "America Movil"},
}
DEVISES = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF", "USDCAD", "NZDUSD",          # majeures
           "EURGBP", "EURJPY", "GBPJPY", "EURCHF", "AUDJPY", "EURAUD", "GBPCHF",           # croisées
           "USDCNY", "USDINR", "USDMXN", "USDBRL", "USDZAR", "USDTRY", "USDSGD",            # émergentes
           "USDSEK", "USDNOK", "USDPLN"]

tl.UNIVERS.clear()
for cat, d in ACTIONS.items():
    tl.UNIVERS[cat] = list(d); tl.NOMS.update(d)
tl.UNIVERS["devises"] = [f"{p}=X" for p in DEVISES]
tl.NOMS.update({f"{p}=X": f"{p[:3]}/{p[3:]}" for p in DEVISES})
tl.CFG.update(levier_total_max=6.0, max_position=1.0,
              params_fichier="params_monde.json", paper_fichier="paper_monde.json")

if __name__ == "__main__":
    (tl.paper if len(sys.argv) > 1 and sys.argv[1] == "paper" else tl.backtest)()
