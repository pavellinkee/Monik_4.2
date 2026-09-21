"""Выбор продавца при выходе из сделки.

Два случая различаются положением сделки (``the_main_rules.md``,
правило 21): немедленная проверка после покупки остаётся на назначенном
продавце, отложенная опрашивает всех включённых и берёт лучшего.
"""

from __future__ import annotations

from decimal import Decimal

from eth_account import Account

from monik.config.secrets import SecretValue
from monik.domain.enums.providers import ProviderId
from monik.domain.enums.trading import PositionStatus
from monik.domain.errors import MonikError
from monik.infrastructure.providers.fake import FakeAdapter
from monik.services.calculator import ProfitCalculator
from monik.services.observability import FakeClock
from monik.services.trading import PositionWatcher, TradeExecutor, TradingWallet
from monik.services.trading.exits import ExitCandidate, projected_output, rank_exits
from tests import factories as f
from tests.unit.trading.support import (
    FixedCosts,
    MemoryPositions,
    MemorySequences,
    ScriptedNode,
    build_account,
    build_sender,
)
from tests.unit.trading.test_executor import _tokens
from tests.unit.trading.test_watcher import COSTS_RAW, _position

#: Назначенный продавец окупает круг, но скромно; второй — заметно
#: лучше. Разрыв выбран большим нарочно: проверяется выбор, а не
#: арифметика на грани порога.
ASSIGNED_RATE = "10.02"
BETTER_RATE = "10.10"


def _watcher(
    positions: MemoryPositions,
    *,
    assigned_rate: str = ASSIGNED_RATE,
    better_rate: str = BETTER_RATE,
    assigned_error: MonikError | None = None,
    min_exit_profit_raw: int = 10_000,
    simulation_ok: bool = True,
) -> tuple[PositionWatcher, dict[ProviderId, FakeAdapter]]:
    """Наблюдатель с двумя включёнными агрегаторами."""
    clock = FakeClock(f.NOW)
    node = ScriptedNode(simulation_ok=simulation_ok)
    costs = FixedCosts(COSTS_RAW)
    wallet = TradingWallet(SecretValue("K", str(Account.create().key.hex())))
    assigned = FakeAdapter(
        ProviderId.UNISWAP, clock, rate=Decimal(assigned_rate), error=assigned_error
    )
    better = FakeAdapter(ProviderId.VELORA, clock, rate=Decimal(better_rate))
    adapters = {
        ProviderId.UNISWAP.value: assigned,
        ProviderId.VELORA.value: better,
    }
    account = build_account(node, clock, wallet.address)
    sender = build_sender(node, clock, wallet)
    executor = TradeExecutor(
        adapters=adapters,
        positions=positions,
        sequences=MemorySequences(),
        account=account,
        sender=sender,
        tokens=_tokens(),
        calculator=ProfitCalculator(clock),
        costs=costs,
        calibration=None,
        clock=clock,
        is_execution_open=lambda: True,
        slippage_bps=0,
        min_entry_profit_raw=0,
        receipt_timeout_seconds=1,
        receipt_poll_seconds=1,
    )
    watcher = PositionWatcher(
        adapters=adapters,
        positions=positions,
        account=account,
        sender=sender,
        executor=executor,
        costs=costs,
        calibration=None,
        clock=clock,
        min_exit_profit_raw=min_exit_profit_raw,
        min_exit_profit_waiting_raw=min_exit_profit_raw,
        slippage_bps=0,
        exit_requires_profit=True,
        long_wait_enabled=False,
        long_wait_seconds=7_200,
        receipt_timeout_seconds=1,
        receipt_poll_seconds=1,
    )
    return watcher, {ProviderId.UNISWAP: assigned, ProviderId.VELORA: better}


class TestImmediateCheck:
    async def test_the_assigned_seller_keeps_the_immediate_check(self) -> None:
        """Сразу после покупки опрашивается только назначенный продавец.

        Маршрут только что выбран проходом по свежим котировкам обеих
        ног. Опрашивать остальных значило бы тратить запросы на уже
        известное, поэтому лучшее предложение соседа здесь не
        рассматривается вовсе.
        """
        positions = MemoryPositions()
        watcher, adapters = _watcher(positions)
        position = _position()
        await positions.create(position)

        await watcher.consider_now(position)

        assert adapters[ProviderId.VELORA].quote_calls == [], "чужого продавца не спрашивали"
        stored = positions.items[str(position.t_id)]
        assert stored.sell_provider_id is ProviderId.UNISWAP, "продавец остался назначенным"
        assert stored.status is not PositionStatus.HOLDING, "выход состоялся"


class TestWaitingCheck:
    async def test_the_waiting_check_surveys_everyone(self) -> None:
        """Отложенная проверка спрашивает всех включённых."""
        positions = MemoryPositions()
        watcher, adapters = _watcher(positions)
        await positions.create(_position())

        await watcher.tick()

        assert adapters[ProviderId.UNISWAP].quote_calls, "назначенного спросили"
        assert adapters[ProviderId.VELORA].quote_calls, "остальных тоже спросили"

    async def test_the_best_offer_wins_and_becomes_the_seller(self) -> None:
        """Выход достаётся лучшему предложению, и он же записан продавцом.

        Записать продавца обязательно: заявку доводит до конца тот, кто
        её принял, и подобрать её потом можно только у него.
        """
        positions = MemoryPositions()
        watcher, _ = _watcher(positions)
        position = _position()
        await positions.create(position)

        await watcher.tick()

        stored = positions.items[str(position.t_id)]
        assert stored.sell_provider_id is ProviderId.VELORA, "продал тот, кто дал больше"
        assert stored.status is not PositionStatus.HOLDING, "выход состоялся"

    async def test_a_refusing_provider_does_not_stop_the_survey(self) -> None:
        """Отказ одного агрегатора не срывает выход.

        Маршрута на этот токен у него может не быть вовсе, и это его
        ответ, а не сбой сделки: остальные опрашиваются как обычно.
        """
        positions = MemoryPositions()
        watcher, _ = _watcher(
            positions,
            assigned_error=MonikError("no route", code="no_route"),
        )
        position = _position()
        await positions.create(position)

        await watcher.tick()

        stored = positions.items[str(position.t_id)]
        assert stored.sell_provider_id is ProviderId.VELORA, "выход нашёлся у отвечающего"
        assert stored.status is not PositionStatus.HOLDING, "выход состоялся"

    async def test_nobody_pays_and_the_trade_keeps_waiting(self) -> None:
        """Если не платит никто, сделка ждёт, а не закрывается в убыток."""
        positions = MemoryPositions()
        watcher, _ = _watcher(positions, assigned_rate="9.50", better_rate="9.60")
        position = _position()
        await positions.create(position)

        await watcher.tick()

        stored = positions.items[str(position.t_id)]
        assert stored.status is PositionStatus.HOLDING, "сделка осталась в ожидании"


class TestRanking:
    def _candidate(self, provider_id: ProviderId, profit: int, **extra: object) -> ExitCandidate:
        adapter = FakeAdapter(provider_id, FakeClock(f.NOW))
        request = object()
        quote = object()
        return ExitCandidate(
            provider_id=provider_id,
            adapter=adapter,  # type: ignore[arg-type]
            request=request,  # type: ignore[arg-type]
            quote=quote,  # type: ignore[arg-type]
            projected_profit=profit,
            **extra,  # type: ignore[arg-type]
        )

    def test_the_larger_projection_goes_first(self) -> None:
        ranked = rank_exits(
            (
                self._candidate(ProviderId.UNISWAP, 10),
                self._candidate(ProviderId.VELORA, 20),
            )
        )
        assert [item.provider_id for item in ranked] == [ProviderId.VELORA, ProviderId.UNISWAP]

    def test_a_hopeless_leg_goes_last_however_large(self) -> None:
        """Валовой прогноз не сравнивается с чистым.

        У не окупающейся даже без газа ноги газ не считали вовсе, и её
        число завышено по построению. Поставить её впереди значило бы
        оборвать обход на кандидате, который и не проверялся.
        """
        ranked = rank_exits(
            (
                self._candidate(ProviderId.UNISWAP, 900, viable=False),
                self._candidate(ProviderId.VELORA, 5),
            )
        )
        assert [item.provider_id for item in ranked] == [ProviderId.VELORA, ProviderId.UNISWAP]

    def test_a_known_cost_wins_a_tie(self) -> None:
        """При равенстве впереди тот, чей расход известен."""
        ranked = rank_exits(
            (
                self._candidate(ProviderId.UNISWAP, 10, gas_known=False),
                self._candidate(ProviderId.VELORA, 10),
            )
        )
        assert [item.provider_id for item in ranked] == [ProviderId.VELORA, ProviderId.UNISWAP]


class TestProjection:
    def test_the_projection_matches_the_adapter_formula(self) -> None:
        """Прогноз считается той же формулой, что и предельная сумма.

        Иначе сравнивали бы рекламу одного с обязательством другого.
        """
        assert projected_output(1_000_000, slippage_bps=0) == 1_000_000
        assert projected_output(1_000_000, slippage_bps=100) == 990_000

    def test_the_projection_never_falls_to_zero(self) -> None:
        """Нулевой предел отдал бы решателю всё."""
        assert projected_output(1, slippage_bps=9_999) == 1


class TestManualExit:
    """Выход по решению оператора.

    Система сама в убыток не закрывает, но правило 11 прямо оставляет
    этот случай человеку: увидев затянувшееся ожидание, он вправе
    решить иначе. Порог выхода к его решению не применяется.
    """

    async def test_the_price_is_shown_even_when_the_circle_loses(self) -> None:
        """Вопрос оператора — «сколько дадут», а не «выгодно ли»."""
        positions = MemoryPositions()
        watcher, _ = _watcher(positions, assigned_rate="9.50", better_rate="9.60")
        position = _position()
        await positions.create(position)

        offer = await watcher.preview_exit(str(position.t_id))

        assert offer is not None, "цена названа и у убыточного круга"
        assert offer.provider == ProviderId.VELORA.value, "выбран лучший из включённых"
        assert offer.profit < 0, "итог честно отрицательный"

    async def test_the_sale_goes_through_at_a_loss(self) -> None:
        positions = MemoryPositions()
        watcher, _ = _watcher(positions, assigned_rate="9.50", better_rate="9.60")
        position = _position()
        await positions.create(position)

        sold = await watcher.sell_at_market(str(position.t_id))

        assert sold is not None
        stored = positions.items[str(position.t_id)]
        assert stored.status is not PositionStatus.HOLDING, "сделка ушла в продажу"
        assert stored.sell_provider_id is ProviderId.VELORA, "продавец записан"

    async def test_the_watcher_would_have_waited_on_its_own(self) -> None:
        """Тот же круг подсистема сама не закрыла бы.

        Проверка нужна, чтобы предыдущая не оказалась случайной: продажа
        состоялась именно потому, что её велел человек.
        """
        positions = MemoryPositions()
        watcher, _ = _watcher(positions, assigned_rate="9.50", better_rate="9.60")
        position = _position()
        await positions.create(position)

        await watcher.tick()

        assert positions.items[str(position.t_id)].status is PositionStatus.HOLDING

    async def test_an_unknown_trade_is_not_invented(self) -> None:
        positions = MemoryPositions()
        watcher, _ = _watcher(positions)
        await positions.create(_position())

        assert await watcher.preview_exit("#T999") is None
        assert await watcher.sell_at_market("#T999") is None

    async def test_a_trade_already_selling_is_left_alone(self) -> None:
        """У сделки, которую ведёт начатая операция, распоряжаться нечем."""
        positions = MemoryPositions()
        watcher, _ = _watcher(positions)
        position = _position(status=PositionStatus.SELLING)
        await positions.create(position)

        assert await watcher.preview_exit(str(position.t_id)) is None
        assert await watcher.sell_at_market(str(position.t_id)) is None


class TestFailedSaleIsNotReportedAsSuccess:
    """Продажа, которая не пошла, не должна выглядеть состоявшейся.

    21.09 оператор нажал «продать по текущей цене» у `#T41`, получил
    «продана» и увидел сделку на месте: транзакция откатывалась бы на
    симуляции, а продажа возвращала успех независимо от исхода.
    """

    async def test_a_reverting_swap_is_reported_as_failure(self) -> None:
        positions = MemoryPositions()
        watcher, _ = _watcher(positions, simulation_ok=False)
        position = _position()
        await positions.create(position)

        sold = await watcher.sell_at_market(str(position.t_id))

        assert sold is None, "об успехе не сообщено"

    async def test_a_reverting_swap_leaves_the_trade_waiting(self) -> None:
        """Сделка остаётся там, где была, и это видно по состоянию."""
        positions = MemoryPositions()
        watcher, _ = _watcher(positions, simulation_ok=False)
        position = _position()
        await positions.create(position)

        await watcher.sell_at_market(str(position.t_id))

        assert positions.items[str(position.t_id)].status is PositionStatus.HOLDING

    async def test_the_watcher_keeps_waiting_too(self) -> None:
        """Тот же отказ на обычном такте сделку не теряет."""
        positions = MemoryPositions()
        watcher, _ = _watcher(positions, simulation_ok=False)
        position = _position()
        await positions.create(position)

        await watcher.tick()

        assert positions.items[str(position.t_id)].status is PositionStatus.HOLDING

    async def test_every_provider_is_tried_before_giving_up(self) -> None:
        """Отказ лидера не отменяет остальных.

        Предложение могло не исполниться у одного и исполниться у
        другого: сдаваться на первом значит бросать деньги в позиции.
        """
        positions = MemoryPositions()
        watcher, adapters = _watcher(positions, simulation_ok=False)
        position = _position()
        await positions.create(position)

        await watcher.sell_at_market(str(position.t_id))

        assert adapters[ProviderId.UNISWAP].quote_calls, "назначенного спросили"
        assert adapters[ProviderId.VELORA].quote_calls, "второго тоже"
