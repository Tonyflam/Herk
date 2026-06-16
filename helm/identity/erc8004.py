"""ERC-8004 agent identity + native x402 signer via the BNB AI Agent SDK.

Two capabilities, both built on the SDK's local-signing wallet
(``EVMWalletProvider`` — keystore-V3, the private key never leaves the machine):

  • register()      — mint HELM a verifiable on-chain identity (ERC-8004). On
                      bsc-testnet this is gas-free, so HELM can prove "who it is"
                      to judges and counterparties with one command.
  • x402_signer()   — an ``X402Signer`` pre-loaded with per-call and per-session
                      spend caps, using ``SigningPolicy.strict_default()`` which
                      *denylists* every unbounded Permit/Permit2 variant and only
                      allows single-use EIP-3009 authorizations. This is the
                      native (non-TWAK) path for paying CoinMarketCap via x402.

API surface is verified against bnbagent==0.3.6. Imports are lazy so paper mode
never requires the on-chain extras (see requirements-live.txt).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..config import REPO_ROOT, Settings

RUNTIME_DIR = REPO_ROOT / "data" / "runtime"
IDENTITY_FILE = RUNTIME_DIR / "identity.json"
KEYSTORE_DIR = RUNTIME_DIR / "keystore"

_CHAIN_ID = {"bsc-testnet": 97, "bsc-mainnet": 56}


@dataclass
class IdentityResult:
    ok: bool
    agent_id: int | None = None
    tx_hash: str = ""
    address: str = ""
    network: str = ""
    agent_uri: str = ""
    note: str = ""

    def __str__(self) -> str:
        if not self.ok:
            return f"[identity] FAILED ({self.network}): {self.note}"
        return (f"[identity] {self.note} · agentId={self.agent_id} "
                f"addr={self.address} net={self.network}"
                + (f" tx={self.tx_hash}" if self.tx_hash else ""))


class Erc8004Identity:
    def __init__(self, settings: Settings):
        self.s = settings
        self.network = (settings.identity.network
                        or settings.secrets.bnb_agent_network or "bsc-testnet")
        self.chain_id = _CHAIN_ID.get(self.network, 97)

    # ----------------------------------------------------------------- sdk
    @staticmethod
    def _sdk():
        import bnbagent  # lazy; only needed on-chain
        return bnbagent

    def _wallet(self):
        bnb = self._sdk()
        pw = self.s.secrets.bnb_agent_wallet_password
        if not pw:
            raise RuntimeError("BNB_AGENT_WALLET_PASSWORD not set")
        KEYSTORE_DIR.mkdir(parents=True, exist_ok=True)
        return bnb.EVMWalletProvider(
            password=pw,
            private_key=self.s.secrets.bnb_agent_private_key or None,
            persist=True,
            wallets_dir=str(KEYSTORE_DIR),
            signing_policy=bnb.SigningPolicy.strict_default(),
        )

    # ------------------------------------------------------------- helpers
    def address(self) -> str:
        return self._wallet().get_wallet_info().get("address", "")

    def _can_register(self) -> tuple[bool, str]:
        if not self.s.secrets.bnb_agent_wallet_password:
            return False, "BNB_AGENT_WALLET_PASSWORD not set (see scripts/setup_live.sh)"
        if self.network == "bsc-mainnet" and not self.s.secrets.execute_chain:
            return False, "mainnet registration requires HELM_EXECUTE_CHAIN=1"
        return True, ""

    # ------------------------------------------------------------ register
    def register(
        self,
        name: str = "HELM",
        description: str | None = None,
        endpoint_url: str | None = None,
        image: str | None = None,
    ) -> IdentityResult:
        ok, why = self._can_register()
        if not ok:
            return IdentityResult(False, network=self.network, note=why)
        try:
            bnb = self._sdk()
        except Exception as e:
            return IdentityResult(False, network=self.network,
                                  note=f"bnbagent not installed: {e}")
        try:
            wallet = self._wallet()
            addr = wallet.get_wallet_info().get("address", "")
            agent = bnb.ERC8004Agent(wallet_provider=wallet, network=self.network)

            # Idempotent: reuse a prior local registration of the same name.
            try:
                existing = agent.get_local_agent_info(name)
            except Exception:
                existing = None
            if existing and existing.get("agentId") is not None:
                out = IdentityResult(True, int(existing["agentId"]), "", addr,
                                     self.network, existing.get("agentURI", ""),
                                     "already registered")
                self._save(out)
                return out

            description = description or (
                "HELM — contest-optimal, self-custody trading agent. "
                "Deterministic risk core; LLM never touches the wallet."
            )
            endpoint_url = endpoint_url or f"http://{self.s.dashboard.host}:{self.s.dashboard.port}"
            uri = agent.generate_agent_uri(
                name=name,
                description=description,
                endpoints=[bnb.AgentEndpoint(
                    name="dashboard", endpoint=endpoint_url,
                    capabilities=["trading", "erc8004", "x402"],
                )],
                image=image,
            )
            res = agent.register_agent(agent_uri=uri)
            agent_id = int(res["agentId"]) if res.get("agentId") is not None else None
            out = IdentityResult(
                ok=bool(res.get("success", False)),
                agent_id=agent_id,
                tx_hash=res.get("transactionHash", ""),
                address=addr,
                network=self.network,
                agent_uri=uri,
                note="registered",
            )
            self._save(out)
            return out
        except Exception as e:
            return IdentityResult(False, network=self.network, note=f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------- x402
    def x402_signer(self):
        """Native x402 signer with bounded per-call / per-session spend caps."""
        bnb = self._sdk()
        wallet = self._wallet()
        max_wei = int(self.s.secrets.x402_max_payment_wei)
        tokens = [addr for (cid, addr) in bnb.networks.known_payment_tokens()
                  if cid == self.chain_id]
        per_call = {addr: max_wei for addr in tokens}
        per_session = {addr: max_wei * 50 for addr in tokens}  # bounded session budget
        return bnb.X402Signer(wallet, max_value_per_call=per_call,
                              session_budget=per_session)

    # ---------------------------------------------------------- persistence
    def _save(self, r: IdentityResult) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        IDENTITY_FILE.write_text(json.dumps({
            "agent_id": r.agent_id,
            "tx_hash": r.tx_hash,
            "address": r.address,
            "network": r.network,
            "agent_uri": r.agent_uri,
        }, indent=2))

    @staticmethod
    def load() -> dict | None:
        if IDENTITY_FILE.exists():
            try:
                return json.loads(IDENTITY_FILE.read_text())
            except Exception:
                return None
        return None
