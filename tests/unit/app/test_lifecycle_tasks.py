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
from monik.app.lifecycle import _search_allowed
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
