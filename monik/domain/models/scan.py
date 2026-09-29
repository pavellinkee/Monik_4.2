"""Scan — один цикл работы Level 1."""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from monik.domain.enums.lifecycle import ScanStatus
from monik.domain.enums.modes import ScanMode
from monik.domain.enums.providers import ProviderId
from monik.domain.models.base import DomainModel
from monik.domain.models.token import TokenKey
from monik.domain.value_objects.amounts import Percentage
from monik.domain.value_objects.identifiers import ScanId
from monik.domain.value_objects.identity import NetworkId
from monik.domain.value_objects.numeric import NonNegativeDecimal
from monik.domain.value_objects.timestamps import UtcDatetime

__all__ = ["BestCombination", "Scan", "ScanScope", "ScanStatistics"]


class ScanScope(DomainModel):
    """Границы одного цикла (``36_DATA_MODELS.md`` §52).

    Scope фиксируется на момент старта: изменение конфигурации применяется
    со следующего цикла (``02_LEVEL1_SCANNER.md`` §69).
    """

    #: Режим прохода. Записывается в scope, а не выводится из состава
    #: токенов: по набору нельзя отличить частый проход по стейблкоинам
    #: от основного, если в основном остались только они.
    mode: ScanMode = ScanMode.UR
    networks: tuple[NetworkId, ...] = Field(min_length=1)
    providers: tuple[ProviderId, ...] = Field(min_length=1)
    tokens: tuple[TokenKey, ...] = Field(min_length=1)
    raw_amounts: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if any(amount <= 0 for amount in self.raw_amounts):
            raise ValueError("scan amounts must be positive")
        return self


class BestCombination(DomainModel):
    """Лучшая комбинация цикла, даже если она не прошла порог.

    Комбинация, не дошедшая до порога, отбрасывается, и по результату
    «ноль возможностей» нельзя понять, не хватило ли десятой доли
    процента или доходность была отрицательной. Без этого длительное
    наблюдение отвечает только на вопрос «нашли или нет», но не на
    вопрос «насколько близко было».

    Значение сохраняется целиком: доходность без указания комбинации
    бесполезна, а комбинация без доходности ничего не сообщает.
    """

    net_roi: Percentage
    gross_roi: Percentage | None = None
    token: TokenKey
    buy_provider: ProviderId
    sell_provider: ProviderId

    #: Разбивка газа: во что он обошёлся в валюте расчёта, сколько единиц
    #: обещали котировки и по какой цене.
    #:
    #: Без разбивки по итоговой стоимости не видно, в чём ошибка — в
    #: расходе или в цене, и первое же расхождение с фактом пришлось бы
    #: разбирать по цепи вручную. Хранятся **обещанные** единицы, без
    #: поправки: поправка известна отдельно, а обещанное — исходные данные,
    #: которые больше взять неоткуда.
    gas_cost: NonNegativeDecimal | None = None
    quoted_gas_units: int | None = Field(default=None, ge=0)
    #: Цена газа **из котировки**, а не та, по которой посчитана
    #: стоимость: её называет узел сети, и в разбивке её нет.
    #: Имя говорит об этом прямо — 29.09 разницу между двумя
    #: величинами приняли за расхождение между проходом и
    #: исполнением, хотя расходились только два журнала.
    quoted_gas_price_wei: int | None = Field(default=None, ge=0)


class ScanStatistics(DomainModel):
    """Счётчики цикла (``36_DATA_MODELS.md`` §53).

    Полная история quotes не сохраняется (``30_DATABASE_SCHEMA.md`` §44) —
    хранится только агрегированная статистика.
    """

    #: Запросы, действительно отправленные провайдерам. Отказ собственного
    #: предохранителя сюда не входит: иначе успешность цикла зависела бы
    #: от состояния Monik, а не от ответов агрегаторов.
    quote_requests: int = Field(default=0, ge=0)
    successful_quotes: int = Field(default=0, ge=0)
    failed_quotes: int = Field(default=0, ge=0)
    #: Запросы, не отправленные из-за закрытого ресурса или очереди.
    refused_requests: int = Field(default=0, ge=0)
    skipped_combinations: int = Field(default=0, ge=0)
    deduplicated_requests: int = Field(default=0, ge=0)
    opportunities_created: int = Field(default=0, ge=0)
    duplicate_opportunities: int = Field(default=0, ge=0)
    #: Сколько комбинаций удалось посчитать полностью. Отличается от числа
    #: успешных котировок: комбинация требует обеих ног и всех издержек.
    evaluated_combinations: int = Field(default=0, ge=0)
    #: Лучшая комбинация цикла независимо от порога. ``None`` означает,
    #: что полностью посчитать не удалось ни одну.
    best_combination: BestCombination | None = None
    #: Сколько комбинаций не дошло до порога из-за неизвестного расхода.
    #:
    #: Порог намеренно не засчитывается, если хотя бы один расход,
    #: влияющий на метрику, неизвестен: неизвестное не приравнивается к
    #: нулю (``CLAUDE.md`` §12, ``09_PROFIT_CALCULATOR.md`` §27). Без
    #: этого счётчика причина отсева не видна вовсе — «ноль
    #: возможностей» выглядит одинаково и когда доходность не дотянула,
    #: и когда порог не оценивался.
    blocked_by_unknown_cost: int = Field(default=0, ge=0)
    #: Какие именно расходы оказались неизвестны — метки вида
    #: ``gas:conversion``. Без них известно только, что расчёт неполон,
    #: но не что чинить. Список различных меток, отсортирован и ограничен
    #: по длине: это диагностика, а не полный перечень.
    unknown_cost_components: tuple[str, ...] = ()
    #: Лучшая комбинация среди токенов, не помеченных как стабильные.
    #:
    #: Круг из базового токена в другой стабильный токен и обратно почти
    #: всегда оказывается лучшим: у стабильной пары нет спреда, и она
    #: теряет меньше остальных. Из-за этого по одной записи лучшей
    #: комбинации не видно, насколько близко подходили волатильные
    #: токены, ради которых сканирование и ведётся. Второе значение
    #: отвечает именно на этот вопрос и не требует ни одного лишнего
    #: запроса: комбинации уже посчитаны.
    #:
    #: ``None`` означает, что ни одной волатильной комбинации посчитать
    #: не удалось. Совпадение с :attr:`best_combination` допустимо и
    #: означает, что лучшей в цикле и так была волатильная комбинация.
    best_volatile_combination: BestCombination | None = None


class Scan(DomainModel):
    """Один цикл Level 1 (``36_DATA_MODELS.md`` §51)."""

    scan_id: ScanId
    status: ScanStatus
    scope: ScanScope
    statistics: ScanStatistics = ScanStatistics()
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("scan finished_at must not precede started_at")
        if self.status is ScanStatus.RUNNING and self.finished_at is not None:
            raise ValueError("running scan must not have finished_at")
        if self.status is not ScanStatus.RUNNING and self.finished_at is None:
            raise ValueError(f"scan in status {self.status.value} must have finished_at")
        return self


class ScanSummary(DomainModel):
    """Чем закончились проходы за промежуток.

    Сводка, а не статистика цикла: она отвечает на вопрос оператора
    «сколько искали и насколько близко подошли», и потому живёт рядом с
    самим проходом, а не в том, кто её показывает.
    """

    passes: int = Field(ge=0)
    #: Лучшая чистая доходность за промежуток. ``None`` — проходов не
    #: было либо ни один не дал сравнимого результата; нулём это не
    #: заменяется (``CLAUDE.md`` §12).
    best_net_roi: Decimal | None = None
