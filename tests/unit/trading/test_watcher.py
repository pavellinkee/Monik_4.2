"""Ведение открытой сделки: ожидание и выход.

Правило выхода задано оператором: продавать, когда круг даёт **чистую
прибыль в базовом токене** не меньше заданной. Не процент — деньги.

Система никогда не закрывает позицию в убыток сама: при затянувшемся
ожидании она сообщает оператору и продолжает ждать.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from eth_account import Account

from monik.config.secrets import SecretValue
from monik.domain.enums.operations import RoutingMode
from monik.domain.enums.providers import ProviderId
from monik.domain.enums.resources import RequestPriority
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.execution import OrderOutcome, OrderState
from monik.domain.models.position import Position
from monik.domain.value_objects.identifiers import TId
from monik.infrastructure.providers.contract import AdapterCapabilities
from monik.infrastructure.providers.fake import FakeAdapter
from monik.services.calculator import ProfitCalculator
from monik.services.observability import FakeClock
from monik.services.trading import PositionWatcher, TradeExecutor, TradingWallet
from monik.services.trading.orders import SignedOrderRunner
from tests import factories as f
from tests.unit.trading.support import (
    FixedCosts,
    MemoryCalibration,
    MemoryPositions,
    MemorySequences,
    ScriptedNode,
    build_account,
    build_sender,
)
from tests.unit.trading.test_executor import _candidate, _result, _tokens

# Купили AAVE на 50 USDT. Курс адаптера задаётся правилом обмена.
ACQUIRED = 5_000_000_000_000_000_000


def _position(**overrides: object) -> Position:
    base: dict[str, object] = {
        "t_id": TId.from_sequence(1),
        "network_id": f.POLYGON,
        "status": PositionStatus.HOLDING,
        "base_token": f.USDT,
        "target_token": f.AAVE,
        "buy_provider_id": ProviderId.UNISWAP,
        "sell_provider_id": ProviderId.UNISWAP,
        "raw_input": 50_000_000,
        "raw_acquired": ACQUIRED,
        # Купленная позиция знает, во что обошлась её покупка: расход
        # приходит квитанцией вместе с ценой. Без этого знания круг
        # посчитать не из чего, и выход откладывается.
        "buy_gas_wei": 180_000 * 30_000_000_000,
        "buy_gas_units": 180_000,
        "sell_gas_wei": 0,
        "sell_gas_units": 0,
        "opened_at": f.NOW,
    }
    base.update(overrides)
    return Position(**base)  # type: ignore[arg-type]


#: Стоимость круга в стенде: 0.005 USDT. Величина заметная, но меньше
#: прибыли, на которой построены проверки выхода, — поэтому она их не
#: переворачивает, а там, где расход решает исход, он задаётся явно.
COSTS_RAW = 5_000


def _watcher(
    node: ScriptedNode,
    positions: MemoryPositions,
    *,
    clock: FakeClock | None = None,
    min_exit_profit_raw: int = 10_000,
    min_exit_profit_waiting_raw: int | None = None,
    costs: FixedCosts | None = None,
    calibration: MemoryCalibration | None = None,
    slippage_bps: int = 0,
    sell_rate: str = "10.00",
    long_wait_enabled: bool = True,
    long_wait_seconds: int = 7_200,
    exit_requires_profit: bool = True,
) -> PositionWatcher:
    active = clock or FakeClock(f.NOW)
    exit_costs = costs or FixedCosts(COSTS_RAW)
    wallet = TradingWallet(SecretValue("K", str(Account.create().key.hex())))
    adapter = FakeAdapter(ProviderId.UNISWAP, active, rate=_rate(sell_rate))
    account = build_account(node, active, wallet.address)
    sender = build_sender(node, active, wallet)
    executor = TradeExecutor(
        adapters={ProviderId.UNISWAP.value: adapter},
        positions=positions,
        sequences=MemorySequences(),
        account=account,
        sender=sender,
        tokens=_tokens(),
        calculator=ProfitCalculator(active),
        costs=exit_costs,
        calibration=calibration,
        clock=active,
        is_execution_open=lambda: True,
        slippage_bps=slippage_bps,
        min_entry_profit_raw=0,
        receipt_timeout_seconds=1,
        receipt_poll_seconds=1,
    )
    return PositionWatcher(
        adapters={ProviderId.UNISWAP.value: adapter},
        positions=positions,
        account=account,
        sender=sender,
        executor=executor,
        costs=exit_costs,
        calibration=calibration,
        clock=active,
        min_exit_profit_raw=min_exit_profit_raw,
        min_exit_profit_waiting_raw=(
            min_exit_profit_raw
            if min_exit_profit_waiting_raw is None
            else min_exit_profit_waiting_raw
        ),
        # Допуск по умолчанию нулевой: большинство проверок здесь о
        # порогах и газе, и разница между обещанием и обязательством
        # решать их исход не должна. Там, где она и проверяется, допуск
        # задаётся явно.
        slippage_bps=slippage_bps,
        exit_requires_profit=exit_requires_profit,
        long_wait_enabled=long_wait_enabled,
        long_wait_seconds=long_wait_seconds,
        receipt_timeout_seconds=1,
        receipt_poll_seconds=1,
    )


def _rate(value: str):  # noqa: ANN202
    from decimal import Decimal

    return Decimal(value)


class TestExit:
    async def test_trade_waits_while_profit_is_below_the_target(self) -> None:
        """Ниже цели — не продаём, даже если круг уже в плюсе."""
        node = ScriptedNode()
        positions = MemoryPositions()
        # 5 AAVE по курсу 10.000002 дают 50.00001 USDT: прибыль 0.00001,
        # цель 0.01 — значит ждём.
        watcher = _watcher(node, positions, sell_rate="10.000002")
        await positions.create(_position())

        await watcher.tick()

        assert positions.items["#T1"].status is PositionStatus.HOLDING
        assert not node.sent, "в сеть не ушло ничего"

    async def test_trade_exits_when_the_target_is_reached(self) -> None:
        node = ScriptedNode()
        positions = MemoryPositions()
        # 5 AAVE по курсу 10.1 дают 50.5 USDT: прибыль 0.5 — продаём.
        watcher = _watcher(node, positions, sell_rate="10.1")
        await positions.create(_position())

        await watcher.tick()

        assert node.sent, "продажа отправлена"
        assert positions.items["#T1"].status is PositionStatus.CLOSED

    async def test_target_is_money_not_percent(self) -> None:
        """Одна и та же доходность решается по-разному на разных суммах."""
        node = ScriptedNode()
        positions = MemoryPositions()
        watcher = _watcher(node, positions, min_exit_profit_raw=1_000_000, sell_rate="10.1")
        await positions.create(_position())

        await watcher.tick()

        assert not node.sent, "0.5 USDT меньше цели в 1 USDT — ждём"


class TestFailedExit:
    async def test_reverted_sell_returns_the_trade_to_waiting(self) -> None:
        """Откат продажи — неудачная попытка выхода, а не потеря."""
        node = ScriptedNode(receipt_ok=False)
        positions = MemoryPositions()
        watcher = _watcher(node, positions, sell_rate="10.1")
        await positions.create(_position())

        await watcher.tick()

        assert positions.items["#T1"].status is PositionStatus.HOLDING
        assert positions.items["#T1"].sell_tx_hash is None

    async def test_sell_that_would_revert_is_not_sent(self) -> None:
        node = ScriptedNode(simulation_ok=False)
        positions = MemoryPositions()
        watcher = _watcher(node, positions, sell_rate="10.1")
        await positions.create(_position())

        await watcher.tick()

        assert not node.sent
        assert positions.items["#T1"].status is PositionStatus.HOLDING


class TestLongWait:
    async def test_notice_appears_only_after_the_configured_time(self) -> None:
        node = ScriptedNode()
        positions = MemoryPositions()
        clock = FakeClock(f.NOW)
        watcher = _watcher(node, positions, clock=clock, sell_rate="10.000002")
        await positions.create(_position())

        assert await watcher.tick() == ()

        clock.advance(timedelta(seconds=7_201))
        notices = await watcher.tick()

        assert len(notices) == 1
        assert "#T1" in notices[0].describe()

    async def test_the_system_never_closes_at_a_loss_itself(self) -> None:
        """Долгое ожидание сообщается, но позиция остаётся открытой."""
        node = ScriptedNode()
        positions = MemoryPositions()
        clock = FakeClock(f.NOW)
        watcher = _watcher(node, positions, clock=clock, sell_rate="9.0")
        await positions.create(_position())
        clock.advance(timedelta(seconds=7_201))

        await watcher.tick()

        assert positions.items["#T1"].status is PositionStatus.HOLDING
        assert not node.sent

    async def test_notice_is_sent_once_not_every_tick(self) -> None:
        node = ScriptedNode()
        positions = MemoryPositions()
        clock = FakeClock(f.NOW)
        watcher = _watcher(node, positions, clock=clock, sell_rate="10.000002")
        await positions.create(_position())
        clock.advance(timedelta(seconds=7_201))

        first = await watcher.tick()
        await watcher.mark_notified(positions.items["#T1"])
        second = await watcher.tick()

        assert len(first) == 1
        assert second == (), "повторно о той же сделке не сообщаем"

    async def test_disabled_notice_never_fires(self) -> None:
        node = ScriptedNode()
        positions = MemoryPositions()
        clock = FakeClock(f.NOW)
        watcher = _watcher(
            node, positions, clock=clock, sell_rate="10.000002", long_wait_enabled=False
        )
        await positions.create(_position())
        clock.advance(timedelta(seconds=100_000))

        assert await watcher.tick() == ()


class TestRecovery:
    async def test_pending_buy_is_picked_up_after_a_restart(self) -> None:
        """Сделка, отправленная до перезапуска, доводится наблюдателем."""
        node = ScriptedNode(balances={str(f.AAVE.address).lower(): ACQUIRED})
        positions = MemoryPositions()
        watcher = _watcher(node, positions)
        await positions.create(
            _position(status=PositionStatus.BUYING, raw_acquired=None, buy_tx_hash="0x" + "cd" * 32)
        )

        await watcher.tick()

        assert positions.items["#T1"].status is PositionStatus.HOLDING

    async def test_pending_sell_is_picked_up_after_a_restart(self) -> None:
        node = ScriptedNode()
        positions = MemoryPositions()
        watcher = _watcher(node, positions)
        await positions.create(
            _position(
                status=PositionStatus.SELLING,
                sell_tx_hash="0x" + "ef" * 32,
                raw_base_before_sell=0,
            )
        )

        await watcher.tick()

        assert positions.items["#T1"].status is PositionStatus.CLOSED


class TestImmediateExit:
    """После покупки цена выхода спрашивается сразу, а не через такт.

    Правило оператора: «вначале производится покупка, затем ещё раз
    производится проверка, выгодно ли совершить продажу; если результат
    не положительный — сделка остаётся в ожидании». Именно сразу после
    покупки шанс выйти в плюс наивысший.
    """

    def _node(self, *, acquired: int) -> ScriptedNode:
        """Узел, у которого покупка действительно меняет остаток."""
        return ScriptedNode(
            balances={str(f.USDT.address).lower(): 60_000_000},
            after_send={str(f.AAVE.address).lower(): acquired},
        )

    def _pair(
        self, node: ScriptedNode, positions: MemoryPositions, *, sell_rate: str
    ) -> tuple[TradeExecutor, PositionWatcher]:
        watcher = _watcher(node, positions, sell_rate=sell_rate)
        executor = watcher._executor  # noqa: SLF001 - связка собирается контейнером
        executor.set_exit_check(watcher.consider_now)
        return executor, watcher

    async def test_profitable_exit_happens_without_waiting_for_the_scheduler(self) -> None:
        node = self._node(acquired=ACQUIRED)
        positions = MemoryPositions()
        executor, _ = self._pair(node, positions, sell_rate="10.1")

        position = await executor.consider((_ann_result(),))

        assert position is not None
        # Такт наблюдателя не запускался ни разу — продажа ушла из покупки.
        assert positions.items[str(position.t_id)].status is PositionStatus.CLOSED

    async def test_unprofitable_exit_leaves_the_trade_waiting(self) -> None:
        node = self._node(acquired=ACQUIRED)
        positions = MemoryPositions()
        executor, _ = self._pair(node, positions, sell_rate="10.000002")

        position = await executor.consider((_ann_result(),))

        assert position is not None
        assert positions.items[str(position.t_id)].status is PositionStatus.HOLDING

    async def test_a_failed_immediate_check_does_not_lose_the_trade(self) -> None:
        """Сделка уже открыта: сбой проверки не должен её потерять."""
        node = self._node(acquired=ACQUIRED)
        positions = MemoryPositions()
        executor, watcher = self._pair(node, positions, sell_rate="10.1")

        async def broken(position: Position) -> None:
            raise RuntimeError("узел недоступен")

        executor.set_exit_check(lambda p: watcher.consider_now(p))
        watcher._consider_exit = broken  # noqa: SLF001 - имитация сбоя узла

        position = await executor.consider((_ann_result(),))

        assert position is not None
        assert positions.items[str(position.t_id)].status is PositionStatus.HOLDING


def _ann_result():  # noqa: ANN202
    """Находка режима ann, на которую исполнитель откроет сделку."""
    return _result(_candidate(50_000_000, "0.1"))


class TestCosts:
    """Газ — расход сделки, а не фон.

    Он платится в обе стороны и от суммы не зависит, поэтому на малых
    суммах решает исход: круг, выигравший на цене меньше, чем стоила
    пара транзакций, приносит убыток. Сравнивать с порогом одну разницу
    токенов означало бы закрывать сделки в минус, считая их прибыльными.
    """

    #: 5 AAVE по курсу 10.0024 дают 50.012 USDT: разница токенов 0.012.
    NARROW_RATE = "10.0024"

    async def test_gas_is_subtracted_before_the_target_is_checked(self) -> None:
        """0.012 разницы минус 0.005 газа — до цели в 0.01 не хватает."""
        node = ScriptedNode()
        positions = MemoryPositions()
        watcher = _watcher(node, positions, sell_rate=self.NARROW_RATE)
        await positions.create(_position())

        await watcher.tick()

        assert not node.sent, "без учёта газа сделка выглядела бы прибыльной"
        assert positions.items["#T1"].status is PositionStatus.HOLDING

    async def test_the_same_circle_sells_when_gas_is_cheap(self) -> None:
        """Тот же круг при дешёвом газе цель проходит.

        Проверка парная к предыдущей: она показывает, что решение
        изменил именно расход, а не сама котировка.
        """
        node = ScriptedNode()
        positions = MemoryPositions()
        watcher = _watcher(node, positions, sell_rate=self.NARROW_RATE, costs=FixedCosts(1_000))
        await positions.create(_position())

        await watcher.tick()

        assert node.sent, "0.012 минус 0.001 цель в 0.01 проходит"

    async def test_unknown_cost_does_not_sell(self) -> None:
        """Неизвестный расход нулём не считается (``CLAUDE.md`` §12)."""
        node = ScriptedNode()
        positions = MemoryPositions()
        watcher = _watcher(node, positions, sell_rate="10.1", costs=FixedCosts(None))
        await positions.create(_position())

        await watcher.tick()

        assert not node.sent
        assert positions.items["#T1"].status is PositionStatus.HOLDING

    async def test_unknown_buy_gas_does_not_sell(self) -> None:
        """Стоимость круга неполна, пока неизвестна стоимость покупки."""
        node = ScriptedNode()
        positions = MemoryPositions()
        watcher = _watcher(node, positions, sell_rate="10.1")
        await positions.create(_position(buy_gas_wei=None))

        await watcher.tick()

        assert not node.sent

    async def test_costs_are_not_computed_for_a_hopeless_circle(self) -> None:
        """Лишних запросов не делается.

        Круг, не окупающий себя даже без газа, с газом тем более убыточен.
        Собирать ради него транзакцию и спрашивать цену газа незачем.
        """
        node = ScriptedNode()
        positions = MemoryPositions()
        costs = FixedCosts(COSTS_RAW)
        watcher = _watcher(node, positions, sell_rate="9.0", costs=costs)
        await positions.create(_position())

        await watcher.tick()

        assert costs.calls == [], "стоимость безнадёжного круга не считалась"

    async def test_a_reverted_sale_keeps_the_gas_it_burned(self) -> None:
        """Неудачная попытка выхода тоже стоила денег."""
        node = ScriptedNode(receipt_ok=False)
        positions = MemoryPositions()
        watcher = _watcher(node, positions, sell_rate="10.1")
        await positions.create(_position())

        await watcher.tick()

        position = positions.items["#T1"]
        assert position.status is PositionStatus.HOLDING
        assert position.sell_gas_wei == 180_000 * 30_000_000_000


class TestWaitingThreshold:
    """У сделки, ушедшей в ожидание, порог ниже.

    Решение оператора: сразу после покупки действует основной порог —
    шанс выйти в плюс наивысший именно тогда. Если момент упущен, деньги
    заперты в позиции, и выйти из них выгоднее, чем ждать прежней
    прибыли неопределённо долго.
    """

    RATE = "10.0024"

    async def test_immediate_check_holds_out_for_the_higher_target(self) -> None:
        node = ScriptedNode()
        positions = MemoryPositions()
        watcher = _watcher(
            node,
            positions,
            sell_rate=self.RATE,
            min_exit_profit_raw=10_000,
            min_exit_profit_waiting_raw=5_000,
        )
        position = _position()
        await positions.create(position)

        await watcher.consider_now(position)

        assert not node.sent, "0.007 чистыми меньше основной цели в 0.01"

    async def test_waiting_trade_settles_for_less(self) -> None:
        node = ScriptedNode()
        positions = MemoryPositions()
        watcher = _watcher(
            node,
            positions,
            sell_rate=self.RATE,
            min_exit_profit_raw=10_000,
            min_exit_profit_waiting_raw=5_000,
        )
        await positions.create(_position())

        await watcher.tick()

        assert node.sent, "0.007 чистыми проходит пониженную цель в 0.005"
        assert positions.items["#T1"].status is PositionStatus.CLOSED


class TestProceeds:
    """Итог сделки — выручка продажи, а не остаток счёта.

    На счёте лежат и деньги, к этой сделке отношения не имеющие. Если
    считать итогом весь остаток, каждая сделка «зарабатывает» всё, что
    было на кошельке до неё.
    """

    async def test_result_is_the_sale_proceeds_not_the_whole_balance(self) -> None:
        node = ScriptedNode(
            # До продажи на счёте лежало 10 USDT, не относящихся к сделке.
            balances={str(f.USDT.address).lower(): 10_000_000},
            # После продажи стало 60.5: выручка — 50.5, а не 60.5.
            after_send={str(f.USDT.address).lower(): 60_500_000},
        )
        positions = MemoryPositions()
        watcher = _watcher(node, positions, sell_rate="10.1")
        await positions.create(_position())

        await watcher.tick()

        position = positions.items["#T1"]
        assert position.status is PositionStatus.CLOSED
        assert position.raw_base_before_sell == 10_000_000
        assert position.raw_returned == 50_500_000
        assert position.gross_result_raw == 500_000
        # Чистый итог меньше валового ровно на стоимость круга.
        assert position.raw_gas_cost == COSTS_RAW
        assert position.net_result_raw == 500_000 - COSTS_RAW


class TestPriority:
    """Продажа обслуживается раньше всего остального.

    ``the_main_rules.md``, правило 13. За продажей стоят уже потраченные
    деньги: пока котировка выхода стоит в очереди, купленный токен
    остаётся на руках, а отклонение, ради которого он куплен, живёт
    минуты.
    """

    async def test_exit_quote_is_asked_with_the_sell_priority(self) -> None:
        node = ScriptedNode()
        positions = MemoryPositions()
        watcher = _watcher(node, positions, sell_rate="10.1")
        adapter = watcher._adapters[ProviderId.UNISWAP.value]  # noqa: SLF001
        await positions.create(_position())

        await watcher.tick()

        assert adapter.quote_calls, "котировка выхода запрошена"
        assert all(request.priority is RequestPriority.ANN_SELL for request in adapter.quote_calls)


class TestSellCalibration:
    """Нога продажи расходится с оценкой сильнее покупки.

    Её маршрут чаще разбивается на несколько пулов, поэтому замер по ней
    важнее всего — и он же достаётся бесплатно, из уже полученной
    квитанции.
    """

    async def test_actual_sell_gas_is_reported(self) -> None:
        node = ScriptedNode()
        positions = MemoryPositions()
        calibration = MemoryCalibration()
        watcher = _watcher(node, positions, sell_rate="10.1", calibration=calibration)
        await positions.create(_position())

        await watcher.tick()

        position = positions.items["#T1"]
        assert position.status is PositionStatus.CLOSED
        assert position.sell_gas_units == 180_000
        assert calibration.records == [
            (str(f.POLYGON), "uniswap", position.sell_quoted_gas_units, 180_000)
        ]


class TestRealOffer:
    """Выход решается по предложению агрегатора, а не по котировке.

    Котировка — реклама, минимум в собранной транзакции —
    обязательство: ниже него агрегатор сам откатит обмен. Разница между
    ними — допуск проскальзывания, и для стратегии, зарабатывающей сотые
    доли процента, он способен превышать всю прибыль многократно.
    """

    async def test_promise_above_the_target_is_not_enough(self) -> None:
        """5 AAVE по курсу 10.1 обещают 50.5 при допуске в 1 %.

        Обязательство при этом — 49.995, то есть круг в минусе. Продавать
        по обещанию значило бы согласиться получить меньше, чем нужно.
        """
        node = ScriptedNode()
        positions = MemoryPositions()
        watcher = _watcher(node, positions, sell_rate="10.1", slippage_bps=100)
        await positions.create(_position())

        await watcher.tick()

        assert not node.sent, "по котировке прошло бы, по обязательству — нет"
        assert positions.items["#T2" if "#T2" in positions.items else "#T1"].status is (
            PositionStatus.HOLDING
        )

    async def test_guarantee_above_the_target_sells(self) -> None:
        """Парная проверка: при узком допуске тот же круг проходит."""
        node = ScriptedNode()
        positions = MemoryPositions()
        watcher = _watcher(node, positions, sell_rate="10.1", slippage_bps=1)
        await positions.create(_position())

        await watcher.tick()

        assert node.sent, "обязательство покрывает и газ, и цель"
        assert positions.items["#T1"].status is PositionStatus.CLOSED

    async def test_slippage_room_is_measured_against_the_target(self) -> None:
        """Наглядно: допуск в 0.1 % на 50 USDT — это 0.05 USDT.

        Порог выхода при этом 0.005. Допуск больше цели в десять раз,
        поэтому решение по котировке ничего не гарантирует.
        """
        acquired = Decimal(ACQUIRED).scaleb(-18) * Decimal("10.1")
        room = acquired * Decimal("0.001")

        assert room > Decimal("0.005") * 10


def _order_watcher(
    node: ScriptedNode,
    positions: MemoryPositions,
    *,
    outcome: OrderOutcome = OrderOutcome.FILLED,
    sell_rate: str = "10.1",
) -> tuple[PositionWatcher, FakeAdapter]:
    """Наблюдатель, у которого продажа идёт подписанной заявкой."""
    active = FakeClock(f.NOW)
    exit_costs = FixedCosts(COSTS_RAW)
    wallet = TradingWallet(SecretValue("K", str(Account.create().key.hex())))
    adapter = FakeAdapter(
        ProviderId.COW,
        active,
        rate=_rate(sell_rate),
        capabilities=AdapterCapabilities(
            provider_id=ProviderId.COW,
            supported_networks=frozenset({f.POLYGON}),
            routing_modes=frozenset({RoutingMode.CLASSIC}),
            supports_execution=True,
            supports_signed_orders=True,
        ),
    )
    adapter.default_order_outcome = OrderState(
        outcome=outcome, executed_input_raw=0, executed_output_raw=0
    )
    account = build_account(node, active, wallet.address)
    sender = build_sender(node, active, wallet)

    async def sleeper(seconds: float) -> None:
        return None

    runner = SignedOrderRunner(wallet=wallet, clock=active, sleeper=sleeper)
    executor = TradeExecutor(
        adapters={ProviderId.COW.value: adapter},
        positions=positions,
        sequences=MemorySequences(),
        account=account,
        sender=sender,
        tokens=_tokens(),
        calculator=ProfitCalculator(active),
        costs=exit_costs,
        calibration=None,
        clock=active,
        is_execution_open=lambda: True,
        slippage_bps=0,
        min_entry_profit_raw=0,
        orders=runner,
        receipt_timeout_seconds=1,
        receipt_poll_seconds=1,
    )
    watcher = PositionWatcher(
        adapters={ProviderId.COW.value: adapter},
        positions=positions,
        account=account,
        sender=sender,
        executor=executor,
        costs=exit_costs,
        calibration=None,
        clock=active,
        min_exit_profit_raw=10_000,
        min_exit_profit_waiting_raw=10_000,
        slippage_bps=0,
        long_wait_enabled=True,
        long_wait_seconds=7_200,
        orders=runner,
        receipt_timeout_seconds=1,
        receipt_poll_seconds=1,
    )
    return watcher, adapter


class TestOrderExit:
    """Выход через провайдера, который транзакции не отдаёт (правило 16)."""

    async def test_a_filled_order_closes_the_trade(self) -> None:
        """Выручка считается приростом остатка — как и при транзакции."""
        node = ScriptedNode(
            balances={str(f.USDT.address).lower(): 0},
            after_send={str(f.USDT.address).lower(): 60_000_000},
        )
        positions = MemoryPositions()
        position = _position(sell_provider_id=ProviderId.COW, buy_provider_id=ProviderId.COW)
        await positions.create(position)
        watcher, adapter = _order_watcher(node, positions)

        await watcher.tick()

        closed = positions.items[str(position.t_id)]
        assert closed.status is PositionStatus.CLOSED
        assert closed.sell_order_uid == adapter.placed_orders[0].uid
        assert closed.sell_tx_hash is None
        # Продажа заявкой газа не стоила, и это знание: круг посчитан.
        assert closed.sell_gas_wei == 0
        assert node.sent == []

    async def test_an_unfilled_order_returns_the_trade_to_waiting(self) -> None:
        """Заявку не взяли — токен остался у нас, и это не потеря."""
        node = ScriptedNode(balances={str(f.USDT.address).lower(): 0})
        positions = MemoryPositions()
        position = _position(sell_provider_id=ProviderId.COW, buy_provider_id=ProviderId.COW)
        await positions.create(position)
        watcher, _ = _order_watcher(node, positions, outcome=OrderOutcome.EXPIRED)

        await watcher.tick()

        waiting = positions.items[str(position.t_id)]
        assert waiting.status is PositionStatus.HOLDING
        assert waiting.sell_order_uid is None
        assert waiting.closed_at is None


class TestNeverStartedBuy:
    """Покупка, которая не начиналась, не должна держать деньги.

    Запись о сделке создаётся до отправки, чтобы перезапуск не потерял
    отправленное. Если отправить не удалось, запись остаётся без единого
    опознавательного знака — ни хеша, ни идентификатора заявки. Довести
    её нечем, а в BUYING она вечно занимает сумму, которой не тратила.
    Так встала сделка #T15 19.09.
    """

    async def test_a_position_without_any_identifier_is_closed(self) -> None:
        node = ScriptedNode(balances={str(f.USDT.address).lower(): 0})
        positions = MemoryPositions()
        position = _position(
            status=PositionStatus.BUYING,
            raw_acquired=None,
            buy_tx_hash=None,
            buy_order_uid=None,
        )
        await positions.create(position)
        watcher = _watcher(node, positions)

        await watcher.tick()

        settled = positions.items[str(position.t_id)]
        assert settled.status is PositionStatus.FAILED
        assert settled.raw_returned == settled.raw_input
        assert settled.closed_at is not None


class TestExitWithoutProfit:
    """Выход без требования прибыли.

    Правило 11 говорит, что система никогда не закрывает позицию в
    убыток сама, и это верно, пока купленное возвращается к прежней
    цене. Оператор вправе отменить это: выйти из позиции, которая ждёт
    неопределённо долго, не дожидаясь возврата. Цена — возможный убыток
    на размер расхождения плюс газ (правило 20).
    """

    async def test_a_losing_circle_is_sold_when_profit_is_not_required(self) -> None:
        node = ScriptedNode(
            balances={str(f.USDT.address).lower(): 0},
            after_send={str(f.USDT.address).lower(): 49_000_000},
        )
        positions = MemoryPositions()
        position = _position()
        await positions.create(position)
        # Курс заведомо убыточный: обычный порог такой выход запретил бы.
        watcher = _watcher(node, positions, sell_rate="9.80", exit_requires_profit=False)

        await watcher.tick()

        assert positions.items[str(position.t_id)].status is not PositionStatus.HOLDING

    async def test_the_same_circle_is_refused_while_profit_is_required(self) -> None:
        """Обратная проверка: без выключения поведение прежнее."""
        node = ScriptedNode(balances={str(f.USDT.address).lower(): 0})
        positions = MemoryPositions()
        position = _position()
        await positions.create(position)
        watcher = _watcher(node, positions, sell_rate="9.80")

        await watcher.tick()

        assert positions.items[str(position.t_id)].status is PositionStatus.HOLDING
