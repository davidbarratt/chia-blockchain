import logging
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from chia.protocols.wallet_protocol import CoinState
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.spend_bundle import SpendBundle
from chia.util.db_wrapper import DBWrapper
from chia.util.hash import std_hash
from chia.util.ints import uint32, uint64
from chia.wallet.cc_wallet.cc_wallet import CCWallet
from chia.wallet.payment import Payment
from chia.wallet.trade_record import TradeRecord
from chia.wallet.trading.offer import Offer, NotarizedPayment
from chia.wallet.trading.trade_status import TradeStatus
from chia.wallet.trading.trade_store import TradeStore
from chia.wallet.transaction_record import TransactionRecord
from chia.wallet.util.transaction_type import TransactionType
from chia.wallet.util.wallet_types import WalletType
from chia.wallet.wallet import Wallet
from chia.wallet.wallet_coin_record import WalletCoinRecord


class TradeManager:
    wallet_state_manager: Any
    log: logging.Logger
    trade_store: TradeStore

    @staticmethod
    async def create(
        wallet_state_manager: Any,
        db_wrapper: DBWrapper,
        name: str = None,
    ):
        self = TradeManager()
        if name:
            self.log = logging.getLogger(name)
        else:
            self.log = logging.getLogger(__name__)

        self.wallet_state_manager = wallet_state_manager
        self.trade_store = await TradeStore.create(db_wrapper)
        return self

    async def get_offers_with_status(self, status: TradeStatus) -> List[TradeRecord]:
        records = await self.trade_store.get_trade_record_with_status(status)
        return records

    async def get_coins_of_interest(
        self,
    ) -> Dict[bytes32, Coin]:
        """
        Returns list of coins we want to check if they are included in filter,
        These will include coins that belong to us and coins that that on other side of treade
        """
        all_pending = []
        pending_accept = await self.get_offers_with_status(TradeStatus.PENDING_ACCEPT)
        pending_confirm = await self.get_offers_with_status(TradeStatus.PENDING_CONFIRM)
        pending_cancel = await self.get_offers_with_status(TradeStatus.PENDING_CANCEL)
        all_pending.extend(pending_accept)
        all_pending.extend(pending_confirm)
        all_pending.extend(pending_cancel)
        interested_dict = {}

        for trade in all_pending:
            for coin in trade.coins_of_interest:
                interested_dict[coin.name()] = coin

        return interested_dict

    async def get_trade_by_coin(self, coin: Coin) -> Optional[TradeRecord]:
        all_trades = await self.get_all_trades()
        for trade in all_trades:
            if trade.status == TradeStatus.CANCELLED.value:
                continue
            if coin in trade.coins_of_interest:
                return trade
        return None

    async def coins_of_interest_farmed(self, coin_state: CoinState):
        """
        If both our coins and other coins in trade got removed that means that trade was successfully executed
        If coins from other side of trade got farmed without ours, that means that trade failed because either someone
        else completed trade or other side of trade canceled the trade by doing a spend.
        If our coins got farmed but coins from other side didn't, we successfully canceled trade by spending inputs.
        """
        trade = await self.get_trade_by_coin(coin_state.coin)
        if trade is None:
            self.log.error(f"Coin: {coin_state.coin}, not in any trade")
            return
        if coin_state.spent_height is None:
            self.log.error(f"Coin: {coin_state.coin}, has not been spent so trade can remain valid")

        # Then let's filter the offer into coins that WE offered
        offer = Offer.from_bytes(trade.offer)
        primary_coin_ids = [c.name() for c in offer.get_primary_coins()]
        our_coin_records: List[WalletCoinRecord] = await self.wallet_state_manager.coin_store.get_multiple_coin_records(
            primary_coin_ids
        )
        our_primary_coins: List[bytes32] = [cr.coin.name() for cr in our_coin_records]
        all_settlement_payments: List[Coin] = [c for coins in offer.get_offered_coins().values() for c in coins]
        our_settlement_payments: List[Coin] = list(filter(lambda c: c.parent_coin_info in our_primary_coins, all_settlement_payments))
        our_settlement_ids: List[bytes32] = [c.name() for c in our_settlement_payments]

        # And get all relevant coin states
        coin_states = await self.wallet_state_manager.get_coin_state(our_settlement_ids)
        assert coin_states is not None
        coin_state_names: List[bytes32] = [cs.coin.name() for cs in coin_states]

        # If any of our settlement_payments were spent, this offer was a success!
        if set(our_settlement_ids) & set(coin_state_names):
            height = coin_states[0].spent_height
            await self.maybe_create_wallets_for_offer(offer)
            await self.trade_store.set_status(trade.trade_id, TradeStatus.CONFIRMED, True, height)
            self.log.info(f"Trade with id: {trade.trade_id} confirmed at height: {height}")
        else:
            # In any other scenario this trade failed
            if trade.status == TradeStatus.PENDING_CANCEL.value:
                await self.trade_store.set_status(trade.trade_id, TradeStatus.CANCELLED, True)
                self.log.info(f"Trade with id: {trade.trade_id} canceled")
            elif trade.status == TradeStatus.PENDING_CONFIRM.value:
                await self.trade_store.set_status(trade.trade_id, TradeStatus.FAILED, True)
                self.log.warning(f"Trade with id: {trade.trade_id} failed")

    async def get_locked_coins(self, wallet_id: int = None) -> Dict[bytes32, WalletCoinRecord]:
        """Returns a dictionary of confirmed coins that are locked by a trade."""
        all_pending = []
        pending_accept = await self.get_offers_with_status(TradeStatus.PENDING_ACCEPT)
        pending_confirm = await self.get_offers_with_status(TradeStatus.PENDING_CONFIRM)
        pending_cancel = await self.get_offers_with_status(TradeStatus.PENDING_CANCEL)
        all_pending.extend(pending_accept)
        all_pending.extend(pending_confirm)
        all_pending.extend(pending_cancel)

        coins_of_interest = []
        for trade_offer in all_pending:
            coins_of_interest.extend([c.name() for c in Offer.from_bytes(trade_offer.offer).get_involved_coins()])

        result = {}
        coin_records = await self.wallet_state_manager.coin_store.get_multiple_coin_records(coins_of_interest)
        for record in coin_records:
            if wallet_id is None or record.wallet_id == wallet_id:
                result[record.name()] = record

        return result

    async def get_all_trades(self):
        all: List[TradeRecord] = await self.trade_store.get_all_trades()
        return all

    async def get_trade_by_id(self, trade_id: bytes32) -> Optional[TradeRecord]:
        record = await self.trade_store.get_trade_record(trade_id)
        return record

    async def cancel_pending_offer(self, trade_id: bytes32):
        await self.trade_store.set_status(trade_id, TradeStatus.CANCELLED, False)

    async def cancel_pending_offer_safely(self, trade_id: bytes32):
        """This will create a transaction that includes coins that were offered"""
        self.log.info(f"Secure-Cancel pending offer with id trade_id {trade_id.hex()}")
        trade = await self.trade_store.get_trade_record(trade_id)
        if trade is None:
            return None

        for coin in Offer.from_bytes(trade.offer).get_primary_coins():
            wallet = await self.wallet_state_manager.get_wallet_for_coin(coin.name())

            if wallet is None:
                continue
            new_ph = await wallet.get_new_puzzlehash()
            if wallet.type() == WalletType.COLOURED_COIN.value:
                txs = await wallet.generate_signed_transaction(
                    [coin.amount], [new_ph], 0, coins={coin}, ignore_max_send_amount=True
                )
                tx = txs[0]
            else:
                tx = await wallet.generate_signed_transaction(
                    coin.amount, new_ph, 0, coins={coin}, ignore_max_send_amount=True
                )
            await self.wallet_state_manager.add_pending_transaction(tx_record=tx)

        await self.trade_store.set_status(trade_id, TradeStatus.PENDING_CANCEL, False)

    async def save_trade(self, trade: TradeRecord):
        await self.trade_store.add_trade_record(trade, False)

    async def create_offer_for_ids(self, offer: Dict[int, int]) -> Tuple[bool, Optional[TradeRecord], Optional[str]]:
        success, created_offer, error = await self._create_offer_for_ids(offer)
        if not success or created_offer is None:
            raise Exception(f"Error creating offer: {error}")

        now = uint64(int(time.time()))
        trade_offer: TradeRecord = TradeRecord(
            confirmed_at_index=uint32(0),
            accepted_at_time=None,
            created_at_time=now,
            is_my_offer=True,
            sent=uint32(0),
            offer=bytes(created_offer),
            coins_of_interest=created_offer.get_involved_coins(),
            trade_id=created_offer.name(),
            status=uint32(TradeStatus.PENDING_ACCEPT.value),
            sent_to=[],
        )

        if success is True and trade_offer is not None:
            await self.save_trade(trade_offer)

        return success, trade_offer, error

    async def _create_offer_for_ids(self, offer_dict: Dict[int, int]) -> Tuple[bool, Optional[Offer], Optional[str]]:
        """
        Offer is dictionary of wallet ids and amount
        """
        try:
            coins_to_offer: Dict[uint32, List[Coin]] = {}
            requested_payments: Dict[Optional[bytes32], List[Payment]] = {}
            for id, amount in offer_dict.items():
                wallet_id = uint32(id)
                wallet = self.wallet_state_manager.wallets[wallet_id]

                if amount > 0:
                    if wallet.type() == WalletType.STANDARD_WALLET:
                        key: Optional[bytes32] = None
                    elif wallet.type() == WalletType.COLOURED_COIN:
                        key = bytes32(bytes.fromhex(wallet.get_colour()))
                    else:
                        raise ValueError(f"Offers are not implemented for {wallet.type()}")
                    p2_ph: bytes32 = await wallet.get_new_puzzlehash()
                    memos: Optional[List[Optional[bytes]]] = None if WalletType.STANDARD_WALLET else [p2_ph]
                    requested_payments[key] = [Payment(p2_ph, uint64(amount), memos)]
                elif amount < 0:
                    balance = await wallet.get_confirmed_balance()
                    if balance < abs(amount):
                        raise Exception(f"insufficient funds in wallet {wallet_id}")
                    coins_to_offer[wallet_id] = await wallet.select_coins(uint64(abs(amount)))

            all_coins: List[Coin] = [c for coins in coins_to_offer.values() for c in coins]
            notarized_payments: Dict[Optional[bytes32], List[NotarizedPayment]] = Offer.notarize_payments(
                requested_payments, all_coins
            )
            announcements_to_assert = Offer.calculate_announcements(notarized_payments)
            self.log.warning(f"Announcements are not implemented yet {announcements_to_assert[0].name()}")

            all_transactions: List[TransactionRecord] = []
            for wallet_id, selected_coins in coins_to_offer.items():
                wallet = self.wallet_state_manager.wallets[wallet_id]
                # This should probably not switch on whether or not we're spending a CAT but it has to for now
                # TODO: Add fee support
                # TODO: ADD ANNOUNCEMENT ASSERTIONS
                if wallet.type() == WalletType.COLOURED_COIN:
                    txs = await wallet.generate_signed_transaction(
                        [abs(offer_dict[int(wallet_id)])], [Offer.ph()], 0, coins=set(selected_coins)
                    )
                    all_transactions.extend(txs)
                else:
                    tx = await wallet.generate_signed_transaction(
                        abs(offer_dict[int(wallet_id)]), Offer.ph(), 0, coins=set(selected_coins)
                    )
                    all_transactions.append(tx)

            total_spend_bundle = SpendBundle.aggregate([tx.spend_bundle for tx in all_transactions])
            offer = Offer(notarized_payments, total_spend_bundle)
            return True, offer, None

        except Exception as e:
            raise e
            tb = traceback.format_exc()
            self.log.error(f"Error with creating trade offer: {type(e)}{tb}")
            return False, None, str(e)

    async def maybe_create_wallets_for_offer(self, offer: Offer):

        for key in offer.arbitrage():
            wsm = self.wallet_state_manager
            wallet: Wallet = wsm.main_wallet
            if key is None:
                continue
            exists: Optional[Wallet] = await wsm.get_wallet_for_colour(key)
            if exists is None:
                self.log.info(f"Creating wallet for asset ID: {key}")
                await CCWallet.create_wallet_for_cc(wsm, wallet, key)

    async def respond_to_offer(self, offer: Offer) -> Tuple[bool, Optional[TradeRecord], Optional[str]]:
        take_offer_dict: Dict[int, int] = {}
        arbitrage: Dict[Optional[bytes32], int] = offer.arbitrage()
        for asset_id, amount in arbitrage.items():
            if asset_id is None:
                wallet = self.wallet_state_manager.main_wallet
            else:
                wallet = await self.wallet_state_manager.get_wallet_for_colour(asset_id.hex())
                if wallet is None:
                    return False, None, f"Do not have a CAT of asset ID: {asset_id} to fulfill offer"
            take_offer_dict[int(wallet.id())] = amount

        success, take_offer, error = await self._create_offer_for_ids(take_offer_dict)
        if not success:
            return False, None, error

        complete_offer = Offer.aggregate([offer, take_offer])
        assert complete_offer.is_valid()
        final_spend_bundle: SpendBundle = complete_offer.to_valid_spend()

        # Now to deal with transaction history before pushing the spend
        settlement_coins: List[Coin] = [c for coins in complete_offer.get_offered_coins().values() for c in coins]
        settlement_coin_ids: List[bytes32] = [c.name() for c in settlement_coins]
        settlement_parents: List[bytes32] = [c.parent_coin_info for c in settlement_coins]
        additions: List[Coin] = final_spend_bundle.not_ephemeral_additions()
        removals: List[Coin] = final_spend_bundle.removals()

        txs = []
        for addition in additions:
            if addition.parent_coin_info in settlement_coin_ids:
                wallet_info = await self.wallet_state_manager.get_wallet_id_for_puzzle_hash(addition.puzzle_hash)
                if wallet_info is not None:
                    wallet_id, _ = wallet_info
                    wallet = self.wallet_state_manager.wallets[wallet_id]
                    to_puzzle_hash = await wallet.convert_puzzle_hash(addition.puzzle_hash)
                    txs.append(
                        TransactionRecord(
                            confirmed_at_height=uint32(0),
                            created_at_time=uint64(int(time.time())),
                            to_puzzle_hash=to_puzzle_hash,
                            amount=addition.amount,
                            fee_amount=uint64(0),
                            confirmed=False,
                            sent=uint32(10),
                            spend_bundle=None,
                            additions=[],
                            removals=[],
                            wallet_id=wallet_id,
                            sent_to=[],
                            trade_id=complete_offer.name(),
                            type=uint32(TransactionType.INCOMING_TRADE.value),
                            name=std_hash(final_spend_bundle.name() + addition.name()),
                            memos=[],
                        )
                    )

        # While we want additions to show up as separate records, removals of the same wallet should show as one
        removal_dict: Dict[uint32, List[Coin]] = {}
        for removal in removals:
            if removal.name() in settlement_parents:
                wallet_info = await self.wallet_state_manager.get_wallet_id_for_puzzle_hash(removal.puzzle_hash)
                if wallet_info is not None:
                    wallet_id, _ = wallet_info
                    removal_dict.setdefault(wallet_id, [])
                    removal_dict[wallet_id].append(removal)

        for wid, grouped_removals in removal_dict.items():
            wallet = self.wallet_state_manager.wallets[wid]
            to_puzzle_hash = bytes32([0] * 32)  # We use all zeros to be clear not to send here
            removal_tree_hash = Program.to([rem.as_list() for rem in grouped_removals]).get_tree_hash()
            txs.append(
                TransactionRecord(
                    confirmed_at_height=uint32(0),
                    created_at_time=uint64(int(time.time())),
                    to_puzzle_hash=to_puzzle_hash,
                    amount=uint64(sum([c.amount for c in grouped_removals])),
                    fee_amount=uint64(0),  # TODO: Add fee support
                    confirmed=False,
                    sent=uint32(10),
                    spend_bundle=None,
                    additions=[],
                    removals=grouped_removals,
                    wallet_id=wallet.id(),
                    sent_to=[],
                    trade_id=complete_offer.name(),
                    type=uint32(TransactionType.OUTGOING_TRADE.value),
                    name=std_hash(final_spend_bundle.name() + removal_tree_hash),
                    memos=[],
                )
            )

        trade_record: TradeRecord = TradeRecord(
            confirmed_at_index=uint32(0),
            accepted_at_time=uint64(int(time.time())),
            created_at_time=uint64(int(time.time())),
            is_my_offer=False,
            sent=uint32(0),
            offer=bytes(complete_offer),
            coins_of_interest=complete_offer.get_involved_coins(),
            trade_id=complete_offer.name(),
            status=uint32(TradeStatus.PENDING_CONFIRM.value),
            sent_to=[],
        )

        await self.save_trade(trade_record)

        # Dummy transaction for the sake of the wallet push
        push_tx = TransactionRecord(
            confirmed_at_height=uint32(0),
            created_at_time=uint64(int(time.time())),
            to_puzzle_hash=bytes32([0] * 32),
            amount=uint64(0),
            fee_amount=uint64(0),
            confirmed=False,
            sent=uint32(0),
            spend_bundle=final_spend_bundle,
            additions=final_spend_bundle.additions(),
            removals=final_spend_bundle.removals(),
            wallet_id=uint32(0),
            sent_to=[],
            trade_id=complete_offer.name(),
            type=uint32(TransactionType.OUTGOING_TRADE.value),
            name=final_spend_bundle.name(),
            memos=list(final_spend_bundle.get_memos().items()),
        )
        await self.wallet_state_manager.add_pending_transaction(push_tx)
        for tx in txs:
            await self.wallet_state_manager.add_transaction(tx)

        return True, trade_record, None
