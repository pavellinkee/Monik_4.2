"""Открытие сделки режима ``ann``.

Подсистема отделена от сканера намеренно (``the_main_rules.md``,
правило 11, сохраняющее суть §56): найденная возможность сама по себе не
является разрешением тратить деньги. Прежде чем отправить покупку,
исполнитель проверяет три вещи, и любая из них отменяет сделку целиком:

1. разрешена ли торговля вообще;
2. хватает ли остатка с учётом уже занятого открытыми сделками;
3. прошла бы транзакция, если отправить её прямо сейчас.

Третья проверка — симуляция — и есть защита от завышенной котировки:
сделка, которая не прошла бы, не отправляется вовсе.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from monik.domain.enums.operations import OperationType
from monik.domain.enums.resources import RequestPriority
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.execution import (
    ExecutionOffer,
    ExecutionPlan,
    OrderOutcome,
    SignedOrderPlan,
    SwapTransaction,
)
from monik.domain.models.opportunity import Candidate
from monik.domain.models.position import Position
from monik.domain.models.token import Token
from monik.domain.value_objects.amounts import TokenAmount
from monik.domain.value_objects.identifiers import RequestId, TId
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.providers.contract import AggregatorAdapter, QuoteRequest
from monik.services.calculator import ProfitCalculator
from monik.services.level1.results import ScanResult
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger, log_fields
from monik.services.registries.tokens import TokenRegistry
from monik.services.trading.chain import ChainAccount, TransactionReceipt
from monik.services.trading.orders import (
    SignedOrderRunner,
    executes_with_orders,
    leg_gas_units,
    leg_slippage_bps,
)
from monik.services.trading.ports import (
    ExecutionCosts,
    GasCalibrationRecorder,
    PositionStore,
    SequenceSource,
)
from monik.services.trading.sender import TransactionSender

__all__ = ["TradeExecutor"]

_LOGGER = get_logger("services.trading.executor")

#: Приоритет всех обращений покупки: она обгоняет сканирование, но
#: уступает продаже (``the_main_rules.md``, правило 13).
_PRIORITY = RequestPriority.ANN_BUY

#: Последовательность сделок.
POSITION_SEQUENCE = "position"


@dataclass(frozen=True, slots=True)
class _Choice:
    """Кандидат, который подсистема готова исполнить."""

    candidate: Candidate
    target: Token
    base: Token


class TradeExecutor:
    """Превращает находку режима ``ann`` в открытую сделку."""

    def __init__(
        self,
        *,
        adapters: dict[str, AggregatorAdapter],
        positions: PositionStore,
        sequences: SequenceSource,
        account: ChainAccount,
        sender: TransactionSender,
        tokens: TokenRegistry,
        calculator: ProfitCalculator,
        costs: ExecutionCosts,
        calibration: GasCalibrationRecorder | None,
        clock: Clock,
        is_execution_open: Callable[[], bool],
        slippage_bps: int,
        min_entry_profit_raw: int,
        order_slippage_bps: int = 0,
        orders: SignedOrderRunner | None = None,
        max_offer_checks: int = 10,
        receipt_timeout_seconds: int = 120,
        receipt_poll_seconds: int = 2,
    ) -> None:
        self._adapters = dict(adapters)
        self._positions = positions
        self._sequences = sequences
        self._account = account
        self._sender = sender
        self._tokens = tokens
        self._calculator = calculator
        self._costs = costs
        self._calibration = calibration
        self._clock = clock
        self._is_execution_open = is_execution_open
        self._slippage_bps = slippage_bps
        self._min_entry_profit_raw = min_entry_profit_raw
        #: Допуск ноги, исполняемой намерением. Отдельный от общего,
        #: потому что промах у неё бесплатен.
        self._order_slippage_bps = order_slippage_bps
        #: Исполнение подписанной заявкой. ``None`` означает, что такой
        #: формы у системы нет: провайдер, исполняющий заявками, тогда
        #: годится для поиска, но сделку по нему открывать нечем.
        self._orders = orders
        #: Сколько находок проверять предложением за один проход. Отказ
        #: стоит запросов, и перебирать весь список в цикле, где ни одна
        #: не подтверждается, незачем.
        self._max_offer_checks = max_offer_checks
        self._receipt_timeout = timedelta(seconds=receipt_timeout_seconds)
        self._receipt_poll = timedelta(seconds=receipt_poll_seconds)
        #: Немедленная проверка выхода после удачной покупки. Ставится
        #: снаружи, потому что решение о продаже принимает наблюдатель, а
        #: он сам зависит от исполнителя.
        self._check_exit: Callable[[Position], Awaitable[None]] | None = None

    def set_exit_check(self, check: Callable[[Position], Awaitable[None]]) -> None:
        """Задать проверку выхода, выполняемую сразу после покупки.

        Правило оператора: «вначале производится покупка, затем **ещё
        раз** производится проверка, выгодно ли совершить продажу; если
        результат не положительный — сделка остаётся в ожидании».
        Немедленная проверка не заменяется периодической: именно сразу
        после покупки шанс выйти в плюс наивысший — расхождение, ради
        которого сделка открылась, ещё живо, а живёт оно минуты.
        """
        self._check_exit = check

    async def consider(self, results: tuple[ScanResult, ...]) -> Position | None:
        """Рассмотреть находки прохода и, если можно, открыть сделку.

        За один проход **открывается** одна сделка: транзакции счёта идут
        по очереди, и вторая покупка всё равно ждала бы первую, а цена за
        это время уже другая.

        Но **проверяется** не одна. Отказ по предложению не означает, что
        плох весь проход: находка с завышенной котировкой выглядит лучшей
        и становится первой, заслоняя собой честные. Поэтому кандидаты
        перебираются по убыванию заработка до первого, чьё предложение
        порог проходит.

        Перебор ограничен: отказ стоит запросов, и в цикле, где ни одна
        находка не подтверждается, тратить их на весь список незачем.
        """
        candidates = await self._affordable(results)
        if not candidates:
            return None
        if not self._is_execution_open():
            _LOGGER.info(
                "trade withheld: execution is disabled",
                extra=self._describe(candidates[0]),
            )
            return None
        for choice in candidates[: self._max_offer_checks]:
            opened = await self._open(choice)
            if opened is not None:
                return opened
        rejected = min(len(candidates), self._max_offer_checks)
        _LOGGER.info(
            "no candidate of this scan offered a paying circle",
            extra=log_fields(checked=rejected, qualified=len(candidates)),
        )
        return None

    # --- выбор ------------------------------------------------------------

    async def _affordable(self, results: tuple[ScanResult, ...]) -> tuple[_Choice, ...]:
        """Находки, которые позволяет остаток счёта, в порядке заработка.

        Порядок задан сканером: торговый режим ранжирует и группы, и
        кандидатов внутри них по заработку в базовом токене
        (``the_main_rules.md``, правило 11). Здесь он только сохраняется.

        Правило «не хватает средств — сделка не производится» относится к
        сумме, а не к набору: находка, которая в остаток не помещается,
        отбрасывается, а остальные рассматриваются дальше.
        """
        available: dict[NetworkId, int] = {}
        affordable: list[_Choice] = []
        for result in results:
            network_id = result.scan.scope.networks[0]
            if network_id not in available:
                available[network_id] = await self._available(network_id)
            for candidate in result.qualified:
                choice = self._as_choice(candidate, network_id)
                if choice is None:
                    continue
                if candidate.buy_quote.input_amount.raw <= available[network_id]:
                    affordable.append(choice)
                    continue
                _LOGGER.info(
                    "trade skipped: balance does not cover the amount",
                    extra=log_fields(
                        network=str(network_id),
                        amount=str(candidate.buy_quote.input_amount.as_decimal),
                        available=str(Decimal(available[network_id])),
                    ),
                )
        return tuple(affordable)

    def _as_choice(self, candidate: Candidate, network_id: NetworkId) -> _Choice | None:
        base = self._tokens.get(candidate.buy_quote.input_token)
        target = self._tokens.get(candidate.buy_quote.output_token)
        if base is None or target is None or base.key == target.key:
            return None
        if base.network_id != network_id:
            return None
        return _Choice(candidate=candidate, target=target, base=base)

    async def _available(self, network_id: NetworkId) -> int:
        """Свободный остаток базового токена сети.

        Из остатка вычитается то, что уже обещано открытым сделкам: иначе
        одни и те же деньги были бы вложены дважды.
        """
        base = self._tokens.base_token(network_id)
        balance = await self._account.token_balance(base, priority=_PRIORITY)
        reserved = await self._positions.reserved_raw_input(network_id)
        return max(balance.raw - reserved, 0)

    async def _offer_allows(self, choice: _Choice, buy: ExecutionOffer) -> bool:
        """Проверить находку по **реальному предложению**, а не по котировке.

        Агрегатор — обменник, а не биржа: он называет цену сам и может
        дать выгодный курс независимо от рыночной стоимости токена.
        Поэтому решение о трате денег принимается не по обещанию
        котировки, а по тому, что агрегатор **обязуется** отдать: по
        минимуму, ниже которого он сам откатит обмен.

        Круг считается по двум таким минимумам подряд. Сначала собирается
        покупка — её минимум и есть количество целевого токена, которое мы
        гарантированно получим. На это количество собирается продажа, и её
        минимум — то, что гарантированно вернётся. Если после вычета газа
        остаётся не меньше порога, сделка возможна; иначе её нет, каким бы
        заманчивым ни было обещание.

        Ноги могут принадлежать **разным** агрегаторам: круг «купил у
        одного, продал у другого» — основная модель арбитража. Поэтому
        каждую ногу собирает её собственный агрегатор.

        Обе ноги стоят по одному запросу, и делаются они у **одного**
        кандидата, прошедшего порог: за проход таких проверок ноль или
        одна, тогда как комбинаций двенадцать.

        Если предложение получить не удалось, сделка не открывается.
        Непроверенное предложение — не предложение (``CLAUDE.md`` §12).
        """
        # Продажу собирает **её** агрегатор, а не тот, у кого куплено:
        # круг может идти между двумя разными, и предложение спрашивается
        # у того, кто будет его исполнять.
        seller = self._adapters.get(choice.candidate.sell_quote.provider_id.value)
        if seller is None:
            _LOGGER.info(
                "trade skipped: the exit aggregator is not available",
                extra=self._describe(choice),
            )
            return False
        guaranteed_target = choice.target.amount_from_base_units(buy.min_output_raw)
        sell = await self._build_exit(choice, seller, guaranteed_target)
        if sell is None:
            return False
        price = await self._account.gas_price(choice.base.network_id, priority=_PRIORITY)
        costs_raw = await self._costs.to_base_raw(
            choice.base.network_id,
            choice.base,
            wei=(leg_gas_units(buy) + leg_gas_units(sell)) * price,
        )
        if costs_raw is None:
            _LOGGER.info(
                "trade skipped: the cost of the circle could not be converted",
                extra=self._describe(choice),
            )
            return False
        raw_input = choice.candidate.buy_quote.input_amount.raw
        profit_raw = self._calculator.guaranteed_round_trip_profit(
            raw_input=raw_input,
            raw_guaranteed_output=sell.min_output_raw,
            raw_costs=costs_raw,
        )
        scale = Decimal(10) ** choice.base.decimals
        fields = self._describe(choice) | {
            "promised_output": str(Decimal(sell.quote.output_amount.raw) / scale),
            "guaranteed_output": str(Decimal(sell.min_output_raw) / scale),
            "guaranteed_target": str(guaranteed_target.as_decimal),
            "exact_gas_units": leg_gas_units(buy) + leg_gas_units(sell),
            "gas_price_wei": price,
            "gas_cost": str(Decimal(costs_raw) / scale),
            "guaranteed_profit": str(Decimal(profit_raw) / scale),
            "needed": str(Decimal(self._min_entry_profit_raw) / scale),
        }
        if profit_raw < self._min_entry_profit_raw:
            # Не открываем того, что не сможем закрыть: порог здесь тот же,
            # при котором подсистема согласна выйти из уже открытой сделки.
            _LOGGER.info("trade skipped: the guaranteed circle does not pay", extra=fields)
            return False
        _LOGGER.info("trade offer verified", extra=fields)
        return True

    def _slippage_for(self, adapter: AggregatorAdapter) -> int:
        """Допуск, уместный форме исполнения этого провайдера."""
        return leg_slippage_bps(
            adapter, transaction=self._slippage_bps, order=self._order_slippage_bps
        )

    async def _build_offer(
        self, adapter: AggregatorAdapter, request: QuoteRequest
    ) -> ExecutionPlan:
        """Спросить у агрегатора его предложение в той форме, в какой он его даёт.

        Одни отдают транзакцию, другой — заявку на подпись
        (``the_main_rules.md``, правило 16). Решение принимается по
        обязательству, а оно есть у обеих форм, поэтому вызывающему коду
        различие не нужно.
        """
        if executes_with_orders(adapter):
            return await adapter.build_order(request)
        return await adapter.build_swap(request)

    async def _build_exit(
        self, choice: _Choice, seller: AggregatorAdapter, amount: TokenAmount
    ) -> ExecutionPlan | None:
        """Собрать ногу продажи на то количество, которое мы получим.

        Сумма — не обещание котировки покупки, а её гарантированный
        минимум: продавать мы будем именно его, и предложение надо
        спрашивать на него же.

        Токена на счёте ещё нет, и узел оценить вызов не смог бы — он
        откатился бы. Агрегатор же собирает транзакцию по котировке, а не
        по остатку, и минимум с пределом газа называет.
        """
        try:
            return await self._build_offer(
                seller,
                QuoteRequest(
                    network_id=choice.base.network_id,
                    operation=OperationType.SELL,
                    input_token=choice.target,
                    output_token=choice.base,
                    input_amount=amount,
                    request_id=RequestId.generate(),
                    slippage_bps=self._slippage_for(seller),
                    priority=_PRIORITY,
                ),
            )
        except Exception as error:  # noqa: BLE001 - отсутствие предложения не сбой
            _LOGGER.info(
                "trade skipped: the exit leg could not be offered",
                extra=self._describe(choice)
                | {"error": type(error).__name__, "detail": str(error)},
            )
            return None

    # --- открытие ---------------------------------------------------------

    async def _open(self, choice: _Choice) -> Position | None:
        """Собрать, проверить и отправить покупку."""
        candidate = choice.candidate
        network_id = choice.base.network_id
        adapter = self._adapters.get(candidate.buy_quote.provider_id.value)
        if adapter is None:
            return None
        transaction = await self._build_offer(
            adapter,
            QuoteRequest(
                network_id=network_id,
                operation=OperationType.BUY,
                input_token=choice.base,
                output_token=choice.target,
                input_amount=candidate.buy_quote.input_amount,
                request_id=RequestId.generate(),
                slippage_bps=self._slippage_for(adapter),
                # Сделка обслуживается раньше поиска
                # (``the_main_rules.md``, правило 13).
                priority=RequestPriority.ANN_BUY,
            ),
        )
        if not await self._offer_allows(choice, transaction):
            return None
        if isinstance(transaction, SwapTransaction):
            simulation = await self._account.simulate(transaction, priority=_PRIORITY)
            if not simulation.succeeded:
                _LOGGER.warning(
                    "trade cancelled: the swap would revert",
                    extra=self._describe(choice) | {"reason": simulation.revert_reason or ""},
                )
                return None
        elif self._orders is None:
            # Провайдер исполняет заявками, а подписать их нечем.
            # Открывать сделку, которую нечем исполнить, нельзя.
            _LOGGER.warning(
                "trade cancelled: signed orders are not available",
                extra=self._describe(choice),
            )
            return None

        before = await self._account.token_balance(choice.target, priority=_PRIORITY)
        sequence = await self._sequences.next_value(POSITION_SEQUENCE)
        now = self._clock.now()
        position = Position(
            t_id=TId.from_sequence(sequence),
            network_id=network_id,
            status=PositionStatus.BUYING,
            base_token=choice.base,
            target_token=choice.target,
            buy_provider_id=candidate.buy_quote.provider_id,
            sell_provider_id=candidate.sell_quote.provider_id,
            raw_input=candidate.buy_quote.input_amount.raw,
            # Остаток до покупки запоминается сразу: полученное считается
            # разностью, а досчитать её может уже другой процесс, если
            # квитанция придёт после перезапуска.
            raw_target_before_buy=before.raw,
            buy_quoted_gas_units=candidate.buy_quote.estimated_gas_units,
            # На продажу пока не потрачено ничего, и это знание, а не
            # пробел: пустое поле означало бы «неизвестно», и стоимость
            # круга не сошлась бы вовсе.
            sell_gas_wei=0,
            sell_gas_units=0,
            opened_at=now,
        )
        # Запись делается ДО отправки: иначе перезапуск между отправкой и
        # записью оставил бы токены, о которых система не знает.
        await self._positions.create(position)

        settled: Position | None
        if isinstance(transaction, SwapTransaction):
            settled = await self._send_buy(choice, position, transaction)
        else:
            settled = await self._place_buy(choice, adapter, position, transaction)
        if settled is None:
            return None
        if settled.status is PositionStatus.HOLDING and self._check_exit is not None:
            # Не ждём следующего такта расписания: между покупкой и первой
            # проверкой прошло бы до десяти секунд, а отклонение столько
            # может и не прожить.
            await self._check_exit(settled)
        return settled

    async def _send_buy(
        self, choice: _Choice, position: Position, transaction: SwapTransaction
    ) -> Position:
        """Отправить покупку транзакцией и дождаться квитанции."""
        sent = await self._sender.send(
            position.network_id,
            to=transaction.to,
            data=transaction.data,
            value=transaction.value,
            gas_limit=transaction.gas_limit,
            priority=_PRIORITY,
        )
        position = position.model_copy(
            update={"buy_tx_hash": sent.tx_hash, "updated_at": self._clock.now()}
        )
        await self._positions.update(position)
        _LOGGER.info(
            "trade opened",
            extra=self._describe(choice) | {"t_id": str(position.t_id), "tx": sent.tx_hash},
        )
        receipt = await self._sender.wait(
            sent, timeout=self._receipt_timeout, poll=self._receipt_poll, priority=_PRIORITY
        )
        if receipt is None:
            # Квитанции ещё нет — сделка остаётся в BUYING, и наблюдатель
            # доведёт её. Считать её неудачной нельзя: транзакция может
            # попасть в блок позже.
            return position
        return await self.settle_buy(position, succeeded=receipt.succeeded, receipt=receipt)

    async def _place_buy(
        self,
        choice: _Choice,
        adapter: AggregatorAdapter,
        position: Position,
        plan: SignedOrderPlan,
    ) -> Position | None:
        """Разместить покупку заявкой и дождаться её судьбы.

        Газа у такой ноги нет, поэтому он записывается нулём, а не
        оставляется неизвестным: неизвестный расход запретил бы считать
        круг, тогда как здесь он действительно нулевой.

        Неисполненная заявка — штатный исход: деньги остались на счету,
        и сделка закрывается как несостоявшаяся, а не как убыточная.
        """
        if self._orders is None:  # pragma: no cover - проверено выше
            return None
        try:
            placed = await self._orders.place(adapter, plan)
        except Exception as error:  # noqa: BLE001 - отказ книги не сбой системы
            # Запись о сделке уже создана — она делается до отправки,
            # чтобы перезапуск не потерял отправленное. Если отправить
            # не удалось, запись обязана закрыться здесь же: иначе она
            # останется в BUYING навсегда, держа сумму занятой, и
            # опознать её будет нечем — ни хеша, ни идентификатора
            # заявки у неё нет. Так и встала сделка #T15 19.09.
            _LOGGER.warning(
                "trade cancelled: the order could not be placed",
                extra=self._describe(choice)
                | {
                    "t_id": str(position.t_id),
                    "error": type(error).__name__,
                    "detail": str(error),
                },
            )
            return await self.settle_buy(position, succeeded=False)
        position = position.model_copy(
            update={
                "buy_order_uid": placed.uid,
                "buy_gas_wei": 0,
                "buy_gas_units": 0,
                "updated_at": self._clock.now(),
            }
        )
        await self._positions.update(position)
        _LOGGER.info(
            "trade opened",
            extra=self._describe(choice) | {"t_id": str(position.t_id), "order": placed.uid},
        )
        state = await self._orders.wait(adapter, placed, operation=OperationType.BUY)
        if state is None:
            # Заявка ещё в книге. Сделка остаётся в BUYING, и наблюдатель
            # доведёт её: решатель может взять заявку позже.
            return position
        if state.outcome is not OrderOutcome.FILLED:
            _LOGGER.info(
                "trade cancelled: the buy order was not filled",
                extra=self._describe(choice)
                | {"t_id": str(position.t_id), "outcome": state.outcome.value},
            )
            return await self.settle_buy(position, succeeded=False)
        return await self.settle_buy(position, succeeded=True)

    async def settle_buy(
        self, position: Position, *, succeeded: bool, receipt: TransactionReceipt | None = None
    ) -> Position:
        """Зафиксировать итог покупки.

        Полученное количество берётся **с самого счёта**, а не из
        котировки: исполнение могло дать меньше обещанного, и дальнейшие
        расчёты должны опираться на факт. Считается при этом **прирост**
        остатка, а не сам остаток: на счёте может лежать тот же токен от
        прошлой сделки, и он к этой отношения не имеет.

        Стоимость газа берётся из той же квитанции — расход и цена
        приходят вместе с ней, поэтому знание о расходе не стоит ни
        одного дополнительного запроса.

        Без квитанции уже известное не затирается. У ноги, исполненной
        заявкой, квитанции не бывает вовсе, а расход у неё — ноль, и это
        знание: заменить его на «неизвестно» значило бы сделать круг
        несчитаемым на ровном месте.
        """
        now = self._clock.now()
        gas_wei = position.buy_gas_wei if receipt is None else receipt.gas_cost_wei
        gas_units = position.buy_gas_units if receipt is None else receipt.gas_used
        await self._record_calibration(position, gas_units)
        if not succeeded:
            updated = position.model_copy(
                update={
                    "status": PositionStatus.FAILED,
                    "updated_at": now,
                    "closed_at": now,
                    "raw_returned": position.raw_input,
                    "buy_gas_wei": gas_wei,
                    "buy_gas_units": gas_units,
                }
            )
            await self._positions.update(updated)
            # Формулировка зависит от того, чем шла нога. У намерения
            # нет транзакции, и откатываться нечему: заявку просто не
            # взяли. Сказать «транзакция откатилась» значило бы послать
            # оператора искать в цепи то, чего там никогда не было.
            if position.buy_order_uid is not None:
                _LOGGER.info(
                    "trade closed without a buy: the order was not filled",
                    extra=log_fields(t_id=str(position.t_id), order=position.buy_order_uid),
                )
            elif position.buy_tx_hash is not None:
                _LOGGER.warning(
                    "trade failed: the buy transaction reverted",
                    extra=log_fields(t_id=str(position.t_id), tx=position.buy_tx_hash),
                )
            else:
                # Ни заявки, ни транзакции: отправить не удалось вовсе.
                # Говорить об откате здесь так же неверно, как о
                # невзятой заявке, — не было ни того, ни другого.
                _LOGGER.info(
                    "trade closed without a buy: nothing was ever sent",
                    extra=log_fields(t_id=str(position.t_id)),
                )
            return updated
        after = await self._account.token_balance(position.target_token, priority=_PRIORITY)
        acquired = after.raw - (position.raw_target_before_buy or 0)
        if acquired <= 0:
            # Покупка удалась, а прироста остатка не видно. Это не «мы
            # ничего не получили» — это «мы ещё не знаем сколько»: узел
            # мог ответить состоянием до включения транзакции.
            #
            # Прежний код подставлял здесь единицу, и сделка #T30 20.09
            # повисла с записью в 0,000001 токена при почти сотне на
            # счету: продать такую позицию было нечем, а причина
            # пряталась за правдоподобным числом.
            #
            # Держать позицию с неизвестным количеством модель не
            # позволяет — и правильно. Поэтому сделка остаётся в
            # BUYING: наблюдатель вернётся к ней следующим тактом, когда
            # остаток станет виден, и доведёт её тем же путём.
            _LOGGER.warning(
                "buy settled but the balance has not caught up yet",
                extra=log_fields(
                    t_id=str(position.t_id),
                    tx=position.buy_tx_hash or position.buy_order_uid or "",
                    before=str(position.raw_target_before_buy),
                    after=str(after.raw),
                ),
            )
            return position
        updated = position.model_copy(
            update={
                "status": PositionStatus.HOLDING,
                "raw_acquired": acquired,
                "buy_gas_wei": gas_wei,
                "buy_gas_units": gas_units,
                "updated_at": now,
            }
        )
        await self._positions.update(updated)
        _LOGGER.info(
            "trade holding",
            extra=log_fields(
                t_id=str(position.t_id),
                token=str(position.target_token.symbol),
                acquired=str(Decimal(acquired).scaleb(-position.target_token.decimals)),
            ),
        )
        return updated

    async def _record_calibration(self, position: Position, actual_units: int | None) -> None:
        """Передать замер расхода газа покупки.

        Обещанное котировкой и потраченное на самом деле известны здесь
        оба, и оба уже получены: одно пришло с котировкой, другое — с
        квитанцией, которую подсистема запрашивает в любом случае. Замер
        не стоит ни одного дополнительного обращения.
        """
        if self._calibration is None or actual_units is None:
            return
        if position.buy_quoted_gas_units is None:
            return
        if position.buy_quoted_gas_units == 0 or actual_units == 0:
            # Нога без газа поправке не подлежит: поправка — отношение
            # потраченного к обещанному, а у нуля к нулю отношения нет.
            # Записать такой замер значило бы испортить поправку, которой
            # пользуются ноги, газ действительно платящие.
            return
        await self._calibration.record(
            position.network_id,
            position.buy_provider_id,
            quoted_units=position.buy_quoted_gas_units,
            actual_units=actual_units,
        )

    def _describe(self, choice: _Choice) -> dict[str, object]:
        result = choice.candidate.preliminary_result
        return log_fields(
            network=str(choice.base.network_id),
            token=str(choice.target.symbol),
            amount=str(choice.candidate.buy_quote.input_amount.as_decimal),
            route=(
                f"{choice.candidate.buy_quote.provider_id.value}->"
                f"{choice.candidate.sell_quote.provider_id.value}"
            ),
            net_profit=str(result.net_profit) if result.net_profit is not None else "",
        )
