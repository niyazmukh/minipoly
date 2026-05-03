from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson


# Online estimator of the basis between a Binance reference (e.g. BTCUSDT
# microprice) and the price index that Polymarket settles against. The
# estimator updates an EMA of (binance_microprice - polymarket_strike) only
# while the Polymarket YES midprice sits inside [0.5 - mid_tol, 0.5 + mid_tol]
# and the time-to-expiry is comfortably above min_tte_us, since under a
# diffusion with zero drift the implied reference price equals the strike
# exactly when the binary midprice equals 0.5. Outside that band the
# estimator holds its previous state.
#
# Persisted to disk via save()/load() so the bot does not lose the basis
# across restarts.


@dataclass(frozen=True, slots=True)
class BasisEstimatorConfig:
    alpha: float = 0.05
    mid_tol: float = 0.05
    min_tte_us: int = 30_000_000  # only update when >= 30s left in window
    seed_basis: float = 0.0
    seed_weight: float = 0.0  # 0..1; >0 means estimator is treated as initialized


class BasisEstimator:
    __slots__ = ("_cfg", "_basis", "_initialized", "_samples")

    def __init__(self, cfg: BasisEstimatorConfig) -> None:
        self._cfg = BasisEstimatorConfig(
            alpha=max(1e-6, min(1.0, float(cfg.alpha))),
            mid_tol=max(0.0, min(0.5, float(cfg.mid_tol))),
            min_tte_us=max(0, int(cfg.min_tte_us)),
            seed_basis=float(cfg.seed_basis),
            seed_weight=max(0.0, min(1.0, float(cfg.seed_weight))),
        )
        self._basis = float(self._cfg.seed_basis)
        self._initialized = self._cfg.seed_weight > 0.0
        self._samples = 0

    @property
    def basis(self) -> float:
        return self._basis

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def samples(self) -> int:
        return self._samples

    def update(self, *, binance_microprice: float, yes_mid: float, strike: float, tte_us: int) -> float:
        if strike <= 0.0 or binance_microprice <= 0.0:
            return self._basis
        if yes_mid <= 0.0 or yes_mid >= 1.0:
            return self._basis
        if tte_us < self._cfg.min_tte_us:
            return self._basis
        if abs(yes_mid - 0.5) > self._cfg.mid_tol:
            return self._basis
        sample = float(binance_microprice) - float(strike)
        if not self._initialized:
            self._basis = sample
            self._initialized = True
        else:
            self._basis += self._cfg.alpha * (sample - self._basis)
        self._samples += 1
        return self._basis

    def effective_strike(self, polymarket_strike: float) -> float:
        if polymarket_strike <= 0.0:
            return 0.0
        return float(polymarket_strike) + self._basis

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": self._basis,
            "initialized": self._initialized,
            "samples": self._samples,
            "alpha": self._cfg.alpha,
            "mid_tol": self._cfg.mid_tol,
            "min_tte_us": self._cfg.min_tte_us,
        }

    def load_state(self, state: dict[str, Any]) -> None:
        if not isinstance(state, dict):
            return
        try:
            basis = float(state.get("basis", self._basis))
        except (TypeError, ValueError):
            return
        self._basis = basis
        self._initialized = bool(state.get("initialized", True))
        try:
            self._samples = int(state.get("samples", self._samples))
        except (TypeError, ValueError):
            pass

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = orjson.dumps(self.to_dict())
        # Atomic write so we never read a half-written file at startup.
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: Path, cfg: BasisEstimatorConfig) -> "BasisEstimator":
        est = cls(cfg)
        try:
            data = orjson.loads(Path(path).read_bytes())
        except (FileNotFoundError, orjson.JSONDecodeError, OSError):
            return est
        if isinstance(data, dict):
            est.load_state(data)
        return est
