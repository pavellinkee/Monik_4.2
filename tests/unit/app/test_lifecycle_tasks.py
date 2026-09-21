"""Условия, при которых проход вообще начинается.

Торговый проход следует за выключателем торговли: оператор, остановивший
торговлю, останавливает и поиск. Искать то, что заведомо не будет
исполнено, значит тратить квоту агрегаторов впустую и держать их под
нагрузкой без причины.

При этом сухой прогон сохраняется: когда торговля запрещена
конфигурацией, проход ищет и записывает находки, не тратя денег
(``the_main_rules.md``, правило 11).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from monik.app.control import TradingSwitch
from monik.app.lifecycle import _budget_allows, _search_allowed, _sell_buttons
from monik.domain.enums.modes import ScanMode


@dataclass
class _Container:
    """Ровно то, что нужно проверяемому правилу."""

    trading: TradingSwitch | None


def _switch(*, allowed: bool, started: bool) -> TradingSwitch:
    switch = TradingSwitch(allowed=allowed)
    if started:
        switch.start()
    return switch


class TestTradingModeFollowsTheSwitch:
    def test_trading_stopped_stops_the_search(self) -> None:
        container = _Container(trading=_switch(allowed=True, started=False))

        assert _search_allowed(container, ScanMode.ANN) is False  # type: ignore[arg-type]

    def test_trading_started_allows_the_search(self) -> None:
        container = _Container(trading=_switch(allowed=True, started=True))

        assert _search_allowed(container, ScanMode.ANN) is True  # type: ignore[arg-type]

    def test_stopping_trading_stops_an_already_running_search(self) -> None:
        switch = _switch(allowed=True, started=True)
        container = _Container(trading=switch)
        assert _search_allowed(container, ScanMode.ANN) is True  # type: ignore[arg-type]

        switch.stop()

        assert _search_allowed(container, ScanMode.ANN) is False  # type: ignore[arg-type]


class TestDryRunSurvives:
    def test_trading_forbidden_by_configuration_still_searches(self) -> None:
        """«Можно искать, не торгуя» — правило 11, и оно не отменяется.

        Выключатель оператора в этом случае бесполезен: включить
        торговлю он не может, и связывать с ним поиск значило бы
        запретить сухой прогон совсем.
        """
        container = _Container(trading=_switch(allowed=False, started=False))

        assert _search_allowed(container, ScanMode.ANN) is True  # type: ignore[arg-type]

    def test_a_missing_trading_subsystem_still_searches(self) -> None:
        """Без ключа подсистемы исполнения нет — но поиск остаётся."""
        container = _Container(trading=None)

        assert _search_allowed(container, ScanMode.ANN) is True  # type: ignore[arg-type]


class TestOtherModesAreUntouched:
    @pytest.mark.parametrize("mode", [ScanMode.UR, ScanMode.FEST])
    def test_modes_that_do_not_trade_ignore_the_switch(self, mode: ScanMode) -> None:
        """``ur`` и ``fest`` ничего не исполняют: торговый выключатель им чужой."""
        container = _Container(trading=_switch(allowed=True, started=False))

        assert _search_allowed(container, mode) is True  # type: ignore[arg-type]


@dataclass
class _Scope:
    """Границы прохода: сети и суммы — всё, что нужно правилу остатка."""

    networks: tuple[str, ...]
    raw_amounts: tuple[int, ...]


class _Level1:
    def __init__(self, scopes: tuple[_Scope, ...]) -> None:
        self._scopes = scopes

    def scopes(self, mode: ScanMode) -> tuple[_Scope, ...]:  # noqa: ARG002
        return self._scopes


class _Executor:
    """Исполнитель, у которого остаток задан заранее."""

    def __init__(self, room: bool) -> None:
        self._room = room
        self.asked: list[tuple[str, tuple[int, ...]]] = []

    async def has_room(self, network_id: str, raw_amounts: tuple[int, ...]) -> bool:
        self.asked.append((network_id, raw_amounts))
        return self._room


@dataclass
class _BudgetContainer:
    trading: TradingSwitch | None
    executor: _Executor | None
    level1: _Level1


def _budget_container(
    *, allowed: bool = True, room: bool = True, scopes: tuple[_Scope, ...] | None = None
) -> _BudgetContainer:
    return _BudgetContainer(
        trading=_switch(allowed=allowed, started=True),
        executor=_Executor(room),
        level1=_Level1(scopes if scopes is not None else (_Scope(("polygon",), (100_000_000,)),)),
    )


class TestBudgetStopsTheSearch:
    """Проход не начинается, когда покупать не на что.

    Исполнитель всё равно отбросит находку по остатку, и опрашивать
    ради неё агрегаторов — тратить их квоту впустую.
    """

    async def test_a_locked_balance_stops_the_trading_search(self) -> None:
        container = _budget_container(room=False)

        assert await _budget_allows(container, ScanMode.ANN) is False  # type: ignore[arg-type]

    async def test_a_free_balance_allows_the_search(self) -> None:
        container = _budget_container(room=True)

        assert await _budget_allows(container, ScanMode.ANN) is True  # type: ignore[arg-type]

    async def test_one_network_with_room_is_enough(self) -> None:
        """Занятость одной сети не отменяет прохода по другой."""
        container = _budget_container(
            room=True,
            scopes=(_Scope(("polygon",), (100_000_000,)), _Scope(("arbitrum",), (100_000_000,))),
        )

        assert await _budget_allows(container, ScanMode.ANN) is True  # type: ignore[arg-type]

    async def test_other_modes_are_untouched(self) -> None:
        """``ur`` и ``fest`` денег не тратят, и остаток им не указ."""
        container = _budget_container(room=False)

        assert await _budget_allows(container, ScanMode.FEST) is True  # type: ignore[arg-type]
        assert await _budget_allows(container, ScanMode.UR) is True  # type: ignore[arg-type]

    async def test_the_dry_run_survives_a_locked_balance(self) -> None:
        """Сухой прогон ищет и при нулевом остатке.

        Когда торговля запрещена конфигурацией, деньги не тратятся
        вовсе: правило 11 прямо разрешает «искать, не торгуя».
        """
        container = _budget_container(allowed=False, room=False)

        assert await _budget_allows(container, ScanMode.ANN) is True  # type: ignore[arg-type]
        assert container.executor is not None
        assert container.executor.asked == [], "остаток у исполнителя не спрашивали"

    async def test_no_scope_leaves_the_reason_to_the_next_check(self) -> None:
        """Пустой набор границ — не про остаток.

        Причину назовёт проверка работающих провайдеров, и подменять её
        сообщением о занятых деньгах нельзя: оператор пошёл бы чинить не
        то.
        """
        container = _budget_container(room=False, scopes=())

        assert await _budget_allows(container, ScanMode.ANN) is True  # type: ignore[arg-type]


class TestLongWaitButton:
    """Под уведомлением о зависшей сделке есть способ её закрыть.

    Сообщить о застрявших деньгах и не дать способа их вернуть —
    половина дела (``the_main_rules.md``, правило 22).
    """

    def test_the_notice_carries_a_sell_button(self) -> None:
        rows = _sell_buttons("#T41")

        labels = [button.label for row in rows for button in row]
        assert labels == ["💸 Продать по текущей цене"]

    def test_the_button_names_its_own_trade(self) -> None:
        """Уведомлений может висеть несколько, и каждое ведёт к своему."""
        assert (
            _sell_buttons("#T41")[0][0].callback_data != _sell_buttons("#T42")[0][0].callback_data
        )
        assert "#T41" in _sell_buttons("#T41")[0][0].callback_data
