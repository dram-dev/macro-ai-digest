"""Technical indicators — pure Python, zero dependencies.

All functions take a list of floats (oldest first) and return a list of the
same length, with None padding where the window hasn't filled yet.
"""
from __future__ import annotations


def sma(prices: list[float], n: int) -> list[float | None]:
    """Simple moving average."""
    result: list[float | None] = [None] * (n - 1)
    for i in range(n - 1, len(prices)):
        result.append(sum(prices[i - n + 1 : i + 1]) / n)
    return result


def ema(prices: list[float], n: int) -> list[float | None]:
    """Exponential moving average, seeded with SMA."""
    if len(prices) < n:
        return [None] * len(prices)
    result: list[float | None] = [None] * (n - 1)
    k = 2.0 / (n + 1)
    val = sum(prices[:n]) / n
    result.append(val)
    for p in prices[n:]:
        val = p * k + val * (1 - k)
        result.append(val)
    return result


def rsi(prices: list[float], n: int = 14) -> list[float | None]:
    """Wilder-smoothed RSI."""
    if len(prices) < n + 1:
        return [None] * len(prices)
    gains = [max(prices[i] - prices[i - 1], 0.0) for i in range(1, len(prices))]
    losses = [max(prices[i - 1] - prices[i], 0.0) for i in range(1, len(prices))]
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    result: list[float | None] = [None] * n
    result.append(100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l))
    for i in range(n, len(gains)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
        result.append(100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l))
    return result


def macd(
    prices: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """MACD line, signal line, histogram. All same length as input."""
    ema_f = ema(prices, fast)
    ema_s = ema(prices, slow)
    macd_line: list[float | None] = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(ema_f, ema_s)
    ]
    # Signal = EMA(macd_line, signal)
    valid = [(i, v) for i, v in enumerate(macd_line) if v is not None]
    if len(valid) < signal:
        return macd_line, [None] * len(prices), [None] * len(prices)
    sig_line: list[float | None] = [None] * (valid[signal - 1][0])
    k = 2.0 / (signal + 1)
    vals = [v for _, v in valid]
    sv = sum(vals[:signal]) / signal
    sig_line.append(sv)
    for v in vals[signal:]:
        sv = v * k + sv * (1 - k)
        sig_line.append(sv)
    histogram: list[float | None] = [
        (m - s) if m is not None and s is not None else None
        for m, s in zip(macd_line, sig_line)
    ]
    return macd_line, sig_line, histogram


def latest_snapshot(prices: list[float]) -> dict:
    """Compute all indicators and return a dict of the most recent values."""
    if len(prices) < 27:
        return {}
    rsi_vals = rsi(prices)
    sma50 = sma(prices, min(50, len(prices)))
    sma20 = sma(prices, 20)
    macd_l, sig_l, hist_l = macd(prices)
    latest = prices[-1]
    prev = prices[-2]

    snap: dict = {
        "price": latest,
        "pct_change_1d": (latest - prev) / prev * 100 if prev else None,
        "rsi14": rsi_vals[-1],
        "sma20": sma20[-1],
        "sma50": sma50[-1],
        "macd": macd_l[-1],
        "macd_signal": sig_l[-1],
        "macd_hist": hist_l[-1],
    }
    if snap["sma50"] is not None:
        snap["pct_vs_sma50"] = (latest - snap["sma50"]) / snap["sma50"] * 100
    if snap["sma20"] is not None:
        snap["pct_vs_sma20"] = (latest - snap["sma20"]) / snap["sma20"] * 100
    return snap
