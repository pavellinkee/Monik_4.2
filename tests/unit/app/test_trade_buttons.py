"""Кнопки продажи под сообщениями об открытых сделках.

Открытая сделка в отчёте — повод её закрыть, а не только повод о ней
прочитать (``the_main_rules.md``, правило 25).
"""

from __future__ import annotations

from monik.app.trade_buttons import sell_buttons, sell_buttons_for
from monik.domain.enums.providers import ProviderId
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.position import Position
from monik.domain.value_objects.identifiers import TId
from tests import factories as f


def _position(sequence: int, status: PositionStatus = PositionStatus.HOLDING) -> Position:
    return Position(
        t_id=TId.from_sequence(sequence),
        network_id=f.POLYGON,
        status=status,
        base_token=f.USDT,
        target_token=f.AAVE,
        buy_provider_id=ProviderId.COW,
        sell_provider_id=ProviderId.UNISWAP,
        raw_input=100_000_000,
        raw_acquired=1,
        opened_at=f.NOW,
    )


class TestOneTrade:
    def test_a_single_trade_needs_no_number_in_the_label(self) -> None:
        """Номер уже в тексте сообщения, повторять его незачем."""
        rows = sell_buttons("#T41")

        assert [button.label for row in rows for button in row] == ["💸 Продать по текущей цене"]

    def test_the_button_names_its_own_trade(self) -> None:
        assert "#T41" in sell_buttons("#T41")[0][0].callback_data


class TestSeveralTrades:
    def test_every_open_trade_gets_its_own_button(self) -> None:
        rows = sell_buttons_for((_position(45), _position(46)))

        assert [button.label for row in rows for button in row] == [
            "💸 Продать #T45",
            "💸 Продать #T46",
        ]

    def test_each_button_carries_its_own_trade(self) -> None:
        """Иначе нажатие закрыло бы не ту сделку."""
        rows = sell_buttons_for((_position(45), _position(46)))

        assert "#T45" in rows[0][0].callback_data
        assert "#T46" in rows[1][0].callback_data

    def test_a_trade_in_progress_gets_no_button(self) -> None:
        """У сделки, которую ведёт начатая операция, распоряжаться нечем.

        Предлагать действие, которое не выполнится, хуже, чем не
        предлагать его вовсе.
        """
        rows = sell_buttons_for(
            (
                _position(45, PositionStatus.SELLING),
                _position(46, PositionStatus.BUYING),
                _position(47),
            )
        )

        assert [button.label for row in rows for button in row] == ["💸 Продать по текущей цене"]
        assert "#T47" in rows[0][0].callback_data

    def test_nothing_open_means_no_buttons(self) -> None:
        assert sell_buttons_for(()) == ()
