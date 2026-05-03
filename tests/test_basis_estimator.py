import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import basis_estimator as basis_module
from basis_estimator import BasisEstimator, BasisEstimatorConfig


def _est(seed_basis: float = 0.0, seed_weight: float = 0.0, alpha: float = 0.5) -> BasisEstimator:
    return BasisEstimator(
        BasisEstimatorConfig(
            alpha=alpha,
            mid_tol=0.05,
            min_tte_us=10_000_000,
            seed_basis=seed_basis,
            seed_weight=seed_weight,
        )
    )


def test_seeds_initial_basis_when_seed_weight_positive() -> None:
    est = _est(seed_basis=20.0, seed_weight=1.0)
    assert est.basis == 20.0
    assert est.initialized is True
    assert est.effective_strike(40000.0) == 40020.0


def test_holds_when_yes_mid_outside_band() -> None:
    est = _est()
    est.update(binance_microprice=40020.0, yes_mid=0.7, strike=40000.0, tte_us=60_000_000)
    assert est.basis == 0.0
    assert est.initialized is False


def test_holds_when_tte_too_short() -> None:
    est = _est()
    est.update(binance_microprice=40020.0, yes_mid=0.50, strike=40000.0, tte_us=5_000_000)
    assert est.basis == 0.0


def test_initialises_on_first_valid_sample() -> None:
    est = _est()
    new = est.update(binance_microprice=40020.0, yes_mid=0.50, strike=40000.0, tte_us=60_000_000)
    assert new == 20.0
    assert est.basis == 20.0
    assert est.initialized is True


def test_ema_converges_to_steady_basis() -> None:
    est = _est(seed_basis=0.0, seed_weight=1.0, alpha=0.5)
    for _ in range(40):
        est.update(binance_microprice=40020.0, yes_mid=0.50, strike=40000.0, tte_us=60_000_000)
    assert abs(est.basis - 20.0) < 1e-6


def test_save_and_load_roundtrip(monkeypatch) -> None:
    writes: dict[Path, bytes] = {}
    pipe_read_fds: dict[str, int] = {}

    def _mkstemp(*, dir, prefix, suffix):
        read_fd, write_fd = os.pipe()
        tmp_name = f"{dir}/{prefix}mem{suffix}"
        pipe_read_fds[tmp_name] = read_fd
        return write_fd, tmp_name

    def _replace(tmp_name, dst):
        read_fd = pipe_read_fds.pop(tmp_name)
        chunks = []
        try:
            while True:
                chunk = os.read(read_fd, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(read_fd)
        writes[Path(dst)] = b"".join(chunks)

    def _read_bytes(self):
        try:
            return writes[self]
        except KeyError as exc:
            raise FileNotFoundError(str(self)) from exc

    monkeypatch.setattr(basis_module.tempfile, "mkstemp", _mkstemp)
    monkeypatch.setattr(basis_module.os, "replace", _replace)
    monkeypatch.setattr(Path, "read_bytes", _read_bytes)

    est = _est(seed_basis=20.0, seed_weight=1.0)
    est.update(binance_microprice=40010.0, yes_mid=0.50, strike=40000.0, tte_us=60_000_000)
    state_path = Path("basis.json")
    est.save(state_path)
    loaded = BasisEstimator.load(state_path, BasisEstimatorConfig(alpha=0.5, mid_tol=0.05, min_tte_us=10_000_000))
    assert abs(loaded.basis - est.basis) < 1e-9
    assert loaded.initialized is True


def test_load_missing_file_returns_default(monkeypatch) -> None:
    def _missing(_self):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(Path, "read_bytes", _missing)

    est = BasisEstimator.load(Path("missing.json"), BasisEstimatorConfig())
    assert est.basis == 0.0
    assert est.initialized is False


def test_effective_strike_zero_when_polymarket_strike_zero() -> None:
    est = _est(seed_basis=20.0, seed_weight=1.0)
    assert est.effective_strike(0.0) == 0.0
