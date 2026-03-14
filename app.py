import os, json, math, time, hmac, hashlib, threading, secrets, logging
from datetime import datetime, timezone
from collections import deque
import requests, schedule
from flask import Flask, jsonify, request, render_template, session
from flask_cors import CORS

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
CORS(app, supports_credentials=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("bot")

BASE = os.environ.get("DATA_DIR", "/tmp")
STATE_FILE  = os.path.join(BASE, "state.json")
TRADES_FILE = os.path.join(BASE, "trades.json")
LOG_FILE    = os.path.join(BASE, "blog.json")

DEFAULTS = {
    "api_key": os.environ.get("BINANCE_API_KEY",""),
    "api_secret": os.environ.get("BINANCE_API_SECRET",""),
    "testnet": True,
    "symbol": "BTCUSDT",
    "interval": "5m",
    "capital_usd": 20.0,
    "risk_pct": 2.0,
    "rr_target": 3.0,
    "candle_threshold_pct": 0.05,
    "max_trades_per_day": 10,
    "direction": "both",
    "ema_fast": 9,
    "ema_slow": 21,
    "rsi_period": 14,
    "rsi_ob": 70,
    "rsi_os": 30,
    "bot_running": False,
    "admin_password": os.environ.get("ADMIN_PASSWORD","admin123"),
}

def load_json(path, fallback):
    try:
        if os.path.exists(path):
            with open(path) as f: return json.load(f)
    except: pass
    return fallback() if callable(fallback) else fallback

def save_json(path, data):
    try:
        with open(path,"w") as f: json.dump(data, f, indent=2, default=str)
    except Exception as e: log.error(f"save {path}: {e}")

cfg = load_json(STATE_FILE, dict(DEFAULTS))
for k,v in DEFAULTS.items(): cfg.setdefault(k,v)
trades    = load_json(TRADES_FILE, list)
bot_logs  = deque(load_json(LOG_FILE, list), maxlen=200)
open_trade = None
bot_thread = None
bot_stop   = threading.Event()
price_cache = {}

def blog(msg, level="INFO", color="muted"):
    e = {"t": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level, "color": color}
    bot_logs.appendleft(e)
    save_json(LOG_FILE, list(bot_logs)[:50])
    log.info(msg)

# ── Binance REST ──────────────────────────────────────────────────
def binance_req(method, path, params=None, signed=False):
    base = "https://testnet.binance.vision" if cfg.get("testnet") else "https://api.binance.com"
    headers = {"X-MBX-APIKEY": cfg.get("api_key","")}
    params = params or {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        qs  = "&".join(f"{k}={v}" for k,v in params.items())
        sig = hmac.new(cfg["api_secret"].encode(), qs.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
    r = getattr(requests, method.lower())(base+path, params=params, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()

def get_price(symbol=None):
    sym = (symbol or cfg["symbol"]).upper().strip()
    try:
        d = binance_req("GET","/api/v3/ticker/price",{"symbol":sym})
        price_cache[sym] = float(d["price"])
        return float(d["price"])
    except: return price_cache.get(sym, 0)

def get_klines(symbol, interval, limit=100):
    d = binance_req("GET","/api/v3/klines",{"symbol":symbol.upper(),"interval":interval,"limit":limit})
    return [{"t":c[0],"o":float(c[1]),"h":float(c[2]),"l":float(c[3]),"c":float(c[4]),"v":float(c[5]),"closed":c[6]<int(time.time()*1000)} for c in d]

def place_order(side, qty):
    if cfg.get("testnet"):
        return {"orderId":f"SIM_{int(time.time())}","fills":[{"price":str(get_price())}],"status":"FILLED"}
    p = {"symbol":cfg["symbol"],"side":side,"type":"MARKET","quantity":qty}
    return binance_req("POST","/api/v3/order",p,signed=True)

def get_balance():
    try:
        d = binance_req("GET","/api/v3/account",signed=True)
        for b in d.get("balances",[]):
            if b["asset"]=="USDT": return float(b["free"])
    except: pass
    return cfg.get("capital_usd",20.0)

def calc_qty(entry, sl):
    risk_usd = cfg["capital_usd"] * (cfg["risk_pct"]/100)
    diff = abs(entry - sl)
    if diff <= 0: return 0
    qty = risk_usd / diff
    try:
        info = binance_req("GET","/api/v3/exchangeInfo",{"symbol":cfg["symbol"].upper()})
        for s in info.get("symbols",[]):
            if s["symbol"] == cfg["symbol"].upper():
                for f in s.get("filters",[]):
                    if f["filterType"]=="LOT_SIZE":
                        step = float(f["stepSize"])
                        if step > 0:
                            prec = max(0, int(round(-math.log10(step))))
                            qty = round(math.floor(qty/step)*step, prec)
    except: qty = round(qty,6)
    return max(qty, 0)

# ── Indicators ────────────────────────────────────────────────────
def ema_calc(vals, period):
    if len(vals) < period: return []
    k = 2/(period+1)
    out = [sum(vals[:period])/period]
    for v in vals[period:]: out.append(v*k + out[-1]*(1-k))
    return out

def rsi_calc(closes, period=14):
    if len(closes) < period+1: return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag = sum(gains[-period:])/period
    al = sum(losses[-period:])/period
    if al == 0: return 100
    return 100-(100/(1+ag/al))

def atr_calc(candles, period=14):
    if len(candles) < period+1: return 0
    trs = []
    for i in range(1,len(candles)):
        c,p = candles[i],candles[i-1]
        trs.append(max(c["h"]-c["l"],abs(c["h"]-p["c"]),abs(c["l"]-p["c"])))
    return sum(trs[-period:])/period

def detect_trend(candles):
    if len(candles) < 55: return "neutral"
    closes = [c["c"] for c in candles]
    e9  = ema_calc(closes, 9)
    e21 = ema_calc(closes, 21)
    e50 = ema_calc(closes, 50)
    if not (e9 and e21 and e50): return "neutral"
    score = 0
    if e9[-1]  > e21[-1]: score += 1
    else: score -= 1
    if e21[-1] > e50[-1]: score += 1
    else: score -= 1
    if closes[-1] > e21[-1]: score += 1
    else: score -= 1
    # Higher highs / lows
    hs = [c["h"] for c in candles[-6:]]
    ls = [c["l"] for c in candles[-6:]]
    if hs[-1]>hs[-2] and ls[-1]>ls[-2]: score += 1
    elif hs[-1]<hs[-2] and ls[-1]<ls[-2]: score -= 1
    if score >= 2: return "bull"
    if score <= -2: return "bear"
    return "neutral"

def htf_trend(symbol):
    try:
        c = get_klines(symbol,"1h",60)
        return detect_trend(c)
    except: return "neutral"

def sup_res(candles, lb=20):
    if len(candles) < lb: return None, None
    r = candles[-lb:]
    return min(c["l"] for c in r), max(c["h"] for c in r)

def vol_ok(candles, lb=10):
    if len(candles) < lb+1: return True
    avg = sum(c["v"] for c in candles[-lb-1:-1])/lb
    return candles[-1]["v"] >= avg * 0.8

# ── Strategy engine ───────────────────────────────────────────────
def run_strategy():
    global open_trade
    if not cfg.get("bot_running"): return
    if not cfg.get("api_key") or not cfg.get("api_secret"):
        blog("No API keys — bot paused","WARN","amber"); return

    sym = cfg["symbol"].upper().strip()
    thr = cfg["candle_threshold_pct"]
    rr  = cfg["rr_target"]
    direction = cfg.get("direction","both")

    try:
        all_c  = get_klines(sym, cfg["interval"], 110)
        closed = [c for c in all_c if c["closed"]][:-1]
        curr   = all_c[-1]
        if len(closed) < 55:
            blog("Not enough candle data","WARN","amber"); return

        closes = [c["c"] for c in closed]
        trend  = detect_trend(closed)
        htf    = htf_trend(sym)
        r_val  = rsi_calc(closes, cfg.get("rsi_period",14))
        at     = atr_calc(closed, 14)
        sup, res = sup_res(closed, 20)
        vok    = vol_ok(closed)
        e9v    = ema_calc(closes, cfg.get("ema_fast",9))
        e21v   = ema_calc(closes, cfg.get("ema_slow",21))
        e9     = e9v[-1] if e9v else 0
        e21    = e21v[-1] if e21v else 0
        prev   = closed[-1]

        blog(f"Scan {sym} | trend={trend} htf={htf} rsi={r_val:.1f} atr={at:.2f}","INFO","dim")

        # Tiny candle check
        rng_pct = (prev["h"]-prev["l"])/prev["h"]*100 if prev["h"] else 0
        tiny    = rng_pct < thr
        if tiny: blog(f"TINY CANDLE {rng_pct:.4f}% | H={prev['h']} L={prev['l']}","SIGNAL","cyan")

        # Check existing trade
        if open_trade:
            price = get_price(sym)
            _check_close(price)
            return

        # Daily limit
        today = datetime.now().strftime("%Y-%m-%d")
        today_count = sum(1 for t in trades if t.get("opened_at","")[:10]==today and t.get("status")=="CLOSED")
        if cfg["max_trades_per_day"] > 0 and today_count >= cfg["max_trades_per_day"]:
            blog(f"Daily limit reached ({cfg['max_trades_per_day']})","INFO","amber"); return

        # Score signals
        ls = ss = 0
        if tiny: ls += 2; ss += 2
        if trend=="bull": ls += 2
        if trend=="bear": ss += 2
        if htf=="bull": ls += 1
        if htf=="bear": ss += 1
        rsi_ob = cfg.get("rsi_ob",70); rsi_os = cfg.get("rsi_os",30)
        if r_val < rsi_os: ls += 2
        if r_val > rsi_ob: ss += 2
        if 40 < r_val < 60: ls += 1; ss += 1
        if e9 > e21: ls += 1
        if e9 < e21: ss += 1
        price_now = curr["c"]
        if price_now > e9: ls += 1
        if price_now < e9: ss += 1
        if vok: ls += 1; ss += 1
        if sup and abs(price_now-sup)/price_now < 0.003: ls += 1
        if res and abs(res-price_now)/price_now < 0.003: ss += 1

        blog(f"Signal scores | LONG={ls} SHORT={ss}","INFO","dim")

        MIN = 5
        entry = price_now
        crng  = prev["h"]-prev["l"]
        if crng <= 0: crng = at if at > 0 else entry*0.001

        tdir = None
        if ls >= MIN and direction in ("both","long"):
            tdir = "LONG"; sl = prev["l"] - at*0.3; tp = entry + crng*rr
        elif ss >= MIN and direction in ("both","short"):
            tdir = "SHORT"; sl = prev["h"] + at*0.3; tp = entry - crng*rr

        if not tdir:
            blog("No signal — waiting","INFO","dim"); return
        if tdir=="LONG"  and (sl>=entry or tp<=entry): return
        if tdir=="SHORT" and (sl<=entry or tp>=entry): return

        qty = calc_qty(entry, sl)
        if qty <= 0:
            blog("Qty too small","WARN","amber"); return

        try:
            order = place_order("BUY" if tdir=="LONG" else "SELL", qty)
            oid   = order.get("orderId", f"SIM_{int(time.time())}")
            if order.get("fills"): entry = float(order["fills"][0]["price"])
        except Exception as e:
            blog(f"Order error: {e}","ERROR","red"); return

        open_trade = {
            "id":str(oid),"symbol":sym,"direction":tdir,
            "entry":entry,"sl":round(sl,6),"tp":round(tp,6),"qty":qty,
            "score":ls if tdir=="LONG" else ss,"trend":trend,"htf":htf,
            "rsi":round(r_val,2),"tiny":tiny,"range_pct":round(rng_pct,5),
            "opened_at":datetime.now(tz=timezone.utc).isoformat(),"status":"OPEN",
        }
        trades.append(open_trade)
        save_json(TRADES_FILE, trades)
        risk_usd = abs(entry-sl)*qty
        rew_usd  = abs(tp-entry)*qty
        blog(f"{'🟢 LONG' if tdir=='LONG' else '🔴 SHORT'} | entry=${entry:.4f} sl=${sl:.4f} tp=${tp:.4f} risk=${risk_usd:.4f} rwd=${rew_usd:.4f}","TRADE","green" if tdir=="LONG" else "red")

    except Exception as e:
        blog(f"Strategy error: {e}","ERROR","red")

def _check_close(price):
    global open_trade
    if not open_trade: return
    t = open_trade; d = t["direction"]
    hit_tp = (d=="LONG" and price>=t["tp"]) or (d=="SHORT" and price<=t["tp"])
    hit_sl = (d=="LONG" and price<=t["sl"]) or (d=="SHORT" and price>=t["sl"])
    if not (hit_tp or hit_sl): return
    reason = "TP" if hit_tp else "SL"
    pnl = (price-t["entry"])*t["qty"] if d=="LONG" else (t["entry"]-price)*t["qty"]
    cfg["capital_usd"] = round(cfg.get("capital_usd",20)+pnl, 6)
    save_json(STATE_FILE, cfg)
    if not cfg.get("testnet"):
        try: place_order("SELL" if d=="LONG" else "BUY", t["qty"])
        except Exception as e: blog(f"Close order err: {e}","ERROR","red")
    for i,tr in enumerate(trades):
        if str(tr.get("id"))==str(t["id"]):
            trades[i].update({"exit":price,"exit_reason":reason,"pnl":round(pnl,6),"closed_at":datetime.now(tz=timezone.utc).isoformat(),"status":"CLOSED"})
            break
    save_json(TRADES_FILE, trades)
    emoji = "✅" if reason=="TP" else "❌"
    blog(f"{emoji} {reason} | exit=${price:.4f} pnl={'+'if pnl>=0 else''}${pnl:.4f} capital=${cfg['capital_usd']:.4f}","TRADE","green" if pnl>=0 else "red")
    open_trade = None

def check_tp_sl():
    if not open_trade: return
    try: _check_close(get_price(open_trade["symbol"]))
    except Exception as e: blog(f"TP/SL check error: {e}","ERROR","red")

def bot_loop():
    schedule.clear()
    imap = {"1m":1,"3m":3,"5m":5,"10m":10,"15m":15,"30m":30,"1h":60}
    mins = imap.get(cfg.get("interval","5m"),5)
    schedule.every(mins).minutes.do(run_strategy)
    schedule.every(1).minutes.do(check_tp_sl)
    run_strategy()
    while not bot_stop.is_set():
        schedule.run_pending(); time.sleep(5)
    schedule.clear()

# ── Auth helpers ──────────────────────────────────────────────────
def authed(): return session.get("authed") is True

# ── Routes ────────────────────────────────────────────────────────
@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/login", methods=["POST"])
def login():
    d = request.json or {}
    if d.get("password") == cfg.get("admin_password","admin123"):
        session["authed"] = True
        return jsonify({"ok":True})
    return jsonify({"ok":False,"error":"Wrong password"}), 401

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear(); return jsonify({"ok":True})

@app.route("/api/config")
def get_config():
    safe = {k:v for k,v in cfg.items() if k not in ("api_key","api_secret","admin_password")}
    safe["has_keys"] = bool(cfg.get("api_key") and cfg.get("api_secret"))
    return jsonify(safe)

@app.route("/api/config", methods=["POST"])
def set_config():
    if not authed(): return jsonify({"ok":False,"error":"Not authenticated"}), 401
    d = request.json or {}
    for k in ["api_key","api_secret","testnet","symbol","interval","capital_usd","risk_pct",
              "rr_target","candle_threshold_pct","max_trades_per_day","direction",
              "ema_fast","ema_slow","rsi_period","rsi_ob","rsi_os","admin_password"]:
        if k in d: cfg[k] = d[k]
    save_json(STATE_FILE, cfg)
    return jsonify({"ok":True})

@app.route("/api/connect", methods=["POST"])
def connect():
    if not authed(): return jsonify({"ok":False,"error":"Not authenticated"}), 401
    d = request.json or {}
    if d.get("api_key"):    cfg["api_key"]    = d["api_key"]
    if d.get("api_secret"): cfg["api_secret"] = d["api_secret"]
    if "testnet" in d:      cfg["testnet"]    = d["testnet"]
    save_json(STATE_FILE, cfg)
    try:
        sym   = cfg["symbol"].upper().strip()
        price = get_price(sym)
        bal   = get_balance()
        return jsonify({"ok":True,"symbol":sym,"price":price,"balance":bal,"mode":"TESTNET" if cfg["testnet"] else "LIVE"})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/price")
def price_route():
    try:
        sym = cfg["symbol"].upper().strip()
        d   = binance_req("GET","/api/v3/ticker/24hr",{"symbol":sym})
        return jsonify({"ok":True,"price":float(d["lastPrice"]),"change":float(d["priceChangePercent"]),"high":float(d["highPrice"]),"low":float(d["lowPrice"]),"vol":float(d["volume"])})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/candles")
def candles_route():
    try:
        sym   = cfg["symbol"].upper().strip()
        intv  = request.args.get("interval", cfg["interval"])
        limit = int(request.args.get("limit",100))
        data  = get_klines(sym, intv, limit)
        thr   = cfg["candle_threshold_pct"]
        closes = [c["c"] for c in data]
        ef = cfg.get("ema_fast",9); es = cfg.get("ema_slow",21)
        e9s  = ema_calc(closes, ef)  if len(closes)>=ef  else []
        e21s = ema_calc(closes, es)  if len(closes)>=es  else []
        e9_pad  = [None]*(len(closes)-len(e9s))  + [round(v,4) for v in e9s]
        e21_pad = [None]*(len(closes)-len(e21s)) + [round(v,4) for v in e21s]
        for i,c in enumerate(data):
            rng = (c["h"]-c["l"])/c["h"]*100 if c["h"] else 0
            c["tiny"] = rng < thr
            c["rng"]  = round(rng,5)
            c["ema9"]  = e9_pad[i]  if i < len(e9_pad)  else None
            c["ema21"] = e21_pad[i] if i < len(e21_pad) else None
        return jsonify({"ok":True,"candles":data})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/indicators")
def ind_route():
    try:
        sym = cfg["symbol"].upper().strip()
        c   = get_klines(sym, cfg["interval"], 110)
        cl  = [x for x in c if x["closed"]]
        closes = [x["c"] for x in cl]
        trend  = detect_trend(cl)
        htf    = htf_trend(sym)
        r      = rsi_calc(closes, cfg.get("rsi_period",14))
        at     = atr_calc(cl, 14)
        e9v    = ema_calc(closes, cfg.get("ema_fast",9))
        e21v   = ema_calc(closes, cfg.get("ema_slow",21))
        e50v   = ema_calc(closes, 50)
        sup,res= sup_res(cl, 20)
        return jsonify({"ok":True,"trend":trend,"htf":htf,"rsi":round(r,2),"atr":round(at,4),
            "ema9":round(e9v[-1],4) if e9v else 0,
            "ema21":round(e21v[-1],4) if e21v else 0,
            "ema50":round(e50v[-1],4) if e50v else 0,
            "support":round(sup,4) if sup else 0,
            "resistance":round(res,4) if res else 0})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/bot/start", methods=["POST"])
def bot_start():
    global bot_thread, bot_stop
    if not authed(): return jsonify({"ok":False,"error":"Not authenticated"}), 401
    if cfg.get("bot_running"): return jsonify({"ok":False,"error":"Already running"})
    if not cfg.get("api_key"): return jsonify({"ok":False,"error":"Set API keys first"})
    cfg["bot_running"] = True; save_json(STATE_FILE, cfg)
    bot_stop.clear()
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    blog("Bot started","INFO","green")
    return jsonify({"ok":True})

@app.route("/api/bot/stop", methods=["POST"])
def bot_stop_r():
    if not authed(): return jsonify({"ok":False,"error":"Not authenticated"}), 401
    cfg["bot_running"] = False; save_json(STATE_FILE, cfg)
    bot_stop.set()
    blog("Bot stopped","INFO","amber")
    return jsonify({"ok":True})

@app.route("/api/bot/status")
def bot_stat():
    return jsonify({"running":cfg.get("bot_running",False),"open_trade":open_trade,"logs":list(bot_logs)[:40]})

@app.route("/api/trades")
def trades_route():
    closed = [t for t in trades if t.get("status")=="CLOSED"]
    wins   = [t for t in closed if (t.get("pnl") or 0)>0]
    total_pnl  = sum(t.get("pnl",0) for t in closed)
    today      = datetime.now().strftime("%Y-%m-%d")
    today_pnl  = sum(t.get("pnl",0) for t in closed if t.get("closed_at","")[:10]==today)
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in closed if t.get("pnl",0)<0))
    pf = round(gp/gl,2) if gl > 0 else (999 if wins else 0)
    peak=dd=0; eq=cfg.get("capital_usd",20)
    for t in closed:
        eq+=t.get("pnl",0)
        if eq>peak: peak=eq
        if peak-eq>dd: dd=peak-eq
    return jsonify({
        "trades": list(reversed(trades[-100:])),
        "stats":{"total":len(closed),"wins":len(wins),"losses":len(closed)-len(wins),
                 "win_rate":round(len(wins)/len(closed)*100,1) if closed else 0,
                 "total_pnl":round(total_pnl,4),"today_pnl":round(today_pnl,4),
                 "capital":round(cfg.get("capital_usd",20),4),
                 "profit_factor":pf,"max_drawdown":round(dd,4)}
    })

@app.route("/api/trades/clear", methods=["POST"])
def clear_trades():
    global trades, open_trade
    if not authed(): return jsonify({"ok":False,"error":"Not authenticated"}), 401
    trades=[]; open_trade=None; save_json(TRADES_FILE, trades)
    return jsonify({"ok":True})

@app.route("/api/trade/manual", methods=["POST"])
def manual_trade():
    global open_trade
    if not authed(): return jsonify({"ok":False,"error":"Not authenticated"}), 401
    if open_trade: return jsonify({"ok":False,"error":"Close existing trade first"})
    d = request.json or {}
    direction = d.get("direction","LONG")
    try:
        sym = cfg["symbol"].upper().strip()
        c   = get_klines(sym, cfg["interval"], 50)
        cl  = [x for x in c if x["closed"]]
        prev = cl[-1]; at = atr_calc(cl,14)
        price = get_price(sym)
        rng = prev["h"]-prev["l"]
        if rng<=0: rng = at if at else price*0.001
        if direction=="LONG":
            sl = prev["l"]-at*0.3; tp = price+rng*cfg["rr_target"]
        else:
            sl = prev["h"]+at*0.3; tp = price-rng*cfg["rr_target"]
        qty = calc_qty(price, sl)
        if qty<=0: return jsonify({"ok":False,"error":"Qty too small"})
        order = place_order("BUY" if direction=="LONG" else "SELL", qty)
        oid   = order.get("orderId",f"MAN_{int(time.time())}")
        open_trade = {"id":str(oid),"symbol":sym,"direction":direction,"entry":price,
                      "sl":round(sl,6),"tp":round(tp,6),"qty":qty,
                      "opened_at":datetime.now(tz=timezone.utc).isoformat(),"status":"OPEN","manual":True}
        trades.append(open_trade); save_json(TRADES_FILE, trades)
        blog(f"Manual {direction} | entry=${price:.4f}","TRADE","cyan")
        return jsonify({"ok":True,"trade":open_trade})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/trade/close", methods=["POST"])
def close_manual():
    global open_trade
    if not authed(): return jsonify({"ok":False,"error":"Not authenticated"}), 401
    if not open_trade: return jsonify({"ok":False,"error":"No open trade"})
    try:
        price = get_price(open_trade["symbol"]); t = open_trade
        pnl = (price-t["entry"])*t["qty"] if t["direction"]=="LONG" else (t["entry"]-price)*t["qty"]
        cfg["capital_usd"] = round(cfg.get("capital_usd",20)+pnl, 6); save_json(STATE_FILE, cfg)
        if not cfg.get("testnet"):
            place_order("SELL" if t["direction"]=="LONG" else "BUY", t["qty"])
        for i,tr in enumerate(trades):
            if str(tr.get("id"))==str(t["id"]):
                trades[i].update({"exit":price,"exit_reason":"MANUAL","pnl":round(pnl,6),"closed_at":datetime.now(tz=timezone.utc).isoformat(),"status":"CLOSED"})
                break
        save_json(TRADES_FILE, trades); open_trade = None
        blog(f"Manual close | pnl={'+'if pnl>=0 else''}${pnl:.4f}","TRADE","green" if pnl>=0 else "red")
        return jsonify({"ok":True,"pnl":round(pnl,6)})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/health")
def health(): return jsonify({"status":"ok","bot":cfg.get("bot_running",False),"ts":datetime.now().isoformat()})

if __name__ == "__main__":
    if cfg.get("bot_running"): cfg["bot_running"]=False; save_json(STATE_FILE,cfg)
    port = int(os.environ.get("PORT",8000))
    print(f"\n{'='*48}\n  CryptoBot Pro\n  http://localhost:{port}\n{'='*48}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
