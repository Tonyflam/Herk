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


# Sanity: curated must be a strict subset of eligible (guards typos at import).
_unknown = [s for s in CURATED_TRADEABLE if s not in eligible_set()]
if _unknown:  # pragma: no cover - defensive
    raise ValueError(f"CURATED_TRADEABLE has non-eligible symbols: {_unknown}")
