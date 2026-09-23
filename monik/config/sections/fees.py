"""Конфигурация Fee System, gas и цен."""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from monik.config.base import ConfigSection
from monik.domain.enums.base import DomainEnum
from monik.domain.value_objects.numeric import PositiveDecimal

__all__ = ["FeeConfig", "GasConfig", "GasSource", "PriceConfig", "PriceSource"]


class GasSource(DomainEnum):
    """Источник цены газа (решение D-4).

    Бизнес-логика не привязана к конкретному источнику: реализация
    выбирается конфигурацией.
    """

    #: Цена газа из самой котировки агрегатора: лишнего запроса не
    #: требует, потому что значение уже пришло вместе с ценой маршрута.
    QUOTE = "quote"
    RPC = "rpc"
    ADAPTER_ESTIMATE = "adapter_estimate"
    STATIC = "static"


class PriceSource(DomainEnum):
    """Источник курса токена в базовой валюте расчёта (решение D-4)."""

    AGGREGATOR_QUOTE = "aggregator_quote"
    HTTP = "http"
    STATIC = "static"


class FeeConfig(ConfigSection):
    """Параметры Fee System (``17_CONFIGURATION.md`` §38-39).

    Поведение при UNKNOWN обязательной комиссии зафиксировано архитектурой:
    неизвестная комиссия никогда не считается нулевой
    (``07_FEE_SYSTEM.md`` §15), поэтому соответствующий флаг не отключается.
    """

    enabled: bool = True
    refresh_on_startup: bool = True
    refresh_interval_days: int = Field(default=1, ge=1, le=365)
    refresh_time: str = Field(default="02:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    freshness_seconds: int = Field(default=86_400, ge=60, le=2_592_000)
    treat_unknown_as_zero: bool = False
    batch_enabled: bool = True

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.treat_unknown_as_zero:
            raise ValueError(
                "treat_unknown_as_zero must remain false: an unknown mandatory fee "
                "is not a zero fee"
            )
        return self


class GasCalibrationConfig(ConfigSection):
    """Уточнение оценки газа по фактическому расходу.

    Фактический расход приходит в квитанции, которую подсистема исполнения
    и так запрашивает, а обещанный котировкой — в самой котировке. Их
    отношение и есть поправка. Она не стоит ни одного дополнительного
    обращения и сама следует за изменением маршрутов.
    """

    enabled: bool = True
    #: Сколько замеров нужно, чтобы поправке верить. До этого действует
    #: заданная конфигурацией: одна сделка — не статистика.
    min_samples: int = Field(default=5, ge=1, le=1_000)
    #: Предел накопленной истории. По его достижении накопленное
    #: уполовинивается: поправка обязана следовать за изменением маршрутов,
    #: а не усреднять их за всё время работы.
    max_samples: int = Field(default=100, ge=2, le=100_000)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.max_samples <= self.min_samples:
            raise ValueError("max_samples must exceed min_samples")
        return self


class GasConfig(ConfigSection):
    """Параметры получения gas (``17_CONFIGURATION.md`` §40, решение D-4)."""

    enabled: bool = True
    #: Порядок источников цены газа. ``QUOTE`` первым и по умолчанию:
    #: цена приходит вместе с котировкой и лишнего запроса не стоит.
    #: Узел сети остаётся запасным для провайдеров, которые её не
    #: сообщают, и используется на этапе подтверждения.
    sources: tuple[GasSource, ...] = (
        GasSource.QUOTE,
        GasSource.ADAPTER_ESTIMATE,
        GasSource.RPC,
    )
    freshness_seconds: int = Field(default=60, ge=1, le=3600)
    request_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    treat_unknown_as_zero: bool = False
    #: Явно заданная цена газа в wei по сетям. Используется только при
    #: источнике ``STATIC``: это явно настроенный fallback, а не
    #: production-источник данных.
    static_wei_per_gas: dict[str, int] = Field(default_factory=dict)

    #: Поправка к оценке расхода газа, приходящей в котировке.
    #:
    #: Агрегатор оценивает голый обмен по одному лучшему пути, а платим мы
    #: за реальный вызов роутера — с разрешениями, обёртками и маршрутом,
    #: который на исполнении может разойтись на несколько пулов. Разница
    #: измерена и оказалась кратной, а не процентной, поэтому поправка
    #: множителем, а не слагаемым.
    #:
    #: Это запасное значение: как только по агрегатору накопятся замеры,
    #: поправка считается по ним (:attr:`calibration`).
    quote_estimate_multiplier: PositiveDecimal = Decimal("1.0")
    #: Поправка отдельных агрегаторов, по их идентификаторам. Оценки у них
    #: расходятся с фактом по-разному, и общий множитель одному занижал бы,
    #: другому завышал.
    quote_estimate_multipliers: dict[str, PositiveDecimal] = Field(default_factory=dict)

    #: Самокалибровка поправки по фактическому расходу.
    calibration: GasCalibrationConfig = GasCalibrationConfig()

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.treat_unknown_as_zero:
            raise ValueError("treat_unknown_as_zero must remain false: unknown gas is not zero gas")
        if not self.sources:
            raise ValueError("at least one gas source must be configured")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("gas sources must be unique")
        if GasSource.STATIC in self.sources and not self.static_wei_per_gas:
            raise ValueError("static gas source requires static_wei_per_gas")
        if any(value <= 0 for value in self.static_wei_per_gas.values()):
            raise ValueError("static gas price must be positive")
        return self


class PriceConfig(ConfigSection):
    """Параметры конверсии native token в валюту расчёта (решение D-4)."""

    enabled: bool = True
    sources: tuple[PriceSource, ...] = (PriceSource.AGGREGATOR_QUOTE,)
    freshness_seconds: int = Field(default=300, ge=1, le=86_400)
    request_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    http_endpoint: str | None = Field(default=None, max_length=512)
    #: Независимый источник рыночной цены. Не участвует в конверсии газа
    #: и в расчёте прибыли: он нужен, чтобы сверить цену агрегатора с
    #: рынком перед тратой денег (``the_main_rules.md``, правило 26).
    market_endpoint: str | None = Field(default=None, max_length=512)
    #: Сколько держать рыночную цену в памяти. Проверка вызывается редко,
    #: но две подряд не должны стоить двух запросов.
    market_ttl_seconds: int = Field(default=300, ge=1, le=86_400)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.market_endpoint is not None and not self.market_endpoint.startswith("https://"):
            raise ValueError("market_endpoint must use https")
        if not self.sources:
            raise ValueError("at least one price source must be configured")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("price sources must be unique")
        if PriceSource.HTTP in self.sources and not self.http_endpoint:
            raise ValueError("http price source requires http_endpoint")
        if self.http_endpoint is not None and not self.http_endpoint.startswith("https://"):
            raise ValueError("http_endpoint must use https")
        return self
