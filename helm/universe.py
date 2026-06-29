"""HELM token universe.

Two tiers:

* ELIGIBLE — the fixed competition set of BEP-20 symbols listed on CoinMarketCap.
  Trades outside this set DO NOT COUNT toward the competition.
* TRADEABLE — a curated, liquid subset we actually trade by default. With small
  capital, naively rotating across all 149 (many are micro-cap / honeypot-risk /
  thin liquidity) is a fast way to bleed to slippage and trip the drawdown gate.
  Survival-first: trade liquid names, hold USDT as the cash leg, BNB for gas only.

Contract addresses are intentionally NOT hardcoded. Symbols are resolved at
execution time by TWAK (and cross-checked against the CMC id), which avoids
wrong-token / multi-pool mispricing risk and stale-address bugs.
"""

from __future__ import annotations

# Cash leg. BNB is NOT in the eligible list (held only to pay gas).
BASE_CURRENCY = "USDT"

# --- Eligible competition set (fixed). Order preserved; duplicates removed. ---
ELIGIBLE: tuple[str, ...] = (
    "ETH", "USDT", "USDC", "XRP", "TRX", "DOGE", "ZEC", "ADA", "LINK", "BCH",
    "DAI", "TON", "USD1", "USDe", "M", "LTC", "AVAX", "SHIB", "XAUt", "WLFI",
    "H", "DOT", "UNI", "ASTER", "DEXE", "USDD", "ETC", "AAVE", "ATOM", "U",
    "STABLE", "FIL", "INJ", "币安人生", "NIGHT", "FET", "TUSD", "BONK", "PENGU",
    "CAKE", "SIREN", "LUNC", "ZRO", "KITE", "FDUSD", "BEAT", "PIEVERSE", "BTT",
    "NFT", "EDGE", "FLOKI", "LDO", "B", "FF", "PENDLE", "NEX", "STG", "AXS",
    "TWT", "HOME", "RAY", "COMP", "GWEI", "XCN", "GENIUS", "XPL", "BAT", "SKYAI",
    "APE", "IP", "SFP", "TAG", "NXPC", "AB", "SAHARA", "1INCH", "CHEEMS",
    "BANANAS31", "RIVER", "MYX", "RAVE", "SNX", "FORM", "LAB", "HTX", "USDf",
    "CTM", "BDX", "SLX", "UB", "DUCKY", "FRAX", "BILL", "WFI", "KOGE", "ALE",
    "FRXUSD", "USDF", "GOMINING", "VCNT", "GUA", "DUSD", "SMILEK", "0G", "BEAM",
    "MY", "SOON", "REAL", "Q", "AIOZ", "ZIG", "YFI", "TAC", "lisUSD", "CYS",
    "ZAMA", "TRIA", "HUMA", "PLUME", "ZIL", "XPR", "ZETA", "BabyDoge", "NILA",
    "ROSE", "VELO", "UAI", "BRETT", "OPEN", "BSB", "TOSHI", "BAS", "ACH", "AXL",
    "LUR", "ELF", "KAVA", "APR", "IRYS", "EURI", "XUSD", "BARD", "DUSK", "SUSHI",
    "PEAQ", "COAI", "BDCA", "XAUM",
)

# --- Stable / pegged assets — treated as cash-like "safe haven", never as a
# momentum trade. Rotating INTO these is how HELM goes risk-off. ---
STABLES: frozenset[str] = frozenset({
    "USDT", "USDC", "DAI", "USD1", "USDe", "TUSD", "FDUSD", "USDD", "STABLE",
    "USDf", "USDF", "FRAX", "FRXUSD", "DUSD", "lisUSD", "XUSD", "EURI", "BILL",
    # Additional stablecoins / fiat quote-bases that surface in full-market
    # discovery (no momentum -> never a risk trade).
    "RLUSD", "PYUSD", "AEUR", "USDP", "EUR", "GBP", "TRY", "BRL", "ARS",
})

# Gold-pegged — low-vol, not USD-stable; excluded from default trading.
PEGGED_COMMODITY: frozenset[str] = frozenset({"XAUt", "XAUM"})

# --- Curated liquid tradeable subset (⊆ ELIGIBLE). High recognition + real
# PancakeSwap/bridged liquidity. This is the survival-first default book. ---
CURATED_TRADEABLE: tuple[str, ...] = (
    "CAKE", "TWT", "ETH", "XRP", "ADA", "DOGE", "LINK", "DOT", "UNI", "LTC",
    "BCH", "AVAX", "ATOM", "FIL", "INJ", "AAVE", "ETC", "PENDLE", "COMP",
    "SUSHI", "APE", "SNX", "FET", "LDO",
)


def eligible_set() -> frozenset[str]:
    """All symbols that count toward the competition."""
    return frozenset(ELIGIBLE)


def is_stable(symbol: str) -> bool:
    return symbol in STABLES


def tradeable_universe(
    use_curated: bool = True,
    extra: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> list[str]:
    """Resolve the active trading book.

    Always a subset of ELIGIBLE. Stables/pegged and the base currency are never
    returned as risk trades. ``extra`` widens the curated book with any eligible
    symbol; ``exclude`` removes names.
    """
    elig = eligible_set()
    base = CURATED_TRADEABLE if use_curated else ELIGIBLE
    out: list[str] = []
    seen: set[str] = set()
    for sym in (*base, *extra):
        if sym in seen:
            continue
        if sym not in elig:
            continue  # never trade something that doesn't count
        if sym in STABLES or sym in PEGGED_COMMODITY or sym == BASE_CURRENCY:
            continue
        if sym in exclude:
            continue
        seen.add(sym)
        out.append(sym)
    return out


# Binance-style leveraged-token suffixes (e.g. BTCUP / ETHDOWN / XRPBULL). These
# decay and must never enter the tradeable book. The prefix-length guard keeps
# legitimate names that merely END in these letters (e.g. JUP, the "UP" is part
# of the name) from being dropped.
_LEVERAGED_SUFFIXES: tuple[str, ...] = ("UP", "DOWN", "BULL", "BEAR")


def _is_leveraged_token(base: str) -> bool:
    for sfx in _LEVERAGED_SUFFIXES:
        if base.endswith(sfx) and (len(base) - len(sfx)) >= 2:
            return True
    return False


def rank_full_market(
    tickers: list[dict],
    quote: str = "USDT",
    top_n: int = 60,
    min_vol_usd: float = 5_000_000.0,
    exclude: tuple[str, ...] = (),
) -> list[str]:
    """Rank a venue's whole 24h-ticker list into the top liquid base symbols.

    Pure / deterministic — no network. Filters a raw exchange ``ticker/24hr``
    list (each row a dict with at least ``symbol`` and ``quoteVolume``) to
    ``{base}{quote}`` spot pairs, drops stables, gold-pegged, leveraged tokens
    and the base/quote currency itself, keeps names with >= ``min_vol_usd`` of
    24h quote volume, sorts by that volume descending and returns the top
    ``top_n`` bare base symbols. This is the OMEGA "thousands of tokens" unlock:
    the book is whatever the live market says is most liquid, not a fixed list.
    """
    q = quote.upper()
    excl = set(exclude)
    rows: list[tuple[str, float]] = []
    seen: set[str] = set()
    for t in tickers:
        sym = str(t.get("symbol", "")).upper()
        if not sym.endswith(q):
            continue
        base = sym[: -len(q)]
        if not base or base in seen or base in excl:
            continue
        if base in STABLES or base in PEGGED_COMMODITY or base == BASE_CURRENCY:
            continue
        if _is_leveraged_token(base):
            continue
        try:
            vol = float(t.get("quoteVolume", 0.0))
        except (TypeError, ValueError):
            continue
        if vol < min_vol_usd:
            continue
        seen.add(base)
        rows.append((base, vol))
    rows.sort(key=lambda r: r[1], reverse=True)
    return [base for base, _ in rows[:top_n]]


# Sanity: curated must be a strict subset of eligible (guards typos at import).
_unknown = [s for s in CURATED_TRADEABLE if s not in eligible_set()]
if _unknown:  # pragma: no cover - defensive
    raise ValueError(f"CURATED_TRADEABLE has non-eligible symbols: {_unknown}")
