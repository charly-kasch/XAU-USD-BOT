import os
import time
import json
import math
import requests
from datetime import datetime, timedelta

# ============================================
# CONFIGURATION (XAUUSD SMC BOT - 10K+ SETTINGS)
# ============================================
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "")
TWELVEDATA_API_KEY   = os.getenv("TWELVEDATA_API_KEY", "81f11b0a3bda4cc6991d8e2f9d4f3da2")

SYMBOL               = "XAU/USD"
INTERVAL             = "5min"
CAPITAL              = float(os.getenv("CAPITAL", "2500.0"))

# Verfeinertes Anti-Martingale Zinseszins-Modell (10k+ Settings)
BASE_RISK_PERCENT    = 0.028   # 2.8% Standard-Risiko
SAFE_RISK_PERCENT    = 0.0125  # 1.25% Schutz-Risiko nach 2 Verlusten in Folge
BOOST_RISK_PERCENT   = 0.038   # 3.8% Wachstums-Risiko am Konto-Allzeithoch
MAX_LOTS             = 5.00

# Spread / Slippage in USD-Bewegung (0.0 wie im 10k+ Lauf)
SPREAD_SLIPPAGE_USD  = 0.0

# Signal- & Risikoparameter
MAX_TRADES_PER_DAY   = 3
MAX_LOSSES_PER_DAY   = 2
MIN_CONFIDENCE       = 65
MIN_ADX              = 15.5
MIN_SL_DOLLARS       = 10.0
MAX_SL_DOLLARS       = 30.0
MIN_FVG_SIZE         = 1.20
EQUAL_LEVEL_TOL      = 0.35
ONLY_KILLZONES       = True
COOLDOWN_SECONDS     = 2700    # 45 Min Pause zwischen gleichen Richtungs-Signalen
POLL_INTERVAL_SEC    = 60

# Verbesserter Runner-Trail ab +3.0R
RUNNER_TRAIL_GAP_R   = 0.80

STATE_FILE           = os.getenv("BOT_STATE_FILE", "bot_state_xauusd.json")
USD_PER_DOLLAR_MOVE_PER_001_LOT = 1.0


# ============================================
# TELEGRAM & STATE PERSISTENCE
# ============================================
def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(text, flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        requests.post(url, json=payload, timeout=15)
    except requests.exceptions.RequestException as e:
        print(f"Telegram-Fehler: {e}", flush=True)


def load_state():
    default_state = {
        "equity": CAPITAL,
        "peak_equity": CAPITAL,
        "consecutive_losses": 0,
        "trades_today": 0,
        "losses_today": 0,
        "last_date": None,
        "last_processed_candle": None,
        "last_sig_dir": None,
        "last_sig_ts": None,
        "active_trade": None,
        "history": [],
    }
    if not os.path.exists(STATE_FILE):
        return default_state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        default_state.update(data)
    except Exception as e:
        print(f"State konnte nicht geladen werden ({e}), nutze Standardwerte.", flush=True)
    return default_state


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        print(f"State konnte nicht gespeichert werden ({e}).", flush=True)


# ============================================
# MARKTDATEN & HIGHER-TF AGGREGATION
# ============================================
def fetch_candles(outputsize=250):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "outputsize": outputsize,
        "timezone": "Europe/Berlin",
        "apikey": TWELVEDATA_API_KEY,
        "format": "JSON",
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "values" not in data or not isinstance(data["values"], list):
        raise RuntimeError(f"Ungültige API-Antwort: {data.get('message', data)}")

    candles = []
    for c in reversed(data["values"]):
        dt = datetime.strptime(c["datetime"], "%Y-%m-%d %H:%M:%S")
        candles.append({
            "dt": dt,
            "time": c["datetime"],
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
        })
    return candles


def aggregate_candles(candles_5m, tf_minutes):
    buckets = {}
    for c in candles_5m:
        dt = c["dt"]
        if tf_minutes == 15:
            b_start = dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)
        elif tf_minutes == 60:
            b_start = dt.replace(minute=0, second=0, microsecond=0)
        else:
            b_start = dt

        key = b_start.strftime("%Y-%m-%d %H:%M:%S")
        if key not in buckets:
            buckets[key] = {
                "dt": b_start,
                "time": key,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
            }
        else:
            b = buckets[key]
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]

    return [buckets[k] for k in sorted(buckets.keys())]


# ============================================
# INDIKATOREN & SMC-LOGIK
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
    closes = [c["close"] for c in candles[-(period + 1):]]
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    if avg_l == 0:
        return 100
    return round(100 - (100 / (1 + avg_g / avg_l)), 2)


def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 6.0
    tr_list = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(tr_list[-period:]) / period


def calculate_adx(candles, period=14):
    if len(candles) < period * 2:
        return 25.0
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(candles)):
        up_move = candles[i]["high"] - candles[i - 1]["high"]
        down_move = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))

    dx_list = []
    for i in range(period, len(tr_list)):
        tr_sum = sum(tr_list[i - period:i])
        if tr_sum == 0:
            continue
        p_di = 100 * (sum(plus_dm[i - period:i]) / tr_sum)
        m_di = 100 * (sum(minus_dm[i - period:i]) / tr_sum)
        if (p_di + m_di) > 0:
            dx_list.append(100 * abs(p_di - m_di) / (p_di + m_di))

    if not dx_list:
        return 25.0
    return round(sum(dx_list[-period:]) / min(len(dx_list), period), 2)


def find_swing_highs(candles, lookback=3):
    swings = []
    for i in range(lookback, len(candles) - lookback):
        if all(
            candles[i]["high"] >= candles[i - j]["high"]
            and candles[i]["high"] >= candles[i + j]["high"]
            for j in range(1, lookback + 1)
        ):
            swings.append((i, candles[i]["high"]))
    return swings


def find_swing_lows(candles, lookback=3):
    swings = []
    for i in range(lookback, len(candles) - lookback):
        if all(
            candles[i]["low"] <= candles[i - j]["low"]
            and candles[i]["low"] <= candles[i + j]["low"]
            for j in range(1, lookback + 1)
        ):
            swings.append((i, candles[i]["low"]))
    return swings


def analyze_structure_bos(candles, allow_strong_ema=False):
    if len(candles) < 50:
        return "NEUTRAL", None, False

    swing_highs = find_swing_highs(candles[-60:], lookback=3)
    swing_lows = find_swing_lows(candles[-60:], lookback=3)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "NEUTRAL", None, False

    curr_price = candles[-1]["close"]
    last_sh, last_sl = swing_highs[-1][1], swing_lows[-1][1]
    prev_sh, prev_sl = swing_highs[-2][1], swing_lows[-2][1]

    closes = [c["close"] for c in candles]
    ema_fast = calculate_ema(closes, 20)
    ema_slow = calculate_ema(closes, 50)
    ema_bias = (
        "BULLISH"
        if ema_fast > ema_slow and curr_price > ema_slow
        else ("BEARISH" if ema_fast < ema_slow and curr_price < ema_slow else "NEUTRAL")
    )

    bullish_bos = curr_price > last_sh
    bearish_bos = curr_price < last_sl

    was_bearish = last_sh < prev_sh and last_sl < prev_sl
    was_bullish = last_sh > prev_sh and last_sl > prev_sl
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
    elif allow_strong_ema and ema_bias in ("BULLISH", "BEARISH"):
        atr_tf = calculate_atr(candles)
        if abs(ema_fast - ema_slow) >= atr_tf * 0.25:
            return ema_bias, None, False
    return "NEUTRAL", None, False


def detect_order_block(candles, bias):
    if len(candles) < 10:
        return None, None, False
    recent_low = min(c["low"] for c in candles[-4:])
    recent_high = max(c["high"] for c in candles[-4:])

    if bias == "BULLISH":
        for i in range(len(candles) - 4, max(len(candles) - 30, 3), -1):
            c = candles[i]
            if c["close"] < c["open"]:
                next_closes = [candles[i + j]["close"] for j in range(1, 3) if i + j < len(candles)]
                if next_closes and max(next_closes) > c["high"]:
                    touched = recent_low <= c["high"] and recent_high >= c["low"]
                    return c["high"], c["low"], touched

    elif bias == "BEARISH":
        for i in range(len(candles) - 4, max(len(candles) - 30, 3), -1):
            c = candles[i]
            if c["close"] > c["open"]:
                next_closes = [candles[i + j]["close"] for j in range(1, 3) if i + j < len(candles)]
                if next_closes and min(next_closes) < c["low"]:
                    touched = recent_low <= c["high"] and recent_high >= c["low"]
                    return c["high"], c["low"], touched
    return None, None, False


def detect_liquidity_zones(closed_candles):
    if len(closed_candles) < 10:
        return False, False, False, False
    recent = closed_candles[-35:-3]
    trigger = closed_candles[-3:]
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]
    equal_highs, equal_lows, swept_high, swept_low = False, False, False, False

    for i in range(len(highs)):
        for j in range(i + 1, len(highs)):
            if abs(highs[i] - highs[j]) <= EQUAL_LEVEL_TOL:
                equal_highs = True
                level = max(highs[i], highs[j])
                if any(c["high"] > level and c["close"] < level for c in trigger):
                    swept_high = True

    for i in range(len(lows)):
        for j in range(i + 1, len(lows)):
            if abs(lows[i] - lows[j]) <= EQUAL_LEVEL_TOL:
                equal_lows = True
                level = min(lows[i], lows[j])
                if any(c["low"] < level and c["close"] > level for c in trigger):
                    swept_low = True
    return equal_highs, equal_lows, swept_high, swept_low


def detect_fvg(closed_candles):
    if len(closed_candles) < 3:
        return None
    for i in range(len(closed_candles) - 3, max(len(closed_candles) - 12, 0), -1):
        c1, c3 = closed_candles[i], closed_candles[i + 2]
        if c1["high"] < c3["low"] and (c3["low"] - c1["high"]) >= MIN_FVG_SIZE:
            if closed_candles[-1]["close"] > c1["high"]:
                return "BULLISH_FVG"
        if c1["low"] > c3["high"] and (c1["low"] - c3["high"]) >= MIN_FVG_SIZE:
            if closed_candles[-1]["close"] < c1["low"]:
                return "BEARISH_FVG"
    return None


def check_entry_confirmation(closed_candles, direction):
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
        engulfing = (
            prev["close"] < prev["open"]
            and is_bullish
            and curr["close"] >= prev["open"]
            and curr["open"] <= prev["close"]
        )
        hammer = is_bullish and (min(curr["open"], curr["close"]) - curr["low"]) > body * 1.5
        if engulfing:
            return True, "BULLISH_ENGULF"
        if hammer:
            return True, "HAMMER"
        if is_bullish and closed_high:
            return True, "BULLISH_REJECTION"
        return False, "None"

    elif direction == "SHORT":
        is_bearish = curr["close"] < curr["open"]
        closed_low = (curr["high"] - curr["close"]) / full_range >= 0.55
        engulfing = (
            prev["close"] > prev["open"]
            and is_bearish
            and curr["close"] <= prev["open"]
            and curr["open"] >= prev["close"]
        )
        shooting_star = is_bearish and (curr["high"] - max(curr["open"], curr["close"])) > body * 1.5
        if engulfing:
            return True, "BEARISH_ENGULF"
        if shooting_star:
            return True, "SHOOTING_STAR"
        if is_bearish and closed_low:
            return True, "BEARISH_REJECTION"
        return False, "None"
    return False, "None"


def calculate_sl_distance(closed_candles, bias, ob_high, ob_low, atr):
    curr = closed_candles[-1]["close"]
    baseline_sl = max(atr * 1.8, MIN_SL_DOLLARS)
    if bias == "LONG":
        swing_lows = find_swing_lows(closed_candles[-25:])
        struct_sl = swing_lows[-1][1] - (atr * 0.6) if swing_lows else curr - baseline_sl
        if ob_low is not None:
            struct_sl = min(struct_sl, ob_low - (atr * 0.6))
        return max(curr - struct_sl, baseline_sl)
    elif bias == "SHORT":
        swing_highs = find_swing_highs(closed_candles[-25:])
        struct_sl = swing_highs[-1][1] + (atr * 0.6) if swing_highs else curr + baseline_sl
        if ob_high is not None:
            struct_sl = max(struct_sl, ob_high + (atr * 0.6))
        return max(struct_sl - curr, baseline_sl)
    return baseline_sl


def get_adaptive_risk_percent(current_equity, peak_equity, consecutive_losses):
    if consecutive_losses >= 2:
        return SAFE_RISK_PERCENT
    if current_equity >= peak_equity and current_equity > CAPITAL:
        return BOOST_RISK_PERCENT
    return BASE_RISK_PERCENT


def calculate_lots(sl_dollars, current_equity, risk_pct, max_lots=MAX_LOTS):
    if sl_dollars <= 0:
        return 0.01, 0, False
    risk_target = current_equity * risk_pct
    raw_lots = (risk_target / sl_dollars) * 0.01
    lots = max(round(min(raw_lots, max_lots), 2), 0.01)
    actual_risk = sl_dollars * (lots / 0.01) * USD_PER_DOLLAR_MOVE_PER_001_LOT
    return lots, round(actual_risk, 2), (actual_risk <= risk_target * 1.5)


def check_session_allowed(dt_close):
    if dt_close.weekday() >= 5:
        return False
    minutes = dt_close.hour * 60 + dt_close.minute
    in_eu = (8 * 60) <= minutes <= (14 * 60 + 20)
    in_us = (14 * 60 + 40) <= minutes <= (20 * 60)
    return in_eu or in_us


def evaluate_signal(closed_5m, closed_15m, closed_1h, current_equity, peak_equity, consecutive_losses):
    if len(closed_5m) < 60 or len(closed_15m) < 50 or len(closed_1h) < 50:
        return None

    curr_price = closed_5m[-1]["close"]
    atr = calculate_atr(closed_5m)

    last_candle_range = closed_5m[-1]["high"] - closed_5m[-1]["low"]
    if last_candle_range > atr * 2.7:
        return None

    adx_15m = calculate_adx(closed_15m)
    if adx_15m < MIN_ADX:
        return None

    tf1h_bias, _, _ = analyze_structure_bos(closed_1h, allow_strong_ema=True)
    if tf1h_bias == "NEUTRAL":
        return None

    tf_bias, _, choch = analyze_structure_bos(closed_15m, allow_strong_ema=False)
    if tf_bias != tf1h_bias:
        return None

    m5_bias, _, _ = analyze_structure_bos(closed_5m, allow_strong_ema=False)
    ob_high, ob_low, price_in_ob = detect_order_block(closed_5m, tf_bias)
    _, _, swept_high, swept_low = detect_liquidity_zones(closed_5m)
    fvg = detect_fvg(closed_5m)
    rsi = calculate_rsi(closed_5m)

    long_score, short_score = 0, 0
    if tf_bias == "BULLISH":
        long_score += 30
    elif tf_bias == "BEARISH":
        short_score += 30

    if choch and tf_bias == "BULLISH":
        long_score += 10
    elif choch and tf_bias == "BEARISH":
        short_score += 10

    if m5_bias == "BULLISH":
        long_score += 15
    elif m5_bias == "BEARISH":
        short_score += 15

    if price_in_ob and tf_bias == "BULLISH":
        long_score += 20
    elif price_in_ob and tf_bias == "BEARISH":
        short_score += 20

    if swept_low and tf_bias == "BULLISH":
        long_score += 15
    if swept_high and tf_bias == "BEARISH":
        short_score += 15

    if fvg == "BULLISH_FVG":
        long_score += 10
    elif fvg == "BEARISH_FVG":
        short_score += 10

    if 28 <= rsi <= 50 and tf_bias == "BULLISH":
        long_score += 8
    elif 50 <= rsi <= 72 and tf_bias == "BEARISH":
        short_score += 8

    signal, score = None, 0
    if long_score > short_score and long_score >= MIN_CONFIDENCE and tf_bias == "BULLISH":
        signal, score = "LONG", min(long_score, 99)
    elif short_score > long_score and short_score >= MIN_CONFIDENCE and tf_bias == "BEARISH":
        signal, score = "SHORT", min(short_score, 99)

    if not signal:
        return None

    confirmed, pattern = check_entry_confirmation(closed_5m, signal)
    if not confirmed:
        return None

    risk_pct = get_adaptive_risk_percent(current_equity, peak_equity, consecutive_losses)
    sl_dollars = min(max(calculate_sl_distance(closed_5m, signal, ob_high, ob_low, atr), MIN_SL_DOLLARS), MAX_SL_DOLLARS)
    lots, actual_risk, is_safe = calculate_lots(sl_dollars, current_equity, risk_pct, MAX_LOTS)
    if not is_safe:
        return None

    if signal == "LONG":
        entry_adj = round(curr_price + SPREAD_SLIPPAGE_USD, 2)
        sl_price = round(entry_adj - sl_dollars, 2)
    else:
        entry_adj = round(curr_price - SPREAD_SLIPPAGE_USD, 2)
        sl_price = round(entry_adj + sl_dollars, 2)

    return {
        "signal": signal,
        "entry": entry_adj,
        "sl": sl_price,
        "sl_dist": sl_dollars,
        "sl_r": -1.0,
        "lots": lots,
        "risk_usd": actual_risk,
        "risk_pct": round(risk_pct * 100, 2),
        "conf": score,
        "pattern": pattern,
        "max_r_reached": 0.0,
    }


# ============================================
# TRADE-MANAGEMENT & LIVE-LOOP
# ============================================
def manage_active_trade(state, candle, close_dt):
    trade = state["active_trade"]
    if not trade:
        return

    d = trade["signal"]
    entry = trade["entry"]
    sl_dist = trade["sl_dist"]

    hit_sl = (candle["low"] <= trade["sl"]) if d == "LONG" else (candle["high"] >= trade["sl"])
    if hit_sl:
        pnl_r = trade["sl_r"]
        pnl_usd = round(pnl_r * trade["risk_usd"], 2)
        state["equity"] = round(state["equity"] + pnl_usd, 2)
        state["peak_equity"] = max(state["peak_equity"], state["equity"])
        if pnl_r < 0:
            state["losses_today"] += 1
            state["consecutive_losses"] += 1
        elif pnl_r > 0:
            state["consecutive_losses"] = 0

        closed_record = {
            **trade,
            "exit_time": close_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "exit_price": trade["sl"],
            "pnl_r": pnl_r,
            "pnl_usd": pnl_usd,
        }
        state["history"].append(closed_record)
        state["active_trade"] = None

        send_telegram_message(
            f"🔔 <b>Trade geschlossen ({SYMBOL})</b>\n"
            f"Richtung: <b>{d}</b> | Ergebnis: <b>{pnl_r:+.1f}R</b> (<b>${pnl_usd:+.2f}</b>)\n"
            f"Exit: {trade['sl']:.2f} | Kontostand: <b>${state['equity']:.2f}</b>"
        )
        return

    reached_r = ((candle["high"] - entry) / sl_dist) if d == "LONG" else ((entry - candle["low"]) / sl_dist)
    trade["max_r_reached"] = max(trade["max_r_reached"], round(reached_r, 2))
    milestone_r = math.floor(reached_r * 2) / 2.0
    if milestone_r >= 1.0:
        new_sl_r = (
            round(milestone_r - RUNNER_TRAIL_GAP_R, 2)
            if milestone_r >= 3.0
            else round(milestone_r - 1.0, 2)
        )
        if new_sl_r > trade["sl_r"]:
            trade["sl_r"] = new_sl_r
            trade["sl"] = round(entry + new_sl_r * sl_dist, 2) if d == "LONG" else round(entry - new_sl_r * sl_dist, 2)


def run_bot():
    state = load_state()
    print(f"🤖 XAU/USD SMC Bot gestartet | Kapital: ${state['equity']:.2f}", flush=True)

    while True:
        try:
            candles_5m = fetch_candles(outputsize=250)
            if len(candles_5m) >= 60:
                latest_candle = candles_5m[-1]
                c_key = latest_candle["time"]

                if c_key != state["last_processed_candle"]:
                    close_dt = latest_candle["dt"] + timedelta(minutes=5)
                    curr_date = close_dt.strftime("%Y-%m-%d")

                    if state["last_date"] != curr_date:
                        state["trades_today"] = 0
                        state["losses_today"] = 0
                        state["last_date"] = curr_date
                        state["last_sig_dir"] = None

                    manage_active_trade(state, latest_candle, close_dt)

                    if (
                        not state["active_trade"]
                        and state["trades_today"] < MAX_TRADES_PER_DAY
                        and state["losses_today"] < MAX_LOSSES_PER_DAY
                        and (not ONLY_KILLZONES or check_session_allowed(close_dt))
                    ):
                        candles_15m = aggregate_candles(candles_5m, 15)
                        candles_1h = aggregate_candles(candles_5m, 60)

                        sig = evaluate_signal(
                            candles_5m[-100:],
                            candles_15m[-100:],
                            candles_1h[-100:],
                            state["equity"],
                            state["peak_equity"],
                            state["consecutive_losses"],
                        )
                        if sig:
                            now_ts = close_dt.timestamp()
                            cooldown_ok = not (
                                state["last_sig_dir"] == sig["signal"]
                                and state["last_sig_ts"] is not None
                                and (now_ts - state["last_sig_ts"]) < COOLDOWN_SECONDS
                            )
                            if cooldown_ok:
                                sig["entry_time"] = close_dt.strftime("%Y-%m-%d %H:%M:%S")
                                state["active_trade"] = sig
                                state["trades_today"] += 1
                                state["last_sig_dir"] = sig["signal"]
                                state["last_sig_ts"] = now_ts

                                send_telegram_message(
                                    f"🚀 <b>Neues {SYMBOL} SMC-Signal</b>\n"
                                    f"Richtung: <b>{sig['signal']}</b> ({sig['pattern']}) | Konfidenz: <b>{sig['conf']}%</b>\n"
                                    f"Entry: <b>{sig['entry']:.2f}</b> | SL: <b>{sig['sl']:.2f}</b> (${sig['sl_dist']:.2f})\n"
                                    f"Lots: <b>{sig['lots']:.2f}</b> | Risiko: <b>${sig['risk_usd']:.2f}</b> ({sig['risk_pct']:.2f}%)"
                                )

                    state["last_processed_candle"] = c_key
                    save_state(state)

        except Exception as e:
            print(f"⚠️ Fehler im Bot-Loop: {e}", flush=True)

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    run_bot()
