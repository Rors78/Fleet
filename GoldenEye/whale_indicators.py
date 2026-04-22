import pandas as pd
import numpy as np
try:
    from numba import njit
    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False
    def njit(*args, **kwargs):
        def decorator(f):
            return f
        return decorator


@njit(cache=True, fastmath=True)
def _compute_all_indicators_numba(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
    adx_period: int = 14,
    chop_period: int = 14,
    vol_period: int = 20,
    w_surge: float = 0.3,
    w_chop: float = 0.4,
    w_adx: float = 0.3
) -> tuple:
    n = len(close)
    if n < max(adx_period, chop_period, vol_period) * 2:
        empty = np.full(n, np.nan)
        return empty, empty, np.zeros(n), np.zeros(n)

    tr = np.zeros(n)
    dm_plus = np.zeros(n)
    dm_minus = np.zeros(n)

    for i in range(1, n):
        h_l = high[i] - low[i]
        h_pc = abs(high[i] - close[i-1])
        l_pc = abs(low[i] - close[i-1])
        tr[i] = max(h_l, h_pc, l_pc)

        up = high[i] - high[i-1]
        down = low[i-1] - low[i]
        dm_plus[i] = up if up > down and up > 0 else 0
        dm_minus[i] = down if down > up and down > 0 else 0

    atr = np.zeros(n)
    di_plus = np.zeros(n)
    di_minus = np.zeros(n)
    dx = np.zeros(n)
    adx = np.full(n, np.nan)

    atr[adx_period-1] = np.mean(tr[1:adx_period])
    sum_dp = np.sum(dm_plus[1:adx_period])
    sum_dm = np.sum(dm_minus[1:adx_period])
    di_plus[adx_period-1] = 100 * sum_dp / atr[adx_period-1] if atr[adx_period-1] != 0 else 0
    di_minus[adx_period-1] = 100 * sum_dm / atr[adx_period-1] if atr[adx_period-1] != 0 else 0

    for i in range(adx_period, n):
        atr[i] = (atr[i-1] * (adx_period - 1) + tr[i]) / adx_period
        di_plus[i] = 100 * (dm_plus[i] + di_plus[i-1] * (adx_period - 1) / adx_period) / atr[i] if atr[i] != 0 else 0
        di_minus[i] = 100 * (dm_minus[i] + di_minus[i-1] * (adx_period - 1) / adx_period) / atr[i] if atr[i] != 0 else 0

        diff = abs(di_plus[i] - di_minus[i])
        sm = di_plus[i] + di_minus[i]
        dx[i] = 100 * diff / sm if sm != 0 else 0

        if i == adx_period:
            adx[i] = np.mean(dx[adx_period-1:i+1])
        else:
            adx[i] = (adx[i-1] * (adx_period - 1) + dx[i]) / adx_period

    chop = np.full(n, np.nan)

    for i in range(chop_period - 1, n):
        atr_window_sum = 0.0
        for j in range(i - chop_period + 1, i + 1):
            atr_window_sum += tr[j]
        
        high_max = high[i - chop_period + 1]
        low_min = low[i - chop_period + 1]
        for j in range(i - chop_period + 2, i + 1):
            if high[j] > high_max:
                high_max = high[j]
            if low[j] < low_min:
                low_min = low[j]
        
        rng = high_max - low_min
        if atr_window_sum > 0 and rng > 0:
            chop[i] = 100 * np.log10(atr_window_sum / rng) / np.log10(chop_period)
        else:
            chop[i] = 50.0

    surge = np.zeros(n)
    mean_vol = np.zeros(n)
    std_vol = np.zeros(n)

    mean_vol[vol_period-1] = np.mean(volume[:vol_period])
    std_vol[vol_period-1] = np.std(volume[:vol_period])

    for i in range(vol_period, n):
        mean_vol[i] = (mean_vol[i-1] * (vol_period - 1) + volume[i]) / vol_period
        delta = volume[i] - mean_vol[i-1]
        var = std_vol[i-1]**2 * (vol_period - 1) + delta**2
        std_vol[i] = np.sqrt(var / vol_period) if var > 0 else 0.0

        if std_vol[i] > 0:
            z = (volume[i] - mean_vol[i]) / std_vol[i]
            surge[i] = max(0.0, z)

    score = np.zeros(n)
    for i in range(n):
        if np.isnan(adx[i]) or np.isnan(chop[i]) or np.isnan(surge[i]):
            continue
        # Normalize all components to 0-100 before weighting
        adx_s = max(0.0, min((adx[i] - 20.0) / 80.0 * 100.0, 100.0))
        chop_s = max(0.0, min(100.0 - chop[i], 100.0))
        vol_s = max(0.0, min((surge[i] + 3.0) / 6.0 * 100.0, 100.0))
        raw = adx_s * w_adx + chop_s * w_chop + vol_s * w_surge
        score[i] = max(0.0, min(100.0, raw))

    return adx, chop, surge, score


class IndicatorsEngine:
    def __init__(self):
        self._use_numba = NUMBA_AVAILABLE
        if not self._use_numba:
            print("Numba not installed → falling back to pure pandas implementation")

    def _calculate_adx_pandas(self, df: pd.DataFrame, window: int) -> pd.Series:
        df = df.copy()
        df["H-L"] = df["high"] - df["low"]
        df["H-PC"] = abs(df["high"] - df["close"].shift(1))
        df["L-PC"] = abs(df["low"] - df["close"].shift(1))
        
        df["TR"] = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
        
        df["+DM"] = np.where(
            (df["high"] - df["high"].shift(1)) > (df["low"].shift(1) - df["low"]),
            np.where((df["high"] - df["high"].shift(1)) > 0, df["high"] - df["high"].shift(1), 0),
            0
        )
        
        df["-DM"] = np.where(
            (df["low"].shift(1) - df["low"]) > (df["high"] - df["high"].shift(1)),
            np.where((df["low"].shift(1) - df["low"]) > 0, df["low"].shift(1) - df["low"], 0),
            0
        )
        
        df["+DM_s"] = df["+DM"].rolling(window=window).sum()
        df["-DM_s"] = df["-DM"].rolling(window=window).sum()
        df["TR_s"] = df["TR"].rolling(window=window).sum()
        
        df["+DI"] = 100 * (df["+DM_s"] / df["TR_s"])
        df["-DI"] = 100 * (df["-DM_s"] / df["TR_s"])
        df["DX"] = 100 * (abs(df["+DI"] - df["-DI"]) / (df["+DI"] + df["-DI"]))
        df["ADX"] = df["DX"].rolling(window=window).mean()
        
        return df["ADX"]

    def _calculate_chopiness_pandas(self, df: pd.DataFrame, window: int) -> pd.Series:
        df = df.copy()
        df["H-L"] = df["high"] - df["low"]
        df["H-PC"] = abs(df["high"] - df["close"].shift(1))
        df["L-PC"] = abs(df["low"] - df["close"].shift(1))
        df["TR"] = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
        
        sum_atr = df["TR"].rolling(window=window).sum()
        high_low_range = df["high"].rolling(window=window).max() - df["low"].rolling(window=window).min()
        
        chopiness = 100 * np.log10(sum_atr / high_low_range) / np.log10(window)
        return chopiness.replace([np.inf, -np.inf], np.nan).fillna(50)

    def _calculate_volume_surge_pandas(self, df: pd.DataFrame, window: int) -> pd.Series:
        rolling_mean = df["volume"].rolling(window=window).mean()
        rolling_std = df["volume"].rolling(window=window).std()
        
        surge = (df["volume"] - rolling_mean) / rolling_std
        return surge.replace([np.inf, -np.inf], np.nan).fillna(0)

    def _calculate_whale_score(self, df: pd.DataFrame) -> pd.Series:
        adx = df.get("ADX", pd.Series(20, index=df.index)).fillna(20)
        chopiness = df.get("chopiness", pd.Series(50, index=df.index)).fillna(50)
        volume_surge = df.get("volume_surge", pd.Series(0, index=df.index)).fillna(0)
        
        adx_score = ((adx - 20) / 80 * 100).clip(0, 100)
        chopiness_score = (100 - chopiness).clip(0, 100)
        volume_score = ((volume_surge + 3) / 6 * 100).clip(0, 100)
        
        whale_score = 0.4 * adx_score + 0.3 * chopiness_score + 0.3 * volume_score
        return whale_score.rolling(window=5).mean().clip(0, 100)

    def add_indicators(
        self,
        df: pd.DataFrame,
        adx_window: int = 14,
        chopiness_window: int = 14,
        volume_window: int = 20
    ) -> pd.DataFrame:
        if df.empty:
            return df

        h = df['high'].to_numpy(np.float64)
        l = df['low'].to_numpy(np.float64)
        c = df['close'].to_numpy(np.float64)
        v = df['volume'].to_numpy(np.float64)

        if self._use_numba:
            try:
                adx, chop, surge, score = _compute_all_indicators_numba(
                    h, l, c, v,
                    adx_period=adx_window,
                    chop_period=chopiness_window,
                    vol_period=volume_window
                )
                df['ADX'] = adx
                df['chopiness'] = chop
                df['volume_surge'] = surge
                df['whale_score'] = score
            except Exception as e:
                print(f"Numba failed: {e} → using pandas fallback")
                self._use_numba = False
                df['ADX'] = self._calculate_adx_pandas(df, adx_window)
                df['chopiness'] = self._calculate_chopiness_pandas(df, chopiness_window)
                df['volume_surge'] = self._calculate_volume_surge_pandas(df, volume_window)
                df['whale_score'] = self._calculate_whale_score(df)
        else:
            df['ADX'] = self._calculate_adx_pandas(df, adx_window)
            df['chopiness'] = self._calculate_chopiness_pandas(df, chopiness_window)
            df['volume_surge'] = self._calculate_volume_surge_pandas(df, volume_window)
            df['whale_score'] = self._calculate_whale_score(df)

        return df


if __name__ == "__main__":
    import time
    
    from kraken_api import KrakenAPI
    api = KrakenAPI()
    
    print("Fetching test data...")
    df = api.get_ohlc("BTC/USD", interval=1)
    
    if df is not None and len(df) > 30:
        print(f"Data loaded: {len(df)} rows")
        engine = IndicatorsEngine()
        
        # Warmup (Numba compilation)
        engine.add_indicators(df.copy())
        
        # Benchmark
        n = 10
        t0 = time.perf_counter()
        for _ in range(n):
            engine.add_indicators(df.copy())
        print(f"Average time ({n} runs): {(time.perf_counter()-t0)/n*1000:.2f} ms")
    else:
        print("Failed to load data")
