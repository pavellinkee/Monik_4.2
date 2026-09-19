"""Открытие сделки: три проверки, каждая из которых отменяет её целиком.

Найденная возможность сама по себе не является разрешением тратить
деньги (``the_main_rules.md``, правило 11, сохраняющее суть §56).
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from eth_account import Account

from monik.config import parse_configuration
from monik.config.secrets import SecretValue
from monik.domain.enums.lifecycle import ScanStatus
from monik.domain.enums.modes import ScanMode
from monik.domain.enums.operations import OperationType, RoutingMode
from monik.domain.enums.providers import ProviderId
from monik.domain.enums.resources import RequestPriority
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.execution import OrderOutcome, OrderState
from monik.domain.models.opportunity import Candidate
from monik.domain.models.scan import Scan, ScanScope
from monik.domain.value_objects.identifiers import ScanId
from monik.infrastructure.providers.contract import AdapterCapabilities
from monik.infrastructure.providers.fake import FakeAdapter
from monik.services.calculator import ProfitCalculator
from monik.services.level1.results import ScanResult
from monik.services.observability import FakeClock
from monik.services.registries import TokenRegistry
from monik.services.trading import TradeExecutor, TradingWallet
from monik.services.trading.orders import SignedOrderRunner
from tests import factories as f
from tests.component.level1.conftest import level1_document
from tests.unit.config.conftest import VALID_ENV
from tests.unit.trading.support import (
    FixedCosts,
    MemoryCalibration,
    MemoryPositions,
    MemorySequences,
    ScriptedNode,
    build_account,
    build_sender,
)

USDT_ADDRESS = str(f.USDT.address).lower()


def _tokens() -> TokenRegistry:
    """Реестр из двух токенов Polygon: базовый и промежуточный."""
    document = level1_document()
    configuration = parse_configuration(document, environ=dict(VALID_ENV)).config
    return TokenRegistry(configuration)


def _wallet() -> TradingWallet:
    return TradingWallet(SecretValue("K", str(Account.create().key.hex())))


def _candidate(
    raw_input: int,
    net_profit: str,
    *,
    buy_provider: ProviderId = ProviderId.UNISWAP,
    sell_provider: ProviderId = ProviderId.UNISWAP,
) -> Candidate:
    buy = f.quote(
        operation=OperationType.BUY,
        provider_id=buy_provider,
        input_token=f.USDT,
        output_token=f.AAVE,
        input_raw=raw_input,
    )
    sell = f.quote(
        operation=OperationType.SELL,
        provider_id=sell_provider,
        input_token=f.AAVE,
        output_token=f.USDT,
        input_raw=buy.output_amount.raw,
        output_raw=raw_input + 100_000,
    )
    result = f.profit_result(input_raw=raw_input, output_raw=raw_input + 100_000)
    return Candidate(
        scan_id=ScanId.generate(),
        buy_quote=buy,
        sell_quote=sell,
        preliminary_result=result.model_copy(update={"net_profit": Decimal(net_profit)}),
        detected_at=f.NOW,
    )


def _result(*candidates: Candidate) -> ScanResult:
    scope = ScanScope(
        mode=ScanMode.ANN,
        networks=(f.POLYGON,),
        providers=(ProviderId.UNISWAP,),
        tokens=(f.AAVE.key,),
        raw_amounts=(50_000_000,),
    )
    scan = Scan(
        scan_id=ScanId.generate(),
        status=ScanStatus.COMPLETE,
        scope=scope,
        started_at=f.NOW,
        finished_at=f.NOW,
    )
    return ScanResult(scan=scan, qualified=candidates)


def _executor(
    node: ScriptedNode,
    positions: MemoryPositions,
    *,
    execution_enabled: bool = True,
    costs: FixedCosts | None = None,
    min_entry_profit_raw: int = 0,
    calibration: MemoryCalibration | None = None,
    slippage_bps: int = 0,
    rate: str = "1",
    sell_rate: str | None = None,
) -> TradeExecutor:
    clock = FakeClock(f.NOW)
    wallet = _wallet()
    return TradeExecutor(
        # Курс по умолчанию делает круг безубыточным, а допуск нулевым:
        # проверки выбора и записи к предложению отношения не имеют, и
        # решать их исход должен не он.
        adapters={
            ProviderId.UNISWAP.value: FakeAdapter(ProviderId.UNISWAP, clock, rate=Decimal(rate)),
            # Второй агрегатор нужен сделкам между ними: круг может быть
            # куплен у одного и продан у другого.
            ProviderId.KYBERSWAP.value: FakeAdapter(
                ProviderId.KYBERSWAP, clock, rate=Decimal(sell_rate or rate)
            ),
        },
        positions=positions,
        sequences=MemorySequences(),
        account=build_account(node, clock, wallet.address),
        sender=build_sender(node, clock, wallet),
        tokens=_tokens(),
        calculator=ProfitCalculator(clock),
        # По умолчанию уточняющая проверка стоимости ничего не отсекает:
        # проверки выбора и отправки к ней отношения не имеют, а там, где
        # решает именно она, стоимость задаётся явно.
        costs=costs or FixedCosts(0),
        calibration=calibration,
        clock=clock,
        is_execution_open=lambda: execution_enabled,
        slippage_bps=slippage_bps,
        min_entry_profit_raw=min_entry_profit_raw,
        receipt_timeout_seconds=1,
        receipt_poll_seconds=1,
    )


class TestChoice:
    async def test_the_most_profitable_affordable_amount_wins(self) -> None:
        """Выбор идёт по заработку, но только среди посильных сумм."""
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000})
        positions = MemoryPositions()
        executor = _executor(node, positions)

        await executor.consider(
            (_result(_candidate(100_000_000, "0.20"), _candidate(50_000_000, "0.10")),)
        )

        assert len(positions.items) == 1
        opened = next(iter(positions.items.values()))
        assert opened.raw_input == 50_000_000, "сто не помещается в остаток, берём пятьдесят"

    async def test_nothing_happens_when_no_amount_fits(self) -> None:
        node = ScriptedNode(balances={USDT_ADDRESS: 1_000_000})
        positions = MemoryPositions()

        executor = _executor(node, positions)

        assert await executor.consider((_result(_candidate(50_000_000, "0.1")),)) is None
        assert not positions.items
        assert not node.sent

    async def test_money_promised_to_open_trades_is_not_counted_twice(self) -> None:
        """Остаток на счёте включает деньги уже открытых сделок."""
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000})
        positions = MemoryPositions()
        executor = _executor(node, positions)

        await executor.consider((_result(_candidate(50_000_000, "0.1")),))
        await executor.consider((_result(_candidate(50_000_000, "0.1")),))

        assert len(positions.items) == 1, "вторая сделка не открывается: деньги заняты"


class TestGuards:
    async def test_disabled_execution_sends_nothing(self) -> None:
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000})
        positions = MemoryPositions()

        result = await _executor(node, positions, execution_enabled=False).consider(
            (_result(_candidate(50_000_000, "0.1")),)
        )

        assert result is None
        assert not positions.items
        assert not node.sent, "в сеть не ушло ничего"

    async def test_a_swap_that_would_revert_is_not_sent(self) -> None:
        """Симуляция — защита от завышенной котировки."""
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000}, simulation_ok=False)
        positions = MemoryPositions()

        result = await _executor(node, positions).consider(
            (_result(_candidate(50_000_000, "0.1")),)
        )

        assert result is None
        assert not positions.items
        assert not node.sent


class TestRecording:
    async def test_position_is_written_before_the_transaction_goes_out(self) -> None:
        """Иначе перезапуск между отправкой и записью потерял бы токены."""
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000})
        positions = MemoryPositions()

        await _executor(node, positions).consider((_result(_candidate(50_000_000, "0.1")),))

        assert positions.created_before_send, "запись сделана"
        assert node.sent, "и только потом отправлено"

    async def test_successful_buy_moves_the_trade_to_holding(self) -> None:
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000})
        positions = MemoryPositions()

        position = await _executor(node, positions).consider(
            (_result(_candidate(50_000_000, "0.1")),)
        )

        assert position is not None
        assert position.status is PositionStatus.HOLDING
        assert position.buy_tx_hash is not None

    async def test_reverted_buy_closes_the_trade_as_failed(self) -> None:
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000}, receipt_ok=False)
        positions = MemoryPositions()

        position = await _executor(node, positions).consider(
            (_result(_candidate(50_000_000, "0.1")),)
        )

        assert position is not None
        assert position.status is PositionStatus.FAILED
        assert not position.status.is_open

    async def test_missing_receipt_leaves_the_trade_open(self) -> None:
        """Транзакция может попасть в блок позже; забывать её нельзя."""
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000}, receipt_ok=None)
        positions = MemoryPositions()

        position = await _executor(node, positions).consider(
            (_result(_candidate(50_000_000, "0.1")),)
        )

        assert position is not None
        assert position.status is PositionStatus.BUYING


class TestRealOffer:
    """Решение принимается по предложению агрегатора, а не по котировке.

    Агрегатор — обменник, а не биржа: цену он называет сам. Котировка —
    реклама, минимум в собранной транзакции — обязательство: ниже него
    он сам откатит обмен. Круг считается по двум таким минимумам
    подряд, поэтому исполнение может выйти лучше расчёта, но не хуже.
    """

    def _node(self) -> ScriptedNode:
        return ScriptedNode(balances={USDT_ADDRESS: 60_000_000})

    async def test_trade_is_skipped_when_the_guaranteed_circle_does_not_pay(self) -> None:
        """Круг гарантирует 0.100 USDT, а стоит 0.150 — сделки не будет."""
        positions = MemoryPositions()
        node = self._node()
        executor = _executor(
            node, positions, costs=FixedCosts(150_000), min_entry_profit_raw=5_000, rate="1.001"
        )

        position = await executor.consider((_result(_candidate(50_000_000, "0.1")),))

        assert position is None
        assert not node.sent, "в сеть не ушло ничего"
        assert positions.items == {}

    async def test_trade_proceeds_when_the_guarantee_leaves_enough(self) -> None:
        """Парная проверка: тот же круг при дешёвом газе проходит."""
        positions = MemoryPositions()
        node = self._node()
        executor = _executor(
            node, positions, costs=FixedCosts(50_000), min_entry_profit_raw=5_000, rate="1.001"
        )

        position = await executor.consider((_result(_candidate(50_000_000, "0.1")),))

        assert position is not None
        assert node.sent

    async def test_promise_above_the_threshold_is_not_enough(self) -> None:
        """Котировка обещает прибыль, обязательство — нет.

        Ровно так была открыта сделка ``#T2``: обещание прошло порог,
        реального предложения на эту сумму не было, и выйти из позиции
        оказалось не во что.
        """
        positions = MemoryPositions()
        node = self._node()
        # Курс даёт круг ровно в ноль, а допуск в 1 % уводит
        # обязательство заметно ниже обещания.
        executor = _executor(
            node,
            positions,
            costs=FixedCosts(0),
            min_entry_profit_raw=0,
            rate="1",
            slippage_bps=100,
        )

        position = await executor.consider((_result(_candidate(50_000_000, "0.1")),))

        assert position is None, "по котировке прошло бы, по обязательству — нет"
        assert not node.sent

    async def test_unknown_cost_cancels_the_trade(self) -> None:
        """Непроверенное предложение — не предложение (``CLAUDE.md`` §12)."""
        positions = MemoryPositions()
        node = self._node()
        executor = _executor(
            node, positions, costs=FixedCosts(None), min_entry_profit_raw=5_000, rate="1.001"
        )

        position = await executor.consider((_result(_candidate(50_000_000, "0.1")),))

        assert position is None
        assert not node.sent


class TestCalibrationRecording:
    """Фактический расход газа известен только здесь.

    Поиск его не узнаёт никогда: котировка обещает, а платит исполнение.
    Замер передаётся отсюда и не стоит ни одного лишнего обращения —
    обещанное пришло с котировкой, фактическое с квитанцией.
    """

    async def test_actual_gas_is_reported_against_the_quoted_estimate(self) -> None:
        positions = MemoryPositions()
        calibration = MemoryCalibration()
        node = ScriptedNode(
            balances={USDT_ADDRESS: 60_000_000},
            after_send={str(f.AAVE.address).lower(): 5 * 10**18},
        )
        executor = _executor(node, positions, calibration=calibration)
        candidate = _candidate(50_000_000, "0.1")
        candidate = candidate.model_copy(
            update={
                "buy_quote": candidate.buy_quote.model_copy(update={"estimated_gas_units": 100_000})
            }
        )

        position = await executor.consider((_result(candidate),))

        assert position is not None
        assert position.buy_quoted_gas_units == 100_000
        # Узел стенда сообщает расход 180 000 — втрое больше обещанного.
        assert position.buy_gas_units == 180_000
        assert calibration.records == [(str(f.POLYGON), "uniswap", 100_000, 180_000)]

    async def test_nothing_is_reported_without_a_quoted_estimate(self) -> None:
        """Отношение не к чему считать, и выдумывать его нельзя."""
        positions = MemoryPositions()
        calibration = MemoryCalibration()
        node = ScriptedNode(
            balances={USDT_ADDRESS: 60_000_000},
            after_send={str(f.AAVE.address).lower(): 5 * 10**18},
        )
        executor = _executor(node, positions, calibration=calibration)

        await executor.consider((_result(_candidate(50_000_000, "0.1")),))

        assert calibration.records == []


class TestPriority:
    """Покупка обгоняет сканирование, но уступает продаже.

    ``the_main_rules.md``, правило 13. Покупка тратит деньги, но пока не
    рискует ими: сделка ещё не открыта, и отказ стоит только упущенной
    возможности. Продажа рискует уже потраченным, поэтому идёт первой.
    """

    async def test_every_buy_request_carries_the_buy_priority(self) -> None:
        positions = MemoryPositions()
        node = ScriptedNode(
            balances={USDT_ADDRESS: 60_000_000},
            after_send={str(f.AAVE.address).lower(): 5 * 10**18},
        )
        executor = _executor(node, positions)
        adapter = executor._adapters[ProviderId.UNISWAP.value]  # noqa: SLF001

        await executor.consider((_result(_candidate(50_000_000, "0.1")),))

        assert adapter.quote_calls, "агрегатор опрошен"
        assert all(request.priority is RequestPriority.ANN_BUY for request in adapter.quote_calls)

    def test_buy_yields_to_sell_and_outranks_scanning(self) -> None:
        assert RequestPriority.ANN_SELL.rank < RequestPriority.ANN_BUY.rank
        assert RequestPriority.ANN_BUY.rank < RequestPriority.ANN_SCAN.rank


class TestCrossAggregator:
    """Круг между двумя агрегаторами: купили у одного, продали у другого.

    Это основная модель арбитража, а не частный случай. Каждую ногу
    собирает **её** агрегатор: спрашивать предложение о продаже у того,
    кто её исполнять не будет, бессмысленно.
    """

    def _node(self) -> ScriptedNode:
        return ScriptedNode(
            balances={USDT_ADDRESS: 60_000_000},
            after_send={str(f.AAVE.address).lower(): 5 * 10**18},
        )

    async def test_each_leg_is_offered_by_its_own_aggregator(self) -> None:
        positions = MemoryPositions()
        node = self._node()
        executor = _executor(node, positions, rate="1.001")
        buyer = executor._adapters[ProviderId.UNISWAP.value]  # noqa: SLF001
        seller = executor._adapters[ProviderId.KYBERSWAP.value]  # noqa: SLF001

        position = await executor.consider(
            (
                _result(
                    _candidate(
                        50_000_000,
                        "0.1",
                        buy_provider=ProviderId.UNISWAP,
                        sell_provider=ProviderId.KYBERSWAP,
                    )
                ),
            )
        )

        assert position is not None
        assert position.buy_provider_id is ProviderId.UNISWAP
        assert position.sell_provider_id is ProviderId.KYBERSWAP
        # Покупку спрашивали у одного, продажу — у другого.
        assert [r.operation for r in buyer.quote_calls] == [OperationType.BUY]
        assert [r.operation for r in seller.quote_calls] == [OperationType.SELL]

    async def test_trade_is_skipped_without_the_exit_aggregator(self) -> None:
        """Продавать некому — покупать незачем."""
        positions = MemoryPositions()
        node = self._node()
        executor = _executor(node, positions, rate="1.001")
        del executor._adapters[ProviderId.KYBERSWAP.value]  # noqa: SLF001

        position = await executor.consider(
            (
                _result(
                    _candidate(
                        50_000_000,
                        "0.1",
                        buy_provider=ProviderId.UNISWAP,
                        sell_provider=ProviderId.KYBERSWAP,
                    )
                ),
            )
        )

        assert position is None
        assert not node.sent


class TestCandidateIteration:
    """Отказ по предложению не заканчивает проход.

    Находка с завышенной котировкой выглядит лучшей и встаёт первой,
    заслоняя собой честные. Так и случилось 18.09: одна находка
    KyberSwap трижды закрывала собой остальные, среди которых были пары
    через Uniswap. Поэтому после отказа перебор идёт дальше — до
    первого кандидата, чьё **предложение** порог проходит.
    """

    def _node(self) -> ScriptedNode:
        return ScriptedNode(
            balances={USDT_ADDRESS: 200_000_000},
            after_send={str(f.AAVE.address).lower(): 5 * 10**18},
        )

    def _results(self, *candidates: Candidate) -> tuple[ScanResult, ...]:
        return (_result(*candidates),)

    async def test_rejected_candidate_does_not_end_the_scan(self) -> None:
        """Первая находка не окупается, вторая окупается — берём вторую."""
        positions = MemoryPositions()
        node = self._node()
        # Стоимость круга 0.15: первая находка обещает 0.1 и не окупается,
        # вторая обещает 1.0 и окупается.
        executor = _executor(
            node, positions, costs=FixedCosts(150_000), min_entry_profit_raw=5_000, rate="1.001"
        )

        position = await executor.consider(
            self._results(
                _candidate(50_000_000, "0.1"),
                _candidate(100_000_000, "1.0"),
            )
        )

        assert position is not None
        assert position.raw_input == 100_000_000, "открыта вторая находка"

    async def test_every_rejection_leaves_the_account_untouched(self) -> None:
        """Ни один отказ не отправляет транзакции."""
        positions = MemoryPositions()
        node = self._node()
        executor = _executor(
            node, positions, costs=FixedCosts(500_000), min_entry_profit_raw=5_000, rate="1.001"
        )

        position = await executor.consider(
            self._results(
                _candidate(50_000_000, "0.1"),
                _candidate(100_000_000, "0.2"),
            )
        )

        assert position is None
        assert not node.sent
        assert positions.items == {}

    async def test_number_of_checks_is_bounded(self) -> None:
        """Отказ стоит запросов, и весь список перебирать незачем."""
        positions = MemoryPositions()
        node = self._node()
        executor = _executor(
            node, positions, costs=FixedCosts(500_000), min_entry_profit_raw=5_000, rate="1.001"
        )
        executor._max_offer_checks = 1  # noqa: SLF001
        adapter = executor._adapters[ProviderId.UNISWAP.value]  # noqa: SLF001

        await executor.consider(
            self._results(
                _candidate(50_000_000, "0.1"),
                _candidate(100_000_000, "0.2"),
                _candidate(50_000_000, "0.3"),
            )
        )

        # Проверен ровно один кандидат: покупка и нога продажи.
        assert len(adapter.quote_calls) == 2

    async def test_unaffordable_candidate_does_not_stop_the_rest(self) -> None:
        """Сумма, не помещающаяся в остаток, отбрасывается, а не всё."""
        positions = MemoryPositions()
        node = ScriptedNode(
            balances={USDT_ADDRESS: 60_000_000},
            after_send={str(f.AAVE.address).lower(): 5 * 10**18},
        )
        executor = _executor(
            node, positions, costs=FixedCosts(0), min_entry_profit_raw=0, rate="1.001"
        )

        position = await executor.consider(
            self._results(
                _candidate(100_000_000, "1.0"),
                _candidate(50_000_000, "0.1"),
            )
        )

        assert position is not None
        assert position.raw_input == 50_000_000


def _order_adapter(clock: FakeClock, rate: str = "1") -> FakeAdapter:
    """Двойник провайдера, исполняющего подписанной заявкой."""
    return FakeAdapter(
        ProviderId.COW,
        clock,
        rate=Decimal(rate),
        capabilities=AdapterCapabilities(
            provider_id=ProviderId.COW,
            supported_networks=frozenset({f.POLYGON}),
            routing_modes=frozenset({RoutingMode.CLASSIC}),
            supports_execution=True,
            supports_signed_orders=True,
        ),
    )


def _order_executor(
    node: ScriptedNode,
    positions: MemoryPositions,
    *,
    orders: SignedOrderRunner | None = None,
    outcome: OrderOutcome = OrderOutcome.FILLED,
    rate: str = "1",
) -> tuple[TradeExecutor, FakeAdapter]:
    """Исполнитель, у которого покупка идёт заявкой, а продажа — тоже."""
    clock = FakeClock(f.NOW)
    wallet = _wallet()
    adapter = _order_adapter(clock, rate=rate)
    adapter.default_order_outcome = OrderState(
        outcome=outcome, executed_input_raw=0, executed_output_raw=0
    )

    async def sleeper(seconds: float) -> None:
        return None

    executor = TradeExecutor(
        adapters={ProviderId.COW.value: adapter},
        positions=positions,
        sequences=MemorySequences(),
        account=build_account(node, clock, wallet.address),
        sender=build_sender(node, clock, wallet),
        tokens=_tokens(),
        calculator=ProfitCalculator(clock),
        costs=FixedCosts(0),
        calibration=None,
        clock=clock,
        is_execution_open=lambda: True,
        slippage_bps=0,
        min_entry_profit_raw=0,
        orders=orders
        if orders is not None
        else SignedOrderRunner(wallet=wallet, clock=clock, sleeper=sleeper),
        receipt_timeout_seconds=1,
        receipt_poll_seconds=1,
    )
    return executor, adapter


class TestOrderExecution:
    """Покупка провайдером, который транзакции не отдаёт (правило 16)."""

    async def test_the_position_records_the_order_and_no_gas(self) -> None:
        """Заявка вместо хеша, ноль вместо неизвестного расхода.

        Неизвестный расход запретил бы считать круг; здесь он
        действительно нулевой, потому что своей транзакции не возникает.
        """
        node = ScriptedNode(balances={USDT_ADDRESS: 200_000_000})
        positions = MemoryPositions()
        executor, adapter = _order_executor(node, positions)

        await executor.consider(
            (
                _result(
                    _candidate(
                        100_000_000,
                        "0.20",
                        buy_provider=ProviderId.COW,
                        sell_provider=ProviderId.COW,
                    )
                ),
            )
        )

        assert len(positions.items) == 1
        opened = next(iter(positions.items.values()))
        assert opened.buy_order_uid == adapter.placed_orders[0].uid
        assert opened.buy_tx_hash is None
        assert opened.buy_gas_wei == 0
        assert opened.buy_gas_units == 0

    async def test_nothing_is_sent_to_the_chain(self) -> None:
        """Заявка подписывается, а не отправляется: своей транзакции нет."""
        node = ScriptedNode(balances={USDT_ADDRESS: 200_000_000})
        positions = MemoryPositions()
        executor, _ = _order_executor(node, positions)

        await executor.consider(
            (
                _result(
                    _candidate(
                        100_000_000,
                        "0.20",
                        buy_provider=ProviderId.COW,
                        sell_provider=ProviderId.COW,
                    )
                ),
            )
        )

        assert node.sent == []

    async def test_an_unfilled_order_closes_the_trade_as_not_taken(self) -> None:
        """Неисполненная заявка — не убыток: деньги остались на счету.

        Сделка закрывается как несостоявшаяся, а возврат равен вложенному:
        мы ничего не потратили, включая газ.
        """
        node = ScriptedNode(balances={USDT_ADDRESS: 200_000_000})
        positions = MemoryPositions()
        executor, _ = _order_executor(node, positions, outcome=OrderOutcome.EXPIRED)

        await executor.consider(
            (
                _result(
                    _candidate(
                        100_000_000,
                        "0.20",
                        buy_provider=ProviderId.COW,
                        sell_provider=ProviderId.COW,
                    )
                ),
            )
        )

        opened = next(iter(positions.items.values()))
        assert opened.status is PositionStatus.FAILED
        assert opened.raw_returned == opened.raw_input
        assert opened.buy_gas_wei == 0

    async def test_the_trade_is_refused_when_orders_cannot_be_signed(self) -> None:
        """Открывать то, что нечем исполнить, нельзя."""
        node = ScriptedNode(balances={USDT_ADDRESS: 200_000_000})
        positions = MemoryPositions()
        clock = FakeClock(f.NOW)
        wallet = _wallet()
        executor = TradeExecutor(
            adapters={ProviderId.COW.value: _order_adapter(clock)},
            positions=positions,
            sequences=MemorySequences(),
            account=build_account(node, clock, wallet.address),
            sender=build_sender(node, clock, wallet),
            tokens=_tokens(),
            calculator=ProfitCalculator(clock),
            costs=FixedCosts(0),
            calibration=None,
            clock=clock,
            is_execution_open=lambda: True,
            slippage_bps=0,
            min_entry_profit_raw=0,
            orders=None,
        )

        await executor.consider(
            (
                _result(
                    _candidate(
                        100_000_000,
                        "0.20",
                        buy_provider=ProviderId.COW,
                        sell_provider=ProviderId.COW,
                    )
                ),
            )
        )

        assert positions.items == {}


class TestFailureWording:
    """Неудача ноги-намерения — не откат транзакции.

    У намерения транзакции нет, и откатываться нечему: заявку просто не
    взяли. Сообщение об откате послало бы оператора искать в цепи то,
    чего там никогда не было. Наблюдено на сделке #T4 19.09.
    """

    async def test_an_unfilled_order_is_not_called_a_revert(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        node = ScriptedNode(balances={USDT_ADDRESS: 200_000_000})
        positions = MemoryPositions()
        executor, _ = _order_executor(node, positions, outcome=OrderOutcome.EXPIRED)

        with caplog.at_level(logging.INFO):
            await executor.consider(
                (
                    _result(
                        _candidate(
                            100_000_000,
                            "0.20",
                            buy_provider=ProviderId.COW,
                            sell_provider=ProviderId.COW,
                        )
                    ),
                )
            )

        messages = [record.message for record in caplog.records]
        assert "trade closed without a buy: the order was not filled" in messages
        assert "trade failed: the buy transaction reverted" not in messages
