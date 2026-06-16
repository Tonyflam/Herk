"""Tamper-evident audit ledger (hash-chained JSONL).

Every decision HELM makes — signals, risk verdicts, trades, halts — is appended
as a record whose ``hash`` chains to the previous record's hash (like a tiny
blockchain). Anyone can replay the file and prove it was not edited after the
fact: change one byte and every downstream hash breaks.

Why it matters for the contest: judges (and a live audience) can trust the
agent's track record. ``helm verify`` re-validates the whole chain offline.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class Ledger:
    """Append-only, hash-chained JSONL ledger. Thread-safe for a single process."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_hash, self._seq = self._read_tail()

    def _read_tail(self) -> tuple[str, int]:
        if not self.path.exists():
            return GENESIS_HASH, 0
        last_line = ""
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s:
                    last_line = s
        if not last_line:
            return GENESIS_HASH, 0
        try:
            obj = json.loads(last_line)
            return str(obj["hash"]), int(obj["seq"])
        except Exception:
            return GENESIS_HASH, 0

    @staticmethod
    def _digest(prev_hash: str, core: dict[str, Any]) -> str:
        return hashlib.sha256((prev_hash + _canonical(core)).encode("utf-8")).hexdigest()

    def append(self, rtype: str, data: dict[str, Any]) -> dict[str, Any]:
        """Append a record; returns the full record (incl. hash)."""
        with self._lock:
            seq = self._seq + 1
            core = {
                "seq": seq,
                "ts": _now_iso(),
                "type": rtype,
                "data": data,
                "prev_hash": self._last_hash,
            }
            h = self._digest(self._last_hash, core)
            record = dict(core, hash=h)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
            self._seq = seq
            self._last_hash = h
            return record

    def verify(self) -> tuple[bool, int, str]:
        """Replay the chain. Returns (ok, records_checked, message)."""
        if not self.path.exists():
            return True, 0, "empty ledger"
        prev = GENESIS_HASH
        n = 0
        with self.path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except Exception:
                    return False, n, f"line {i}: invalid JSON"
                stored = obj.pop("hash", None)
                if obj.get("prev_hash") != prev:
                    return False, n, f"seq {obj.get('seq')}: prev_hash mismatch"
                if self._digest(prev, obj) != stored:
                    return False, n, f"seq {obj.get('seq')}: hash mismatch (tampered)"
                prev = stored
                n += 1
        return True, n, "chain intact"

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines: list[str] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s:
                    lines.append(s)
        out = []
        for s in lines[-n:]:
            try:
                out.append(json.loads(s))
            except Exception:
                continue
        return out
