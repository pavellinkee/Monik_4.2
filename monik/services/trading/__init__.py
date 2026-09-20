"""Исполнение сделок режима ``ann``.

Подсистема отделена от сканера намеренно (``the_main_rules.md``,
правило 11, сохраняющее суть §56): поиск возможностей и распоряжение
деньгами — разные полномочия, и выключаются они по отдельности.
"""

from monik.services.trading.approvals import (
    ApprovalRequest,
    encode_approve,
    encode_permit2_approve,
)
from monik.services.trading.chain import (
    ChainAccount,
    SimulationResult,
    TokenBalance,
    TransactionReceipt,
)
from monik.services.trading.executor import TradeExecutor
from monik.services.trading.exits import ExitCandidate, projected_output, rank_exits
from monik.services.trading.ports import PositionStore, SequenceSource
from monik.services.trading.sender import SentTransaction, TransactionSender
from monik.services.trading.wallet import TradingWallet
from monik.services.trading.watcher import LongWaitNotice, PositionWatcher

__all__ = [
    "ApprovalRequest",
    "ChainAccount",
    "LongWaitNotice",
    "PositionStore",
    "PositionWatcher",
    "SentTransaction",
    "SequenceSource",
    "SimulationResult",
    "TokenBalance",
    "TradeExecutor",
    "TradingWallet",
    "TransactionReceipt",
    "TransactionSender",
    "ExitCandidate",
    "encode_approve",
    "encode_permit2_approve",
    "projected_output",
    "rank_exits",
]
