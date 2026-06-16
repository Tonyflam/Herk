"""Ledger: the audit chain must be append-only and tamper-evident.

If these break, judges (and the audience) can no longer trust HELM's track
record — so the tamper test is non-negotiable.
"""

from __future__ import annotations

import json

from helm.ledger import GENESIS_HASH, Ledger


def test_append_and_verify_intact(tmp_path):
    led = Ledger(tmp_path / "audit.jsonl")
    led.append("signal", {"top": ["UNI", "AAVE"]})
    led.append("trade", {"symbol": "UNI", "qty": 5, "side": "buy"})
    led.append("mark", {"equity": 101.2})

    ok, n, msg = led.verify()
    assert ok is True
    assert n == 3
    assert msg == "chain intact"


def test_first_record_chains_to_genesis(tmp_path):
    p = tmp_path / "audit.jsonl"
    rec = Ledger(p).append("signal", {"x": 1})
    assert rec["prev_hash"] == GENESIS_HASH
    assert rec["seq"] == 1
    assert len(rec["hash"]) == 64


def test_tamper_is_detected(tmp_path):
    p = tmp_path / "audit.jsonl"
    led = Ledger(p)
    led.append("trade", {"symbol": "UNI", "qty": 5})
    led.append("trade", {"symbol": "AAVE", "qty": 1})

    # Tamper: rewrite the first record's payload but keep its stored hash.
    lines = p.read_text().splitlines()
    obj = json.loads(lines[0])
    obj["data"]["symbol"] = "SCAM"
    lines[0] = json.dumps(obj)
    p.write_text("\n".join(lines) + "\n")

    ok, _, msg = led.verify()
    assert ok is False
    assert "tampered" in msg.lower() or "mismatch" in msg.lower()


def test_truncation_breaks_following_links(tmp_path):
    # Removing a middle record breaks the prev_hash linkage of the next one.
    p = tmp_path / "audit.jsonl"
    led = Ledger(p)
    for i in range(4):
        led.append("mark", {"i": i})

    lines = p.read_text().splitlines()
    del lines[1]  # drop seq 2
    p.write_text("\n".join(lines) + "\n")

    ok, _, _ = led.verify()
    assert ok is False


def test_chain_continues_across_instances(tmp_path):
    # A fresh Ledger over the same file must resume the seq/hash chain.
    p = tmp_path / "audit.jsonl"
    Ledger(p).append("a", {"i": 1})
    Ledger(p).append("b", {"i": 2})
    ok, n, _ = Ledger(p).verify()
    assert ok is True
    assert n == 2


def test_empty_ledger_is_valid(tmp_path):
    ok, n, _ = Ledger(tmp_path / "none.jsonl").verify()
    assert ok is True
    assert n == 0
