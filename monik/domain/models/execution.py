"""Готовая к отправке транзакция обмена.

Как котировка превращается в транзакцию — особенность конкретного
агрегатора: Uniswap отдаёт готовый вызов роутера отдельным методом,
KyberSwap собирает calldata из выбранного маршрута. Эти различия
остаются в адаптерах (``CLAUDE.md`` §7), а наружу выходит одно понятие:
что вызвать, с какими данными и какой минимум мы согласны получить.

Минимум — не формальность. Транзакция несёт его в себе, и если пул не
даёт обещанного, обмен **откатывается сетью**. Поэтому завышенная
котировка приводит к потере газа, а не к исполнению по плохому курсу.
"""

from __future__ import annotations

from typing import Any, Protocol, Self, runtime_checkable

from pydantic import Field, model_validator

from monik.domain.enums.base import DomainEnum
from monik.domain.enums.providers import ProviderId
from monik.domain.models.base import DomainModel
from monik.domain.models.quote import Quote
from monik.domain.value_objects.identity import NetworkId

__all__ = [
    "AllowanceKind",
    "AllowanceRequirement",
    "ExecutionOffer",
    "OrderOutcome",
    "OrderState",
    "PlacedOrder",
    "SignedOrderPlan",
    "SwapTransaction",
]


class AllowanceKind(DomainEnum):
    """Каким вызовом выдаётся разрешение."""

    #: Стандартный ``approve(address,uint256)`` у самого токена.
    ERC20 = "erc20"
    #: ``approve(address,address,uint160,uint48)`` у контракта Permit2.
    #: Uniswap списывает токены через него, и разрешения нужны оба:
    #: токен разрешает Permit2, Permit2 разрешает роутеру.
    PERMIT2 = "permit2"


class AllowanceRequirement(DomainModel):
    """Разрешение, без которого транзакция не исполнится.

    Сколько таких требований и какого они вида — особенность агрегатора.
    KyberSwap списывает токен напрямую и требует одно разрешение; Uniswap
    ходит через Permit2 и требует два. Общая логика этих различий не
    знает: она просто выдаёт то, что названо.
    """

    kind: AllowanceKind
    #: Контракт, которому адресуется вызов ``approve``.
    contract: str = Field(min_length=42, max_length=42)
    #: Кто получает право списывать.
    spender: str = Field(min_length=42, max_length=42)

    def describe(self) -> str:
        """Строка для журнала и подтверждения оператором."""
        return f"{self.kind.value}: {self.contract} → {self.spender}"


class SwapTransaction(DomainModel):
    """Вызов роутера агрегатора, собранный из котировки."""

    provider_id: ProviderId
    network_id: NetworkId
    chain_id: int = Field(gt=0)
    #: Контракт, которому адресована транзакция.
    to: str = Field(min_length=42, max_length=42)
    #: Закодированный вызов.
    data: str = Field(min_length=2)
    #: Сколько native token отправляется вместе с вызовом. Для обмена
    #: между ERC-20 токенами — ноль.
    value: int = Field(ge=0)
    #: Предел газа, названный агрегатором.
    gas_limit: int = Field(gt=0)
    #: Разрешения, без которых транзакция откатится. Порядок значим:
    #: выдавать их нужно в том же порядке, в каком они перечислены.
    allowances: tuple[AllowanceRequirement, ...] = Field(min_length=1)
    #: Котировка, из которой собрана транзакция. Хранится целиком: решение
    #: об отправке принимается по ней, а не по той, на которой возможность
    #: была найдена минутой раньше.
    quote: Quote
    #: Минимум, ниже которого сделка откатится.
    min_output_raw: int = Field(gt=0)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Минимум обязан быть не выше ожидаемого выхода.

        Обратное означало бы транзакцию, которая откатится всегда: мы
        требуем больше, чем сам агрегатор обещает.
        """
        if self.min_output_raw > self.quote.output_amount.raw:
            raise ValueError(
                "minimum output exceeds the quoted output: such a swap can only revert"
            )
        if not self.data.startswith("0x"):
            raise ValueError("swap calldata must be hex-encoded with a 0x prefix")
        return self

    @property
    def slippage_room_raw(self) -> int:
        """На сколько выход может просесть, прежде чем сделка откатится."""
        return self.quote.output_amount.raw - self.min_output_raw


@runtime_checkable
class ExecutionOffer(Protocol):
    """Общее в предложении агрегатора, чем бы оно ни исполнялось.

    Решение об открытии сделки принимается по **обязательству**, а не по
    котировке (``the_main_rules.md``, правило 12). Обязательство есть у
    обеих форм исполнения: у транзакции это минимум, зашитый в calldata,
    у подписанной заявки — предельная сумма самой заявки. Проверяющему
    коду достаточно этих двух полей, и знать форму исполнения ему не
    нужно.
    """

    @property
    def quote(self) -> Quote:
        """Котировка, из которой собрано предложение."""
        ...

    @property
    def min_output_raw(self) -> int:
        """Минимум, ниже которого сделка не состоится."""
        ...


class OrderOutcome(DomainEnum):
    """Состояние размещённой заявки."""

    #: Заявка в книге, решатель её ещё не взял.
    OPEN = "open"
    #: Заявка исполнена — полностью или частично.
    FILLED = "filled"
    #: Срок истёк, исполнения не было. Это штатный исход, а не сбой.
    EXPIRED = "expired"
    #: Заявка отозвана.
    CANCELLED = "cancelled"

    @property
    def is_final(self) -> bool:
        """Изменится ли состояние ещё."""
        return self is not OrderOutcome.OPEN


class SignedOrderPlan(DomainModel):
    """Заявка, которую остаётся подписать и разместить.

    Форма исполнения провайдера, который транзакции не отдаёт
    (``the_main_rules.md``, правило 16). Отличия от
    :class:`SwapTransaction` существенные и намеренно видны в типе:

    * подписывается не транзакция, а типизированные данные EIP-712;
    * газа у такой ноги нет — стоимость исполнения удержана из
      продаваемого токена и уже учтена в котировке;
    * симулировать её вызовом узла невозможно: транзакции не существует.
      Защитой служит ``min_output_raw`` — решатель обязан выдать не
      меньше, иначе расчёт не состоится.
    """

    provider_id: ProviderId
    network_id: NetworkId
    chain_id: int = Field(gt=0)
    #: Полные типизированные данные EIP-712: домен, типы и сама заявка.
    #: Хранятся целиком и непрозрачно — ровно как ``data`` у транзакции.
    #: Собирает их адаптер, подписывает кошелёк, разбирать их общей
    #: логике не нужно.
    typed_data: dict[str, Any]
    #: Разрешения, без которых заявка не исполнится.
    allowances: tuple[AllowanceRequirement, ...] = Field(min_length=1)
    #: Котировка, из которой собрана заявка.
    quote: Quote
    #: Предельная сумма: меньше решатель отдать не может.
    min_output_raw: int = Field(gt=0)
    #: Момент, после которого заявка недействительна (unix-время).
    valid_to: int = Field(gt=0)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Предел не может быть выше обещанного выхода."""
        if self.min_output_raw > self.quote.output_amount.raw:
            raise ValueError(
                "minimum output exceeds the quoted output: such an order can never be filled"
            )
        if "message" not in self.typed_data:
            raise ValueError("typed data must carry the order itself under 'message'")
        return self


class PlacedOrder(DomainModel):
    """Заявка, принятая книгой."""

    provider_id: ProviderId
    network_id: NetworkId
    #: Идентификатор заявки в книге провайдера.
    uid: str = Field(min_length=1)
    #: Момент, после которого заявка недействительна (unix-время).
    valid_to: int = Field(gt=0)


class OrderState(DomainModel):
    """Состояние заявки, как его сообщает книга.

    Исполненные суммы приходят вместе с состоянием, поэтому отдельного
    запроса за ними не требуется (``the_main_rules.md``, минимум
    обращений).
    """

    outcome: OrderOutcome
    #: Сколько продано и сколько получено на деле. Ноль у неисполненной
    #: заявки — это знание, а не пробел.
    executed_input_raw: int = Field(ge=0)
    executed_output_raw: int = Field(ge=0)

    @property
    def is_final(self) -> bool:
        """Изменится ли состояние ещё."""
        return self.outcome.is_final
