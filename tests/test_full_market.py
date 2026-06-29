"""Tests for OMEGA full-market discovery: the pure ranker + the keyless fetcher.

No network: the fetcher is driven by a fake httpx-like client, and the ranker is
a pure function over a synthetic ticker list.
"""

from __future__ import annotations

from helm.data.sources import fetch_24h_tickers
from helm.universe import rank_full_market


def _t(symbol: str, vol: float) -> dict:
    return {"symbol": symbol, "quoteVolume": vol}


# --------------------------------------------------------------------------- #
# Pure ranker                                                                 #
# --------------------------------------------------------------------------- #
def test_ranks_by_quote_volume_desc_and_caps_top_n():
    tickers = [_t("BTCUSDT", 100), _t("ETHUSDT", 80), _t("SOLUSDT", 60), _t("XRPUSDT", 40)]
    out = rank_full_market(tickers, top_n=2, min_vol_usd=0)
    assert out == ["BTC", "ETH"]


def test_drops_non_quote_pairs():
    tickers = [_t("BTCUSDT", 100), _t("ETHBTC", 999), _t("ADABUSD", 999)]
    out = rank_full_market(tickers, quote="USDT", min_vol_usd=0)
    assert out == ["BTC"]


def test_drops_stables_and_quote_currency():
    tickers = [_t("BTCUSDT", 100), _t("USDCUSDT", 999), _t("DAIUSDT", 999), _t("FDUSDUSDT", 999)]
    out = rank_full_market(tickers, min_vol_usd=0)
    assert out == ["BTC"]


def test_min_volume_filter():
    tickers = [_t("BTCUSDT", 9_000_000), _t("PEPEUSDT", 1_000)]
    out = rank_full_market(tickers, min_vol_usd=5_000_000)
    assert out == ["BTC"]


def test_drops_leveraged_tokens_but_keeps_jup():
    # BTCUP / ETHDOWN / XRPBULL are leveraged; JUP merely ends in "UP".
    tickers = [
        _t("BTCUPUSDT", 500),
        _t("ETHDOWNUSDT", 500),
        _t("XRPBULLUSDT", 500),
        _t("JUPUSDT", 400),
    ]
    out = rank_full_market(tickers, min_vol_usd=0)
    assert out == ["JUP"]


def test_exclude_list_respected():
    tickers = [_t("BTCUSDT", 100), _t("ETHUSDT", 80)]
    out = rank_full_market(tickers, min_vol_usd=0, exclude=("BTC",))
    assert out == ["ETH"]


def test_dedupes_and_handles_bad_rows():
    tickers = [
        _t("BTCUSDT", 100),
        {"symbol": "BTCUSDT", "quoteVolume": "nan-ish"},  # bad volume -> skipped
        {"symbol": "ETHUSDT"},                              # missing volume -> 0
        {},                                                 # empty -> skipped
    ]
    out = rank_full_market(tickers, min_vol_usd=0)
    assert out == ["BTC", "ETH"]


def test_empty_input_returns_empty():
    assert rank_full_market([], min_vol_usd=0) == []


# --------------------------------------------------------------------------- #
# Keyless fetcher (fake client)                                               #
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _Client:
    def __init__(self, payload, fail=False):
        self._p = payload
        self.fail = fail

    def get(self, url, params=None, headers=None, timeout=None):
        if self.fail:
            raise RuntimeError("network down")
        return _Resp(self._p)

    def close(self):
        pass


def test_fetch_returns_ticker_list():
    payload = [{"symbol": "BTCUSDT", "quoteVolume": "123"}]
    out = fetch_24h_tickers(client=_Client(payload))
    assert out == payload


def test_fetch_returns_empty_on_total_failure():
    out = fetch_24h_tickers(client=_Client(None, fail=True))
    assert out == []


def test_fetch_then_rank_end_to_end():
    payload = [_t("BTCUSDT", 100), _t("ETHUSDT", 80), _t("USDCUSDT", 999)]
    tickers = fetch_24h_tickers(client=_Client(payload))
    assert rank_full_market(tickers, top_n=5, min_vol_usd=0) == ["BTC", "ETH"]
