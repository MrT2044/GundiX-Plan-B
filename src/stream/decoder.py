"""Swap decoder: raw Solana transaction -> canonical ``SwapEvent``.

Implements the aggregation rules of 03_S0_INTEGRATIONSVERTRAG section 5. Two properties of
Solana shape the implementation and are worth stating up front:

**1. The two sides of a swap are not equally visible.**
The SPL-token side is always an explicit ``transfer`` / ``transferChecked`` instruction and
can be read exactly. The SOL side often is not: a Pump.fun bonding curve moves lamports
with ``sub_lamports``/``add_lamports`` inside the program, where no instruction exists to
read. Only the balances show it. The decoder therefore reads the token side from the
transfers and derives the SOL side from *sphere accounting* - the lamport change across the
wallet's own account plus its token accounts, corrected for the transaction fee, for rent
locked into freshly created token accounts, and for transfers outside the swap.
Verified against Pump.fun's own on-chain ``TradeEvent``: token amounts match exactly and
SOL matches to the lamport once the venue fee is accounted for
(``tests/stream/test_decoder_program_events.py``).

**2. Venue calls are routinely wrapped by third-party programs.**
A top-level instruction counts as a swap instruction when it *contains* a venue or router
program anywhere beneath it, not only when it is one.

Fail-closed behaviour (S0 rule 9): if any program in the swap path is unknown, the whole
transaction goes to quarantine for that wallet and **no** event is emitted. Half-truths are
more dangerous than gaps.

Input format: ``getTransaction`` with ``encoding="jsonParsed"`` and
``maxSupportedTransactionVersion=0``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from gundix_contracts.enums import (
    DecodeConfidence,
    EventSource,
    FeeAttribution,
    Finality,
    RouterLabel,
    Side,
    Venue,
)
from gundix_contracts.ids import compute_event_id
from gundix_contracts.mints import KNOWN_DECIMALS, WSOL_MINT, choose_quote_mint
from gundix_contracts.models import SCHEMA_VERSIONS, RouterInfo, SwapEvent, SwapFees
from gundix_contracts.types import instruction_path_key

from src.stream.programs import (
    ROUTER_PROGRAMS,
    SYSTEM_PROGRAM,
    is_known_program,
    is_token_program,
    venue_for,
)

DECODER_VERSION = "1.0.0"
DECODER_COMPONENT = "stream.decoder"

#: Out-of-protocol tip destinations. Deliberately empty: an unverified address list would
#: misclassify real swap payments as tips. Populate only with verified accounts.
TIP_ACCOUNTS: frozenset[str] = frozenset()


class DecodeError(ValueError):
    """The payload is not shaped like a Solana RPC transaction at all."""


class UnknownProgramPolicy(StrEnum):
    """How to treat an unrecognised program inside the swap path.

    ``QUARANTINE_TRANSACTION`` is the S0 rule 9 default and the only behaviour permitted
    without an approved CCR. ``RECONCILE_BALANCES`` is the alternative proposed in
    CCR-001; it is off by default, must be enabled explicitly, and every decoder run logs
    which policy was active so no result can be mistaken for the contractual one.
    """

    QUARANTINE_TRANSACTION = "QUARANTINE_TRANSACTION"
    RECONCILE_BALANCES = "RECONCILE_BALANCES"


class QuarantineReason:
    """Machine readable quarantine causes. Reported in the decoder coverage artifact."""

    UNKNOWN_PROGRAM_IN_PATH = "unknown_program_in_path"
    UNRESOLVED_PAIR = "unresolved_pair"
    NO_SOL_SIDE_VISIBLE = "no_sol_side_visible"
    BALANCE_MISMATCH = "balance_mismatch"
    NON_POSITIVE_AMOUNT = "non_positive_amount"


@dataclass(frozen=True, slots=True)
class TokenAccountInfo:
    mint: str
    owner: str | None
    decimals: int


@dataclass(frozen=True, slots=True)
class _Movement:
    path: str
    top_index: int
    mint: str
    amount: int
    decimals: int
    incoming: bool
    native: bool

    @property
    def signed(self) -> int:
        return self.amount if self.incoming else -self.amount


@dataclass(slots=True)
class _Group:
    """Everything found under one top-level swap instruction."""

    top_index: int
    venues: list[Venue] = field(default_factory=list)
    venue_program_calls: int = 0
    routers: dict[str, RouterLabel] = field(default_factory=dict)
    pools: set[str] = field(default_factory=set)
    movements: list[_Movement] = field(default_factory=list)


@dataclass(slots=True)
class _Candidate:
    """One reduced swap: a signed base and quote delta for one mint pair."""

    top_index: int
    base_mint: str
    quote_mint: str
    base_delta: int
    quote_delta: int
    venues: list[Venue]
    venue_program_calls: int
    routers: dict[str, RouterLabel]
    pools: set[str]
    sol_from_sphere: bool


@dataclass(frozen=True, slots=True)
class WalletDecode:
    wallet: str
    events: tuple[SwapEvent, ...]
    quarantine_reasons: tuple[str, ...]
    wash_same_tx: int
    had_movements_outside_swap: bool
    touched_swap: bool


@dataclass(frozen=True, slots=True)
class DecodeResult:
    signature: str
    slot: int
    block_time_utc: datetime | None
    success: bool
    fee_lamports: int
    events: tuple[SwapEvent, ...]
    unknown_program_ids: tuple[str, ...]
    involved_wallets: tuple[str, ...]
    quarantined_wallets: tuple[str, ...]
    quarantine_reasons: tuple[str, ...]
    wash_same_tx: int
    had_transfers_without_swap: bool
    touched_swap: bool

    @property
    def is_quarantined(self) -> bool:
        return bool(self.quarantined_wallets)


# --------------------------------------------------------------------------------------
# payload helpers
# --------------------------------------------------------------------------------------
def account_keys(transaction: Mapping[str, Any]) -> list[str]:
    message = transaction.get("transaction", {}).get("message", {})
    resolved: list[str] = []
    for entry in message.get("accountKeys") or []:
        if isinstance(entry, str):
            resolved.append(entry)
        elif isinstance(entry, Mapping):
            resolved.append(str(entry.get("pubkey")))
        else:
            raise DecodeError(f"unexpected accountKeys entry: {entry!r}")
    loaded = (transaction.get("meta") or {}).get("loadedAddresses") or {}
    for bucket in ("writable", "readonly"):
        for address in loaded.get(bucket) or []:
            if address not in resolved:
                resolved.append(address)
    return resolved


def token_accounts(
    transaction: Mapping[str, Any], keys: Sequence[str]
) -> dict[str, TokenAccountInfo]:
    info: dict[str, TokenAccountInfo] = {}
    meta = transaction.get("meta") or {}
    for bucket in ("preTokenBalances", "postTokenBalances"):
        for entry in meta.get(bucket) or []:
            index = entry.get("accountIndex")
            if index is None or index >= len(keys):
                continue
            amount = entry.get("uiTokenAmount") or {}
            if amount.get("decimals") is None:
                continue
            info[keys[index]] = TokenAccountInfo(
                mint=str(entry.get("mint")),
                owner=str(entry["owner"]) if entry.get("owner") else None,
                decimals=int(amount["decimals"]),
            )
    return info


def token_balance_deltas(transaction: Mapping[str, Any], wallet: str) -> dict[str, int]:
    """Net SPL-token balance change of ``wallet`` across the whole transaction."""
    meta = transaction.get("meta") or {}
    totals: dict[str, int] = {}
    for bucket, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for entry in meta.get(bucket) or []:
            if entry.get("owner") != wallet:
                continue
            amount = (entry.get("uiTokenAmount") or {}).get("amount")
            if amount is None:
                continue
            mint = str(entry.get("mint"))
            totals[mint] = totals.get(mint, 0) + sign * int(amount)
    return {mint: value for mint, value in totals.items() if value != 0}


def sol_sphere_flow(
    transaction: Mapping[str, Any],
    keys: Sequence[str],
    accounts: Mapping[str, TokenAccountInfo],
    wallet: str,
    *,
    outside_native_net: int = 0,
) -> int | None:
    """Net lamport flow caused by the swap itself. Positive = the wallet received SOL.

    * the wallet's own lamport change,
    * plus the lamport change of its token accounts - wrapped SOL lives there, and for
      other mints the change is pure rent, which is not swap value either way,
    * plus the transaction fee if the wallet paid it - transacting is not the swap,
    * minus transfers outside the swap instruction (tips, bot fees), which would otherwise
      distort the executed price.

    Returns ``None`` when the wallet does not appear in the balance arrays.
    """
    meta = transaction.get("meta") or {}
    pre = meta.get("preBalances")
    post = meta.get("postBalances")
    if not isinstance(pre, list) or not isinstance(post, list):
        return None
    try:
        wallet_index = keys.index(wallet)
    except ValueError:
        return None
    if wallet_index >= len(pre) or wallet_index >= len(post):
        return None

    flow = int(post[wallet_index]) - int(pre[wallet_index])
    for index, address in enumerate(keys):
        if index >= len(pre) or index >= len(post):
            continue
        account = accounts.get(address)
        if account is None or account.owner != wallet:
            continue
        flow += int(post[index]) - int(pre[index])

    if keys and keys[0] == wallet:
        flow += int(meta.get("fee", 0))
    return flow - outside_native_net


def iter_instructions(
    transaction: Mapping[str, Any],
) -> Iterable[tuple[str, Mapping[str, Any], int]]:
    """Yield ``(instruction_path, instruction, top_level_index)``.

    ``"<i>"`` for top-level instruction *i*, ``"<i>.<j>"`` for the *j*-th entry of its
    ``innerInstructions`` list in RPC order.
    """
    message = transaction.get("transaction", {}).get("message", {})
    inner_by_index: dict[int, list[Mapping[str, Any]]] = {}
    for group in (transaction.get("meta") or {}).get("innerInstructions") or []:
        inner_by_index[int(group.get("index", -1))] = list(group.get("instructions") or [])
    for i, instruction in enumerate(message.get("instructions") or []):
        yield str(i), instruction, i
        for j, inner in enumerate(inner_by_index.get(i, [])):
            yield f"{i}.{j}", inner, i


def program_id_of(instruction: Mapping[str, Any]) -> str:
    return str(instruction.get("programId") or instruction.get("program") or "")


def _parsed_movement(
    instruction: Mapping[str, Any],
    accounts: Mapping[str, TokenAccountInfo],
    wallet: str,
) -> tuple[str, int, int, bool, bool] | None:
    """``(mint, amount, decimals, incoming, native)`` if this instruction moves the wallet's value."""
    program_id = program_id_of(instruction)
    parsed = instruction.get("parsed")
    if not isinstance(parsed, Mapping):
        return None
    kind = parsed.get("type")
    info = parsed.get("info")
    if not isinstance(info, Mapping):
        return None

    if program_id == SYSTEM_PROGRAM and kind == "transfer":
        lamports = info.get("lamports")
        if lamports is None:
            return None
        if info.get("source") == wallet:
            return WSOL_MINT, int(lamports), KNOWN_DECIMALS[WSOL_MINT], False, True
        if info.get("destination") == wallet:
            return WSOL_MINT, int(lamports), KNOWN_DECIMALS[WSOL_MINT], True, True
        return None

    if is_token_program(program_id) and kind in {"transfer", "transferChecked"}:
        source_info = accounts.get(str(info.get("source", "")))
        dest_info = accounts.get(str(info.get("destination", "")))
        if kind == "transferChecked":
            token_amount = info.get("tokenAmount") or {}
            amount = token_amount.get("amount")
            decimals = token_amount.get("decimals")
            mint = info.get("mint")
        else:
            amount = info.get("amount")
            resolved = source_info or dest_info
            mint = resolved.mint if resolved else None
            decimals = resolved.decimals if resolved else None
        if amount is None or mint is None or decimals is None:
            return None
        if source_info is not None and source_info.owner == wallet:
            return str(mint), int(amount), int(decimals), False, False
        if dest_info is not None and dest_info.owner == wallet:
            return str(mint), int(amount), int(decimals), True, False
    return None


def _counterparty_owners(
    instruction: Mapping[str, Any],
    accounts: Mapping[str, TokenAccountInfo],
    wallet: str,
) -> set[str]:
    parsed = instruction.get("parsed")
    if not isinstance(parsed, Mapping):
        return set()
    info = parsed.get("info")
    if not isinstance(info, Mapping):
        return set()
    owners: set[str] = set()
    for key in ("source", "destination"):
        account = info.get(key)
        if not isinstance(account, str):
            continue
        account_info = accounts.get(account)
        if account_info is not None and account_info.owner and account_info.owner != wallet:
            owners.add(account_info.owner)
    return owners


def swap_top_indices(transaction: Mapping[str, Any]) -> set[int]:
    """Top-level instructions containing a venue or router program anywhere beneath."""
    tops: set[int] = set()
    for _, instruction, top_index in iter_instructions(transaction):
        program_id = program_id_of(instruction)
        if venue_for(program_id) is not None or program_id in ROUTER_PROGRAMS:
            tops.add(top_index)
    return tops


def unknown_programs_in_swap_path(
    transaction: Mapping[str, Any], swap_tops: set[int]
) -> tuple[str, ...]:
    """S0 rule 9: any unrecognised program under a swap instruction quarantines the wallet."""
    unknown: list[str] = []
    for _, instruction, top_index in iter_instructions(transaction):
        if top_index not in swap_tops:
            continue
        program_id = program_id_of(instruction)
        if program_id and not is_known_program(program_id) and program_id not in unknown:
            unknown.append(program_id)
    return tuple(unknown)


def all_unknown_programs(transaction: Mapping[str, Any]) -> tuple[str, ...]:
    unknown: list[str] = []
    for _, instruction, _ in iter_instructions(transaction):
        program_id = program_id_of(instruction)
        if program_id and not is_known_program(program_id) and program_id not in unknown:
            unknown.append(program_id)
    return tuple(unknown)


# --------------------------------------------------------------------------------------
# decoder
# --------------------------------------------------------------------------------------
class SwapDecoder:
    """Turns raw ``getTransaction`` payloads into canonical ``SwapEvent``s."""

    def __init__(
        self,
        *,
        source: EventSource = EventSource.LIVE_STREAM,
        source_provider: str = "public_rpc",
        unknown_program_policy: UnknownProgramPolicy = UnknownProgramPolicy.QUARANTINE_TRANSACTION,
    ) -> None:
        self.source = source
        self.source_provider = source_provider
        self.unknown_program_policy = unknown_program_policy

    def decode(
        self,
        transaction: Mapping[str, Any],
        *,
        wallets: Iterable[str],
        observed_at_utc: datetime,
        finality: Finality = Finality.CONFIRMED,
        signature: str | None = None,
    ) -> DecodeResult:
        if not isinstance(transaction, Mapping):
            raise DecodeError("transaction payload must be a mapping")
        meta = transaction.get("meta")
        if meta is None:
            raise DecodeError("transaction has no meta; cannot decode without balances")

        signatures = transaction.get("transaction", {}).get("signatures") or []
        resolved_signature = signature or (signatures[0] if signatures else None)
        if not resolved_signature:
            raise DecodeError("transaction has no signature")
        resolved_signature = str(resolved_signature)

        slot = int(transaction.get("slot", 0))
        block_time_raw = transaction.get("blockTime")
        block_time = (
            datetime.fromtimestamp(int(block_time_raw), tz=UTC)
            if block_time_raw is not None
            else None
        )
        success = meta.get("err") is None
        fee_lamports = int(meta.get("fee", 0))

        keys = account_keys(transaction)
        accounts = token_accounts(transaction, keys)
        swap_tops = swap_top_indices(transaction)
        unknown_all = all_unknown_programs(transaction)
        unknown_in_path = unknown_programs_in_swap_path(transaction, swap_tops)
        wallet_list = list(dict.fromkeys(wallets))

        # A failed transaction executed nothing: its balances never changed, so any amount
        # derived from its instructions would be fiction. S0 4.1.7 keeps failures
        # countable, which the pipeline does from the raw event - no SwapEvent is emitted.
        if not success:
            return DecodeResult(
                signature=resolved_signature,
                slot=slot,
                block_time_utc=block_time,
                success=False,
                fee_lamports=fee_lamports,
                events=(),
                unknown_program_ids=unknown_all,
                involved_wallets=tuple(w for w in wallet_list if w in keys),
                quarantined_wallets=(),
                quarantine_reasons=(),
                wash_same_tx=0,
                had_transfers_without_swap=False,
                touched_swap=bool(swap_tops),
            )

        events: list[SwapEvent] = []
        involved: list[str] = []
        quarantined: list[str] = []
        reasons: list[str] = []
        wash_total = 0
        outside_seen = False

        for wallet in wallet_list:
            decoded = self._decode_wallet(
                transaction=transaction,
                keys=keys,
                accounts=accounts,
                swap_tops=swap_tops,
                unknown_in_path=unknown_in_path,
                wallet=wallet,
                signature=resolved_signature,
                slot=slot,
                block_time=block_time,
                fee_lamports=fee_lamports,
                observed_at_utc=observed_at_utc,
                finality=finality,
            )
            outside_seen = outside_seen or decoded.had_movements_outside_swap
            wash_total += decoded.wash_same_tx
            if decoded.events or decoded.quarantine_reasons or decoded.touched_swap:
                involved.append(wallet)
            if decoded.quarantine_reasons:
                quarantined.append(wallet)
                reasons.extend(decoded.quarantine_reasons)
            events.extend(decoded.events)

        return DecodeResult(
            signature=resolved_signature,
            slot=slot,
            block_time_utc=block_time,
            success=True,
            fee_lamports=fee_lamports,
            events=tuple(events),
            unknown_program_ids=unknown_all,
            involved_wallets=tuple(involved),
            quarantined_wallets=tuple(quarantined),
            quarantine_reasons=tuple(reasons),
            wash_same_tx=wash_total,
            had_transfers_without_swap=outside_seen,
            touched_swap=bool(swap_tops),
        )

    # -- internals ---------------------------------------------------------------------
    def _collect(
        self,
        transaction: Mapping[str, Any],
        accounts: Mapping[str, TokenAccountInfo],
        swap_tops: set[int],
        wallet: str,
    ) -> tuple[list[_Group], dict[str, int], int, bool, dict[str, int]]:
        groups: dict[int, _Group] = {}
        outside_token_net: dict[str, int] = {}
        outside_native_net = 0
        outside_seen = False
        decimals_by_mint: dict[str, int] = dict(KNOWN_DECIMALS)

        for path, instruction, top_index in iter_instructions(transaction):
            program_id = program_id_of(instruction)
            if top_index in swap_tops:
                group = groups.setdefault(top_index, _Group(top_index=top_index))
                venue = venue_for(program_id)
                if venue is not None:
                    group.venues.append(venue)
                    group.venue_program_calls += 1
                if program_id in ROUTER_PROGRAMS:
                    group.routers[program_id] = ROUTER_PROGRAMS[program_id]

            found = _parsed_movement(instruction, accounts, wallet)
            if found is None:
                continue
            mint, amount, decimals, incoming, native = found
            decimals_by_mint.setdefault(mint, decimals)
            if amount == 0:
                continue

            signed = amount if incoming else -amount
            if top_index not in swap_tops:
                outside_seen = True
                if native:
                    outside_native_net += signed
                else:
                    outside_token_net[mint] = outside_token_net.get(mint, 0) + signed
                continue

            group = groups.setdefault(top_index, _Group(top_index=top_index))
            group.movements.append(
                _Movement(
                    path=path,
                    top_index=top_index,
                    mint=mint,
                    amount=amount,
                    decimals=decimals,
                    incoming=incoming,
                    native=native,
                )
            )
            group.pools |= _counterparty_owners(instruction, accounts, wallet)

        populated = sorted(
            (group for group in groups.values() if group.movements), key=lambda g: g.top_index
        )
        return populated, outside_token_net, outside_native_net, outside_seen, decimals_by_mint

    def _decode_wallet(
        self,
        *,
        transaction: Mapping[str, Any],
        keys: Sequence[str],
        accounts: Mapping[str, TokenAccountInfo],
        swap_tops: set[int],
        unknown_in_path: Sequence[str],
        wallet: str,
        signature: str,
        slot: int,
        block_time: datetime | None,
        fee_lamports: int,
        observed_at_utc: datetime,
        finality: Finality,
    ) -> WalletDecode:
        groups, outside_token_net, outside_native_net, outside_seen, decimals_by_mint = (
            self._collect(transaction, accounts, swap_tops, wallet)
        )
        if not groups:
            return WalletDecode(
                wallet=wallet,
                events=(),
                quarantine_reasons=(),
                wash_same_tx=0,
                had_movements_outside_swap=outside_seen,
                touched_swap=False,
            )

        # S0 rule 9, applied before anything else: an unknown program in the swap path
        # puts the whole transaction for this wallet into quarantine. No partial event.
        if (
            unknown_in_path
            and self.unknown_program_policy is UnknownProgramPolicy.QUARANTINE_TRANSACTION
        ):
            return WalletDecode(
                wallet=wallet,
                events=(),
                quarantine_reasons=(
                    f"{QuarantineReason.UNKNOWN_PROGRAM_IN_PATH}:{','.join(unknown_in_path)}",
                ),
                wash_same_tx=0,
                had_movements_outside_swap=outside_seen,
                touched_swap=True,
            )

        sphere = sol_sphere_flow(
            transaction, keys, accounts, wallet, outside_native_net=outside_native_net
        )
        single_group = len(groups) == 1

        candidates: list[_Candidate] = []
        reasons: list[str] = []
        for group in groups:
            candidate, failure = self._reduce_group(group, sphere=sphere if single_group else None)
            if candidate is None:
                reasons.append(f"{failure}:{signature}:{wallet}:{group.top_index}")
                continue
            candidates.append(candidate)

        if reasons:
            # Half-truths are more dangerous than gaps: if one swap in the transaction
            # cannot be described, nothing from this transaction is published.
            return WalletDecode(
                wallet=wallet,
                events=(),
                quarantine_reasons=tuple(reasons),
                wash_same_tx=0,
                had_movements_outside_swap=outside_seen,
                touched_swap=True,
            )

        merged, wash_count = self._merge_pairs(candidates)

        # S0 rule 6: several mint pairs produce several events, ordered by the outermost
        # instruction path.
        merged.sort(key=lambda c: instruction_path_key(str(c.top_index)))

        observed_delta = token_balance_deltas(transaction, wallet)
        events: list[SwapEvent] = []
        for net_swap_index, candidate in enumerate(merged):
            reconciles = self._base_reconciles(
                candidate=candidate,
                outside_token_net=outside_token_net,
                observed_delta=observed_delta,
                single_pair=len(merged) == 1,
            )
            if unknown_in_path and not reconciles:
                # CCR-001 policy: an unknown program is tolerated only while the wallet's
                # real token balance change proves it moved nothing we failed to attribute.
                return WalletDecode(
                    wallet=wallet,
                    events=(),
                    quarantine_reasons=(
                        f"{QuarantineReason.UNKNOWN_PROGRAM_IN_PATH}:{','.join(unknown_in_path)}",
                    ),
                    wash_same_tx=wash_count,
                    had_movements_outside_swap=outside_seen,
                    touched_swap=True,
                )
            if not reconciles:
                return WalletDecode(
                    wallet=wallet,
                    events=(),
                    quarantine_reasons=(
                        f"{QuarantineReason.BALANCE_MISMATCH}:{signature}:{wallet}:{candidate.base_mint}",
                    ),
                    wash_same_tx=wash_count,
                    had_movements_outside_swap=outside_seen,
                    touched_swap=True,
                )
            events.append(
                self._build_event(
                    candidate=candidate,
                    net_swap_index=net_swap_index,
                    wallet=wallet,
                    signature=signature,
                    slot=slot,
                    block_time=block_time,
                    fee_lamports=fee_lamports,
                    decimals_by_mint=decimals_by_mint,
                    observed_at_utc=observed_at_utc,
                    finality=finality,
                )
            )

        return WalletDecode(
            wallet=wallet,
            events=tuple(events),
            quarantine_reasons=(),
            wash_same_tx=wash_count,
            had_movements_outside_swap=outside_seen,
            touched_swap=True,
        )

    @staticmethod
    def _reduce_group(group: _Group, *, sphere: int | None) -> tuple[_Candidate | None, str]:
        """Reduce one top-level swap instruction to a single mint pair.

        Intermediate mints of a multi-hop route net to zero here and disappear on their
        own, which is exactly S0 rule 2.
        """
        token_net: dict[str, int] = {}
        wsol_leg_net = 0
        for movement in group.movements:
            if movement.native or movement.mint == WSOL_MINT:
                wsol_leg_net += movement.signed
            else:
                token_net[movement.mint] = token_net.get(movement.mint, 0) + movement.signed
        token_net = {mint: value for mint, value in token_net.items() if value != 0}

        def make(
            base: str, base_delta: int, quote: str, quote_delta: int, from_sphere: bool
        ) -> _Candidate:
            return _Candidate(
                top_index=group.top_index,
                base_mint=base,
                quote_mint=quote,
                base_delta=base_delta,
                quote_delta=quote_delta,
                venues=list(group.venues),
                venue_program_calls=group.venue_program_calls,
                routers=dict(group.routers),
                pools=set(group.pools),
                sol_from_sphere=from_sphere,
            )

        if len(token_net) == 2:
            mints = list(token_net)
            if sum(1 for value in token_net.values() if value > 0) != 1:
                return None, QuarantineReason.UNRESOLVED_PAIR
            quote_mint = choose_quote_mint(mints[0], mints[1])
            base_mint = mints[0] if quote_mint == mints[1] else mints[1]
            return make(
                base_mint, token_net[base_mint], quote_mint, token_net[quote_mint], False
            ), ""

        if len(token_net) == 1:
            base_mint = next(iter(token_net))
            base_delta = token_net[base_mint]
            # Prefer sphere accounting: it also sees SOL a bonding curve moved without an
            # instruction, and it excludes fees and rent.
            if sphere is not None and sphere != 0:
                if (sphere > 0) == (base_delta > 0):
                    return None, QuarantineReason.UNRESOLVED_PAIR
                return make(base_mint, base_delta, WSOL_MINT, sphere, True), ""
            if wsol_leg_net != 0 and (wsol_leg_net > 0) != (base_delta > 0):
                return make(base_mint, base_delta, WSOL_MINT, wsol_leg_net, False), ""
            return None, QuarantineReason.NO_SOL_SIDE_VISIBLE

        return None, QuarantineReason.UNRESOLVED_PAIR

    @staticmethod
    def _merge_pairs(candidates: Sequence[_Candidate]) -> tuple[list[_Candidate], int]:
        """S0 rules 1 and 3: net per (base_mint, quote_mint); a zero net is a wash trade."""
        merged: dict[tuple[str, str], _Candidate] = {}
        for candidate in candidates:
            key = (candidate.base_mint, candidate.quote_mint)
            existing = merged.get(key)
            if existing is None:
                merged[key] = candidate
                continue
            existing.base_delta += candidate.base_delta
            existing.quote_delta += candidate.quote_delta
            existing.venues.extend(candidate.venues)
            existing.venue_program_calls += candidate.venue_program_calls
            existing.routers.update(candidate.routers)
            existing.pools |= candidate.pools
            existing.top_index = min(existing.top_index, candidate.top_index)
            existing.sol_from_sphere = existing.sol_from_sphere or candidate.sol_from_sphere

        kept: list[_Candidate] = []
        wash = 0
        for candidate in merged.values():
            if candidate.base_delta == 0:
                wash += 1
                continue
            kept.append(candidate)
        return kept, wash

    @staticmethod
    def _base_reconciles(
        *,
        candidate: _Candidate,
        outside_token_net: Mapping[str, int],
        observed_delta: Mapping[str, int],
        single_pair: bool,
    ) -> bool:
        """Does the attributed base movement match the wallet's real balance change?

        Skipped when the transaction holds several mint pairs, because the whole-transaction
        delta cannot be split between them. Wrapped SOL is skipped too: ``syncNative``
        changes a WSOL balance without any transfer instruction to observe.
        """
        if not single_pair or candidate.base_mint == WSOL_MINT:
            return True
        expected = candidate.base_delta + outside_token_net.get(candidate.base_mint, 0)
        return observed_delta.get(candidate.base_mint, 0) == expected

    def _build_event(
        self,
        *,
        candidate: _Candidate,
        net_swap_index: int,
        wallet: str,
        signature: str,
        slot: int,
        block_time: datetime | None,
        fee_lamports: int,
        decimals_by_mint: Mapping[str, int],
        observed_at_utc: datetime,
        finality: Finality,
    ) -> SwapEvent:
        distinct_venues = {venue for venue in candidate.venues}
        if not distinct_venues:
            venue = Venue.UNKNOWN
        elif len(distinct_venues) == 1:
            venue = next(iter(distinct_venues))
        else:
            # The S0 venue enum has no value for a route that crossed several venues.
            # UNKNOWN is the honest answer and keeps the event out of economic use.
            # See CCR-003.
            venue = Venue.UNKNOWN

        pool = next(iter(candidate.pools)) if len(candidate.pools) == 1 else None
        router = None
        if candidate.routers:
            program_id = sorted(candidate.routers)[0]
            router = RouterInfo(
                program_id=program_id,
                label=candidate.routers[program_id],
                hops=max(1, candidate.venue_program_calls),
            )

        side = Side.BUY if candidate.base_delta > 0 else Side.SELL
        # S0 4.1.8: transaction-wide fees sit entirely on net_swap_index 0.
        if net_swap_index == 0:
            fees = SwapFees(
                network_fee_lamports=fee_lamports,
                priority_fee_lamports=0,
                tip_lamports=0,
                fee_attribution=FeeAttribution.FULL,
            )
        else:
            fees = SwapFees(fee_attribution=FeeAttribution.NONE)

        # The SOL side derived from balance accounting is exact, but it is not read from an
        # instruction. It stays COMPLETE only because the token side reconciles against the
        # wallet's real balance change; without a known venue it can never be COMPLETE.
        confidence = (
            DecodeConfidence.COMPLETE if venue is not Venue.UNKNOWN else DecodeConfidence.UNKNOWN
        )

        instruction_path = str(candidate.top_index)
        return SwapEvent(
            schema_version=SCHEMA_VERSIONS["swap_event"],
            event_id=compute_event_id(
                "solana", signature, wallet, instruction_path, net_swap_index
            ),
            signature=signature,
            slot=slot,
            block_time_utc=block_time or observed_at_utc,
            transaction_index=None,
            instruction_path=instruction_path,
            net_swap_index=net_swap_index,
            wallet=wallet,
            base_mint=candidate.base_mint,
            quote_mint=candidate.quote_mint,
            side=side,
            base_amount_raw=abs(candidate.base_delta),
            quote_amount_raw=abs(candidate.quote_delta),
            base_decimals=decimals_by_mint.get(candidate.base_mint, 0),
            quote_decimals=decimals_by_mint.get(candidate.quote_mint, 0),
            venue=venue,
            pool=pool,
            router=router,
            success=True,
            source=self.source,
            source_provider=self.source_provider,
            observed_at_utc=observed_at_utc,
            finality=finality,
            fees=fees,
            decode_confidence=confidence,
        )


__all__ = [
    "DECODER_COMPONENT",
    "DECODER_VERSION",
    "TIP_ACCOUNTS",
    "DecodeError",
    "DecodeResult",
    "QuarantineReason",
    "SwapDecoder",
    "UnknownProgramPolicy",
    "WalletDecode",
    "account_keys",
    "sol_sphere_flow",
    "swap_top_indices",
    "token_accounts",
    "token_balance_deltas",
]
