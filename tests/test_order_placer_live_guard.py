import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from order_placer import MinimalOrderConfig


def test_order_config_requires_explicit_live_order_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLY_PK", "0x" + "1" * 64)
    monkeypatch.delenv("POLY_ALLOW_LIVE_ORDERS", raising=False)

    with pytest.raises(RuntimeError, match="POLY_ALLOW_LIVE_ORDERS=true"):
        MinimalOrderConfig.from_env()


def test_order_config_accepts_explicit_live_order_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLY_PK", "0x" + "1" * 64)
    monkeypatch.setenv("POLY_ALLOW_LIVE_ORDERS", "true")

    cfg = MinimalOrderConfig.from_env()

    assert cfg.private_key == os.environ["POLY_PK"]


def test_order_config_accepts_dry_run_order_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLY_PK", "0x" + "1" * 64)
    monkeypatch.delenv("POLY_ALLOW_LIVE_ORDERS", raising=False)
    monkeypatch.setenv("MINIMAL_DRY_RUN_ORDERS", "true")

    cfg = MinimalOrderConfig.from_env()

    assert cfg.private_key == os.environ["POLY_PK"]


def test_manual_sell_requires_separate_untracked_sell_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLY_PK", "0x" + "1" * 64)
    monkeypatch.setenv("POLY_ALLOW_LIVE_ORDERS", "true")
    monkeypatch.delenv("POLY_ALLOW_UNTRACKED_SELL", raising=False)

    cfg = MinimalOrderConfig.from_env()

    with pytest.raises(RuntimeError, match="POLY_ALLOW_UNTRACKED_SELL=true"):
        cfg.require_manual_sell_allowed()
