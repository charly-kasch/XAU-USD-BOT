import os
import math
import requests
import time
from datetime import datetime
import pytz

# ============================================
# CONFIGURATION
# ============================================
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")

SYMBOL       = "XAU/USD"
CAPITAL      = 2500
RISK_PERCENT = 0.02
RISK_AMOUNT  = CAPITAL * RISK_PERCENT   # $50.00 max. Risiko pro Trade (2%)

MAX_TRADES_PER_DAY = 3
MIN_CONFIDENCE     = 75      # Lässt solide SMC-Setups durch
MIN_SL_DOLLARS     = 10.0    # Schutz vor normalem Gold-Rauschen
MAX_SL_DOLLARS     = 30.0
MIN_FVG_SIZE       = 1.50
EQUAL_LEVEL_TOL    = 0.30
ONLY_KILLZONES     = True    # True = 08:00-21:00 MEZ (ohne 14:30 News-Spike & Nacht-Rauschen)
TZ                 = pytz.timezone('Europe/Berlin')

PAPER_MODE = True

# Gold contract: 1.00 lot = 100oz -> $1 move = $100/lot -> $1 move = $1 per 0.01 lot
USD_PER_DOLLAR_MOVE_PER_001_LOT = 1.0

# ============================================
# STATE
# ============================================
trades_today          = 0
last_trade_date       = None
last_signal_direction = None
last_signal_time      = 0
active_trade          = None  # Überwacht den laufenden Trade für dynamisches SL-Trailing

# ============================================
# TELEGRAM
# ============================================
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram Error: TELEGRAM_TOKEN oder TELEGRAM_CHAT_ID fehlt!")
        return
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"Telegram: {r.status_code}")
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
            print(f"API Error ({interval}): {data}")
            return None
        candles = []
        for c in reversed(data["values"]):
            candles.append({
                "time": c["datetime"], "open": float(c["open"]),
                "high": float(c["high"]), "low": float(c["low"]),
                "close": float(c["close"])
            })
        return candles
    except Exception as e:
        print(f"Candle fetch error ({interval}): {e}")
        return None

# ============================================
# INDICATORS: TRUE EMA, RSI, ATR, ADX
# ============================================
def calculate_ema(closes, period):
    if len(closes) < period:
        return sum(closes) / len(closes)
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calculate_rsi(candles, period=14):
    if len(candles) < period + 1:
        return 50
    closes = [c["close"] for c in candles[-(period+1):]]
    gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    avg_g  = sum(gains) / period
    avg_l  = sum(losses) / period
    if avg_l == 0:
        return 100
    return round(100 - (100 / (1 + avg_g / avg_l)), 2)

def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 6.0
    tr_list = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(tr_list[-period:]) / period

def calculate_adx(candles, period=14):
    """Misst die Trendstärke. Unter 18 = Seitwärtsmarkt (Gefahr für SL)."""
    if len(candles) < period * 2:
        return 25.0
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(candles)):
        up_move   = candles[i]["high"] - candles[i-1]["high"]
        down_move = candles[i-1]["low"] - candles[i]["low"]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))

    dx_list = []
    for i in range(period, len(tr_list)):
        tr_sum = sum(tr_list[i-period:i])
        if tr_sum == 0:
            continue
        p_di = 100 * (sum(plus_dm[i-period:i]) / tr_sum)
        m_di = 100 * (sum(minus_dm[i-period:i]) / tr_sum)
        if (p_di + m_di) > 0:
            dx_list.append(100 * abs(p_di - m_di) / (p_di + m_di))

    if not dx_list:
        return 25.0
    return round(sum(dx_list[-period:]) / min(len(dx_list), period), 2)

# ============================================
# SWING HIGH / LOW DETECTION
# ============================================
def find_swing_highs(candles, lookback=3):
    swings = []
    for i in range(lookback, len(candles) - lookback):
        if all(candles[i]["high"] >= candles[i-j]["high"] and
               candles[i]["high"] >= candles[i+j]["high"]
               for j in range(1, lookback+1)):
            swings.append((i, candles[i]["high"]))
    return swings

def find_swing_lows(candles, lookback=3):
    swings = []
    for i in range(lookback, len(candles) - lookback):
        if all(candles[i]["low"] <= candles[i-j]["low"] and
               candles[i]["low"] <= candles[i+j]["low"]
               for j in range(1, lookback+1)):
            swings.append((i, candles[i]["low"]))
    return swings

# ============================================
# MARKET STRUCTURE — BOS & CHoCH (TRUE EMA)
# ============================================
def analyze_structure_bos(candles):
    if len(candles) < 50:
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
    ema_fast = calculate_ema(closes, 20)
    ema_slow = calculate_ema(closes, 50)
    ema_bias = "BULLISH" if ema_fast > ema_slow and curr_price > ema_slow else (
               "BEARISH" if ema_fast < ema_slow and curr_price < ema_slow else "NEUTRAL")

    bullish_bos = curr_price > last_sh
    bearish_bos = curr_price < last_sl

    was_bearish   = last_sh < prev_sh and last_sl < prev_sl
    was_bullish   = last_sh > prev_sh and last_sl > prev_sl
    bullish_choch = was_bearish and bullish_bos
    bearish_choch = was_bullish and bearish_bos

    if bullish_choch and ema_bias != "BEARISH":
        return "BULLISH", "CHoCH", True
    elif bearish_choch and ema_bias != "BULLISH":
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
# ORDER BLOCK, LIQUIDITY & FVG (ON CLOSED CANDLES)
# ============================================
def detect_order_block(candles, bias):
    if len(candles) < 10:
        return None, None, False
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

def detect_liquidity_zones(closed_candles):
    """Prüft auf geschlossener Kerze, ob Stops abgefischt wurden und der Kurs wieder innen schloss."""
    if len(closed_candles) < 10:
        return False, False, False, False

    recent     = closed_candles[-30:]
    highs      = [c["high"] for c in recent[:-1]]
    lows       = [c["low"]  for c in recent[:-1]]
    last_c     = closed_candles[-1]

    equal_highs, equal_lows, swept_high, swept_low = False, False, False, False

    for i in range(len(highs)):
        for j in range(i+1, len(highs)):
            if abs(highs[i] - highs[j]) <= EQUAL_LEVEL_TOL:
                equal_highs = True
                level = max(highs[i], highs[j])
                if last_c["high"] > level and last_c["close"] < level:
                    swept_high = True

    for i in range(len(lows)):
        for j in range(i+1, len(lows)):
            if abs(lows[i] - lows[j]) <= EQUAL_LEVEL_TOL:
                equal_lows = True
                level = min(lows[i], lows[j])
                if last_c["low"] < level and last_c["close"] > level:
                    swept_low = True

    return equal_highs, equal_lows, swept_high, swept_low

def detect_fvg(closed_candles):
    if len(closed_candles) < 3:
        return None
    for i in range(len(closed_candles)-3, max(len(closed_candles)-10, 0), -1):
        c1, c3 = closed_candles[i], closed_candles[i+2]
        if c1["high"] < c3["low"] and (c3["low"] - c1["high"]) >= MIN_FVG_SIZE:
            if closed_candles[-1]["close"] > c1["high"]:
                return "BULLISH_FVG"
        if c1["low"] > c3["high"] and (c1["low"] - c3["high"]) >= MIN_FVG_SIZE:
            if closed_candles[-1]["close"] < c1["low"]:
                return "BEARISH_FVG"
    return None

# ============================================
# CONFIRMATION CANDLE (ANTI-FALLING-KNIFE)
# ============================================
def check_entry_confirmation(closed_candles, direction):
    """
    Verhindert Einstiege, während der Kurs noch ungebremst gegen uns läuft.
    Verlangt eine geschlossene Umkehr-/Bestätigungskerze in Trade-Richtung.
    """
    if len(closed_candles) < 2:
        return False, "None"

    prev = closed_candles[-2]
    curr = closed_candles[-1]
    body = abs(curr["close"] - curr["open"])
    full_range = curr["high"] - curr["low"]
    if full_range == 0:
        return False, "Doji"

    if direction == "LONG":
        is_bullish = curr["close"] > curr["open"]
        closed_high = (curr["close"] - curr["low"]) / full_range >= 0.55
        engulfing = (prev["close"] < prev["open"] and is_bullish and
                     curr["close"] >= prev["open"] and curr["open"] <= prev["close"])
        lower_wick = min(curr["open"], curr["close"]) - curr["low"]
        hammer = is_bullish and lower_wick > body * 1.5

        if engulfing:
            return True, "BULLISH_ENGULF"
        if hammer:
            return True, "HAMMER"
        if is_bullish and closed_high:
            return True, "BULLISH_REJECTION"
        return False, "No Bullish Confirmation"

    elif direction == "SHORT":
        is_bearish = curr["close"] < curr["open"]
        closed_low = (curr["high"] - curr["close"]) / full_range >= 0.55
        engulfing = (prev["close"] > prev["open"] and is_bearish and
                     curr["close"] <= prev["open"] and curr["open"] >= prev["close"])
        upper_wick = curr["high"] - max(curr["open"], curr["close"])
        shooting_star = is_bearish and upper_wick > body * 1.5

        if engulfing:
            return True, "BEARISH_ENGULF"
        if shooting_star:
            return True, "SHOOTING_STAR"
        if is_bearish and closed_low:
            return True, "BEARISH_REJECTION"
        return False, "No Bearish Confirmation"

    return False, "None"

# ============================================
# DYNAMIC SL & LOTS
# ============================================
def calculate_sl_distance(closed_candles, bias, ob_high, ob_low, atr):
    curr        = closed_candles[-1]["close"]
    baseline_sl = max(atr * 1.8, MIN_SL_DOLLARS)

    if bias == "LONG":
        swing_lows = find_swing_lows(closed_candles[-25:])
        struct_sl  = swing_lows[-1][1] - (atr * 0.6) if swing_lows else curr - baseline_sl
        if ob_low is not None:
            struct_sl = min(struct_sl, ob_low - (atr * 0.6))
        return max(curr - struct_sl, baseline_sl)

    elif bias == "SHORT":
        swing_highs = find_swing_highs(closed_candles[-25:])
        struct_sl   = swing_highs[-1][1] + (atr * 0.6) if swing_highs else curr + baseline_sl
        if ob_high is not None:
            struct_sl = max(struct_sl, ob_high + (atr * 0.6))
        return max(struct_sl - curr, baseline_sl)

    return baseline_sl

def calculate_lots(sl_dollars):
    if sl_dollars <= 0:
        return 0.01, 0, False
    raw_lots    = (RISK_AMOUNT / sl_dollars) * 0.01
    lots        = round(min(raw_lots, 0.50), 2)
    if lots < 0.01:
        lots = 0.01
    actual_risk = sl_dollars * (lots / 0.01) * USD_PER_DOLLAR_MOVE_PER_001_LOT
    is_safe     = actual_risk <= RISK_AMOUNT * 1.5
    return lots, round(actual_risk, 2), is_safe

# ============================================
# ACTIVE TRADE MONITOR (1:3+ UNLIMITED PROFIT TRAIL)
# ============================================
def monitor_active_trade(closed_5m):
    """
    Zieht den SL stufenweise nach, ohne den Gewinn bei 1:3 zu deckeln:
    - Bis 2.5R: 1.0R Abstand (1.0R -> BE, 1.5R -> +0.5R, 2.0R -> +1.0R, 2.5R -> +1.5R)
    - Ab 3.0R (1:3 erreicht): 0.5R Abstand (3.0R -> +2.5R, 3.5R -> +3.0R, 4.0R -> +3.5R...)
    """
    global active_trade
    if not active_trade or not closed_5m:
        return

    last_c  = closed_5m[-1]
    d       = active_trade["signal"]
    entry   = active_trade["entry"]
    sl      = active_trade["sl"]
    sl_dist = active_trade["sl_dist"]
    sl_r    = active_trade["sl_r"]

    if d == "LONG":
        if last_c["low"] <= sl:
            locked_r   = round((sl - entry) / sl_dist, 1)
            locked_usd = round(locked_r * RISK_AMOUNT, 2)
            if locked_r > 0:
                status = f"💰 <b>PROFIT GESICHERT (+{locked_r}R | ~+${locked_usd:.2f})!</b>"
            elif locked_r == 0:
                status = "🛡️ <b>BREAK-EVEN HIT ($0)</b>"
            else:
                status = f"🛑 <b>STOP LOSS HIT (-1R | ~-${RISK_AMOUNT:.2f})</b>"
            send_telegram(f"{status}\nXAUUSD LONG @ {entry:.2f} geschlossen bei <b>{sl:.2f}</b>.")
            active_trade = None
            return

        reached_r = (last_c["high"] - entry) / sl_dist

    elif d == "SHORT":
        if last_c["high"] >= sl:
            locked_r   = round((entry - sl) / sl_dist, 1)
            locked_usd = round(locked_r * RISK_AMOUNT, 2)
            if locked_r > 0:
                status = f"💰 <b>PROFIT GESICHERT (+{locked_r}R | ~+${locked_usd:.2f})!</b>"
            elif locked_r == 0:
                status = "🛡️ <b>BREAK-EVEN HIT ($0)</b>"
            else:
                status = f"🛑 <b>STOP LOSS HIT (-1R | ~-${RISK_AMOUNT:.2f})</b>"
            send_telegram(f"{status}\nXAUUSD SHORT @ {entry:.2f} geschlossen bei <b>{sl:.2f}</b>.")
            active_trade = None
            return

        reached_r = (entry - last_c["low"]) / sl_dist
    else:
        return

    milestone_r = math.floor(reached_r * 2) / 2.0
    if milestone_r < 1.0:
        return

    new_sl_r = round(milestone_r - 0.5, 1) if milestone_r >= 3.0 else round(milestone_r - 1.0, 1)

    if new_sl_r > sl_r:
        new_sl_price = round(entry + (new_sl_r * sl_dist), 2) if d == "LONG" else round(entry - (new_sl_r * sl_dist), 2)
        active_trade["sl"]   = new_sl_price
        active_trade["sl_r"] = new_sl_r
        locked_usd           = round(new_sl_r * RISK_AMOUNT, 2)

        if new_sl_r == 0.0:
            header = "🛡️ <b>BREAK-EVEN AKTIVIERT (0R)!</b>"
            info   = "Trade ist jetzt komplett risikofrei."
        elif new_sl_r >= 2.5:
            header = f"🚀 <b>1:3+ RUNNER-TRAIL (+{new_sl_r}R gesichert)!</b>"
            info   = f"Sicherer Gewinn: <b>~+${locked_usd:.2f}</b> — Trade läuft ohne Deckel weiter!"
        else:
            header = f"💰 <b>PROFIT-LOCK AKTIVIERT (+{new_sl_r}R)!</b>"
            info   = f"Sicherer Gewinn: <b>~+${locked_usd:.2f}</b>"

        send_telegram(
            f"{header}\n"
            f"XAUUSD {d} hat <b>{milestone_r:.1f}R</b> erreicht.\n"
            f"👉 Ziehe deinen SL in MT5 jetzt auf <b>{new_sl_price:.2f}</b>!\n"
            f"{info}"
        )

# ============================================
# MAIN SIGNAL ENGINE
# ============================================
def generate_signal(candles_5m, candles_15m, candles_1h):
    if not candles_5m or not candles_15m or not candles_1h:
        return None

    curr_price = candles_5m[-1]["close"]
    closed_5m  = candles_5m[:-1]
    closed_15m = candles_15m[:-1]
    closed_1h  = candles_1h[:-1]

    # 1. ADX Trendstärke-Check (verhindert SL in flachen Seitwärtsmärkten)
    adx_15m = calculate_adx(closed_15m)
    if adx_15m < 18:
        print(f"Price: {curr_price:.2f} | ADX 15M = {adx_15m} (<18) — Markt zu flach, kein Trade.")
        return None

    # 2. 1H Macro Trend Gate
    tf1h_bias, _, _ = analyze_structure_bos(closed_1h)
    if tf1h_bias == "NEUTRAL":
        print(f"Price: {curr_price:.2f} | 1H = NEUTRAL — No trade (Macro Trend unclear)")
        return None

    # 3. 15M Medium Trend Gate
    tf_bias, bos_type, choch = analyze_structure_bos(closed_15m)
    if tf_bias != tf1h_bias:
        print(f"Price: {curr_price:.2f} | 15M ({tf_bias}) != 1H ({tf1h_bias}) — No trade")
        return None

    # 4. 5M Analyse auf abgeschlossenen Kerzen
    m5_bias, m5_bos, _                       = analyze_structure_bos(closed_5m)
    ob_high, ob_low, price_in_ob             = detect_order_block(closed_5m, tf_bias)
    eq_highs, eq_lows, swept_high, swept_low = detect_liquidity_zones(closed_5m)
    fvg                                      = detect_fvg(closed_5m)
    rsi                                      = calculate_rsi(closed_5m)
    atr                                      = calculate_atr(closed_5m)

    long_score,  long_reasons  = 0, []
    short_score, short_reasons = 0, []

    if tf_bias == "BULLISH":
        long_score += 30
        long_reasons.append(f"1H & 15M Bullish (ADX {adx_15m})")
    elif tf_bias == "BEARISH":
        short_score += 30
        short_reasons.append(f"1H & 15M Bearish (ADX {adx_15m})")

    if choch and tf_bias == "BULLISH":
        long_score += 10
        long_reasons.append("15M CHoCH Reversal")
    elif choch and tf_bias == "BEARISH":
        short_score += 10
        short_reasons.append("15M CHoCH Reversal")

    if m5_bias == "BULLISH":
        long_score += 15
        long_reasons.append(f"M5 Bullish{' BOS' if m5_bos else ''}")
    elif m5_bias == "BEARISH":
        short_score += 15
        short_reasons.append(f"M5 Bearish{' BOS' if m5_bos else ''}")

    if price_in_ob and tf_bias == "BULLISH":
        long_score += 20
        long_reasons.append("Price in Bullish Order Block")
    elif price_in_ob and tf_bias == "BEARISH":
        short_score += 20
        short_reasons.append("Price in Bearish Order Block")

    if swept_low and tf_bias == "BULLISH":
        long_score += 15
        long_reasons.append("Equal Lows Swept (Stop-Hunt confirmed)")
    if swept_high and tf_bias == "BEARISH":
        short_score += 15
        short_reasons.append("Equal Highs Swept (Stop-Hunt confirmed)")

    if fvg == "BULLISH_FVG":
        long_score += 10
        long_reasons.append(f"Bullish FVG (>=${MIN_FVG_SIZE})")
    elif fvg == "BEARISH_FVG":
        short_score += 10
        short_reasons.append(f"Bearish FVG (>=${MIN_FVG_SIZE})")

    if 30 <= rsi <= 45 and tf_bias == "BULLISH":
        long_score += 8
        long_reasons.append(f"RSI Pullback Zone ({rsi})")
    elif 55 <= rsi <= 70 and tf_bias == "BEARISH":
        short_score += 8
        short_reasons.append(f"RSI Pullback Zone ({rsi})")

    signal, score, reasons = None, 0, []
    if long_score > short_score and long_score >= MIN_CONFIDENCE and tf_bias == "BULLISH":
        signal, score, reasons = "LONG", min(long_score, 99), long_reasons
    elif short_score > long_score and short_score >= MIN_CONFIDENCE and tf_bias == "BEARISH":
        signal, score, reasons = "SHORT", min(short_score, 99), short_reasons

    if not signal:
        print(f"Price: {curr_price:.2f} | Long: {long_score} | Short: {short_score} (<{MIN_CONFIDENCE})")
        return None

    # 5. Pflicht-Bestätigungskerze prüfen (kein Einstieg gegen laufende Kerze!)
    confirmed, candle_pattern = check_entry_confirmation(closed_5m, signal)
    if not confirmed:
        print(f"Signal {signal} zurückgehalten: Warte auf Bestätigungskerze ({candle_pattern}).")
        return None
    reasons.append(f"Entry Confirmation: {candle_pattern}")

    sl_dollars = calculate_sl_distance(closed_5m, signal, ob_high, ob_low, atr)
    sl_dollars = min(max(sl_dollars, MIN_SL_DOLLARS), MAX_SL_DOLLARS)
    tp3_dollars = sl_dollars * 3.0

    lots, actual_risk, is_safe = calculate_lots(sl_dollars)
    if not is_safe:
        return None

    return {
        "signal": signal, "price": curr_price,
        "sl_dollars": round(sl_dollars, 2), "tp3_dollars": round(tp3_dollars, 2),
        "lots": lots, "potential_loss": actual_risk, "confidence": score,
        "reasons": reasons, "rsi": rsi, "atr": round(atr, 2), "adx": adx_15m,
        "tf_bias": tf_bias, "bos_type": bos_type or "None", "choch": choch,
        "ob_high": ob_high, "ob_low": ob_low, "price_in_ob": price_in_ob,
        "swept_high": swept_high, "swept_low": swept_low,
        "fvg": fvg or "None", "pattern": candle_pattern
    }

# ============================================
# FORMAT & SEND SIGNAL
# ============================================
def send_signal(sig):
    global active_trade
    direction = "🟢 LONG (BUY)" if sig["signal"] == "LONG" else "🔴 SHORT (SELL)"
    emoji     = "📈" if sig["signal"] == "LONG" else "📉"

    if sig["signal"] == "LONG":
        sl_price  = sig["price"] - sig["sl_dollars"]
        be_price  = sig["price"] + sig["sl_dollars"]
        tp3_price = sig["price"] + sig["tp3_dollars"]
    else:
        sl_price  = sig["price"] + sig["sl_dollars"]
        be_price  = sig["price"] - sig["sl_dollars"]
        tp3_price = sig["price"] - sig["tp3_dollars"]

    active_trade = {
        "signal":  sig["signal"],
        "entry":   sig["price"],
        "sl":      sl_price,
        "sl_dist": sig["sl_dollars"],
        "sl_r":    -1.0
    }

    reasons_text = "\n".join([f"  ✅ {r}" for r in sig["reasons"]])
    now_str      = datetime.now(TZ).strftime('%d %b %Y %H:%M %Z')
    mode_banner  = (
        "🧪 <b>PAPER SIGNAL — NOT EXECUTED</b>\n<i>Review only.</i>\n\n"
        if PAPER_MODE else "⚡ <b>LIVE SIGNAL</b>\n\n"
    )

    msg = f"""{mode_banner}⚔️ <b>XAUUSD SIGNAL</b> {emoji}
━━━━━━━━━━━━━━━━━━━━
📊 <b>Direction:</b> {direction}
💰 <b>Entry:</b> {sig["price"]:.2f} (Market)
🛑 <b>Stop Loss:</b> {sl_price:.2f} (${sig["sl_dollars"]:.2f} away)
🛡️ <b>1. Schritt (BE 1:1):</b> {be_price:.2f}
🎯 <b>1:3 Ziel-Marke:</b> {tp3_price:.2f} (${sig["tp3_dollars"]:.2f} away)
🔓 <b>Take Profit:</b> <b>OFFEN</b> (SL trailt ab 1:3 weiter!)
📦 <b>Lots:</b> {sig["lots"]} | <b>Conf:</b> {sig["confidence"]}%
💵 <b>Max Risk:</b> ~${sig["potential_loss"]:.2f}
━━━━━━━━━━━━━━━━━━━━
📋 <b>WHY THIS TRADE:</b>
{reasons_text}

📈 <b>1H/15M Bias:</b> {sig["tf_bias"]} | <b>ADX:</b> {sig["adx"]}
🕯 <b>Trigger:</b> {sig["pattern"]} | <b>ATR:</b> ${sig["atr"]}
━━━━━━━━━━━━━━━━━━━━
🕐 {now_str}
⚠️ <i>In MT5 KEINEN festen TP setzen — Bot meldet jede SL-Stufe!</i>"""

    send_telegram(msg)
    print(f"✅ Signal: {sig['signal']} @ {sig['price']} | 1:3 Ziel @ {tp3_price:.2f}")

# ============================================
# SESSION & KILLZONE CHECK (08:00 - 21:00 MEZ)
# ============================================
def check_session_allowed(now):
    """
    Deckt den kompletten europäischen & US-Handelstag ab (08:00 - 21:00 MEZ)
    und pausiert nur rund um 14:30 Uhr (US-Wirtschaftsdaten) sowie in der
    volumenarmen Nachtsession (21:00 - 08:00 Uhr), um API-Credits zu sparen.
    """
    minutes    = now.hour * 60 + now.minute
    in_eu      = (8 * 60) <= minutes <= (14 * 60 + 20)
    in_us      = (14 * 60 + 40) <= minutes <= (21 * 60)

    if in_eu:
        return True, "EU/London Session 🇪🇺🇬🇧"
    if in_us:
        return True, "US/New York Session 🗽"
    return False, "Nachtruhe / News-Schutz ⏸️"

# ============================================
# MAIN LOOP
# ============================================
def main():
    global trades_today, last_trade_date, last_signal_direction, last_signal_time

    print("🚀 XAUUSD SMC Bot Starting...")
    send_telegram(
        f"🚀 <b>XAUUSD SMC Bot LIVE</b>\n\n"
        f"{'🧪 PAPER MODE' if PAPER_MODE else '⚡ LIVE MODE'}\n\n"
        f"Strategy: BOS + CHoCH + Order Blocks\n"
        f"+ Liquidity Sweeps + FVG + ADX + Confirmation\n\n"
        f"Capital: ${CAPITAL} | Risk/trade: ${RISK_AMOUNT:.2f}\n"
        f"Session: 08:00 - 21:00 MEZ (Credit-Saver aktiv)\n"
        f"Target: 1:3+ Runner (Unlimited Trailing SL)\n"
        f"Min Confidence: {MIN_CONFIDENCE}% | Max/Day: {MAX_TRADES_PER_DAY}\n\n"
        f"Scanning every 5 minutes ⚔️"
    )

    while True:
        try:
            now   = datetime.now(TZ)
            today = now.date()

            if last_trade_date != today:
                trades_today          = 0
                last_trade_date       = today
                last_signal_direction = None
                send_telegram(f"📅 <b>New Day: {today}</b>\nSignals remaining: {MAX_TRADES_PER_DAY}")

            if now.weekday() >= 5:
                time.sleep(3600)
                continue

            allowed, session_label = check_session_allowed(now)
            print(f"\n[{now.strftime('%H:%M')}] {session_label} | Signals: {trades_today}/{MAX_TRADES_PER_DAY}")

            # Außerhalb 08:00-21:00 Uhr keine API-Credits verbrauchen (außer ein Trade läuft noch)
            if not active_trade and ONLY_KILLZONES and not allowed:
                print("Nachtruhe / News-Schutz — pausiere API-Abfragen (spart Credits).")
                ts         = time.time()
                sleep_time = (300 - (ts % 300)) + 3
                time.sleep(sleep_time)
                continue

            candles_5m  = get_candles("5min", 100)
            time.sleep(2)
            candles_15m = get_candles("15min", 100)
            time.sleep(2)
            candles_1h  = get_candles("1h", 100)

            if not candles_5m or not candles_15m or not candles_1h:
                print("Fetch failed. Retry in 2min...")
                time.sleep(120)
                continue

            # Laufenden Trade zuerst auf dynamische SL-Stufen prüfen
            if active_trade:
                monitor_active_trade(candles_5m[:-1])

            if active_trade:
                print("Aktiver Trade läuft noch — warte auf Abschluss vor neuem Signal.")
            elif trades_today >= MAX_TRADES_PER_DAY:
                print("Tageslimit erreicht.")
            else:
                sig = generate_signal(candles_5m, candles_15m, candles_1h)
                if sig:
                    now_ts = time.time()
                    if last_signal_direction == sig["signal"] and (now_ts - last_signal_time < 3600):
                        print("Cooldown aktiv — überspringe Signal.")
                    else:
                        send_signal(sig)
                        trades_today         += 1
                        last_signal_time      = now_ts
                        last_signal_direction = sig["signal"]

            ts         = time.time()
            sleep_time = (300 - (ts % 300)) + 3
            print(f"Sleeping {int(sleep_time)}s until next candle close...")
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            send_telegram("🔴 <b>Bot stopped.</b>")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(120)

if __name__ == "__main__":
    main()
