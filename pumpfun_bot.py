"""
pump.fun Autonomous Trading Bot
================================
Creates a token on pump.fun, buys on creation, waits, sells,
then sweeps all profit to a destination wallet.

Dependencies:
    pip install -r requirements.txt

Usage:
    1. Copy .env.example to .env and fill in your values
    2. Place your token image at the path set in TOKEN_IMAGE_PATH
    3. Run: python pumpfun_bot.py
"""

import asyncio
import json
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests
from colorama import Fore, init
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Finalized, Processed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_price
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

# ─────────────────────────────────────────────────────────────
# Bootstrap
# ─────────────────────────────────────────────────────────────
init(autoreset=True)
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
    ],
)
log = logging.getLogger("pumpfun_bot")


# ─────────────────────────────────────────────────────────────
# ⚙️  Configuration
# ─────────────────────────────────────────────────────────────
@dataclass
class BotConfig:

    # ── Wallet ────────────────────────────────────────────────
    private_key: str = os.getenv("PRIVATE_KEY", "")

    # ── RPC ───────────────────────────────────────────────────
    # Use a premium RPC (Helius, QuickNode, Triton) for production.
    rpc_url: str = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")

    # ── Token Metadata ────────────────────────────────────────
    token_name: str        = os.getenv("TOKEN_NAME", "MyToken")
    token_symbol: str      = os.getenv("TOKEN_SYMBOL", "MTK")
    token_description: str = os.getenv("TOKEN_DESCRIPTION", "A token created by the bot")
    token_image_path: str  = os.getenv("TOKEN_IMAGE_PATH", "token_image.png")
    token_twitter: str     = os.getenv("TOKEN_TWITTER", "")
    token_telegram: str    = os.getenv("TOKEN_TELEGRAM", "")
    token_website: str     = os.getenv("TOKEN_WEBSITE", "")

    # ── Dev Buy ───────────────────────────────────────────────
    # SOL to spend on the bundled dev-buy at token creation.
    dev_buy_sol: float = float(os.getenv("DEV_BUY_SOL", "0.5"))

    # ── Sell ──────────────────────────────────────────────────
    # Seconds to wait after creation before selling.
    sell_delay_seconds: float = float(os.getenv("SELL_DELAY_SECONDS", "10"))
    # Percentage of token balance to sell (0-100).
    sell_percentage: float = float(os.getenv("SELL_PERCENTAGE", "100.0"))

    # ── Slippage ──────────────────────────────────────────────
    # Stored as basis points (100 bps = 1%).
    # Sent to the API as whole-number percent (2000 bps → 20).
    buy_slippage_bps: int  = int(os.getenv("BUY_SLIPPAGE_BPS", "2000"))
    sell_slippage_bps: int = int(os.getenv("SELL_SLIPPAGE_BPS", "2500"))

    # ── Priority Fee ──────────────────────────────────────────
    # Stored as lamports. Sent to pumpportal as SOL float (÷1e9).
    # Used for both trade txs (via API) and sweep tx (built locally).
    #   Low  : 50_000 – 100_000  lamports (quiet network)
    #   Mid  : 100_000 – 500_000 lamports (normal)
    #   High : 500_000+           lamports (busy / sniping)
    priority_fee_lamports: int = int(os.getenv("PRIORITY_FEE_LAMPORTS", "100000"))

    # ── Jito ──────────────────────────────────────────────────
    # Set > 0 and point RPC_URL at a Jito endpoint to enable bundles.
    jito_tip_lamports: int = int(os.getenv("JITO_TIP_LAMPORTS", "0"))

    # ── Transaction Settings ──────────────────────────────────
    tx_commitment: str          = os.getenv("TX_COMMITMENT", "confirmed")
    max_retries: int            = int(os.getenv("MAX_RETRIES", "3"))
    retry_delay_seconds: float  = float(os.getenv("RETRY_DELAY_SECONDS", "1.5"))
    confirm_timeout_seconds: int = int(os.getenv("CONFIRM_TIMEOUT_SECONDS", "60"))

    # ── Safety Guards ─────────────────────────────────────────
    max_sol_exposure: float = float(os.getenv("MAX_SOL_EXPOSURE", "2.0"))
    min_sol_balance: float  = float(os.getenv("MIN_SOL_BALANCE", "0.05"))
    # Set DRY_RUN=true to simulate without sending real transactions.
    dry_run: bool = os.getenv("DRY_RUN", "false").lower() == "true"

    # ── Profit Sweep ──────────────────────────────────────────
    # Base58 address to receive all SOL after the sell.
    # Leave blank to skip. Change between runs in .env.
    sweep_wallet: str = os.getenv("SWEEP_WALLET", "")
    # SOL kept in trading wallet after sweep (for future fees).
    sweep_reserve_lamports: int = int(os.getenv("SWEEP_RESERVE_LAMPORTS", "10000000"))
    # Priority fee for the sweep transfer (plain SOL send, low is fine).
    sweep_priority_fee_lamports: int = int(os.getenv("SWEEP_PRIORITY_FEE_LAMPORTS", "5000"))

    # ── Derived ───────────────────────────────────────────────
    @property
    def commitment(self):
        return {"processed": Processed, "confirmed": Confirmed, "finalized": Finalized}.get(
            self.tx_commitment, Confirmed
        )

    @property
    def priority_fee_sol(self) -> float:
        """Priority fee as SOL float for the pumpportal API."""
        return self.priority_fee_lamports / 1_000_000_000

    @property
    def buy_slippage_pct(self) -> int:
        """Buy slippage as whole-number percent for the pumpportal API."""
        return max(1, self.buy_slippage_bps // 100)

    @property
    def sell_slippage_pct(self) -> int:
        """Sell slippage as whole-number percent for the pumpportal API."""
        return max(1, self.sell_slippage_bps // 100)


# ─────────────────────────────────────────────────────────────
# Logging helpers
# ─────────────────────────────────────────────────────────────
def log_section(title: str):
    log.info(Fore.CYAN + f"\n{'═'*55}\n  {title}\n{'═'*55}")

def log_ok(msg: str):
    log.info(Fore.GREEN + f"✅ {msg}")

def log_warn(msg: str):
    log.warning(Fore.YELLOW + f"⚠️  {msg}")

def log_err(msg: str):
    log.error(Fore.RED + f"❌ {msg}")

def lamports_to_sol(lamps: int) -> float:
    return lamps / 1_000_000_000


# ─────────────────────────────────────────────────────────────
# SSL / requests session
# ─────────────────────────────────────────────────────────────
def _build_session() -> requests.Session:
    """
    Build a requests.Session that verifies SSL with certifi if available,
    falls back to system certs, and as a last resort disables verification
    with a clear warning.
    """
    s = requests.Session()
    try:
        import certifi
        s.verify = certifi.where()
    except ImportError:
        s.verify = True  # system certs

    # Quick connectivity test — catch SSL errors here cleanly
    try:
        s.get("https://pump.fun", timeout=5)
    except requests.exceptions.SSLError:
        log_warn(
            "SSL verification failed — disabling cert checks. "
            "Install certifi (`pip install certifi`) to fix this properly."
        )
        s.verify = False
    except Exception:
        pass  # network issue — will surface properly later
    return s


# ─────────────────────────────────────────────────────────────
# pump.fun / pumpportal API client
# ─────────────────────────────────────────────────────────────
# Official endpoint reference:
#   IPFS upload : POST https://pump.fun/api/ipfs           multipart/form-data
#   Create+buy  : POST https://pumpportal.fun/api/trade-local  form-encoded
#   Sell        : POST https://pumpportal.fun/api/trade-local  form-encoded
#
# Key field rules (confirmed from official Python examples):
#   • ALL trade-local calls use requests data={} (form-encoded), NOT json={}
#   • tokenMetadata must be a JSON *string* embedded in the form, not a dict
#   • slippage    = whole-number percent  (e.g. 20, not 2000 bps)
#   • priorityFee = SOL float             (e.g. 0.0001, not 100000 lamports)
#   • amount      = SOL float for buys, raw token int for sells
# ─────────────────────────────────────────────────────────────
PUMPPORTAL_API = "https://pumpportal.fun/api/trade-local"
PUMP_IPFS_URL  = "https://pump.fun/api/ipfs"


class PumpFunClient:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self._s = _build_session()

    def close(self):
        self._s.close()

    # ── helpers ───────────────────────────────────────────────
    def _post(self, url: str, **kwargs) -> requests.Response:
        """Thin wrapper — all kwargs forwarded to requests.post."""
        return self._s.post(url, timeout=30, **kwargs)

    # ── IPFS metadata upload ──────────────────────────────────
    def upload_metadata(self) -> dict:
        """
        Upload token image + metadata to pump.fun IPFS.
        Returns the full JSON response which contains `metadataUri`.
        """
        img = self.cfg.token_image_path
        if not os.path.exists(img):
            raise FileNotFoundError(f"Token image not found: {img}")

        with open(img, "rb") as fh:
            img_bytes = fh.read()

        form = {
            "name":        self.cfg.token_name,
            "symbol":      self.cfg.token_symbol,
            "description": self.cfg.token_description,
            "showName":    "true",
        }
        if self.cfg.token_twitter:  form["twitter"]  = self.cfg.token_twitter
        if self.cfg.token_telegram: form["telegram"] = self.cfg.token_telegram
        if self.cfg.token_website:  form["website"]  = self.cfg.token_website

        files = {"file": (os.path.basename(img), img_bytes, "image/png")}
        resp = self._post(PUMP_IPFS_URL, data=form, files=files)

        if resp.status_code != 200:
            raise RuntimeError(f"IPFS upload failed ({resp.status_code}): {resp.text[:300]}")

        data = resp.json()
        log_ok(f"Metadata uploaded → {data.get('metadataUri', 'N/A')}")
        return data

    # ── Create + dev-buy ──────────────────────────────────────
    def get_create_tx_bytes(
        self,
        creator_pubkey: str,
        mint_pubkey: str,
        metadata_uri: str,
    ) -> bytes:
        """
        Returns raw serialized VersionedTransaction bytes from pumpportal.
        The `tokenMetadata` field must be a JSON string (not a nested dict)
        because the entire payload is form-encoded.
        """
        token_metadata_str = json.dumps({
            "name":   self.cfg.token_name,
            "symbol": self.cfg.token_symbol,
            "uri":    metadata_uri,
        })

        payload = {
            "publicKey":        creator_pubkey,
            "action":           "create",
            "tokenMetadata":    token_metadata_str,   # JSON string in form field
            "mint":             mint_pubkey,
            "denominatedInSol": "true",
            "amount":           str(self.cfg.dev_buy_sol),
            "slippage":         str(self.cfg.buy_slippage_pct),
            "priorityFee":      str(self.cfg.priority_fee_sol),
            "pool":             "pump",
        }

        log.debug(f"Create payload → {payload}")
        resp = self._post(PUMPPORTAL_API, data=payload)

        if resp.status_code != 200:
            raise RuntimeError(f"Create tx failed ({resp.status_code}): {resp.text[:300]}")
        return resp.content

    # ── Sell ──────────────────────────────────────────────────
    def get_sell_tx_bytes(
        self,
        seller_pubkey: str,
        mint_pubkey: str,
        token_amount: int,
    ) -> bytes:
        payload = {
            "publicKey":        seller_pubkey,
            "action":           "sell",
            "mint":             mint_pubkey,
            "denominatedInSol": "false",
            "amount":           str(token_amount),
            "slippage":         str(self.cfg.sell_slippage_pct),
            "priorityFee":      str(self.cfg.priority_fee_sol),
            "pool":             "pump",
        }

        log.debug(f"Sell payload → {payload}")
        resp = self._post(PUMPPORTAL_API, data=payload)

        if resp.status_code != 200:
            raise RuntimeError(f"Sell tx failed ({resp.status_code}): {resp.text[:300]}")
        return resp.content


# ─────────────────────────────────────────────────────────────
# Solana transaction manager
# ─────────────────────────────────────────────────────────────
class SolanaManager:
    TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    ASSOC_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe8bSS")

    def __init__(self, cfg: BotConfig, wallet: Keypair):
        self.cfg    = cfg
        self.wallet = wallet
        self.rpc    = AsyncClient(cfg.rpc_url, commitment=cfg.commitment)

    async def close(self):
        await self.rpc.close()

    # ── balances ──────────────────────────────────────────────
    async def sol_balance(self, pk: Optional[Pubkey] = None) -> float:
        r = await self.rpc.get_balance(pk or self.wallet.pubkey())
        return lamports_to_sol(r.value)

    async def token_balance(self, mint: Pubkey) -> int:
        """Raw token balance (base units) from the wallet's ATA."""
        ata, _ = Pubkey.find_program_address(
            [bytes(self.wallet.pubkey()), bytes(self.TOKEN_PROGRAM_ID), bytes(mint)],
            self.ASSOC_TOKEN_PROGRAM_ID,
        )
        try:
            r = await self.rpc.get_token_account_balance(ata)
            return int(r.value.amount)
        except Exception:
            return 0

    # ── sign & send ───────────────────────────────────────────
    async def sign_and_send(self, tx_bytes: bytes, extra_signers: list[Keypair] = None) -> str:
        """
        The pumpportal API returns a tx that already has some accounts filled
        in. We must re-sign using VersionedTransaction(message, signers) —
        NOT .sign() which would wipe the existing partial signature state.
        """
        raw_tx  = VersionedTransaction.from_bytes(tx_bytes)
        signers = [self.wallet] + (extra_signers or [])
        signed  = VersionedTransaction(raw_tx.message, signers)

        if self.cfg.dry_run:
            log_warn("DRY RUN — tx not sent.")
            return "DRY_RUN_" + datetime.now().isoformat()

        opts = TxOpts(
            skip_preflight=False,
            preflight_commitment=self.cfg.commitment,
            max_retries=0,
        )
        resp = await self.rpc.send_raw_transaction(bytes(signed), opts=opts)
        return str(resp.value)

    async def confirm(self, sig: str) -> bool:
        """Poll RPC until confirmed or timeout."""
        deadline = time.time() + self.cfg.confirm_timeout_seconds
        log.info(f"⏳ Confirming {sig[:20]}…")
        while time.time() < deadline:
            try:
                r      = await self.rpc.get_signature_statuses([sig])
                result = r.value[0]
                if result is not None:
                    if result.err:
                        log_err(f"On-chain error: {result.err}")
                        return False
                    # confirmation_status is a ConfirmationStatus enum — compare .value
                    status = result.confirmation_status
                    if status is not None and status.value in ("confirmed", "finalized"):
                        log_ok(f"Confirmed: {sig}")
                        return True
            except Exception as e:
                log.debug(f"Poll error: {e}")
            await asyncio.sleep(1.5)
        log_err(f"Timed out waiting for: {sig}")
        return False

    async def send_with_retry(
        self, tx_bytes: bytes, label: str, extra_signers: list[Keypair] = None
    ) -> Optional[str]:
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                log.info(f"📡 [{label}] attempt {attempt}/{self.cfg.max_retries}")
                sig = await self.sign_and_send(tx_bytes, extra_signers)
                if await self.confirm(sig):
                    return sig
                log_warn(f"[{label}] attempt {attempt} not confirmed, retrying…")
            except Exception as e:
                log_err(f"[{label}] attempt {attempt} error: {e}")
                log.debug(traceback.format_exc())
            if attempt < self.cfg.max_retries:
                await asyncio.sleep(self.cfg.retry_delay_seconds)
        log_err(f"[{label}] all {self.cfg.max_retries} attempts failed")
        return None

    # ── sweep ─────────────────────────────────────────────────
    async def sweep(self, destination: str) -> Optional[str]:
        """Send all SOL minus reserve to the sweep wallet."""
        dest = Pubkey.from_string(destination)
        bal  = (await self.rpc.get_balance(self.wallet.pubkey())).value
        amt  = bal - self.cfg.sweep_reserve_lamports

        if amt <= 0:
            log_warn(f"Sweep skipped — balance ({bal}) ≤ reserve ({self.cfg.sweep_reserve_lamports})")
            return None

        log.info(
            f"💸 Sweeping {lamports_to_sol(amt):.6f} SOL → {destination}  "
            f"(balance {lamports_to_sol(bal):.6f}, reserve {lamports_to_sol(self.cfg.sweep_reserve_lamports):.6f})"
        )

        if self.cfg.dry_run:
            log_warn("DRY RUN — sweep not sent.")
            return "DRY_RUN_SWEEP_" + datetime.now().isoformat()

        try:
            bh  = (await self.rpc.get_latest_blockhash()).value.blockhash
            msg = MessageV0.try_compile(
                payer=self.wallet.pubkey(),
                instructions=[
                    set_compute_unit_price(self.cfg.sweep_priority_fee_lamports),
                    transfer(TransferParams(
                        from_pubkey=self.wallet.pubkey(),
                        to_pubkey=dest,
                        lamports=amt,
                    )),
                ],
                address_lookup_table_accounts=[],
                recent_blockhash=bh,
            )
            tx   = VersionedTransaction(msg, [self.wallet])
            opts = TxOpts(skip_preflight=False, preflight_commitment=self.cfg.commitment, max_retries=0)
            sig  = str((await self.rpc.send_raw_transaction(bytes(tx), opts=opts)).value)

            if await self.confirm(sig):
                log_ok(f"Sweep done → https://solscan.io/tx/{sig}")
                return sig
            log_err("Sweep tx did not confirm.")
        except Exception as e:
            log_err(f"Sweep error: {e}")
            log.debug(traceback.format_exc())
        return None


# ─────────────────────────────────────────────────────────────
# Bot orchestrator
# ─────────────────────────────────────────────────────────────
class PumpFunBot:
    def __init__(self, cfg: BotConfig):
        self.cfg    = cfg
        self.wallet = Keypair.from_base58_string(cfg.private_key)
        self.sol    = SolanaManager(cfg, self.wallet)
        self.api    = PumpFunClient(cfg)

    async def run(self):
        log_section("pump.fun Autonomous Trading Bot")
        log.info(f"Wallet  : {self.wallet.pubkey()}")
        log.info(f"RPC     : {self.cfg.rpc_url}")
        log.info(f"Dry run : {self.cfg.dry_run}")

        await self._preflight()

        # Fresh mint keypair for this token
        mint_kp  = Keypair()
        mint_pub = str(mint_kp.pubkey())
        log.info(f"🪙 Mint : {mint_pub}")

        try:
            # ── Step 1: Upload metadata ────────────────────────────────
            log_section("Step 1 — Upload Metadata to IPFS")
            meta     = await asyncio.to_thread(self.api.upload_metadata)
            meta_uri = meta.get("metadataUri")
            if not meta_uri:
                raise RuntimeError("No metadataUri in IPFS response.")

            # ── Step 2: Create token + dev-buy ─────────────────────────
            log_section("Step 2 — Create Token & Dev-Buy")
            log.info(
                f"  Token      : {self.cfg.token_name} ({self.cfg.token_symbol})\n"
                f"  Dev buy    : {self.cfg.dev_buy_sol} SOL\n"
                f"  Slippage   : {self.cfg.buy_slippage_pct}%\n"
                f"  Priority   : {self.cfg.priority_fee_sol:.8f} SOL\n"
                f"  Metadata   : {meta_uri}"
            )

            create_bytes = await asyncio.to_thread(
                self.api.get_create_tx_bytes,
                str(self.wallet.pubkey()),
                mint_pub,
                meta_uri,
            )

            create_sig = await self.sol.send_with_retry(
                create_bytes, "CREATE+BUY",
                extra_signers=[mint_kp],   # mint must co-sign its own creation
            )
            if not create_sig:
                raise RuntimeError("Token creation failed.")

            log_ok(
                f"Token live!\n"
                f"  TX   : https://solscan.io/tx/{create_sig}\n"
                f"  Mint : https://pump.fun/{mint_pub}"
            )

            # ── Step 3: Wait ───────────────────────────────────────────
            log_section(f"Step 3 — Waiting {self.cfg.sell_delay_seconds}s Before Sell")
            await asyncio.sleep(self.cfg.sell_delay_seconds)

            # ── Step 4: Sell ───────────────────────────────────────────
            log_section("Step 4 — Sell")
            bal = await self.sol.token_balance(mint_kp.pubkey())
            if bal == 0:
                log_warn("Token balance is 0 — nothing to sell.")
            else:
                to_sell = int(bal * self.cfg.sell_percentage / 100)
                log.info(f"  Balance : {bal}  |  Selling : {to_sell} ({self.cfg.sell_percentage}%)")

                sell_bytes = await asyncio.to_thread(
                    self.api.get_sell_tx_bytes,
                    str(self.wallet.pubkey()),
                    mint_pub,
                    to_sell,
                )
                sell_sig = await self.sol.send_with_retry(sell_bytes, "SELL")
                if sell_sig:
                    log_ok(f"Sold → https://solscan.io/tx/{sell_sig}")
                else:
                    log_err("Sell failed after all retries.")

            # ── Step 5: Sweep ──────────────────────────────────────────
            if self.cfg.sweep_wallet:
                log_section("Step 5 — Sweep Profits")
                log.info(f"  Destination : {self.cfg.sweep_wallet}")
                log.info(f"  Reserve     : {lamports_to_sol(self.cfg.sweep_reserve_lamports):.6f} SOL")
                sw = await self.sol.sweep(self.cfg.sweep_wallet)
                if not sw:
                    log_warn("Sweep incomplete — funds remain in trading wallet.")
            else:
                log_warn("SWEEP_WALLET not set — profits stay in trading wallet.")

        finally:
            # ── Summary ────────────────────────────────────────────────
            log_section("Session Summary")
            final = await self.sol.sol_balance()
            log.info(
                f"  Mint      : {mint_pub}\n"
                f"  Dev buy   : {self.cfg.dev_buy_sol} SOL\n"
                f"  Remaining : {final:.6f} SOL\n"
                f"  Sweep to  : {self.cfg.sweep_wallet or 'disabled'}"
            )
            await self.sol.close()
            self.api.close()

    # ── pre-flight ────────────────────────────────────────────
    async def _preflight(self):
        log_section("Pre-flight Checks")

        if not self.cfg.private_key:
            raise EnvironmentError("PRIVATE_KEY not set in .env")

        bal = await self.sol.sol_balance()
        log.info(f"  Balance : {bal:.4f} SOL")

        if bal < self.cfg.min_sol_balance:
            raise RuntimeError(f"Balance {bal:.4f} < minimum {self.cfg.min_sol_balance} SOL")

        needed = self.cfg.dev_buy_sol + 0.05
        if bal < needed:
            raise RuntimeError(f"Need ~{needed:.3f} SOL for dev-buy, have {bal:.4f}")

        if self.cfg.dev_buy_sol > self.cfg.max_sol_exposure:
            raise RuntimeError(
                f"dev_buy_sol {self.cfg.dev_buy_sol} > max_sol_exposure {self.cfg.max_sol_exposure}"
            )

        if self.cfg.sweep_wallet:
            try:
                Pubkey.from_string(self.cfg.sweep_wallet)
                log.info(f"  Sweep   : {self.cfg.sweep_wallet} ✓")
            except Exception:
                raise ValueError(f"SWEEP_WALLET is not a valid Solana address: {self.cfg.sweep_wallet}")
        else:
            log_warn("SWEEP_WALLET not set.")

        log_ok("Pre-flight passed.")


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────
async def main():
    cfg = BotConfig()
    bot = PumpFunBot(cfg)
    try:
        await bot.run()
    except KeyboardInterrupt:
        log_warn("Interrupted.")
    except Exception as e:
        log_err(f"Fatal error: {e}")
        log.debug(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
