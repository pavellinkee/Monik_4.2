"""Исполнение ноги подписанной заявкой.

Вторая форма исполнения (``the_main_rules.md``, правило 16). Отличается
от транзакции тремя вещами, и все три собраны здесь, а не размазаны по
исполнителю и наблюдателю:

1. **подпись вместо отправки.** Заявка подписывается кошельком и
   отдаётся книге провайдера; своей транзакции у нас не возникает, и
   газа мы не платим;
2. **опрос вместо квитанции.** Судьба заявки узнаётся у книги. Пока
   заявка открыта, исход неизвестен — и это не сбой;
3. **истечение срока — штатный исход.** Заявка может не исполниться
   вовсе. Для покупки это безопасно: деньги остались на счету, позиция
   просто не открылась.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

from monik.domain.enums.operations import OperationType
from monik.domain.models.execution import (
    ExecutionOffer,
    OrderState,
    PlacedOrder,
    SignedOrderPlan,
    SwapTransaction,
)
from monik.infrastructure.providers.contract import AggregatorAdapter
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger, log_fields
from monik.services.trading.wallet import TradingWallet

__all__ = [
    "SignedOrderRunner",
    "executes_with_orders",
    "leg_gas_units",
    "leg_slippage_bps",
]

_LOGGER = get_logger("services.trading.orders")

#: Пауза между опросами книги.
DEFAULT_POLL_SECONDS = 3

#: Предельное ожидание исхода. Дольше срока жизни самой заявки ждать
#: незачем: после него заявка недействительна.
DEFAULT_TIMEOUT_SECONDS = 300


def executes_with_orders(adapter: AggregatorAdapter) -> bool:
    """Исполняет ли провайдер заявками, а не транзакциями.

    Форма исполнения — свойство провайдера, а не выбор вызывающего:
    спрашивать надо у адаптера, а не проверять его идентификатор
    (``CLAUDE.md`` §7).
    """
    return adapter.capabilities.supports_signed_orders


def leg_slippage_bps(adapter: AggregatorAdapter, *, transaction: int, order: int) -> int:
    """Какой допуск уместен этой ноге.

    Различие принадлежит форме исполнения, а не провайдеру. Транзакции
    допуск нужен: между сборкой и включением в блок пул шевелится, и без
    запаса обмен откатывается, теряя газ. Подписанному намерению он не
    нужен: если решатель не готов отдать названную сумму, он просто не
    возьмёт заявку — это несостоявшаяся сделка, а не убыток.

    Уступка вычитается по разу на ногу, поэтому на круге удваивается.
    При пороге в один базисный пункт уступка того же размера на обеих
    ногах делает прибыльный круг невозможным по построению: замер 19.09
    дал требуемое расхождение около 0,029 % там, где рынок давал 0,015 %.
    """
    return order if executes_with_orders(adapter) else transaction


def leg_gas_units(offer: ExecutionOffer) -> int:
    """Сколько газа потребует нога.

    У заявки — ноль, и это знание, а не пробел: своей транзакции не
    возникает, а стоимость исполнения уже удержана внутри названной
    суммы получения. Прибавлять к ней газ значило бы посчитать его
    дважды и объявить прибыльный круг убыточным.
    """
    return offer.gas_limit if isinstance(offer, SwapTransaction) else 0


class SignedOrderRunner:
    """Подписывает, размещает и доводит заявку до исхода."""

    __slots__ = ("_clock", "_poll", "_sleeper", "_timeout", "_wallet")

    def __init__(
        self,
        *,
        wallet: TradingWallet,
        clock: Clock,
        sleeper: Callable[[float], Awaitable[None]],
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._wallet = wallet
        self._clock = clock
        self._sleeper = sleeper
        self._poll = timedelta(seconds=poll_seconds)
        self._timeout = timedelta(seconds=timeout_seconds)

    async def place(self, adapter: AggregatorAdapter, plan: SignedOrderPlan) -> PlacedOrder:
        """Подписать заявку и отдать её книге.

        Подпись выполняет кошелёк: ключ не покидает его и сюда не
        попадает.
        """
        signature = self._wallet.sign_typed_data(plan.typed_data)
        placed = await adapter.place_order(plan, signature)
        _LOGGER.info(
            "order placed",
            extra=log_fields(
                provider=placed.provider_id.value,
                uid=placed.uid,
                minimum=str(plan.min_output_raw),
            ),
        )
        return placed

    async def wait(
        self,
        adapter: AggregatorAdapter,
        placed: PlacedOrder,
        *,
        operation: OperationType,
    ) -> OrderState | None:
        """Дождаться исхода заявки.

        ``None`` означает «исход ещё не известен»: заявка осталась
        открытой дольше, чем мы согласны ждать. Считать её неудачной
        нельзя — решатель может взять её и позже, вплоть до истечения
        срока.
        """
        deadline = self._clock.now() + self._timeout
        while True:
            state = await adapter.order_state(placed.network_id, placed.uid, operation=operation)
            if state.is_final:
                _LOGGER.info(
                    "order settled",
                    extra=log_fields(
                        provider=placed.provider_id.value,
                        uid=placed.uid,
                        outcome=state.outcome.value,
                        received=str(state.executed_output_raw),
                    ),
                )
                return state
            if self._clock.now() >= deadline:
                _LOGGER.info(
                    "order still open",
                    extra=log_fields(provider=placed.provider_id.value, uid=placed.uid),
                )
                return None
            await self._sleeper(self._poll.total_seconds())
