"""
Human motion and timing model.

Pure functions only — no Selenium, no AdsPower — so the behaviour model can be
tested and tuned without opening a browser. joiner.py wires these into the
driver (CDP scroll gestures, cursor moves, reading pauses).

Why these shapes:
- Uniform random timing is a tell. Real gaps cluster around a mean (gaussian)
  and the distribution is heavy tailed: mostly quick skims, some real reading,
  and the occasional long dwell when something catches the eye.
- A single scroll mechanism is also a tell. Two engines, biased per session,
  keep the signature from being identical on every run.
"""

from __future__ import annotations

import hashlib
import os
import random
import secrets
import threading
import time
from typing import Any, Callable, List, Sequence, Tuple

Number = float
Range = Tuple[float, float]

_tls = threading.local()


def begin_session_rng(user_id: str = "", label: str = "") -> random.Random:
    """Fresh random stream for one account sitting. Other profiles do not share it."""
    mixer = hashlib.blake2b(
        "|".join(
            (
                str(user_id or ""),
                str(label or ""),
                str(time.time_ns()),
                secrets.token_hex(16),
                str(os.getpid()),
                str(threading.get_ident()),
            )
        ).encode(),
        digest_size=16,
    ).digest()
    seed = int.from_bytes(mixer, "big")
    stream = random.Random(seed)
    _tls.rng = stream
    _tls.seed = seed
    return stream


def session_seed() -> int:
    return int(getattr(_tls, "seed", 0) or 0)


def rng() -> random.Random:
    stream = getattr(_tls, "rng", None)
    if stream is None:
        return random  # type: ignore[return-value]
    return stream


def end_session_rng() -> None:
    _tls.rng = None
    _tls.seed = 0


def _ordered(span: Sequence[float]) -> Range:
    low, high = float(span[0]), float(span[1])
    return (low, high) if low <= high else (high, low)


def gaussian_between(low: float, high: float) -> float:
    """
    A value inside [low, high] clustered around the middle. Six sigma across
    the range puts almost everything inside without a hard wall at the edges.
    """
    lo, hi = _ordered((low, high))
    if hi <= lo:
        return lo
    mean = (lo + hi) / 2.0
    std = (hi - lo) / 6.0
    return max(lo, min(hi, rng().gauss(mean, std)))


def gaussian_int(low: int, high: int) -> int:
    return int(round(gaussian_between(low, high)))


def human_chance(low: float = 0.3, high: float = 0.7) -> float:
    """A probability clustered in a band instead of flat across 0..1."""
    return gaussian_between(low, high)


def heavy_tailed_pause(
    read_range: Sequence[float],
    dwell_range: Sequence[float] = None,
    skim_share: float = 0.52,
    read_share: float = 0.38,
) -> float:
    """
    One inter-scroll pause drawn from three regimes so consecutive pauses look
    nothing alike:

      skim  - quick flick to flick, shorter than the nominal minimum
      read  - the configured range, clustered around its middle
      dwell - something caught the eye, well past the maximum
    """
    lo, hi = _ordered(read_range)
    roll = rng().random()
    if roll < skim_share:
        skim_lo = max(0.12, lo * 0.20)
        skim_hi = max(skim_lo + 0.06, lo * 0.75)
        return rng().uniform(skim_lo, skim_hi)
    if roll < skim_share + read_share:
        return gaussian_between(lo, hi)
    dwell_lo, dwell_hi = _ordered(dwell_range or (hi, hi * 2.2))
    return gaussian_between(max(dwell_lo, hi), max(dwell_hi, hi * 1.2))


def reading_seconds(
    text_length: int,
    base_range: Sequence[float] = (2.0, 5.0),
    per_char: float = 0.012,
    extra_cap: float = 9.0,
) -> float:
    """
    Reading time that scales with how much there is to read. Reddit is a text
    feed, so a long self post should take meaningfully longer than a one liner.
    """
    chars = max(0, int(text_length or 0))
    return gaussian_between(*base_range) + min(chars * per_char, extra_cap)


def bezier_path(
    start: Tuple[float, float],
    end: Tuple[float, float],
    steps: int,
    spread: float = 80.0,
) -> List[Tuple[float, float]]:
    """
    Cubic bezier points from start to end with jittered control points, so the
    cursor arcs across the page instead of sliding down a straight line.
    """
    steps = max(2, int(steps))
    x0, y0 = float(start[0]), float(start[1])
    x3, y3 = float(end[0]), float(end[1])
    x1 = x0 + rng().uniform(-spread, spread)
    y1 = y0 + rng().uniform(-spread * 0.75, spread * 0.75)
    x2 = x3 + rng().uniform(-spread, spread)
    y2 = y3 + rng().uniform(-spread * 0.75, spread * 0.75)
    points: List[Tuple[float, float]] = []
    for index in range(1, steps + 1):
        t = index / float(steps)
        u = 1.0 - t
        w0 = u * u * u
        w1 = 3.0 * u * u * t
        w2 = 3.0 * u * t * t
        w3 = t * t * t
        points.append(
            (
                w0 * x0 + w1 * x1 + w2 * x2 + w3 * x3,
                w0 * y0 + w1 * y1 + w2 * y2 + w3 * y3,
            )
        )
    return points


def scroll_step_sizes(total_px: int, tick_range: Sequence[float]) -> List[int]:
    """
    Split a scroll into wheel ticks ramped accelerate -> cruise -> decelerate,
    so a stepped scroll still reads as one fluid gesture.
    """
    total = abs(int(total_px))
    if total <= 0:
        return []
    lo, hi = _ordered(tick_range)
    steps: List[int] = []
    sent = 0
    while sent < total:
        progress = sent / float(total)
        # Fast through the middle, gentle at both ends
        speed = 0.45 + 0.55 * (1.0 - abs(0.5 - progress) * 2.0)
        size = int(round(rng().uniform(lo, hi) * (0.55 + speed)))
        size = max(4, size)
        if sent + size > total:
            size = total - sent
        steps.append(size)
        sent += size
    return steps


def order_by_human_interest(
    items: Sequence[Any],
    score_fn: Callable[[Any], float],
) -> List[Any]:
    """
    Best first, but with gaussian jitter proportional to the score, because a
    person does not always pick the single highest ranked thing on the page.
    """
    scored = []
    for item in items:
        try:
            base = float(score_fn(item))
        except Exception:
            base = 1.0
        jitter = rng().gauss(0.0, max(abs(base) * 0.25, 1.0))
        scored.append((base + jitter, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored]


def shuffled(items: Sequence[Any]) -> List[Any]:
    out = list(items)
    rng().shuffle(out)
    return out


def changed_mind(bail_chance: float = 0.12) -> bool:
    """True when the account should back out of an action at the last moment."""
    return rng().random() < max(0.0, min(1.0, float(bail_chance)))


def scroll_speed_px_per_sec(span: Sequence[float] = (650.0, 1600.0)) -> int:
    """Momentum gesture speed with natural variance."""
    return int(gaussian_between(*span))
