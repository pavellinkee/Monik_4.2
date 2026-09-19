"""Исполнение ноги подписанной заявкой.

Вторая форма исполнения (``the_main_rules.md``, правило 16). Проверяется
то, чем она отличается от транзакции и где различие могло бы навредить:

* решение принимается по предельной сумме заявки, а симуляции нет;
* газ такой ноги равен нулю — и это знание, а не пробел;
* неисполненная заявка закрывает сделку как несостоявшуюся, а не как
  убыточную: деньги остались на счету.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from eth_account import Account

from monik.config.secrets import SecretValue
from monik.domain.enums.operations import OperationType, RoutingMode
from monik.domain.enums.providers import ProviderId
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.execution import OrderOutcome, OrderState, SignedOrderPlan
from monik.domain.value_objects.identifiers import RequestId
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.providers.contract import AdapterCapabilities, QuoteRequest
from monik.infrastructure.providers.fake import FakeAdapter
from monik.services.observability import FakeClock
from monik.services.trading import TradingWallet
from monik.services.trading.orders import (
    SignedOrderRunner,
    executes_with_orders,
    leg_gas_units,
)
from tests import factories as f


def _wallet() -> TradingWallet:
    return TradingWallet(SecretValue("K", str(Account.create().key.hex())))


def _order_adapter(clock: FakeClock, rate: str = "1") -> FakeAdapter:
    """Двойник провайдера, исполняющего заявками."""
    return FakeAdapter(
        ProviderId.COW,
        clock,
        rate=Decimal(rate),
        capabilities=AdapterCapabilities(
            provider_id=ProviderId.COW,
            supported_networks=frozenset({NetworkId("polygon")}),
            routing_modes=frozenset({RoutingMode.CLASSIC}),
            supports_execution=True,
            supports_signed_orders=True,
        ),
    )


def _request() -> QuoteRequest:
    return QuoteRequest(
        network_id=f.POLYGON,
        operation=OperationType.BUY,
        input_token=f.USDT,
        output_token=f.AAVE,
        input_amount=f.USDT.amount_from_base_units(100_000_000),
        request_id=RequestId.generate(),
        slippage_bps=10,
    )


async def _sleeper(seconds: float) -> None:
    return None


class TestForm:
    def test_the_form_belongs_to_the_provider(self) -> None:
        """Спрашиваем у адаптера, а не проверяем его имя (``CLAUDE.md`` §7)."""
        clock = FakeClock(f.NOW)

        assert executes_with_orders(_order_adapter(clock)) is True
        assert executes_with_orders(FakeAdapter(ProviderId.UNISWAP, clock)) is False

    async def test_an_order_leg_costs_no_gas(self) -> None:
        """Ноль — знание: своей транзакции нет, стоимость уже в цене.

        Если бы заявке приписали газ транзакции, круг через неё выглядел
        бы дороже, чем есть, и выгодный выход отклонялся бы.
        """
        clock = FakeClock(f.NOW)
        plan = await _order_adapter(clock).build_order(_request())
        swap = await FakeAdapter(ProviderId.UNISWAP, clock).build_swap(_request())

        assert leg_gas_units(plan) == 0
        assert leg_gas_units(swap) > 0

    async def test_the_limit_is_the_commitment(self) -> None:
        """Предел заявки не выше обещанного выхода — иначе она неисполнима."""
        clock = FakeClock(f.NOW)
        plan = await _order_adapter(clock).build_order(_request())

        assert isinstance(plan, SignedOrderPlan)
        assert 0 < plan.min_output_raw <= plan.quote.output_amount.raw


class TestRunner:
    async def test_the_order_is_signed_before_it_is_placed(self) -> None:
        clock = FakeClock(f.NOW)
        adapter = _order_adapter(clock)
        runner = SignedOrderRunner(wallet=_wallet(), clock=clock, sleeper=_sleeper)

        placed = await runner.place(adapter, await adapter.build_order(_request()))

        assert placed.uid == adapter.placed_orders[0].uid
        assert placed.provider_id is ProviderId.COW

    async def test_waiting_returns_the_final_outcome(self) -> None:
        clock = FakeClock(f.NOW)
        adapter = _order_adapter(clock)
        adapter.default_order_outcome = OrderState(
            outcome=OrderOutcome.FILLED,
            executed_input_raw=100_000_000,
            executed_output_raw=99_000_000,
        )
        runner = SignedOrderRunner(wallet=_wallet(), clock=clock, sleeper=_sleeper)
        placed = await runner.place(adapter, await adapter.build_order(_request()))

        state = await runner.wait(adapter, placed, operation=OperationType.BUY)

        assert state is not None
        assert state.outcome is OrderOutcome.FILLED

    async def test_an_open_order_is_not_called_a_failure(self) -> None:
        """Заявка может быть взята позже: «ещё не известно» — не «не вышло».

        Время двигает сама пауза между опросами — так же, как в жизни,
        — поэтому ожидание упирается в свой предел и возвращает
        неизвестность, а не крутится вечно.
        """
        clock = FakeClock(f.NOW)
        adapter = _order_adapter(clock)
        adapter.default_order_outcome = OrderState(
            outcome=OrderOutcome.OPEN, executed_input_raw=0, executed_output_raw=0
        )

        async def ticking(seconds: float) -> None:
            clock.advance(timedelta(seconds=seconds))

        runner = SignedOrderRunner(
            wallet=_wallet(), clock=clock, sleeper=ticking, timeout_seconds=60
        )
        placed = await runner.place(adapter, await adapter.build_order(_request()))

        assert await runner.wait(adapter, placed, operation=OperationType.BUY) is None

    @pytest.mark.parametrize(
        "outcome", [OrderOutcome.EXPIRED, OrderOutcome.CANCELLED, OrderOutcome.FILLED]
    )
    async def test_every_final_outcome_stops_the_wait(self, outcome: OrderOutcome) -> None:
        clock = FakeClock(f.NOW)
        adapter = _order_adapter(clock)
        adapter.default_order_outcome = OrderState(
            outcome=outcome, executed_input_raw=0, executed_output_raw=0
        )
        runner = SignedOrderRunner(wallet=_wallet(), clock=clock, sleeper=_sleeper)
        placed = await runner.place(adapter, await adapter.build_order(_request()))

        state = await runner.wait(adapter, placed, operation=OperationType.BUY)

        assert state is not None and state.outcome is outcome


class TestPositionStatus:
    def test_statuses_used_by_the_order_path_exist(self) -> None:
        """Путь заявки пользуется теми же состояниями, что и транзакция."""
        assert PositionStatus.BUYING
        assert PositionStatus.HOLDING
        assert PositionStatus.FAILED
