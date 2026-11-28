"""Tests for rug-token dev / insider wallet labeling."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from dev_label_study import (  # noqa: E402
    LABEL_CREATOR,
    LABEL_EARLY_SELLER,
    LABEL_FUNDED_SNIPER,
    LABEL_INSIDER_RECIPIENT,
    LABEL_LAUNCHER,
    LABEL_POOL_SEEDER,
    _received_without_buy,
    find_creator,
    funded_recipients,
    label_mint,
)

MINT = "Mint1111111111111111111111111111111111111111"
VAULT = "Vault111111111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
LAUNCHER = "Launcher11111111111111111111111111111111111"
SEEDER = LAUNCHER
SNIPER = "Sniper1111111111111111111111111111111111111"
BUYER = "Buyer111111111111111111111111111111111111111"
INSIDER = "Insider1111111111111111111111111111111111111"
ENTRY_S = 1_700_000_000


def btransfer(source, dest, amount, mint=MINT):
    return {"fromUserAccount": source, "toUserAccount": dest, "mint": mint, "tokenAmount": amount}


def ntransfer(source, dest, amount):
    return {"fromUserAccount": source, "toUserAccount": dest, "amount": amount}


def tx(signature, timestamp, kind="SWAP", token=None, native=None, fee_payer=LAUNCHER):
    return {
        "signature": signature,
        "timestamp": timestamp,
        "type": kind,
        "feePayer": fee_payer,
        "tokenTransfers": token or [],
        "nativeTransfers": native or [],
        "accountData": [],
    }


def scenario():
    """Fresh AMM launch; launcher seeds pool, funds a sniper, insider dumps."""
    return [
        tx("mint", ENTRY_S - 20, kind="TOKEN_MINT",
           token=[btransfer(LAUNCHER, LAUNCHER, 1_000_000_000.0)], fee_payer=LAUNCHER),
        tx(
            "create", ENTRY_S - 18, kind="CREATE_POOL",
            token=[
                btransfer(SEEDER, VAULT, 1_000_000_000.0),
                ntransfer(SEEDER, VAULT, 200_000_000_000),  # 200 SOL seed
                btransfer(SEEDER, VAULT, 200.0, mint=WSOL),
            ],
            native=[
                ntransfer(SEEDER, VAULT, 200_000_000_000),
                ntransfer(LAUNCHER, SNIPER, 10_000_000),  # 0.01 SOL to sniper
            ],
            fee_payer=LAUNCHER,
        ),
        tx("insider-recv", ENTRY_S - 5, kind="TRANSFER",
           token=[btransfer(LAUNCHER, INSIDER, 50_000_000.0)], fee_payer=LAUNCHER),
        # a normal buyer pays SOL in and takes base out of the vault
        tx("buy", ENTRY_S + 1, kind="SWAP",
           token=[btransfer(VAULT, BUYER, 5_000_000.0)],
           native=[ntransfer(BUYER, VAULT, 50_000_000)], fee_payer=BUYER),
        # sniper sells early
        tx("sniper-sell", ENTRY_S + 3, kind="SWAP",
           token=[btransfer(SNIPER, VAULT, 10_000_000.0)], fee_payer=SNIPER),
        # insider sells early
        tx("insider-sell", ENTRY_S + 8, kind="SWAP",
           token=[btransfer(INSIDER, VAULT, 50_000_000.0)], fee_payer=INSIDER),
    ]


class LabelMintTests(unittest.TestCase):
    def _label(self, txs=None):
        return label_mint(
            txs if txs is not None else scenario(),
            mint=MINT, entry_s=ENTRY_S, rule="scanner_pumpfun_amm", day="2026-11-27",
            entry_ts_ms=ENTRY_S * 1000, return_pct=-0.98, pnl_sol=-0.02,
        )

    def test_creator_launcher_seeder(self) -> None:
        label = self._label()
        assert label is not None
        self.assertEqual(label.creator, LAUNCHER)
        self.assertEqual(label.launcher, LAUNCHER)
        self.assertEqual(label.pool_seeder, SEEDER)
        self.assertTrue(label.creation_in_window)
        wallets = {w.wallet: w for w in label.dev_wallets}
        self.assertIn(LABEL_CREATOR, wallets[LAUNCHER].labels)
        self.assertIn(LABEL_LAUNCHER, wallets[LAUNCHER].labels)
        self.assertIn(LABEL_POOL_SEEDER, wallets[LAUNCHER].labels)

    def test_funded_sniper_requires_funding_and_early_sell(self) -> None:
        label = self._label()
        assert label is not None
        wallets = {w.wallet: w for w in label.dev_wallets}
        self.assertIn(SNIPER, wallets)
        self.assertIn(LABEL_FUNDED_SNIPER, wallets[SNIPER].labels)
        self.assertEqual(wallets[SNIPER].confidence, "high")
        self.assertIn(SNIPER, label.funded_recipients)
        self.assertIn(SNIPER, label.early_sellers_15s)

    def test_normal_buyer_is_early_seller_only_not_dev(self) -> None:
        label = self._label()
        assert label is not None
        # buyer who buys then... here buyer doesn't sell; check it's not dev
        wallets = {w.wallet: w for w in label.dev_wallets}
        self.assertNotIn(BUYER, wallets)
        self.assertNotIn(BUYER, label.funded_recipients)
        self.assertNotIn(BUYER, label.insider_transfer_recipients)

    def test_insider_transfer_recipient(self) -> None:
        label = self._label()
        assert label is not None
        wallets = {w.wallet: w for w in label.dev_wallets}
        self.assertIn(INSIDER, wallets)
        self.assertIn(LABEL_INSIDER_RECIPIENT, wallets[INSIDER].labels)
        self.assertIn(INSIDER, label.insider_transfer_recipients)
        self.assertIn(INSIDER, label.early_sellers_60s)
        self.assertNotIn(LABEL_EARLY_SELLER, wallets.get(BUYER, type("X", (), {"labels": []})()).labels)

    def test_wsol_mint_is_ignored_for_base_token(self) -> None:
        label = self._label()
        assert label is not None
        self.assertEqual(label.pool_seeder, SEEDER)  # not the wrapped-SOL transfer

    def test_no_create_pool_returns_none(self) -> None:
        self.assertIsNone(self._label(txs=[tx("swap", ENTRY_S, token=[btransfer(VAULT, BUYER, 1.0)])]))

    def test_no_early_distribution_flag(self) -> None:
        txs = [
            tx("mint", ENTRY_S - 20, kind="TOKEN_MINT", token=[btransfer(LAUNCHER, LAUNCHER, 1e9)]),
            tx("create", ENTRY_S - 18, kind="CREATE_POOL",
               token=[btransfer(SEEDER, VAULT, 1e9)], fee_payer=LAUNCHER),
        ]
        label = self._label(txs=txs)
        assert label is not None
        self.assertTrue(label.no_early_distribution)
        self.assertEqual(label.early_sellers_15s, [])
        self.assertEqual(label.early_sellers_60s, [])

    def test_honesty_notes_present(self) -> None:
        label = self._label()
        assert label is not None
        joined = " ".join(label.notes)
        self.assertIn("wallet != person", joined)
        self.assertIn("not inferred", joined)


class FundingTests(unittest.TestCase):
    def test_funded_recipients_excludes_token_accounts_and_funder(self) -> None:
        txs = scenario()
        token_accounts = {VAULT}
        out = funded_recipients(
            txs, funder={LAUNCHER}, entry_s=ENTRY_S, create_s=ENTRY_S - 18,
            token_accounts=token_accounts, exclude={LAUNCHER, VAULT},
        )
        self.assertIn(SNIPER, out)
        self.assertNotIn(VAULT, out)
        self.assertNotIn(LAUNCHER, out)


class CreatorTests(unittest.TestCase):
    def test_token_mint_fee_payer_is_creator(self) -> None:
        creator, basis = find_creator(scenario(), MINT, LAUNCHER)
        self.assertEqual(creator, LAUNCHER)
        self.assertEqual(basis, "token_mint_fee_payer")

    def test_fallback_unknown(self) -> None:
        creator, basis = find_creator([], MINT, None)
        self.assertIsNone(creator)
        self.assertEqual(basis, "unknown")


class ReceivedWithoutBuyTests(unittest.TestCase):
    def test_transfer_recipient_counts_buyer_does_not(self) -> None:
        txs = scenario()
        self.assertTrue(_received_without_buy(txs, MINT, VAULT, INSIDER, ENTRY_S, "create"))
        self.assertFalse(_received_without_buy(txs, MINT, VAULT, BUYER, ENTRY_S, "create"))


if __name__ == "__main__":
    unittest.main()
