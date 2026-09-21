"""Кнопки действий над открытыми сделками.

Отдельный модуль, потому что кнопки нужны двум разным сообщениям —
уведомлению о затянувшемся ожидании и получасовому отчёту, — а собирают
их разные части приложения. Держать их в любой из этих частей значило бы
заставить другую импортировать её целиком.

Формат данных кнопки принадлежит подсистеме команд: здесь он только
берётся готовым, а подписи выбираются по месту.
"""

from __future__ import annotations

from collections.abc import Sequence

from monik.domain.enums.trading import PositionStatus
from monik.domain.models.position import Position
from monik.services.commands.parser import sell_callback_data
from monik.services.notifications.ports import MessageButton

__all__ = ["sell_buttons", "sell_buttons_for"]


def sell_buttons(*t_ids: object) -> tuple[tuple[MessageButton, ...], ...]:
    """Кнопки «продать по текущей цене» для названных сделок.

    Номер сделки несёт сама кнопка: открытых сделок и сообщений о них
    может быть несколько, и нажатое должно относиться к своей. Поэтому
    каждой сделке — своя кнопка и своя строка.

    Под сообщением об одной сделке номер в подписи не нужен: он уже есть
    в тексте, и повторять его — занимать место без пользы.
    """
    single = len(t_ids) == 1
    return tuple(
        (
            MessageButton(
                label="💸 Продать по текущей цене" if single else f"💸 Продать {t_id}",
                callback_data=sell_callback_data(str(t_id)),
            ),
        )
        for t_id in t_ids
    )


def sell_buttons_for(positions: Sequence[Position]) -> tuple[tuple[MessageButton, ...], ...]:
    """Кнопки продажи для открытых сделок отчёта.

    Закрывать имеет смысл только то, что дождалось покупки: у сделки,
    которую ведёт начатая покупка или продажа, распоряжаться нечем, и
    предлагать действие, которое не выполнится, хуже, чем не предлагать
    его вовсе.
    """
    return sell_buttons(
        *(position.t_id for position in positions if position.status is PositionStatus.HOLDING)
    )
