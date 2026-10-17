"""
Dashboard temps réel du bot de trading.
Aucune dépendance : bibliothèque standard Python uniquement.

Terminal 1 : python bot/trading_bot.py
Terminal 2 : python dashboard/dashboard.py      -> http://localhost:8000
"""
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ETAT = sys.argv[1] if len(sys.argv) > 1 else "bot_state_v2.json"
PORT = int(os.getenv("PORT", 8000))

PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bot de trading — Live</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--tx:#e6edf3;--mu:#8b949e;--up:#3fb950;--dn:#f85149;--ac:#58a6ff;--wa:#d29922}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.45 system-ui,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--bd)}
h1{font-size:17px;margin:0;flex:1}.badge{padding:3px 10px;border-radius:99px;font-size:12px;font-weight:600}
.alive{background:#3fb95022;color:var(--up)}.dead{background:#f8514922;color:var(--dn)}.mode{background:#58a6ff22;color:var(--ac)}
main{padding:16px 20px;display:grid;gap:14px;grid-template-columns:repeat(12,1fr)}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px;min-width:0}
.k{grid-column:span 3}.c8{grid-column:span 8}.c4{grid-column:span 4}.c6{grid-column:span 6}.c12{grid-column:span 12}
@media(max-width:900px){.k{grid-column:span 6}.c8,.c4,.c6{grid-column:span 12}}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mu);margin:0 0 10px}
.big{font-size:26px;font-weight:700}.sub{color:var(--mu);font-size:12px}.up{color:var(--up)}.dn{color:var(--dn)}
.bar{height:6px;background:var(--bd);border-radius:3px;margin-top:8px;overflow:hidden}.bar>i{display:block;height:100%}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:6px 4px;text-align:left;border-bottom:1px solid var(--bd)}
th{color:var(--mu);font-weight:500}td.n{text-align:right;font-variant-numeric:tabular-nums}
.chk{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--bd)}
#logs{height:280px;overflow:auto;font:12px/1.5 ui-monospace,monospace;color:var(--mu);white-space:pre-wrap}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px}.kv b{font-weight:500;color:var(--mu)}
.empty{color:var(--mu);font-style:italic}#err{color:var(--wa);font-size:12px}
</style></head><body>
<header><h1>🤖 Bot de trading — supervision en temps réel</h1>
<span id="err"></span><span id="mode" class="badge mode">—</span><span id="vie" class="badge">—</span>
<span class="sub" id="maj">—</span></header>
<main>
 <div class="card k"><h2>Équité</h2><div class="big" id="eq">—</div><div class="sub" id="eqd">—</div></div>
 <div class="card k"><h2>Distance à la mort</h2><div class="big" id="dist">—</div><div class="bar"><i id="distbar"></i></div><div class="sub" id="seuil">—</div></div>
 <div class="card k"><h2>Trades</h2><div class="big" id="nb">—</div><div class="sub" id="wr">—</div></div>
 <div class="card k"><h2>PnL cumulé</h2><div class="big" id="pnl">—</div><div class="sub" id="best">—</div></div>

 <div class="card c8"><h2>Courbe d'équité</h2><canvas id="chart" height="110"></canvas></div>
 <div class="card c4"><h2>Exposition</h2><div id="expo" class="kv"></div></div>
 <div class="card c12"><h2>Positions ouvertes</h2>
   <table><thead><tr><th>Marché</th><th>Sens</th><th class="n">Levier</th><th class="n">Marge</th><th class="n">Entrée</th><th class="n">Prix</th><th class="n">PnL latent</th><th class="n">Gain max atteint</th><th class="n">Filet à</th><th class="n">Liquidation</th><th class="n">Proba gain</th><th class="n">Temps</th></tr></thead><tbody id="pos"></tbody></table></div>

 <div class="card c6"><h2>Dernière analyse <span class="sub" id="anh"></span></h2>
   <div class="sub" id="sent" style="margin-bottom:8px">—</div>
   <table><thead><tr><th>Paire</th><th>Sens</th><th class="n">Score</th><th class="n">Sentiment</th><th class="n">Confiance</th><th class="n">Levier</th><th class="n">Espérance</th><th>Décision</th></tr></thead><tbody id="cands"></tbody></table></div>
 <div class="card c6"><h2>Indicateurs et analyse approfondie <span class="sub" id="chp"></span></h2><div id="checks" class="empty">En attente d'un signal…</div></div>

 <div class="card c12"><h2>Historique des trades</h2>
   <table><thead><tr><th>Heure</th><th>Paire</th><th>Sens</th><th class="n">Levier</th><th class="n">Entrée</th><th class="n">Sortie</th><th class="n">PnL</th><th>Raison</th></tr></thead><tbody id="trades"></tbody></table></div>
 <div class="card c12"><h2>Journal</h2><div id="logs"></div></div>
</main>
<script>
const $=id=>document.getElementById(id), f=(x,d=2)=>x==null?'—':Number(x).toFixed(d);
const usd=x=>x==null?'—':(x<0?'-':'')+Math.abs(x).toFixed(2)+' $';
const cls=x=>x>0?'up':x<0?'dn':'';
let chart;
function draw(c,seuil,init){
  const lab=c.map(p=>new Date(p[0]*1000).toLocaleTimeString('fr-FR')), val=c.map(p=>p[1]);
  if(!chart){chart=new Chart($('chart'),{type:'line',data:{labels:lab,datasets:[
    {label:'Équité',data:val,borderColor:'#58a6ff',backgroundColor:'#58a6ff22',fill:true,pointRadius:0,tension:.2},
    {label:'Seuil de mort',data:val.map(()=>seuil),borderColor:'#f85149',borderDash:[6,4],pointRadius:0},
    {label:'Capital initial',data:val.map(()=>init),borderColor:'#8b949e',borderDash:[2,4],pointRadius:0}]},
    options:{animation:false,plugins:{legend:{labels:{color:'#8b949e'}}},scales:{x:{ticks:{color:'#8b949e',maxTicksLimit:8},grid:{color:'#30363d'}},y:{ticks:{color:'#8b949e'},grid:{color:'#30363d'}}}}});}
  else{chart.data.labels=lab;chart.data.datasets[0].data=val;chart.data.datasets[1].data=val.map(()=>seuil);chart.data.datasets[2].data=val.map(()=>init);chart.update();}
}
async function tick(){
  let s; try{const r=await fetch('/api/state',{cache:'no-store'}); if(!r.ok)throw 0; s=await r.json(); $('err').textContent='';}
  catch(e){$('err').textContent='⚠ en attente du bot…';return;}
  $('mode').textContent=s.mode||'—';
  $('vie').textContent=s.vivant?'● VIVANT':'💀 MORT'; $('vie').className='badge '+(s.vivant?'alive':'dead');
  $('maj').textContent='MAJ '+(s.maj||'').replace('T',' ');
  const eq=s.equite??s.capital, d=eq-s.capital_initial;
  $('eq').textContent=usd(eq); $('eqd').innerHTML=`<span class="${cls(d)}">${d>=0?'+':''}${f(d)} $ (${f(d/s.capital_initial*100)} %)</span> depuis le départ`;
  const marge=eq-s.seuil_mort, pct=Math.max(0,Math.min(100,marge/(s.capital_initial-s.seuil_mort+1e-9)*100));
  $('dist').textContent=usd(marge); $('dist').className='big '+(pct<30?'dn':pct<60?'':'up');
  $('distbar').style.cssText=`width:${pct}%;background:${pct<30?'#f85149':pct<60?'#d29922':'#3fb950'}`;
  $('seuil').textContent=`Seuil de mort : ${usd(s.seuil_mort)}`;
  const h=s.historique||[], g=h.filter(x=>x>0).length, tot=h.reduce((a,b)=>a+b,0);
  $('nb').textContent=h.length; $('wr').textContent=h.length?`${g} gagnants · taux ${f(g/h.length*100,0)} %`:'aucun trade clôturé';
  $('pnl').textContent=usd(tot); $('pnl').className='big '+cls(tot);
  $('best').textContent=h.length?`meilleur ${usd(Math.max(...h))} · pire ${usd(Math.min(...h))}`:'—';
  draw(s.courbe||[],s.seuil_mort,s.capital_initial);
  const P=s.positions||[], lat=P.reduce((x,p)=>x+(p.pnl_latent||0),0), mg=P.reduce((x,p)=>x+p.marge,0);
  $('expo').innerHTML=`<b>Positions</b><span>${P.length} / ${s.max_positions||'—'}</span><b>Marge engagée</b><span>${usd(mg)}</span>
    <b>Capital libre</b><span>${usd(s.capital)}</span><b>PnL latent</b><span class="${cls(lat)}">${usd(lat)}</span>
    <b>File de candidats prêts</b><span>${s.file_candidats??'—'}</span><b>Perte max (tous les filets)</b><span class="dn">${usd(-P.reduce((x,p)=>x+(p.perte_prevue||0),0))}</span>`;
  $('pos').innerHTML=P.map(p=>`<tr><td>${p.sym.replace(':USDT','')}<div class="sub">${p.type||''}</div></td><td class="${p.sens==='long'?'up':'dn'}">${p.sens}</td><td class="n">x${p.lev}</td><td class="n">${usd(p.marge)}</td>
    <td class="n">${f(p.entree,5)}</td><td class="n">${f(p.prix,5)}</td><td class="n ${cls(p.pnl_latent)}">${usd(p.pnl_latent)}</td>
    <td class="n up">${usd(p.pic||0)}</td><td class="n dn">${p.stop?f(p.stop,5):'liquidation'}</td><td class="n dn">${f(p.liq,5)}</td>
<td class="n">${f((p.proba||p.conf||0)*100,0)} %</td><td class="n">${p.t_ouv?(p.duree_min?Math.max(0,Math.round(p.duree_min*60-(Date.now()/1000-p.t_ouv)))+' s restantes':'ouvert depuis '+Math.round(Date.now()/1000-p.t_ouv)+' s'):'—'}</td></tr>`).join('')
    ||'<tr><td colspan="13" class="empty">Aucune position — le bot cherche des opportunités.</td></tr>';
  const a=s.analyse||{};
  $('anh').textContent=a.heure?'· '+a.heure.replace('T',' '):'';
  $('sent').textContent=a.heure?`${a.nb_paires?a.nb_paires+' paires scannées · ':''}Fear & Greed : ${f(a.fear_greed)} · Tendances web : ${(a.tendances||[]).join(', ')||'—'}`:'Pas encore d\'analyse (le bot a peut-être une position ouverte).';
  $('cands').innerHTML=(a.candidats||[]).map(c=>`<tr><td>${c.paire}</td><td class="${c.sens==='long'?'up':'dn'}">${c.sens}</td><td class="n">${f(c.score,3)}</td><td class="n ${cls(c.sentiment)}">${f(c.sentiment)}</td><td class="n">${c.confiance!=null?f(c.confiance*100,0)+' %':'—'}</td><td class="n">${c.levier?'x'+c.levier:'—'}</td><td class="n ${cls(c.esperance)}">${c.esperance!=null?usd(c.esperance):'—'}</td><td class="${c.decision==='OK'?'up':'sub'}">${c.decision||'—'}</td></tr>`).join('')||'<tr><td colspan="8" class="empty">Aucune paire ne valide tous les indicateurs</td></tr>';
  $('chp').textContent=a.checks_paire?'· '+a.checks_paire:'';
  const ck=a.checks||{};
  $('checks').className=Object.keys(ck).length?'':'empty';
  $('checks').innerHTML=Object.keys(ck).length?Object.entries(ck).map(([k,v])=>`<div class="chk"><span>${k}</span><span>${v?'✅':'❌'}</span></div>`).join(''):'En attente d\'un signal…';
  $('trades').innerHTML=(s.trades||[]).slice().reverse().map(t=>`<tr><td>${(t.heure||'').replace('T',' ')}</td><td>${t.paire}</td><td class="${t.sens==='long'?'up':'dn'}">${t.sens}</td><td class="n">x${t.levier}</td><td class="n">${f(t.entree,4)}</td><td class="n">${f(t.sortie,4)}</td><td class="n ${cls(t.pnl)}">${usd(t.pnl)}</td><td>${t.raison}</td></tr>`).join('')||'<tr><td colspan="8" class="empty">Aucun trade</td></tr>';
  const L=$('logs'), bas=L.scrollHeight-L.scrollTop-L.clientHeight<30;
  L.textContent=(s.logs||[]).join('\n'); if(bas)L.scrollTop=L.scrollHeight;
}
tick(); setInterval(tick,2000);
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            try:
                with open(ETAT, "rb") as f:
                    data = f.read()
                json.loads(data)  # valide
                self._send(200, data, "application/json")
            except (FileNotFoundError, json.JSONDecodeError):
                self._send(503, b'{"erreur":"etat indisponible"}', "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"404", "text/plain")

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    print(f"Dashboard sur http://localhost:{PORT}  (fichier d'état : {ETAT})")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
