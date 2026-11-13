import os
import requests
import time
from datetime import datetime, date
import pytz

# ============================================
# CONFIGURATION
# ============================================
TELEGRAM_TOKEN     = "8881921114:AAEXluT-1VMUoQdSsUqIKRZdEEqQWVl7mUQ"
TELEGRAM_CHAT_ID   = "801891845"
TWELVEDATA_API_KEY = "af4d2d5a945644708b051fae5ec8e543"

SYMBOL       = "XAU/USD"
CAPITAL      = 2500
RISK_PERCENT = 0.05
RISK_AMOUNT  = CAPITAL * RISK_PERCENT   # $125 Risiko pro Trade max.

MAX_TRADES_PER_DAY = 3
MIN_CONFIDENCE     = 80      # Erhöht für bessere Setups
MIN_SL_DOLLARS     = 10.0    # Erhöht, damit das Marktrauschen nicht sofort triggert
MAX_SL_DOLLARS     = 30.0    # Angehoben für dynamische ATR-Stops
MIN_FVG_SIZE       = 1.50    
EQUAL_LEVEL_TOL    = 0.30    
IST                = pytz.timezone('Europe/Berlin') # Auf deine Zeitzone angepasst

PAPER_MODE = True # Auf False setzen, wenn der Testlauf profitabel ist

# Gold contract: 1.00 lot = 100oz
USD_PER_DOLLAR_MOVE_PER_001_LOT = 1.0

# ============================================
# STATE
# ============================================
trades_today          = 0
last_trade_date       = None
last_signal_direction = None
last_signal_time      = 0

# ============================================
# TELEGRAM
# ============================================
def send_telegram(message):
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

# ============================================
# FETCH CANDLES
# ============================================
def get_candles(interval="5min", outputsize=100):
    url    = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL, "interval": interval,
        "outputsize": outputsize, "apikey": TWELVEDATA_API_KEY, "format": "JSON"
    }
    try:
        r    = requests.get(url, params=params, timeout=15)
        data = r.json()
        if "values" not in data:
            return None
        candles = []
        for c in reversed(data["values"]):
            candles.append({
                "time": c["datetime"], "open": float(c["open"]),
                "high": float(c["high"]), "low": float(c["low"]),
                "close": float(c["close"])
            })
        return candles
    except Exception:
        return None

# ============================================
# SWING HIGH / LOW DETECTION
# ============================================
def find_swing_highs(candles, lookback=3):
    swings = []
    for i in range(lookback, len(candles) - lookback):
        if all(candles[i]["high"] >= candles[i-j]["high"] and
               candles[i]["high"] >= candles[i+j]["high"] for j in range(1, lookback+1)):
            swings.append((i, candles[i]["high"]))
    return swings

def find_swing_lows(candles, lookback=3):
    swings = []
    for i in range(lookback, len(candles) - lookback):
        if all(candles[i]["low"] <= candles[i-j]["low"] and
               candles[i]["low"] <= candles[i+j]["low"] for j in range(1, lookback+1)):
            swings.append((i, candles[i]["low"]))
    return swings

# ============================================
# MARKET STRUCTURE — BOS & CHoCH
# ============================================
def analyze_structure_bos(candles):
    if len(candles) < 20:
        return "NEUTRAL", None, False

    swing_highs = find_swing_highs(candles[-60:], lookback=3)
    swing_lows  = find_swing_lows(candles[-60:],  lookback=3)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "NEUTRAL", None, False

    curr_price = candles[-1]["close"]
    last_sh    = swing_highs[-1][1]
    last_sl    = swing_lows[-1][1]
    prev_sh    = swing_highs[-2][1]
    prev_sl    = swing_lows[-2][1]

    closes   = [c["close"] for c in candles]
    ema_fast = sum(closes[-10:]) / 10
    ema_slow = sum(closes[-30:]) / 30
    ema_bias = "BULLISH" if ema_fast > ema_slow else "BEARISH"

    bullish_bos = curr_price > last_sh
    bearish_bos = curr_price < last_sl

    was_bearish   = last_sh < prev_sh and last_sl < prev_sl
    was_bullish   = last_sh > prev_sh and last_sl > prev_sl
    bullish_choch = was_bearish and bullish_bos
    bearish_choch = was_bullish and bearish_bos

    if bullish_choch:
        return "BULLISH", "CHoCH", True
    elif bearish_choch:
        return "BEARISH", "CHoCH", True
    elif bullish_bos and ema_bias == "BULLISH" and last_sh > prev_sh:
        return "BULLISH", "BOS", False
    elif bearish_bos and ema_bias == "BEARISH" and last_sl < prev_sl:
        return "BEARISH", "BOS", False
    elif ema_bias == "BULLISH" and last_sh > prev_sh and last_sl > prev_sl:
        return "BULLISH", None, False
    elif ema_bias == "BEARISH" and last_sh < prev_sh and last_sl < prev_sl:
        return "BEARISH", None, False
    else:
        return "NEUTRAL", None, False

# ============================================
# SMART MONEY CONCEPTS (OB, FVG, LIQUIDITY)
# ============================================
def detect_order_block(candles, bias):
    if len(candles) < 10: return None, None, False
    curr_price = candles[-1]["close"]

    if bias == "BULLISH":
        for i in range(len(candles)-3, max(len(candles)-20, 3), -1):
            c = candles[i]
            if c["close"] < c["open"]:
                next_closes = [candles[i+j]["close"] for j in range(1, 3) if i+j < len(candles)]
                if next_closes and max(next_closes) > c["high"]:
                    return c["high"], c["low"], (c["low"] <= curr_price <= c["high"])

    elif bias == "BEARISH":
        for i in range(len(candles)-3, max(len(candles)-20, 3), -1):
            c = candles[i]
            if c["close"] > c["open"]:
                next_closes = [candles[i+j]["close"] for j in range(1, 3) if i+j < len(candles)]
                if next_closes and min(next_closes) < c["low"]:
                    return c["high"], c["low"], (c["low"] <= curr_price <= c["high"])
    return None, None, False

def detect_liquidity_zones(candles):
    if len(candles) < 10: return False, False, False, False
    recent = candles[-30:]
    highs, lows = [c["high"] for c in recent[:-2]], [c["low"] for c in recent[:-2]]
    last_high, last_low = candles[-1]["high"], candles[-1]["low"]
    
    eq_highs, eq_lows, swept_high, swept_low = False, False, False, False
    
    for i in range(len(highs)):
        for j in range(i+1, len(highs)):
            if abs(highs[i] - highs[j]) <= EQUAL_LEVEL_TOL:
                eq_highs = True
                if last_high > max(highs[i], highs[j]): swept_high = True
    for i in range(len(lows)):
        for j in range(i+1, len(lows)):
            if abs(lows[i] - lows[j]) <= EQUAL_LEVEL_TOL:
                eq_lows = True
                if last_low < min(lows[i], lows[j]): swept_low = True
                
    return eq_highs, eq_lows, swept_high, swept_low

def detect_fvg(candles):
    if len(candles) < 3: return None
    for i in range(len(candles)-3, max(len(candles)-10, 0), -1):
        c1, c3 = candles[i], candles[i+2]
        if c1["high"] < c3["low"] and (c3["low"] - c1["high"]) >= MIN_FVG_SIZE:
            if candles[-1]["close"] > c1["high"]: return "BULLISH_FVG"
        if c1["low"] > c3["high"] and (c1["low"] - c3["high"]) >= MIN_FVG_SIZE:
            if candles[-1]["close"] < c1["low"]: return "BEARISH_FVG"
    return None

# ============================================
# INDICATORS (ATR & RSI)
# ============================================
def calculate_rsi(candles, period=14):
    if len(candles) < period + 1: return 50
    closes = [c["close"] for c in candles[-(period+1):]]
    gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    avg_g, avg_l = sum(gains)/period, sum(losses)/period
    if avg_l == 0: return 100
    return round(100 - (100 / (1 + avg_g / avg_l)), 2)

def calculate_atr(candles, period=14):
    if len(candles) < period + 1: return 5.0
    tr_list = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(tr_list[-period:]) / period

# ============================================
# DYNAMIC SMART SL PLACEMENT
# ============================================
def calculate_sl_distance(candles, bias, ob_high, ob_low, atr):
    curr = candles[-1]["close"]
    baseline_sl = max(atr * 1.5, MIN_SL_DOLLARS) # Dynamischer Stop basierend auf Marktvolatilität
    
    if bias == "LONG":
        if ob_low is not None:
            sl_level = ob_low - (atr * 0.5)
        else:
            sl_level = curr - baseline_sl
        return max(curr - sl_level, MIN_SL_DOLLARS)

    elif bias == "SHORT":
        if ob_high is not None:
            sl_level = ob_high + (atr * 0.5)
        else:
            sl_level = curr + baseline_sl
        return max(sl_level - curr, MIN_SL_DOLLARS)
    
    return MIN_SL_DOLLARS

def calculate_lots(sl_dollars):
    if sl_dollars <= 0: return 0.01, 0, False
    raw_lots = (RISK_AMOUNT / sl_dollars) * 0.01
    lots = round(min(raw_lots, 0.50), 2)
    if lots < 0.01: lots = 0.01
    actual_risk = sl_dollars * (lots / 0.01) * USD_PER_DOLLAR_MOVE_PER_001_LOT
    is_safe = actual_risk <= RISK_AMOUNT * 1.5
    return lots, round(actual_risk, 2), is_safe

# ============================================
# MAIN SIGNAL ENGINE
# ============================================
def generate_signal(candles_5m, candles_15m, candles_1h):
    if not candles_5m or not candles_15m or not candles_1h: return None
    curr_price = candles_5m[-1]["close"]

    # 1H Macro Trend Filter (Verhindert das Traden gegen den ganz großen Trend)
    tf1h_bias, _, _ = analyze_structure_bos(candles_1h)
    if tf1h_bias == "NEUTRAL":
        print(f"Price: {curr_price:.2f} | 1H = NEUTRAL — No trade (Macro Trend unclear)")
        return None

    # 15M Medium Trend
    tf15_bias, bos_type, choch = analyze_structure_bos(candles_15m)
    if tf15_bias != tf1h_bias:
        print(f"Price: {curr_price:.2f} | 15M ({tf15_bias}) clashes with 1H ({tf1h_bias}) — No trade")
        return None

    m5_bias, m5_bos, m5_choch = analyze_structure_bos(candles_5m)
    ob_high, ob_low, price_in_ob = detect_order_block(candles_5m, tf15_bias)
    eq_highs, eq_lows, swept_high, swept_low = detect_liquidity_zones(candles_5m)
    fvg = detect_fvg(candles_5m)
    rsi = calculate_rsi(candles_5m)
    atr = calculate_atr(candles_5m)

    long_score, short_score = 0, 0
    reasons = []

    if tf15_bias == "BULLISH": long_score += 30; reasons.append("15M/1H Bullish Trend")
    elif tf15_bias == "BEARISH": short_score += 30; reasons.append("15M/1H Bearish Trend")

    if m5_bias == "BULLISH": long_score += 20; reasons.append("5M Bullish Structure")
    elif m5_bias == "BEARISH": short_score += 20; reasons.append("5M Bearish Structure")

    if price_in_ob and tf15_bias == "BULLISH": long_score += 20; reasons.append("Inside Bullish OB")
    elif price_in_ob and tf15_bias == "BEARISH": short_score += 20; reasons.append("Inside Bearish OB")

    if swept_low and tf15_bias == "BULLISH": long_score += 15; reasons.append("Sell-side Liquidity Swept")
    if swept_high and tf15_bias == "BEARISH": short_score += 15; reasons.append("Buy-side Liquidity Swept")

    if fvg == "BULLISH_FVG": long_score += 10; reasons.append("Bullish FVG")
    elif fvg == "BEARISH_FVG": short_score += 10; reasons.append("Bearish FVG")

    if rsi < 35: long_score += 10; reasons.append(f"RSI Oversold ({rsi})")
    elif rsi > 65: short_score += 10; reasons.append(f"RSI Overbought ({rsi})")

    signal = None
    if long_score > short_score and long_score >= MIN_CONFIDENCE and tf15_bias == "BULLISH":
        signal, score = "LONG", min(long_score, 99)
    elif short_score > long_score and short_score >= MIN_CONFIDENCE and tf15_bias == "BEARISH":
        signal, score = "SHORT", min(short_score, 99)

    if not signal: return None

    # Dynamischer ATR-Stop-Loss
    sl_dollars = calculate_sl_distance(candles_5m, signal, ob_high, ob_low, atr)
    sl_dollars = min(max(sl_dollars, MIN_SL_DOLLARS), MAX_SL_DOLLARS)
    tp_dollars = sl_dollars * 2.0 # 1:2 Risk Reward

    lots, actual_risk, is_safe = calculate_lots(sl_dollars)
    if not is_safe:
        print(f"Risk ${actual_risk:.2f} too high. SL was ${sl_dollars:.2f}")
        return None

    return {
        "signal": signal, "price": curr_price, "sl_dollars": round(sl_dollars, 2),
        "tp_dollars": round(tp_dollars, 2), "lots": lots, "potential_loss": actual_risk,
        "confidence": score, "reasons": reasons, "rsi": rsi, "atr": round(atr, 2),
        "tf_bias": tf15_bias, "ob_high": ob_high, "ob_low": ob_low
    }

# ============================================
# FORMAT & SEND SIGNAL
# ============================================
def send_signal(sig):
    direction = "🟢 LONG (BUY)" if sig["signal"] == "LONG" else "🔴 SHORT (SELL)"
    sl_price = sig["price"] - sig["sl_dollars"] if sig["signal"] == "LONG" else sig["price"] + sig["sl_dollars"]
    tp_price = sig["price"] + sig["tp_dollars"] if sig["signal"] == "LONG" else sig["price"] - sig["tp_dollars"]

    reasons_text = "\n".join([f"  ✅ {r}" for r in sig["reasons"]])
    mode_banner  = "🧪 <b>PAPER SIGNAL — DEMO</b>\n" if PAPER_MODE else "⚡ <b>LIVE SIGNAL</b>\n"

    msg = f"""{mode_banner}⚔️ <b>XAUUSD SMC SIGNAL</b>
━━━━━━━━━━━━━━━━━━━━
📊 <b>Dir:</b> {direction}
💰 <b>Entry:</b> {sig["price"]:.2f}
🛑 <b>SL:</b> {sl_price:.2f} (${sig["sl_dollars"]:.2f})
🎯 <b>TP:</b> {tp_price:.2f} (${sig["tp_dollars"]:.2f})
📦 <b>Lots:</b> {sig["lots"]} (Risk: ${sig["potential_loss"]:.2f})
🎯 <b>Conf:</b> {sig["confidence"]}% | <b>ATR:</b> {sig["atr"]}
━━━━━━━━━━━━━━━━━━━━
📋 <b>REASONS:</b>
{reasons_text}"""
    send_telegram(msg)
    print(f"✅ Signal: {sig['signal']} @ {sig['price']} | Conf: {sig['confidence']}%")

# ============================================
# MAIN LOOP
# ============================================
def main():
    global trades_today, last_trade_date, last_signal_direction, last_signal_time
    print(f"🚀 XAUUSD SMC Bot LIVE | Paper: {PAPER_MODE} | Min Conf: {MIN_CONFIDENCE}%")
    send_telegram(f"🚀 <b>Bot Gestartet</b>\nModus: {'Paper' if PAPER_MODE else 'Live'}\nFilter: 1H HTF + ATR Stop-Loss")

    while True:
        try:
            today, now_ts = date.today(), time.time()
            if last_trade_date != today:
                trades_today, last_trade_date, last_signal_direction = 0, today, None

            if trades_today >= MAX_TRADES_PER_DAY or datetime.now().weekday() >= 5:
                time.sleep(3600); continue

            candles_5m = get_candles("5min", 100)
            candles_15m = get_candles("15min", 100)
            candles_1h = get_candles("1h", 100) # Neuer 1H Filter

            if not candles_5m or not candles_15m or not candles_1h:
                time.sleep(120); continue

            sig = generate_signal(candles_5m, candles_15m, candles_1h)

            if sig:
                # Cooldown: 60 Minuten Pause in dieselbe Richtung
                if last_signal_direction == sig["signal"] and (now_ts - last_signal_time < 3600):
                    print("Cooldown aktiv — überspringe Signal.")
                else:
                    send_signal(sig)
                    trades_today += 1
                    last_signal_time = now_ts
                    last_signal_direction = sig["signal"]

            sleep_time = (300 - (time.time() % 300)) + 3
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            send_telegram("🔴 <b>Bot gestoppt.</b>")
            break
        except Exception as e:
            print(f"Error: {e}"); time.sleep(120)

if __name__ == "__main__":
    main()
