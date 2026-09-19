"""Сделка режима ``ann``: покупка, ожидание, продажа.

Позиция — единственное место, где Monik держит деньги. Поэтому она
хранится в базе с самого начала, ещё до того как покупка попадёт в блок:
иначе перезапуск между отправкой и записью оставил бы токены, о которых
система не знает.

Суммы хранятся в base units и в знаках своего токена. Выводить знаки из
символа запрещено (``09_PROFIT_CALCULATOR.md`` §5), поэтому у каждой
суммы свой токен.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from monik.domain.enums.providers import ProviderId
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.base import DomainModel
from monik.domain.models.token import Token
from monik.domain.value_objects.identifiers import TId
from monik.domain.value_objects.identity import NetworkId
from monik.domain.value_objects.timestamps import UtcDatetime

__all__ = ["Position"]


class Position(DomainModel):
    """Одна сделка: круг ``base → target → base``."""

    t_id: TId
    network_id: NetworkId
    status: PositionStatus
    #: Токен, которым входим и выходим.
    base_token: Token
    #: Токен, который покупаем и держим.
    target_token: Token
    buy_provider_id: ProviderId
    sell_provider_id: ProviderId

    #: Сколько базового токена вложено.
    raw_input: int = Field(gt=0)
    #: Сколько промежуточного токена фактически получено. ``None``, пока
    #: покупка не подтверждена: расчётное количество не подставляется —
    #: неизвестное не равно ожидаемому.
    raw_acquired: int | None = None
    #: Сколько базового токена получено обратно. Это **выручка продажи**,
    #: а не остаток счёта: остаток включает деньги, к этой сделке
    #: отношения не имеющие.
    raw_returned: int | None = None

    #: Остатки до каждой ноги. Хранятся, потому что «сколько пришло»
    #: считается разностью, а разность нужно уметь досчитать и после
    #: перезапуска: квитанция может прийти в другом процессе.
    raw_target_before_buy: int | None = None
    raw_base_before_sell: int | None = None

    #: Фактическая стоимость каждой ноги в wei native token сети. Берётся
    #: из квитанции (расход × цена) и потому точна, а не оценочна.
    buy_gas_wei: int | None = None
    sell_gas_wei: int | None = None

    #: Расход газа: обещанный котировкой и фактический, по каждой ноге.
    #:
    #: Хранится не ради отчёта. Во-первых, по общей стоимости не видно,
    #: в чём ошибка — в расходе или в цене; во-вторых, отношение факта к
    #: обещанному и есть поправка, которой уточняется оценка будущих
    #: возможностей.
    buy_quoted_gas_units: int | None = None
    sell_quoted_gas_units: int | None = None
    buy_gas_units: int | None = None
    sell_gas_units: int | None = None
    #: Стоимость газа круга, пересчитанная в базовый токен по курсу на
    #: момент закрытия. ``None`` означает «неизвестна», а не «ноль»:
    #: подставлять ноль запрещено (``CLAUDE.md`` §12).
    raw_gas_cost: int | None = None

    buy_tx_hash: str | None = None
    sell_tx_hash: str | None = None
    #: Идентификаторы подписанных заявок — у ног, исполняемых книгой, а
    #: не цепью (``the_main_rules.md``, правило 16). Ноги круга могут
    #: исполняться по-разному, поэтому поля отдельные: по ним видно, чем
    #: именно шла нога, и незавершённую есть чем восстановить.
    buy_order_uid: str | None = None
    sell_order_uid: str | None = None

    opened_at: UtcDatetime
    updated_at: UtcDatetime | None = None
    closed_at: UtcDatetime | None = None
    #: Когда оператору сообщили о слишком долгом ожидании. Пустое поле
    #: означает «ещё не сообщали»: уведомление шлётся один раз, а не
    #: каждые десять секунд.
    long_wait_notified_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.base_token.network_id != self.network_id:
            raise ValueError("position base token belongs to another network")
        if self.target_token.network_id != self.network_id:
            raise ValueError("position target token belongs to another network")
        if self.base_token.key == self.target_token.key:
            raise ValueError("position must exchange two different tokens")
        if self.status.holds_tokens and self.raw_acquired is None:
            raise ValueError("a position holding tokens must know how many were acquired")
        if self.status is PositionStatus.CLOSED and self.raw_returned is None:
            raise ValueError("a closed position must know how much came back")
        return self

    @property
    def input_amount(self) -> Decimal:
        """Вложенная сумма в человеческих единицах."""
        return Decimal(self.raw_input).scaleb(-self.base_token.decimals)

    @property
    def gross_result_raw(self) -> int | None:
        """Разница токенов круга, **без** стоимости газа.

        ``None``, пока круг не завершён: незавершённая сделка не имеет
        результата, и подставлять вместо него ноль нельзя.
        """
        if self.raw_returned is None:
            return None
        return self.raw_returned - self.raw_input

    @property
    def net_result_raw(self) -> int | None:
        """Итог круга в base units базового токена, **с учётом газа**.

        Газ — такой же расход сделки, как и разница курсов, и без него
        «заработок» не является заработком: круг, выигравший на цене
        меньше, чем стоила пара транзакций, приносит убыток.

        ``None``, если неизвестна хотя бы одна составляющая. Неизвестный
        расход нулём не считается (``CLAUDE.md`` §12).
        """
        gross = self.gross_result_raw
        if gross is None or self.raw_gas_cost is None:
            return None
        return gross - self.raw_gas_cost

    @property
    def net_result(self) -> Decimal | None:
        """Итог круга в человеческих единицах."""
        raw = self.net_result_raw
        if raw is None:
            return None
        return Decimal(raw).scaleb(-self.base_token.decimals)

    @property
    def gross_result(self) -> Decimal | None:
        """Разница токенов круга в человеческих единицах."""
        raw = self.gross_result_raw
        if raw is None:
            return None
        return Decimal(raw).scaleb(-self.base_token.decimals)

    def profit_if_sold_for(self, raw_output: int, *, raw_costs: int = 0) -> int:
        """Каким был бы итог, продай мы сейчас за ``raw_output``.

        ``raw_costs`` — стоимость газа круга в базовом токене. По
        умолчанию ноль, и это не поблажка: ноль здесь означает «расход
        учтён снаружи», а вызывающая сторона обязана передать его явно,
        когда решает, продавать ли.
        """
        return raw_output - self.raw_input - raw_costs
