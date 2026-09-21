"""Ведение открытых сделок: ожидание и продажа.

Наблюдатель — вторая половина подсистемы исполнения. Он не ищет
возможностей и не открывает сделок; он доводит до конца уже открытые.
Разделение не формальное: открытие можно запретить, оставив ведение
работать, — иначе выключение торговли бросало бы купленные токены.

Правило выхода задано оператором: продавать, когда круг даёт **чистую
прибыль в базовом токене** не меньше заданной. Не процент — деньги.

Чистая — значит за вычетом газа. Газ платится в обе стороны и от суммы
сделки не зависит, поэтому на малых суммах он и решает исход: круг,
выигравший на цене меньше, чем стоила пара транзакций, приносит убыток.
Сравнивать с порогом одну лишь разницу токенов означало бы закрывать
сделки в минус, считая их прибыльными.

Порогов два. Сразу после покупки действует основной: шанс выйти в плюс
наивысший именно тогда, и соглашаться на меньшее незачем. Если момент
упущен и сделка ушла в ожидание, действует пониженный порог — деньги
заперты, и выйти из них выгоднее, чем ждать прежней прибыли
(решение оператора).

Различаются и продавцы. Немедленная проверка остаётся на назначенном:
маршрут только что выбран проходом. Отложенная опрашивает всех
включённых агрегаторов и берёт лучшее предложение — назначение к тому
времени устарело (``the_main_rules.md``, правило 21).

Система никогда не закрывает позицию в убыток сама. Если ожидание
затянулось, она **сообщает оператору** и продолжает ждать: решение
принимает человек (``the_main_rules.md``, правило 11).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.domain.enums.resources import RequestPriority
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.execution import (
    ExecutionPlan,
    OrderOutcome,
    SignedOrderPlan,
    SwapTransaction,
)
from monik.domain.models.position import Position
from monik.domain.value_objects.identifiers import RequestId
from monik.infrastructure.providers.contract import AggregatorAdapter, QuoteRequest
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger, log_fields
from monik.services.trading.chain import ChainAccount
from monik.services.trading.executor import TradeExecutor
from monik.services.trading.exits import (
    ExitCandidate,
    ExitQuote,
    projected_output,
    rank_exits,
)
from monik.services.trading.orders import (
    SignedOrderRunner,
    executes_with_orders,
    leg_gas_units,
    leg_slippage_bps,
)
from monik.services.trading.ports import ExecutionCosts, GasCalibrationRecorder, PositionStore
from monik.services.trading.reporting import OperatorNotifier, closing_text
from monik.services.trading.sender import SentTransaction, TransactionSender

__all__ = ["LongWaitNotice", "PositionWatcher"]

_LOGGER = get_logger("services.trading.watcher")

#: Приоритет всех обращений продажи — наивысший в системе. За продажей
#: стоят уже потраченные деньги (``the_main_rules.md``, правило 13).
_PRIORITY = RequestPriority.ANN_SELL


class LongWaitNotice:
    """Что сообщить оператору о затянувшейся сделке."""

    __slots__ = ("position", "waited")

    def __init__(self, position: Position, waited: timedelta) -> None:
        self.position = position
        self.waited = waited

    def describe(self) -> str:
        """Текст для оператора."""
        hours = self.waited.total_seconds() / 3600
        return (
            f"{self.position.t_id}: {self.position.input_amount} "
            f"{self.position.base_token.symbol} → {self.position.target_token.symbol} "
            f"ждёт выхода {hours:.1f} ч"
        )


class PositionWatcher:
    """Доводит открытые сделки до конца."""

    def __init__(
        self,
        *,
        adapters: dict[str, AggregatorAdapter],
        positions: PositionStore,
        account: ChainAccount,
        sender: TransactionSender,
        executor: TradeExecutor,
        costs: ExecutionCosts,
        calibration: GasCalibrationRecorder | None,
        clock: Clock,
        min_exit_profit_raw: int,
        min_exit_profit_waiting_raw: int,
        slippage_bps: int,
        long_wait_enabled: bool,
        long_wait_seconds: int,
        exit_requires_profit: bool = True,
        order_slippage_bps: int = 0,
        orders: SignedOrderRunner | None = None,
        notify: OperatorNotifier | None = None,
        receipt_timeout_seconds: int = 120,
        receipt_poll_seconds: int = 2,
    ) -> None:
        self._adapters = dict(adapters)
        self._positions = positions
        self._account = account
        self._sender = sender
        self._executor = executor
        self._costs = costs
        self._calibration = calibration
        self._clock = clock
        self._min_exit_profit_raw = min_exit_profit_raw
        self._min_exit_profit_waiting_raw = min_exit_profit_waiting_raw
        self._slippage_bps = slippage_bps
        self._long_wait_enabled = long_wait_enabled
        self._long_wait = timedelta(seconds=long_wait_seconds)
        #: Требовать ли прибыли для выхода. Выключение отменяет и
        #: пороги, и гарантию «не закрывать в убыток».
        self._exit_requires_profit = exit_requires_profit
        #: Допуск ноги, исполняемой намерением: у неё промах бесплатен.
        self._order_slippage_bps = order_slippage_bps
        self._orders = orders
        #: Кому сообщить об итоге завершённой сделки. Необязателен:
        #: подсистема исполнения работает и без канала уведомлений.
        self._notify = notify
        self._receipt_timeout = timedelta(seconds=receipt_timeout_seconds)
        self._receipt_poll = timedelta(seconds=receipt_poll_seconds)

    async def tick(self) -> tuple[LongWaitNotice, ...]:
        """Один проход по открытым сделкам.

        Возвращает то, о чём стоит сообщить оператору. Сам наблюдатель
        ничего не отправляет: доставка — дело системы уведомлений.
        """
        notices: list[LongWaitNotice] = []
        for position in await self._positions.open_positions():
            try:
                notice = await self._advance(position)
            except Exception as error:  # noqa: BLE001 - сделка не должна ронять остальные
                _LOGGER.error(
                    "position could not be advanced",
                    extra=log_fields(
                        t_id=str(position.t_id), error=type(error).__name__, detail=str(error)
                    ),
                )
                continue
            if notice is not None:
                notices.append(notice)
        return tuple(notices)

    async def consider_now(self, position: Position) -> None:
        """Проверить выход у одной сделки немедленно.

        Вызывается исполнителем сразу после удачной покупки. Отдельный
        вход, а не полный такт: остальные сделки в этот момент трогать
        незачем, и обход всего списка только задержал бы проверку той,
        ради которой он затеян.
        """
        try:
            await self._consider_exit(position, immediate=True)
        except Exception as error:  # noqa: BLE001 - сделка уже открыта, её ведёт наблюдатель
            _LOGGER.error(
                "immediate exit check failed",
                extra=log_fields(
                    t_id=str(position.t_id), error=type(error).__name__, detail=str(error)
                ),
            )

    async def _advance(self, position: Position) -> LongWaitNotice | None:
        if position.status is PositionStatus.BUYING:
            await self._settle_pending_buy(position)
            return None
        if position.status is PositionStatus.SELLING:
            await self._settle_pending_sell(position)
            return None
        return await self._consider_exit(position, immediate=False)

    async def _settle_pending_buy(self, position: Position) -> None:
        """Подобрать покупку, исход которой ещё не известен."""
        if position.buy_order_uid is not None:
            await self._settle_pending_buy_order(position)
            return
        if position.buy_tx_hash is None:
            # Ни транзакции, ни заявки: покупка не начиналась вовсе.
            # Запись о сделке создаётся **до** отправки, чтобы перезапуск
            # не потерял отправленное, — и если отправить не удалось, а
            # процесс на этом прервался, запись остаётся без единого
            # опознавательного знака. Довести её нечем, а держать в
            # BUYING нельзя: она вечно занимает сумму, которой не
            # тратила. Закрываем как несостоявшуюся — деньги на месте.
            _LOGGER.warning(
                "trade closed without a buy: nothing was ever sent",
                extra=log_fields(t_id=str(position.t_id)),
            )
            await self._executor.settle_buy(position, succeeded=False)
            return
        # Подбор покупки — работа покупки, а не продажи: приоритет тот же,
        # с которым эта транзакция отправлялась.
        receipt = await self._account.receipt(
            position.network_id, position.buy_tx_hash, priority=RequestPriority.ANN_BUY
        )
        if receipt is None:
            return
        await self._executor.settle_buy(position, succeeded=receipt.succeeded, receipt=receipt)

    async def _settle_pending_buy_order(self, position: Position) -> None:
        """Подобрать покупку, размещённую заявкой.

        Заявка могла исполниться уже после того, как мы перестали ждать,
        или после перезапуска. Пока она открыта, исход неизвестен — и
        это не сбой: решатель вправе взять её вплоть до истечения срока.
        """
        adapter = self._adapters.get(position.buy_provider_id.value)
        if adapter is None or position.buy_order_uid is None:
            return
        state = await adapter.order_state(
            position.network_id, position.buy_order_uid, operation=OperationType.BUY
        )
        if not state.is_final:
            return
        await self._executor.settle_buy(position, succeeded=state.outcome is OrderOutcome.FILLED)

    async def _settle_pending_sell(self, position: Position) -> None:
        """Подобрать продажу, исход которой ещё не известен."""
        if position.sell_order_uid is not None:
            await self._settle_pending_sell_order(position)
            return
        if position.sell_tx_hash is None:
            return
        receipt = await self._account.receipt(
            position.network_id, position.sell_tx_hash, priority=_PRIORITY
        )
        if receipt is None:
            return
        now = self._clock.now()
        # Откатившаяся попытка тоже стоила газа, и этот расход относится
        # к сделке: считать его нулём значило бы объявить неудачу
        # бесплатной.
        spent_on_selling = _total_wei(position.sell_gas_wei, receipt.gas_cost_wei)
        sold_units = _total_wei(position.sell_gas_units, receipt.gas_used)
        await self._record_calibration(position, receipt.gas_used)
        if not receipt.succeeded:
            # Продажа откатилась — токен остался у нас. Возвращаемся к
            # ожиданию: это не потеря, а неудачная попытка выхода.
            await self._positions.update(
                position.model_copy(
                    update={
                        "status": PositionStatus.HOLDING,
                        "sell_tx_hash": None,
                        "sell_gas_wei": spent_on_selling,
                        "sell_gas_units": sold_units,
                        "updated_at": now,
                    }
                )
            )
            return
        await self._close_after_sale(
            position, spent_on_selling=spent_on_selling, sold_units=sold_units
        )

    async def _settle_pending_sell_order(self, position: Position) -> None:
        """Подобрать продажу, размещённую заявкой."""
        adapter = self._adapters.get(position.sell_provider_id.value)
        if adapter is None or position.sell_order_uid is None:
            return
        state = await adapter.order_state(
            position.network_id, position.sell_order_uid, operation=OperationType.SELL
        )
        if not state.is_final:
            return
        await self._settle_order_sell(position, state.outcome)

    async def _close_after_sale(
        self, position: Position, *, spent_on_selling: int | None, sold_units: int | None
    ) -> None:
        """Закрыть сделку по факту продажи.

        Общее для обеих форм исполнения: выручка считается приростом
        остатка, а не берётся из обещания, и потому не зависит от того,
        транзакцией шла продажа или подписанной заявкой.
        """
        now = self._clock.now()
        if position.raw_base_before_sell is None:
            # Так может выглядеть только сделка, записанная версией без
            # этого поля: с тех пор его ставит сама отправка продажи.
            # Выручку в этом случае считать не из чего, и придумывать её
            # нельзя — итог объявляется неизвестным, а сделка всё равно
            # закрывается: токены проданы, держать запись открытой не за
            # что.
            _LOGGER.error(
                "trade closed with an unknown result: the balance before the sale was not recorded",
                extra=log_fields(t_id=str(position.t_id), tx=position.sell_tx_hash or ""),
            )
            unknown = position.model_copy(
                update={
                    "status": PositionStatus.CLOSED,
                    "raw_returned": position.raw_input,
                    "raw_gas_cost": None,
                    "sell_gas_wei": spent_on_selling,
                    "sell_gas_units": sold_units,
                    "updated_at": now,
                    "closed_at": now,
                }
            )
            await self._positions.update(unknown)
            await self._announce_closed(unknown)
            return
        after = await self._account.token_balance(position.base_token, priority=_PRIORITY)
        # Выручка — это прирост остатка, а не сам остаток: на счёте лежат
        # и деньги, к этой сделке отношения не имеющие.
        returned = after.raw - position.raw_base_before_sell
        gas_cost = await self._round_trip_cost_raw(
            position, buy_wei=position.buy_gas_wei, sell_wei=spent_on_selling
        )
        closed = position.model_copy(
            update={
                "status": PositionStatus.CLOSED,
                "raw_returned": max(returned, 0),
                "sell_gas_wei": spent_on_selling,
                "sell_gas_units": sold_units,
                "raw_gas_cost": gas_cost,
                "updated_at": now,
                "closed_at": now,
            }
        )
        await self._positions.update(closed)
        _LOGGER.info(
            "trade closed",
            extra=log_fields(
                t_id=str(position.t_id),
                tx=position.sell_tx_hash or position.sell_order_uid or "",
                returned=str(
                    Decimal(closed.raw_returned or 0).scaleb(-position.base_token.decimals)
                ),
                gross=_money(closed.gross_result),
                gas=_money(
                    None
                    if gas_cost is None
                    else Decimal(gas_cost).scaleb(-position.base_token.decimals)
                ),
                net=_money(closed.net_result),
            ),
        )
        await self._announce_closed(closed)

    async def preview_exit(self, t_id: str) -> ExitQuote | None:
        """Во что обойдётся выход из сделки прямо сейчас.

        Спрашивает оператор, а не подсистема, поэтому порог выхода здесь
        не применяется: вопрос не «выгодно ли», а «сколько дадут». Цена
        берётся лучшая среди всех включённых агрегаторов.

        ``None`` означает «показать нечего»: такой открытой сделки нет,
        она уже в продаже, или предложения не дал никто.
        """
        position = await self._holding(t_id)
        if position is None:
            return None
        for candidate in await self._exit_candidates(position):
            described = await self._describe_exit(position, candidate)
            if described is not None:
                return described[0]
        return None

    async def sell_at_market(self, t_id: str) -> ExitQuote | None:
        """Продать сделку по текущей цене по решению оператора.

        Порог выхода не применяется: правило 11 прямо оставляет этот
        случай человеку — система сама в убыток не закрывает, но
        оператор, увидев затянувшееся ожидание, вправе решить иначе.

        Цена спрашивается заново, а не берётся из показанной ранее:
        между подтверждением и продажей проходят секунды, и решение
        принимается по свежему обязательству.

        Если предложение лидера не исполнилось — транзакция откатилась
        бы, книга не приняла заявку, — берётся следующий по выгоде.
        Возвращается то, по чему сделка **действительно** ушла, и
        ``None``, если не ушла ни у кого: сообщить об успехе выхода,
        которого не было, хуже, чем сообщить о неудаче.
        """
        position = await self._holding(t_id)
        if position is None:
            return None
        for candidate in await self._exit_candidates(position):
            described = await self._describe_exit(position, candidate)
            if described is None:
                continue
            quote, plan = described
            _LOGGER.info(
                "manual exit requested by operator",
                extra=log_fields(
                    t_id=quote.t_id,
                    provider=quote.provider,
                    output=str(quote.output),
                    profit=str(quote.profit),
                ),
            )
            sold = await self._sell(
                position.model_copy(
                    update={
                        "sell_provider_id": candidate.provider_id,
                        "sell_quoted_gas_units": candidate.quote.estimated_gas_units,
                    }
                ),
                plan,
            )
            if sold:
                return quote
            _LOGGER.warning(
                "manual exit did not go through",
                extra=log_fields(t_id=quote.t_id, provider=quote.provider),
            )
        return None

    async def _holding(self, t_id: str) -> Position | None:
        """Открытая сделка, ожидающая продажи.

        Только ``HOLDING``: у сделки, уже ушедшей в покупку или продажу,
        распоряжаться нечем — её ведёт начатая операция.
        """
        for position in await self._positions.open_positions():
            if str(position.t_id) == t_id and position.status is PositionStatus.HOLDING:
                return position
        return None

    async def _exit_candidates(self, position: Position) -> tuple[ExitCandidate, ...]:
        """Все включённые агрегаторы по убыванию выгоды, без оглядки на порог."""
        if position.raw_acquired is None:
            return ()
        adapters = self._exit_adapters(position, immediate=False)
        if not adapters:
            return ()
        return await self._quote_exits(position, adapters, needed=None)

    async def _describe_exit(
        self, position: Position, candidate: ExitCandidate
    ) -> tuple[ExitQuote, ExecutionPlan] | None:
        """Обязательство одного агрегатора в понятном оператору виде.

        Отказ собрать предложение не отменяет остальных кандидатов:
        отвечать за весь выход один агрегатор не обязан.
        """
        try:
            plan = await self._build_offer(candidate.adapter, candidate.request)
        except Exception as error:  # noqa: BLE001 - отказ одного выхода не сбой сделки
            _LOGGER.info(
                "exit offer unavailable",
                extra=log_fields(
                    t_id=str(position.t_id),
                    provider=candidate.provider_id.value,
                    error=type(error).__name__,
                    detail=str(error),
                ),
            )
            return None
        costs = await self._exit_costs_raw(position, plan)
        decimals = position.base_token.decimals
        quote = ExitQuote(
            t_id=str(position.t_id),
            provider=candidate.provider_id.value,
            output=Decimal(plan.min_output_raw).scaleb(-decimals),
            profit=Decimal(
                position.profit_if_sold_for(plan.min_output_raw, raw_costs=costs or 0)
            ).scaleb(-decimals),
            symbol=str(position.base_token.symbol),
            costs_known=costs is not None,
        )
        return quote, plan

    async def _consider_exit(self, position: Position, *, immediate: bool) -> LongWaitNotice | None:
        """Проверить, выгодно ли продавать сейчас.

        Решение принимается по **реальному предложению** агрегатора, а
        не по котировке: по сумме, ниже которой он сам откатит обмен.
        Агрегатор — обменник, а не биржа, и обязательство у него своё,
        не равное рекламе.

        Кого спрашивать, решает положение сделки, а не техника: сразу
        после покупки — назначенного продавца, в ожидании — всех
        включённых (``the_main_rules.md``, правило 21).

        Порядок шагов выбран так, чтобы не тратить запросов впустую.
        Сначала берутся котировки — они служат дешёвым отсевом, потому
        что обязательство котировку не превышает: круг, не окупающийся
        даже по обещанию, не окупится и по минимуму. Предложение
        собирается только у тех, у кого есть шанс, и в порядке прогноза.
        """
        if position.raw_acquired is None:
            return None
        adapters = self._exit_adapters(position, immediate=immediate)
        if not adapters:
            return None
        # Ноль — это не «порог отключён», а «согласны на любой исход».
        # Отрицательные значения сюда не попадают: порог задан
        # положительным, и выключение заменяет его целиком.
        needed: int | None = (
            self._min_exit_profit_raw if immediate else self._min_exit_profit_waiting_raw
        )
        if not self._exit_requires_profit:
            needed = None
        candidates = await self._quote_exits(position, adapters, needed=needed)
        if not candidates:
            return None
        decimals = position.base_token.decimals
        #: Лучшее, что удалось посчитать **по обязательству**. Пока оно
        #: не посчитано, в журнал ожидания идёт прогноз лидера: своего
        #: значения выдавать за проверенное нельзя.
        observed: int | None = None
        observed_costs: int | None = None
        for candidate in candidates:
            # Кандидаты упорядочены по убыванию прогноза, поэтому первый
            # непроходной закрывает и весь остаток списка.
            if needed is not None and candidate.projected_profit < needed:
                break
            transaction = await self._build_offer(candidate.adapter, candidate.request)
            costs = await self._exit_costs_raw(position, transaction)
            if costs is None and needed is not None:
                # Стоимость круга неизвестна. Продавать вслепую нельзя:
                # неизвестный расход нулём не считается (``CLAUDE.md`` §12).
                #
                # Когда прибыль не требуется, неизвестная стоимость выходу
                # не мешает: она влияет на решение, а решение уже принято.
                return self._wait(
                    position, profit=candidate.projected_profit, needed=needed, costs=None
                )
            # Решение принимается по тому, что агрегатор **обязуется**
            # отдать, а не по тому, что обещает. Котировка — реклама,
            # минимум — обязательство: ниже него обмен откатится он сам.
            # Значит исполнение может выйти лучше расчёта, но не хуже.
            profit = position.profit_if_sold_for(transaction.min_output_raw, raw_costs=costs or 0)
            if needed is not None and profit < needed:
                # Обязательство оказалось хуже прогноза. Это не конец
                # проверки: следующий кандидат ещё может пройти, и его
                # прогноз проверяется на входе в виток.
                if observed is None or profit > observed:
                    observed, observed_costs = profit, costs
                continue
            _LOGGER.info(
                "exit offer verified",
                extra=log_fields(
                    t_id=str(position.t_id),
                    provider=candidate.provider_id.value,
                    surveyed=len(candidates),
                    promised=str(Decimal(candidate.quote.output_amount.raw).scaleb(-decimals)),
                    guaranteed=str(Decimal(transaction.min_output_raw).scaleb(-decimals)),
                    profit=str(Decimal(profit).scaleb(-decimals)),
                    # Прибыль может не требоваться вовсе — тогда порога нет,
                    # и писать вместо него ноль значило бы солгать в журнале.
                    needed=_money(None if needed is None else Decimal(needed).scaleb(-decimals)),
                    gas=_money(None if costs is None else Decimal(costs).scaleb(-decimals)),
                ),
            )
            sold = await self._sell(
                position.model_copy(
                    update={
                        # Продавец мог смениться: в ожидании выход
                        # достаётся тому, кто дал лучшее предложение, и
                        # доводить заявку до конца обязан он же.
                        "sell_provider_id": candidate.provider_id,
                        "sell_quoted_gas_units": candidate.quote.estimated_gas_units,
                    }
                ),
                transaction,
            )
            if sold:
                return None
            # Предложение не исполнилось: транзакция откатилась бы или
            # книга не приняла заявку. Это не конец выхода — следующий
            # кандидат ещё может сработать.
            _LOGGER.info(
                "exit offer did not go through",
                extra=log_fields(t_id=str(position.t_id), provider=candidate.provider_id.value),
            )
            if observed is None or profit > observed:
                observed, observed_costs = profit, costs
        if needed is None:  # pragma: no cover - без порога выход уже состоялся
            return None
        return self._wait(
            position,
            profit=candidates[0].projected_profit if observed is None else observed,
            needed=needed,
            costs=observed_costs,
        )

    def _exit_adapters(
        self, position: Position, *, immediate: bool
    ) -> tuple[tuple[ProviderId, AggregatorAdapter], ...]:
        """Кого спрашивать о выходе.

        Немедленная проверка остаётся на назначенном продавце: маршрут
        только что выбран проходом по свежим котировкам обеих ног, и
        опрос остальных потратил бы запросы на уже известное. Отложенная
        спрашивает всех включённых: назначение сделано минуты назад, а
        деньги заперты сейчас (``the_main_rules.md``, правило 21).
        """
        if immediate:
            adapter = self._adapters.get(position.sell_provider_id.value)
            return () if adapter is None else ((position.sell_provider_id, adapter),)
        return tuple(
            (ProviderId(code), adapter) for code, adapter in sorted(self._adapters.items())
        )

    def _exit_request(self, position: Position, adapter: AggregatorAdapter) -> QuoteRequest:
        """Запрос продажи всего купленного у одного агрегатора.

        Допуск принадлежит форме исполнения: у транзакции промах стоит
        газа, у подписанной заявки — ничего.
        """
        return QuoteRequest(
            network_id=position.network_id,
            operation=OperationType.SELL,
            input_token=position.target_token,
            output_token=position.base_token,
            input_amount=position.target_token.amount_from_base_units(position.raw_acquired or 0),
            request_id=RequestId.generate(),
            slippage_bps=leg_slippage_bps(
                adapter, transaction=self._slippage_bps, order=self._order_slippage_bps
            ),
            # Открытая сделка ждать не может: пока котировка выхода стоит
            # в очереди, купленный токен остаётся на руках
            # (``the_main_rules.md``, правило 13).
            priority=_PRIORITY,
        )

    async def _quote_exits(
        self,
        position: Position,
        adapters: tuple[tuple[ProviderId, AggregatorAdapter], ...],
        *,
        needed: int | None,
    ) -> tuple[ExitCandidate, ...]:
        """Опросить кандидатов и упорядочить их по прогнозу.

        Отказ одного не отменяет остальных: маршрута на этот токен у
        агрегатора может не быть вовсе, и это его ответ, а не сбой
        сделки. Единственный опрошенный при немедленной проверке ведёт
        себя как прежде: не ответил — выход откладывается до такта.

        Цена газа берётся один раз на весь опрос и только если она
        кому-то понадобилась: она общая для всех кандидатов, а круг, не
        окупающийся даже **без** газа, с газом тем более убыточен —
        считать его расход незачем.
        """
        price: int | None = None
        candidates: list[ExitCandidate] = []
        for provider_id, adapter in adapters:
            request = self._exit_request(position, adapter)
            try:
                quote = await adapter.get_quote(request)
            except Exception as error:  # noqa: BLE001 - отказ выхода не сбой сделки
                _LOGGER.info(
                    "exit quote unavailable",
                    extra=log_fields(
                        t_id=str(position.t_id),
                        provider=provider_id.value,
                        error=type(error).__name__,
                        detail=str(error),
                    ),
                )
                continue
            guaranteed = projected_output(
                quote.output_amount.raw, slippage_bps=request.slippage_bps or 0
            )
            gross = position.profit_if_sold_for(guaranteed)
            if needed is not None and gross < needed:
                # Безнадёжен и без газа. Ни цена газа, ни пересчёт в
                # базовый токен ему уже ничем не помогут.
                candidates.append(
                    ExitCandidate(
                        provider_id=provider_id,
                        adapter=adapter,
                        request=request,
                        quote=quote,
                        projected_profit=gross,
                        gas_known=False,
                        viable=False,
                    )
                )
                continue
            if price is None:
                price = await self._account.gas_price(position.network_id, priority=_PRIORITY)
            units = 0 if executes_with_orders(adapter) else quote.estimated_gas_units
            costs = await self._projected_costs_raw(position, units=units, price=price)
            candidates.append(
                ExitCandidate(
                    provider_id=provider_id,
                    adapter=adapter,
                    request=request,
                    quote=quote,
                    projected_profit=position.profit_if_sold_for(guaranteed, raw_costs=costs or 0),
                    gas_known=costs is not None,
                )
            )
        return rank_exits(candidates)

    async def _projected_costs_raw(
        self, position: Position, *, units: int | None, price: int
    ) -> int | None:
        """Во что обойдётся круг по оценке котировки.

        От :meth:`_exit_costs_raw` отличается только источником предела
        газа: там он взят у собранного предложения, здесь — у котировки.
        Служит порядку опроса, а не решению: решение принимается по
        собранному предложению и по его же расходу.
        """
        if units is None:
            return None
        total_wei = _total_wei(position.buy_gas_wei, position.sell_gas_wei, units * price)
        if total_wei is None:
            return None
        return await self._costs.to_base_raw(
            position.network_id, position.base_token, wei=total_wei
        )

    async def _build_offer(
        self, adapter: AggregatorAdapter, request: QuoteRequest
    ) -> ExecutionPlan:
        """Предложение агрегатора в той форме, в какой он его даёт.

        Одни отдают транзакцию, другой — заявку на подпись
        (``the_main_rules.md``, правило 16). Обязательство есть у обеих
        форм, и решение о выходе принимается по нему одинаково.
        """
        if executes_with_orders(adapter):
            return await adapter.build_order(request)
        return await adapter.build_swap(request)

    async def _exit_costs_raw(self, position: Position, transaction: ExecutionPlan) -> int | None:
        """Во что обойдётся круг целиком, в базовом токене.

        Считается всё, что сделка стоит и будет стоить: покупка, уже
        потраченное на неудачные попытки выхода и предстоящая продажа.
        Предел газа берётся у агрегатора — он же поедет в транзакцию,
        поэтому отдельная оценка узлом не нужна.
        """
        price = await self._account.gas_price(position.network_id, priority=_PRIORITY)
        total_wei = _total_wei(
            position.buy_gas_wei, position.sell_gas_wei, leg_gas_units(transaction) * price
        )
        if total_wei is None:
            _LOGGER.warning(
                "exit postponed: the gas already spent is unknown",
                extra=log_fields(t_id=str(position.t_id)),
            )
            return None
        return await self._costs.to_base_raw(
            position.network_id, position.base_token, wei=total_wei
        )

    async def _announce_closed(self, position: Position) -> None:
        """Сообщить оператору, чем кончилась сделка.

        Сообщение отдельное и не зависит от того, чем закрытие вызвано:
        такт наблюдателя, проверка после покупки или прямой приказ
        оператора. 21.09 продажа `#T41` состоялась, а ответ на нажатие
        кнопки пропал вместе с упавшим обработчиком команды, и оператор
        остался без вести о собственных деньгах.

        Недоставка сообщения сделку не трогает: уведомление — рассказ о
        результате, а не его часть (``CLAUDE.md`` §35).
        """
        if self._notify is None:
            return
        try:
            await self._notify.notify_operator(closing_text(position))
        except Exception as error:  # noqa: BLE001 - доставка не должна ронять торговлю
            _LOGGER.warning(
                "closing notice was not delivered",
                extra=log_fields(
                    t_id=str(position.t_id),
                    error=type(error).__name__,
                    detail=str(error),
                ),
            )

    async def _record_calibration(self, position: Position, actual_units: int) -> None:
        """Передать замер расхода газа продажи.

        Нога продажи расходится с оценкой сильнее покупки: её маршрут чаще
        разбивается на несколько пулов. Именно поэтому замер по ней важнее
        всего, и он же достаётся бесплатно — из уже полученной квитанции.
        """
        if self._calibration is None or position.sell_quoted_gas_units is None:
            return
        await self._calibration.record(
            position.network_id,
            position.sell_provider_id,
            quoted_units=position.sell_quoted_gas_units,
            actual_units=actual_units,
        )

    async def _round_trip_cost_raw(
        self, position: Position, *, buy_wei: int | None, sell_wei: int | None
    ) -> int | None:
        """Фактическая стоимость круга в базовом токене."""
        total_wei = _total_wei(buy_wei, sell_wei)
        if total_wei is None:
            return None
        return await self._costs.to_base_raw(
            position.network_id, position.base_token, wei=total_wei
        )

    def _wait(
        self, position: Position, *, profit: int, needed: int, costs: int | None
    ) -> LongWaitNotice | None:
        """Отложить выход и при необходимости сообщить оператору."""
        decimals = position.base_token.decimals
        _LOGGER.info(
            "trade waiting",
            extra=log_fields(
                t_id=str(position.t_id),
                profit=str(Decimal(profit).scaleb(-decimals)),
                needed=str(Decimal(needed).scaleb(-decimals)),
                gas=_money(None if costs is None else Decimal(costs).scaleb(-decimals)),
            ),
        )
        return self._long_wait_notice(position)

    async def _sell(self, position: Position, offer: ExecutionPlan) -> bool:
        """Проверить и исполнить продажу.

        Возвращает, **пошла ли продажа**. Молчаливый отказ здесь
        недопустим: предложение может не исполниться — транзакция
        откатится на симуляции, заявку не примет книга, — и вызывающая
        сторона обязана об этом узнать. Иначе оператор получает «продано»
        по сделке, которая осталась на месте.

        Предложение приходит уже собранным: его собрали, чтобы узнать
        стоимость выхода, и собирать второй раз значило бы потратить
        лишний запрос на то же самое.

        Форма исполнения принадлежит агрегатору: один исполнит
        транзакцией, другой — подписанной заявкой.
        """
        if not isinstance(offer, SwapTransaction):
            return await self._place_sell(position, offer)
        transaction = offer
        simulation = await self._account.simulate(transaction, priority=_PRIORITY)
        if not simulation.succeeded:
            _LOGGER.warning(
                "exit postponed: the swap would revert",
                extra=log_fields(t_id=str(position.t_id), reason=simulation.revert_reason or ""),
            )
            return False
        # Остаток до продажи запоминается перед отправкой: выручка —
        # это прирост, и прирост должен быть досчитан даже если квитанцию
        # подберёт уже другой процесс.
        before = await self._account.token_balance(position.base_token, priority=_PRIORITY)
        sent: SentTransaction = await self._sender.send(
            position.network_id,
            to=transaction.to,
            data=transaction.data,
            value=transaction.value,
            gas_limit=transaction.gas_limit,
            priority=_PRIORITY,
        )
        selling = position.model_copy(
            update={
                "status": PositionStatus.SELLING,
                "sell_tx_hash": sent.tx_hash,
                "raw_base_before_sell": before.raw,
                "updated_at": self._clock.now(),
            }
        )
        await self._positions.update(selling)
        receipt = await self._sender.wait(
            sent, timeout=self._receipt_timeout, poll=self._receipt_poll, priority=_PRIORITY
        )
        if receipt is not None:
            await self._settle_pending_sell(selling)
        return True

    async def _place_sell(self, position: Position, plan: SignedOrderPlan) -> bool:
        """Исполнить продажу подписанной заявкой.

        Симуляции здесь нет и быть не может: транзакции не существует.
        Защитой служит предельная сумма самой заявки — меньше решатель
        отдать не вправе, и именно по ней принято решение о выходе.

        Газ у такой ноги нулевой, и записывается он нулём: «неизвестно»
        запретило бы посчитать круг, тогда как расход действительно
        отсутствует.
        """
        if self._orders is None:
            _LOGGER.warning(
                "exit postponed: signed orders are not available",
                extra=log_fields(t_id=str(position.t_id)),
            )
            return False
        adapter = self._adapters.get(position.sell_provider_id.value)
        if adapter is None:  # pragma: no cover - проверено вызывающим
            return False
        # Остаток до продажи запоминается перед размещением: выручка —
        # это прирост, и досчитать его может уже другой процесс.
        before = await self._account.token_balance(position.base_token, priority=_PRIORITY)
        placed = await self._orders.place(adapter, plan)
        selling = position.model_copy(
            update={
                "status": PositionStatus.SELLING,
                "sell_order_uid": placed.uid,
                "sell_gas_wei": 0,
                "sell_gas_units": 0,
                "raw_base_before_sell": before.raw,
                "updated_at": self._clock.now(),
            }
        )
        await self._positions.update(selling)
        state = await self._orders.wait(adapter, placed, operation=OperationType.SELL)
        if state is None:
            # Заявка ещё в книге: сделка остаётся в SELLING, и следующий
            # такт наблюдателя её подберёт. Размещение состоялось, и это
            # не отказ — отсюда True.
            return True
        await self._settle_order_sell(selling, state.outcome)
        return True

    async def _settle_order_sell(self, position: Position, outcome: OrderOutcome) -> None:
        """Зафиксировать исход продажи, исполнявшейся заявкой."""
        if outcome is not OrderOutcome.FILLED:
            # Заявка не исполнилась — токен остался у нас, и денег это не
            # стоило. Возвращаемся к ожиданию: это не потеря, а неудачная
            # попытка выхода.
            _LOGGER.info(
                "exit order was not filled",
                extra=log_fields(
                    t_id=str(position.t_id),
                    order=position.sell_order_uid or "",
                    outcome=outcome.value,
                ),
            )
            await self._positions.update(
                position.model_copy(
                    update={
                        "status": PositionStatus.HOLDING,
                        "sell_order_uid": None,
                        "updated_at": self._clock.now(),
                    }
                )
            )
            return
        await self._close_after_sale(
            position, spent_on_selling=position.sell_gas_wei, sold_units=position.sell_gas_units
        )

    def _long_wait_notice(self, position: Position) -> LongWaitNotice | None:
        """Сообщить о затянувшемся ожидании — один раз, а не каждый такт."""
        if not self._long_wait_enabled or position.long_wait_notified_at is not None:
            return None
        waited = self._clock.now() - position.opened_at
        if waited < self._long_wait:
            return None
        return LongWaitNotice(position=position, waited=waited)

    async def mark_notified(self, position: Position) -> None:
        """Запомнить, что об этой сделке уже сообщили."""
        await self._positions.update(
            position.model_copy(update={"long_wait_notified_at": self._clock.now()})
        )


def _total_wei(*parts: int | None) -> int | None:
    """Сумма расходов в wei.

    Неизвестное слагаемое делает неизвестной всю сумму: подставить вместо
    него ноль означало бы объявить часть расхода бесплатной
    (``CLAUDE.md`` §12).
    """
    total = 0
    for part in parts:
        if part is None:
            return None
        total += part
    return total


def _money(value: Decimal | None) -> str:
    """Денежная величина для журнала. Неизвестное так и называется."""
    return "неизвестно" if value is None else str(value)
