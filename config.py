from dataclasses import dataclass

from utils import env_bool, env_float, env_int, env_str


@dataclass(frozen=True, slots=True)
class BotConfig:
    clob_host: str
    market_slug_fmt: str
    market_window_s: int
    auth_cache_max_entries: int
    http_dns_ttl_s: int
    http_keepalive_timeout_s: float
    http_connect_timeout_s: float
    http_sock_connect_timeout_s: float
    clob_http_timeout_total_s: float
    gamma_http_timeout_total_s: float
    binance_symbol: str
    disable_gc: bool

    @staticmethod
    def from_env() -> "BotConfig":
        return BotConfig(
            clob_host=env_str("POLY_CLOB_HOST", "https://clob.polymarket.com"),
            market_slug_fmt=env_str("MARKET_SLUG_FMT", "btc-updown-5m-{ts}"),
            market_window_s=env_int("MARKET_WINDOW_S", 300, min_value=60, max_value=3600),
            auth_cache_max_entries=env_int("POLY_AUTH_CACHE_MAX_ENTRIES", 10, min_value=1),
            http_dns_ttl_s=env_int("POLY_HTTP_DNS_TTL_S", 600, min_value=1),
            http_keepalive_timeout_s=env_float("POLY_HTTP_KEEPALIVE_S", 60.0, min_value=0.1),
            http_connect_timeout_s=env_float("POLY_HTTP_CONNECT_TIMEOUT_S", 1.0, min_value=0.01),
            http_sock_connect_timeout_s=env_float("POLY_HTTP_SOCK_CONNECT_TIMEOUT_S", 1.0, min_value=0.01),
            clob_http_timeout_total_s=env_float("POLY_HTTP_TIMEOUT_S", 3.0, min_value=0.05),
            gamma_http_timeout_total_s=env_float("GAMMA_HTTP_TIMEOUT_S", 3.0, min_value=0.05),
            binance_symbol=env_str("BINANCE_SYMBOL", "BTCUSDT").upper(),
            disable_gc=env_bool("DISABLE_GC", True),
        )


@dataclass(slots=True)
class MinimalOrderConfig:
    host: str
    chain_id: int
    private_key: str
    signature_type: int
    funder: str

    @staticmethod
    def from_env() -> "MinimalOrderConfig":
        if not env_bool("POLY_ALLOW_LIVE_ORDERS", False) and not env_bool("MINIMAL_DRY_RUN_ORDERS", False):
            raise RuntimeError(
                "Refusing to initialize live order client. Set POLY_ALLOW_LIVE_ORDERS=true "
                "for live CLOB orders or MINIMAL_DRY_RUN_ORDERS=true for non-transactional smoke tests."
            )
        return MinimalOrderConfig(
            host=env_str("POLY_CLOB_HOST", "https://clob.polymarket.com").strip()
            or "https://clob.polymarket.com",
            chain_id=env_int("POLY_CHAIN_ID", 137),
            private_key=env_str("POLY_PK", required=True).strip(),
            signature_type=env_int("POLY_SIG_TYPE", 0),
            funder=env_str("POLY_FUNDER", "").strip(),
        )
