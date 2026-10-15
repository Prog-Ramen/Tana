"""Analyse a board's averaged sonar recording: find echoes and turn delays into distances."""
from __future__ import annotations

import base64

import numpy as np

SPEED_OF_SOUND = 343.0  # m/s at ~20 °C


def chirp(rate: int, f0: float, f1: float, ms: float) -> np.ndarray:
    n = int(rate * ms / 1000)
    t = np.arange(n) / rate
    T = n / rate
    x = np.sin(2 * np.pi * (f0 * t + (f1 - f0) * t * t / (2 * T)))
    return x * np.hanning(n)


def _envelope(cross_spec: np.ndarray, n: int) -> np.ndarray:
    """Magnitude of the analytic (one-sided) cross-correlation: a smooth echo envelope."""
    full = np.zeros(n, dtype=complex)
    full[: len(cross_spec)] = cross_spec
    full[1: len(cross_spec)] *= 2  # analytic signal: keep positive frequencies only
    return np.abs(np.fft.ifft(full))


def decode(result: dict) -> np.ndarray:
    return np.frombuffer(base64.b64decode(result["pcm"]), dtype="<i2").astype(float)


def analyse(rec: np.ndarray, rate: int, params: dict, noise: np.ndarray | None = None) -> dict:
    """Matched-filter the averaged period against the chirp (circularly: the recording is periodic).
    The strongest peak is the direct speaker->mic path; later peaks are echoes."""
    ref = chirp(rate, params["f0"], params["f1"], params["chirp_ms"])
    n = len(rec)
    # band-limit to the chirp band to drop speech/hum, then correlate via FFT
    spec = np.fft.rfft(rec)
    f = np.fft.rfftfreq(n, 1 / rate)
    band = (f >= params["f0"] - 500) & (f <= params["f1"] + 500)
    spec[~band] = 0
    env = _envelope(spec * np.conj(np.fft.rfft(ref, n)), n)
    direct = int(np.argmax(env))
    env_rolled = np.roll(env, -direct)  # direct path at index 0
    peak = env_rolled[0]
    # noise floor of the correlation: the far end of the period (beyond ~8 m), or a silent capture
    if noise is not None and len(noise) == n:
        nspec = np.fft.rfft(noise)
        nspec[~band] = 0
        floor = float(np.median(_envelope(nspec * np.conj(np.fft.rfft(ref, n)), n)))
    else:
        floor = float(np.median(env_rolled[n // 2:]))
    # band energy vs. out-of-band: did the microphone hear the chirp at all?
    inband = float(np.sqrt(np.mean(np.abs(spec[band]) ** 2)))
    # echoes: local maxima after the direct path's own ringing (~1 ms), up to 10 m
    start = int(0.002 * rate)  # skip the direct sound's own tail (~35 cm)
    # stop before the period wraps back to the next direct sound
    stop = min(n - int(0.004 * rate), int(2 * 10.0 / SPEED_OF_SOUND * rate))
    seg = env_rolled[start:stop]
    # strongest true peaks first, at least ~15 cm apart (the chirp resolves ~9 cm)
    min_gap = int(2 * 0.15 / SPEED_OF_SOUND * rate)
    is_peak = np.r_[False, (seg[1:-1] > seg[:-2]) & (seg[1:-1] >= seg[2:]), False]
    order = [i for i in np.argsort(seg)[::-1] if is_peak[i] and seg[i] > 4 * floor]
    picked: list[int] = []
    for i in order:
        if all(abs(i - j) > min_gap for j in picked):
            picked.append(int(i))
        if len(picked) >= 12:
            break
    echoes = [{"distance_m": round((start + i) / rate * SPEED_OF_SOUND / 2, 2),
               "strength": round(float(seg[i] / peak), 3)} for i in picked]
    profile = env_rolled[:stop] / peak
    return {
        "direct_snr": round(float(peak / (floor + 1e-9)), 1),
        "inband_level": round(inband, 1),
        "echoes": sorted(echoes[:12], key=lambda e: e["distance_m"]),
        "profile": [round(float(v), 3) for v in profile[:: max(1, len(profile) // 300)]],
        "profile_step_m": round(max(1, len(profile) // 300) / rate * SPEED_OF_SOUND / 2, 4),
    }
