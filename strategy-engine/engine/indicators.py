"""Indicator stack shared by the live strategies.

EXTRACTED VERBATIM from the Version 1 scripts. The bodies are unchanged: this
is a move, not a rewrite, because indicator maths is strategy logic and
changing it silently would change every signal.

The two Version 1 engines each carried their own copy, written in different
styles — S21's were compressed to one-liners and S17's were commented at
length. They were compared numerically before being merged here, on random
walks, trends and flat series: `add_smooth_macd_cycles`, `_pine_ema_series`,
`_pine_kama_series`, `_heikin_ashi_df`, `calc_sma_kama`, `calc_emakama` and
`calc_kama_line` all produce bit-identical output in both. test_indicators.py
pins that equivalence against the original files so it cannot drift.

One caveat on `calc_kama_line`: S17's docstring describes a `0.991 * max_pwr`
decay as the KEY DIFFERENCE from an older implementation. That line multiplies
zero and is vestigial — the running max it seeds is equivalent to S21's plain
max because r1_arr is non-negative by construction. Both were verified to
agree exactly. The docstring is kept as written for provenance.

VolumeCalculator comes in two flavours because the engines genuinely differed:
S17's works on time windows and is timeframe-agnostic; S21's counts bars and
assumes 15-minute candles. Both are kept, named for what they do.
"""

from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import talib

from .notify import log

# --- constants, verbatim from Version 1 ---------------------------------------

SMOOTH_MIN_RUN   = 4
BB_PERIOD        = 20
BB_STD           = 2.0

EK_ER_LEN   = 10
EK_FAST_LEN = 2
EK_SLOW_LEN = 30

KL_LENGTH      = 14
KL_FAST_LENGTH = 2
KL_SLOW_LENGTH = 30
KL_HP_PERIOD   = 48

SMA_KAMA_LENGTH = 5
SMA_KAMA_FAST   = 2.5
SMA_KAMA_SLOW   = 20

SMA_EMA_LENGTH  = 20

FIB_LEVELS_RAW = [
    (0.0, "0"), (0.236, "0.236"), (0.382, "0.382"), (0.5, "0.5"), (0.618, "0.618"),
    (0.786, "0.786"), (1.0, "1"), (1.272, "1.272"), (1.414, "1.414"), (1.618, "1.618"),
    (2.0, "2"), (2.618, "2.618")
]

EMA50_COL = "ema50"

# volume windows, in minutes
WIN_1H  = 60;    WIN_2H  = 120;   WIN_4H  = 240
WIN_12H = 720;   WIN_24H = 1440;  WIN_48H = 2880
WIN_7D  = 10080; WIN_10D = 14400; WIN_30D = 43200

K_SMA     = "SMA(kama_ma_dataset)"
K_EMAKAMA = "EMAKAMA"
K_KLINE   = "KamaLine"
K_EMA50   = "EMA 50"


def add_smooth_macd_cycles(df: pd.DataFrame) -> pd.DataFrame:
    _, _, hist_raw = talib.MACD(df["close"].values.astype(float),
                                fastperiod=12, slowperiod=26, signalperiod=9)
    hist = hist_raw.copy(); plus_run = 0; minus_run = 0
    for i in range(len(hist)):
        if np.isnan(hist[i]): continue
        if hist[i] < 0:
            minus_run += 1
            if 0 < plus_run < SMOOTH_MIN_RUN and i > plus_run and hist[i - plus_run - 1] < 0:
                hist[i - plus_run:i] *= -1.0
            plus_run = 0
        else:
            plus_run += 1
            if 0 < minus_run < SMOOTH_MIN_RUN and i > minus_run and hist[i - minus_run - 1] > 0:
                hist[i - minus_run:i] *= -1.0
            minus_run = 0
    sign  = np.where(np.isnan(hist), 0.0, np.sign(hist))
    cycle = pd.Series(sign, index=df.index).diff().ne(0).cumsum().values.astype(int)
    color = np.where(sign > 0, "Green", np.where(sign < 0, "Red", "Flat"))
    df["sm_hist"] = hist; df["sm_sign"] = sign
    df["sm_cycle"] = cycle; df["sm_color"] = color
    return df


def _pine_ema_series(series: pd.Series, length: int) -> pd.Series:
    """Standard EMA used for SMA-KAMA output smoothing."""
    alpha = 2.0 / (length + 1)
    out   = np.zeros(len(series))
    out[0] = series.iloc[0]
    for i in range(1, len(series)):
        out[i] = alpha * series.iloc[i] + (1.0 - alpha) * out[i - 1]
    return pd.Series(out, index=series.index)


def _heikin_ashi_df(df: pd.DataFrame) -> pd.DataFrame:
    """Heikin-Ashi OHLC — identical to Strategy 14 _heikin_ashi()."""
    ha       = pd.DataFrame(index=df.index)
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    ha_open  = np.zeros(len(df))
    ha_open[0] = (float(df["open"].iloc[0]) + float(df["close"].iloc[0])) / 2.0
    hc = ha_close.values
    for i in range(1, len(df)):
        ha_open[i] = (ha_open[i - 1] + hc[i - 1]) / 2.0
    ha["open"]  = ha_open
    ha["close"] = hc
    ha["high"]  = np.maximum.reduce([df["high"].values, ha_open, hc])
    ha["low"]   = np.minimum.reduce([df["low"].values,  ha_open, hc])
    return ha


def _pine_kama_series(series: pd.Series,
                      length: int = SMA_KAMA_LENGTH,
                      fast:   float = SMA_KAMA_FAST,
                      slow:   float = SMA_KAMA_SLOW) -> pd.Series:
    """
    KAMA sub-step inside the SMA indicator.
    Mirrors Strategy 14 _pine_kama_sma() exactly — uses SMA_KAMA_* constants.
    """
    xvnoise = abs(series - series.shift(1))
    nsignal = abs(series - series.shift(length))
    nnoise  = xvnoise.rolling(length).sum()
    nfast   = 2.0 / (fast + 1)
    nslow   = 2.0 / (slow + 1)
    arr     = np.zeros(len(series))
    for i in range(len(series)):
        if i == 0:
            arr[0] = 0.0
            continue
        sig = nsignal.iloc[i] if not np.isnan(nsignal.iloc[i]) else 0.0
        noi = nnoise.iloc[i]  if not np.isnan(nnoise.iloc[i])  else 0.0
        er  = sig / noi if noi != 0 else 0.0
        sc  = (er * (nfast - nslow) + nslow) ** 2
        arr[i] = arr[i - 1] + sc * (series.iloc[i] - arr[i - 1])
    return pd.Series(arr, index=series.index)


def calc_sma_kama(df: pd.DataFrame, length: int = SMA_EMA_LENGTH) -> pd.Series:
    """
    SMA indicator — mirrors Strategy 14 compute_sma() exactly.
    Pipeline: Heikin-Ashi  →  HLC/3  →  _pine_kama_series  →  _pine_ema_series(length=20).

    `length` is accepted because every Version 1 source declares it and
    Strategy 21 passes it by keyword. Dropping it during the migration made
    S21 raise TypeError on every scan. The default matches what all of them
    pass, so the numbers are unchanged.
    """
    ha   = _heikin_ashi_df(df)
    hlc3 = pd.Series(
        (ha["high"].values + ha["low"].values + ha["close"].values) / 3.0,
        index=df.index,
    )
    return _pine_ema_series(_pine_kama_series(hlc3), length)


def calc_emakama(close: np.ndarray) -> np.ndarray:
    """
    EMAKAMA — verbatim port of Strategy 14 compute_emakama().
    Uses EK_ER_LEN=10, EK_FAST_LEN=2, EK_SLOW_LEN=30.
    """
    n       = len(close)
    kama    = np.full(n, np.nan, dtype=float)
    fast_sc = 2.0 / (EK_FAST_LEN + 1)
    slow_sc = 2.0 / (EK_SLOW_LEN + 1)
    if n == 0:
        return kama
    kama[0] = close[0]
    for i in range(1, n):
        if i < EK_ER_LEN:
            kama[i] = close[i]
            continue
        change     = abs(close[i] - close[i - EK_ER_LEN])
        volatility = sum(abs(close[j] - close[j - 1])
                         for j in range(i - EK_ER_LEN + 1, i + 1))
        er      = change / volatility if volatility != 0 else 0.0
        sc      = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        kama[i] = kama[i - 1] + sc * (close[i] - kama[i - 1])
    return kama


def calc_kama_line(src: np.ndarray) -> np.ndarray:
    """
    Spectral KAMA (kama_lines) — verbatim port of Strategy 14 compute_kama_lines().

    KEY DIFFERENCE vs the old S17 version:
      • max_pwr is reset to 0.0 each bar then decayed with  0.991 * max_pwr
        BEFORE the per-period loop — exactly as PineScript does it.
      • The old S17 version used  max(...list...)  which is a one-shot
        Python max and does NOT carry state between bars, giving wrong values
        on XAUUSD data.

    Parameters (match Strategy 14 constants):
      KL_LENGTH=14, KL_FAST_LENGTH=2, KL_SLOW_LENGTH=30, KL_HP_PERIOD=48.
    Input: ohlc4 = (open + high + low + close) / 4  array.
    """
    n       = len(src)
    pi      = 2.0 * np.arcsin(1.0)

    alpha1 = ((np.cos(.707 * 2 * pi / KL_HP_PERIOD) +
               np.sin(.707 * 2 * pi / KL_HP_PERIOD) - 1.0) /
               np.cos(.707 * 2 * pi / KL_HP_PERIOD))

    a1  = np.exp(-1.414 * pi / 10.0)
    b1  = 2.0 * a1 * np.cos(1.414 * 180.0 / 10.0)
    c2  = b1;  c3 = -a1 * a1;  c1 = 1.0 - c2 - c3

    fastest = 2.0 / (KL_FAST_LENGTH + 1)
    slowest = 2.0 / (KL_SLOW_LENGTH + 1)

    hp       = np.zeros(n)
    filt     = np.zeros(n)
    kama     = np.zeros(n)
    corr_arr = np.zeros(KL_LENGTH)
    r1_arr   = np.zeros(KL_LENGTH)
    r2_arr   = np.zeros(KL_LENGTH)

    log("  [kama_lines] Computing spectral KAMA ...")

    for i in range(n):
        s0 = src[i]
        s1 = src[i - 1] if i >= 1 else 0.0
        s2 = src[i - 2] if i >= 2 else 0.0
        h1 = hp[i - 1]   if i >= 1 else 0.0
        h2 = hp[i - 2]   if i >= 2 else 0.0

        # High-pass filter
        hp[i] = (((1.0 - alpha1 / 2.0) ** 2) * (s0 - 2.0 * s1 + s2)
                 + 2.0 * (1.0 - alpha1) * h1
                 - ((1.0 - alpha1) ** 2) * h2)

        # Super-smoother
        f1 = filt[i - 1] if i >= 1 else 0.0
        f2 = filt[i - 2] if i >= 2 else 0.0
        hp_p = hp[i - 1]  if i >= 1 else 0.0
        filt[i] = c1 * (hp[i] + hp_p) / 2.0 + c2 * f1 + c3 * f2

        # Correlation periodogram
        for lag in range(KL_LENGTH):
            m  = lag
            sx = sy = sxx = syy = sxy = 0.0
            for count in range(m + 1):
                ix = i - count
                iy = i - lag - count
                x  = filt[ix] if ix >= 0 else 0.0
                y  = filt[iy] if iy >= 0 else 0.0
                sx  += x;  sy  += y
                sxx += x * x;  sxy += x * y;  syy += y * y
            denom = (m * sxx - sx * sx) * (m * syy - sy * sy)
            if denom > 0:
                corr_arr[lag] = (m * sxy - sx * sy) / np.sqrt(denom)

        sq_sum = np.zeros(KL_LENGTH)
        for period in range(8, KL_LENGTH):
            cp_ = sp_ = 0.0
            for n2 in range(8, KL_LENGTH):
                cp_ += corr_arr[n2] * np.cos(360.0 * n2 / period)
                sp_ += corr_arr[n2] * np.sin(360.0 * n2 / period)
            sq_sum[period] = cp_ * cp_ + sp_ * sp_

        for p2 in range(8, KL_LENGTH):
            r2_arr[p2] = r1_arr[p2]
            r1_arr[p2] = 0.2 * sq_sum[p2] ** 2 + 0.8 * r2_arr[p2]

        # ── CRITICAL: mirrors PineScript — reset then decay each bar ──────
        max_pwr = 0.0
        max_pwr = 0.991 * max_pwr          # = 0.0 on first pass; state is in r1_arr
        for p3 in range(8, KL_LENGTH):
            if r1_arr[p3] > max_pwr:
                max_pwr = r1_arr[p3]

        pwr = np.zeros(KL_LENGTH)
        for p4 in range(8, KL_LENGTH):
            pwr[p4] = r1_arr[p4] / max_pwr if max_pwr != 0 else 0.0

        spx_ = sp__ = 0.0
        for p5 in range(8, KL_LENGTH):
            if pwr[p5] >= 0.5:
                spx_ += p5 * pwr[p5]
                sp__ += pwr[p5]

        dc_val  = max(8.0, min(14.0, spx_ / sp__ if sp__ != 0 else 0.0))
        dc_int  = int(dc_val)
        idx_dc  = i - dc_int
        src_dc  = src[idx_dc] if idx_dc >= 0 else 0.0

        num   = abs(s0 - src_dc)
        denom = 0.0
        for j in range(dc_int):
            ij  = i - j
            ij1 = i - j - 1
            if ij >= 0 and ij1 >= 0:
                denom += abs(src[ij] - src[ij1])

        er      = num / denom if denom != 0 else 0.0
        sc      = (er * (fastest - slowest) + slowest) ** 2
        kprev   = kama[i - 1] if i > 0 else 0.0
        kama[i] = kprev + sc * (s0 - kprev)

        if i % 50000 == 0 and i > 0:
            log(f"  [kama_lines] bar {i:,}/{n:,}")

    log("  [kama_lines] Done.")
    return kama


def prepare_df(df_raw: pd.DataFrame) -> pd.DataFrame:
    """
    Full indicator stack for 5min / 1min data.
    All three KAMA indicators now use the XAUUSD-correct Strategy 14 logic.
    talib is still used for BB and EMA 50 (those are unchanged and correct).
    """
    df    = df_raw.copy()
    close = df["close"].values.astype(float)
    op    = df["open"].values.astype(float)
    hi    = df["high"].values.astype(float)
    lo    = df["low"].values.astype(float)

    # Bollinger Bands — unchanged
    bb_upper, _, bb_lower = talib.BBANDS(
        close, timeperiod=BB_PERIOD, nbdevup=BB_STD, nbdevdn=BB_STD, matype=0
    )
    df["bb_upper"] = bb_upper
    df["bb_lower"] = bb_lower

    # ── Three KAMA indicators — all use S14-correct implementations ──
    df["sma_kama"]  = calc_sma_kama(df)                          # SMA indicator
    df["emakama"]   = pd.Series(calc_emakama(close), index=df.index)  # EMAKAMA
    ohlc4           = (op + hi + lo + close) / 4.0
    df["kama_line"] = pd.Series(calc_kama_line(ohlc4), index=df.index)  # kama_lines

    # EMA 50 — unchanged
    df[EMA50_COL] = talib.EMA(close, timeperiod=50)

    # Smooth MACD cycles — unchanged
    df = add_smooth_macd_cycles(df)
    return df


def prepare_df_tf(df_raw: pd.DataFrame) -> pd.DataFrame:
    """
    Lightweight indicator prep for higher-timeframe data.
    Only computes EMA 50, EMA 100, EMA 200.
    """
    df = df_raw.copy()
    close = df["close"].values.astype(float)
    df["ema50"]  = talib.EMA(close, timeperiod=50)
    df["ema100"] = talib.EMA(close, timeperiod=100)
    df["ema200"] = talib.EMA(close, timeperiod=200)
    return df


def make_fast_arrays(df: pd.DataFrame) -> Dict[str, Any]:
    """Full fast-array dict for 5min / 1min data."""
    fa = {
        "idx":      df.index,
        "open":     df["open"].values.astype(float),
        "high":     df["high"].values.astype(float),
        "low":      df["low"].values.astype(float),
        "close":    df["close"].values.astype(float),
        "bb_upper": df["bb_upper"].values.astype(float),
        "bb_lower": df["bb_lower"].values.astype(float),
        "sma_kama": df["sma_kama"].values.astype(float),
        "emakama":  df["emakama"].values.astype(float),
        "kama_line":df["kama_line"].values.astype(float),
        "sm_cycle": df["sm_cycle"].values.astype(int),
        "sm_color": df["sm_color"].values.astype(object),
        "volume":   df["volume"].values.astype(float),
    }
    fa["ema50"] = (df[EMA50_COL].values.astype(float)
                   if EMA50_COL in df.columns else np.full(len(df), np.nan))
    return fa


def make_tf_arrays(df: pd.DataFrame) -> Dict[str, Any]:
    """Lightweight array dict for 2H / 4H / 1D EMA data."""
    return {
        "idx":    df.index,
        "close":  df["close"].values.astype(float),
        "ema50":  df["ema50"].values.astype(float),
        "ema100": df["ema100"].values.astype(float),
        "ema200": df["ema200"].values.astype(float),
    }


# S17 flavour: time-window based, works on any timeframe.
class VolumeWindows:
    def __init__(self, df: pd.DataFrame):
        self.idx = df.index
        self.cum = df["volume"].cumsum()

    def _win(self, end_ts: pd.Timestamp, mins: int) -> float:
        st = end_ts - pd.Timedelta(minutes=mins)
        ep = self.idx.searchsorted(end_ts, side="right") - 1
        sp = self.idx.searchsorted(st, side="right")
        if ep < 0 or sp >= len(self.idx) or sp > ep: return 0.0
        return float(self.cum.iloc[ep] - (self.cum.iloc[sp - 1] if sp > 0 else 0.0))

    def ratios(self, ts: pd.Timestamp) -> Dict[str, float]:
        s1  = self._win(ts, WIN_1H);  s2  = self._win(ts, WIN_2H)
        s4  = self._win(ts, WIN_4H);  s12 = self._win(ts, WIN_12H)
        s24 = self._win(ts, WIN_24H); s48 = self._win(ts, WIN_48H)
        s7  = self._win(ts, WIN_7D);  s10 = self._win(ts, WIN_10D)
        s30 = self._win(ts, WIN_30D)
        a1 = s1*24; a2 = s2*12; a4 = s4*6; a12 = s12*2
        a24 = s24; a48 = s48/2; a7d = s7/7; a10d = s10/10; a30d = s30/30
        def p(n, d): return round(100.0*n/d, 2) if d and d > 0 else 0.0
        return {
            "v_48_4":  p(a4, a48),   "v_7_1":   p(a24, a7d),
            "v_10_2":  p(a48, a10d), "v_30_10":  p(a10d, a30d),
            "v_12_1":  p(a12, a1),   "v_24_1":   p(a24, a1),
            "v_24_2":  p(a24, a2),   "v_48_2":   p(a48, a2),
        }


def check_ema_position(arr_tf: Dict[str, Any],
                       entry_ts: pd.Timestamp) -> Dict[str, Optional[str]]:
    """
    Given a TF fast-array and an entry datetime:
      1. Locate the CURRENT (still-forming) candle using searchsorted.
      2. Step back one position to get the PREVIOUS COMPLETED candle.
      3. Compare its close against EMA 50 / 100 / 200.
      Returns {"ema50": "Above"/"Below"/None, "ema100": ..., "ema200": ...}
    """
    idx = arr_tf["idx"]
    n   = len(idx)
    if n < 2:
        return {"ema50": None, "ema100": None, "ema200": None}

    # searchsorted side='right' → first position whose value > entry_ts
    # Subtract 1 → last candle that started AT OR BEFORE entry_ts = current forming candle
    cur_pos  = int(idx.searchsorted(entry_ts, side="right")) - 1
    prev_pos = cur_pos - 1   # previous COMPLETED candle

    if prev_pos < 0 or prev_pos >= n:
        return {"ema50": None, "ema100": None, "ema200": None}

    cl = float(arr_tf["close"][prev_pos])

    def chk(ema_arr: np.ndarray) -> Optional[str]:
        v = float(ema_arr[prev_pos])
        if np.isnan(v): return None
        return "Above" if cl > v else "Below"

    return {
        "ema50":  chk(arr_tf["ema50"]),
        "ema100": chk(arr_tf["ema100"]),
        "ema200": chk(arr_tf["ema200"]),
    }


# S21 flavour: counts bars, assumes 15-minute candles.
class VolumeBars:
    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.vol = df["volume"]
    def ratios(self, ts: pd.Timestamp):
        if ts not in self.df.index: return {}
        try:
            pos = self.df.index.get_loc(ts)
            if isinstance(pos, slice): pos = pos.start
            elif isinstance(pos, (np.ndarray, list)): pos = pos[0]
        except: return {}
        cur_vol = self.vol.iloc[pos]
        def _get_past_vol(hours):
            bars = hours * 4
            if pos - bars < 0: return 0.0
            v = self.vol.iloc[pos - bars:pos].mean()
            return float(cur_vol / v) if v > 0 else 0.0
        return {
            "Current 15min:last 12 hrs Vol": _get_past_vol(12),
            "Current 15min:last 24 hrs Vol": _get_past_vol(24),
            "Current 15min:last 48 hrs Vol": _get_past_vol(48)
        }


def get_fib_levels(hh: float, ll: float, side: str) -> List[Tuple[float, str]]:
    rng = hh - ll; levels = []
    for k, name in FIB_LEVELS_RAW:
        price = (ll + k * rng) if side == "sell" else (hh - k * rng)
        levels.append((price, name))
    if side == "sell": levels.sort(key=lambda x: x[0])
    else: levels.sort(key=lambda x: x[0], reverse=True)
    return levels
