"""Build ``getTransaction`` payloads with the exact structure the RPC returns.

Recorded mainnet transactions prove the decoder reads reality correctly - that is what
``tests/stream/`` is for. They are useless for testing a *sequence*: nobody recorded a
wallet that buys, then sells 30 %, then exits, in three transactions with amounts chosen to
make an assertion sharp.

So these builders produce payloads with the same shape the RPC produces - the same
instruction nesting, the same balance arrays, the same Pump.fun pattern where SOL moves
through a system transfer on a buy and through nothing at all on a sell - with the amounts
under the test's control. The decoder cannot tell them apart from the real thing, which is
the point.
"""

from __future__ import annotations

from typing import Any

from gundix_contracts.mints import WSOL_MINT

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"

CURVE = "GseMAnNDvntR5uFePZ51yZBXzNSn7GdFPkfHwfr6d77J"
CURVE_ATA = "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw"
WALLET_ATA = "9XDYTfQKwW8sHPqnFdUreMmtmffmkHVPGTNV2e3LKxNW"

DEFAULT_FEE = 5_000
STARTING_LAMPORTS = 10_000_000_000
ATA_RENT = 2_039_280


def pumpfun_buy(
    *,
    signature: str,
    wallet: str,
    mint: str,
    sol_in: int,
    tokens_out: int,
    slot: int,
    block_time: int,
    decimals: int = 6,
    wallet_tokens_before: int = 0,
    curve_tokens_before: int = 1_000_000_000_000_000,
) -> dict[str, Any]:
    """A buy: SOL leaves through a system transfer, tokens arrive through an SPL transfer."""
    keys = [wallet, WALLET_ATA, PUMP_PROGRAM, CURVE, CURVE_ATA, SPL_TOKEN_PROGRAM, SYSTEM_PROGRAM]
    pre_balances = [STARTING_LAMPORTS, ATA_RENT, 1, 1_000_000_000, ATA_RENT, 1, 1]
    post_balances = list(pre_balances)
    post_balances[0] = STARTING_LAMPORTS - sol_in - DEFAULT_FEE
    post_balances[3] = pre_balances[3] + sol_in

    return _envelope(
        signature=signature,
        slot=slot,
        block_time=block_time,
        keys=keys,
        pre_balances=pre_balances,
        post_balances=post_balances,
        inner=[
            _system_transfer(wallet, CURVE, sol_in),
            _token_transfer_checked(CURVE_ATA, WALLET_ATA, mint, tokens_out, decimals),
        ],
        pre_token=[
            _token_balance(1, mint, wallet, wallet_tokens_before, decimals),
            _token_balance(4, mint, CURVE, curve_tokens_before, decimals),
        ],
        post_token=[
            _token_balance(1, mint, wallet, wallet_tokens_before + tokens_out, decimals),
            _token_balance(4, mint, CURVE, curve_tokens_before - tokens_out, decimals),
        ],
    )


def pumpfun_sell(
    *,
    signature: str,
    wallet: str,
    mint: str,
    tokens_in: int,
    sol_out: int,
    slot: int,
    block_time: int,
    decimals: int = 6,
    wallet_tokens_before: int,
    curve_tokens_before: int = 1_000_000_000_000_000,
) -> dict[str, Any]:
    """A sell: tokens leave through an SPL transfer, SOL arrives with no instruction at all.

    This is the case that forces sphere accounting: the bonding curve credits lamports
    directly, so there is nothing in the instruction list to read.
    """
    keys = [wallet, WALLET_ATA, PUMP_PROGRAM, CURVE, CURVE_ATA, SPL_TOKEN_PROGRAM, SYSTEM_PROGRAM]
    pre_balances = [STARTING_LAMPORTS, ATA_RENT, 1, 1_000_000_000, ATA_RENT, 1, 1]
    post_balances = list(pre_balances)
    post_balances[0] = STARTING_LAMPORTS + sol_out - DEFAULT_FEE
    post_balances[3] = pre_balances[3] - sol_out

    return _envelope(
        signature=signature,
        slot=slot,
        block_time=block_time,
        keys=keys,
        pre_balances=pre_balances,
        post_balances=post_balances,
        inner=[_token_transfer_checked(WALLET_ATA, CURVE_ATA, mint, tokens_in, decimals)],
        pre_token=[
            _token_balance(1, mint, wallet, wallet_tokens_before, decimals),
            _token_balance(4, mint, CURVE, curve_tokens_before, decimals),
        ],
        post_token=[
            _token_balance(1, mint, wallet, wallet_tokens_before - tokens_in, decimals),
            _token_balance(4, mint, CURVE, curve_tokens_before + tokens_in, decimals),
        ],
    )


def plain_transfer(
    *,
    signature: str,
    wallet: str,
    mint: str,
    amount: int,
    slot: int,
    block_time: int,
    decimals: int = 6,
    wallet_tokens_before: int,
) -> dict[str, Any]:
    """An SPL transfer with no swap program involved. Never a SwapEvent (S0 rule 4)."""
    other = CURVE_ATA
    keys = [wallet, WALLET_ATA, SPL_TOKEN_PROGRAM, other]
    pre_balances = [STARTING_LAMPORTS, ATA_RENT, 1, ATA_RENT]
    post_balances = list(pre_balances)
    post_balances[0] = STARTING_LAMPORTS - DEFAULT_FEE

    payload = _envelope(
        signature=signature,
        slot=slot,
        block_time=block_time,
        keys=keys,
        pre_balances=pre_balances,
        post_balances=post_balances,
        inner=[],
        pre_token=[_token_balance(1, mint, wallet, wallet_tokens_before, decimals)],
        post_token=[_token_balance(1, mint, wallet, wallet_tokens_before - amount, decimals)],
    )
    # The transfer is the top-level instruction; there is no venue program anywhere.
    payload["transaction"]["message"]["instructions"] = [
        _compute_budget(),
        _token_transfer_checked(WALLET_ATA, other, mint, amount, decimals),
    ]
    payload["meta"]["innerInstructions"] = []
    return payload


def failed(payload: dict[str, Any], error: Any = None) -> dict[str, Any]:
    """Mark a transaction as having failed on chain, with no balance changes."""
    payload = {**payload, "meta": {**payload["meta"]}}
    payload["meta"]["err"] = error or {"InstructionError": [1, {"Custom": 6002}]}
    payload["meta"]["postBalances"] = list(payload["meta"]["preBalances"])
    payload["meta"]["postTokenBalances"] = payload["meta"]["preTokenBalances"]
    return payload


# -- pieces ------------------------------------------------------------------------------
def _envelope(
    *,
    signature: str,
    slot: int,
    block_time: int,
    keys: list[str],
    pre_balances: list[int],
    post_balances: list[int],
    inner: list[dict[str, Any]],
    pre_token: list[dict[str, Any]],
    post_token: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "slot": slot,
        "blockTime": block_time,
        "transaction": {
            "signatures": [signature],
            "message": {
                "accountKeys": [
                    {"pubkey": key, "signer": index == 0, "writable": True, "source": "transaction"}
                    for index, key in enumerate(keys)
                ],
                "instructions": [_compute_budget(), _pump_instruction()],
                "recentBlockhash": "11111111111111111111111111111111",
            },
        },
        "meta": {
            "err": None,
            "fee": DEFAULT_FEE,
            "preBalances": pre_balances,
            "postBalances": post_balances,
            "preTokenBalances": pre_token,
            "postTokenBalances": post_token,
            "innerInstructions": [{"index": 1, "instructions": inner}] if inner else [],
            "logMessages": [],
            "loadedAddresses": {"writable": [], "readonly": []},
        },
        "version": 0,
    }


def _compute_budget() -> dict[str, Any]:
    return {"programId": COMPUTE_BUDGET, "accounts": [], "data": "3gJqkocMWaMm"}


def _pump_instruction() -> dict[str, Any]:
    return {"programId": PUMP_PROGRAM, "accounts": [], "data": "2K7nL28PxCW8ejnyCeuMpbY"}


def _system_transfer(source: str, destination: str, lamports: int) -> dict[str, Any]:
    return {
        "programId": SYSTEM_PROGRAM,
        "program": "system",
        "parsed": {
            "type": "transfer",
            "info": {"source": source, "destination": destination, "lamports": lamports},
        },
    }


def _token_transfer_checked(
    source: str, destination: str, mint: str, amount: int, decimals: int
) -> dict[str, Any]:
    return {
        "programId": SPL_TOKEN_PROGRAM,
        "program": "spl-token",
        "parsed": {
            "type": "transferChecked",
            "info": {
                "source": source,
                "destination": destination,
                "mint": mint,
                "authority": CURVE,
                "tokenAmount": {
                    "amount": str(amount),
                    "decimals": decimals,
                    "uiAmountString": str(amount),
                },
            },
        },
    }


def _token_balance(
    account_index: int, mint: str, owner: str, amount: int, decimals: int
) -> dict[str, Any]:
    return {
        "accountIndex": account_index,
        "mint": mint,
        "owner": owner,
        "programId": SPL_TOKEN_PROGRAM,
        "uiTokenAmount": {
            "amount": str(amount),
            "decimals": decimals,
            "uiAmountString": str(amount),
        },
    }


__all__ = [
    "ATA_RENT",
    "DEFAULT_FEE",
    "PUMP_PROGRAM",
    "STARTING_LAMPORTS",
    "WSOL_MINT",
    "failed",
    "plain_transfer",
    "pumpfun_buy",
    "pumpfun_sell",
]
