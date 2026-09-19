"""Наблюдение за исходом обращений к провайдеру.

Health Monitoring описывает доступность по фактическим наблюдениям
(``19_HEALTH_MONITORING.md`` §43): без записи результатов обращений
состояние провайдера навсегда осталось бы ``UNKNOWN``.

Обёртка не меняет поведение адаптера: она не подавляет ошибки, не
добавляет повторов и не принимает бизнес-решений. Запись выполняется
через узкий порт, поэтому инфраструктура провайдеров не знает реализацию
Health Monitoring.

Health описывает **доступность** провайдера, а не исход конкретного
запроса (``19_HEALTH_MONITORING.md`` §54-56). Поэтому отказом считается
не любая ошибка, а только та, что говорит о недоступности API: сеть,
таймаут, ограничение частоты, ошибка самого провайдера и отвергнутые
credentials. Отсутствие ликвидности для пары, отвергнутый запрос,
неподдерживаемая операция и наши собственные ограничения ресурса
доступности не опровергают — провайдер на них ответил. Классификацию
задаёт общий перечень категорий, второй классификации здесь нет.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, Protocol, runtime_checkable

from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import MonikError
from monik.domain.errors.classification import is_availability_failure
from monik.domain.models.execution import (
    OrderState,
    PlacedOrder,
    SignedOrderPlan,
    SwapTransaction,
)
from monik.domain.models.fee import Fee
from monik.domain.models.quote import Quote
from monik.domain.value_objects.identifiers import RequestId
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.providers.contract import (
    AdapterCapabilities,
    AdapterHealth,
    AggregatorAdapter,
    QuoteRequest,
    RouteValidation,
)

__all__ = ["HealthTrackingAdapter", "ProviderHealthRecorder"]


@runtime_checkable
class ProviderHealthRecorder(Protocol):
    """Приёмник наблюдений о доступности провайдера."""

    def record_provider_success(self, provider_id: ProviderId) -> object:
        """Учесть успешное обращение."""
        ...

    def record_provider_failure(
        self, provider_id: ProviderId, *, reason: str | None = None
    ) -> object:
        """Учесть неудачное обращение."""
        ...


class HealthTrackingAdapter:
    """Адаптер, сообщающий Health Monitoring исход каждого обращения."""

    def __init__(self, adapter: AggregatorAdapter, recorder: ProviderHealthRecorder) -> None:
        self._adapter = adapter
        self._recorder = recorder

    @property
    def provider_id(self) -> ProviderId:
        """Идентификатор провайдера."""
        return self._adapter.provider_id

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Заявленные возможности обёрнутого адаптера."""
        return self._adapter.capabilities

    @property
    def wrapped(self) -> AggregatorAdapter:
        """Обёрнутый адаптер."""
        return self._adapter

    async def get_quote(self, request: QuoteRequest) -> Quote:
        """Получить котировку, зафиксировав исход обращения."""
        return await self._observe(lambda: self._adapter.get_quote(request))

    async def build_swap(self, request: QuoteRequest) -> SwapTransaction:
        """Собрать транзакцию, зафиксировав исход обращения."""
        return await self._observe(lambda: self._adapter.build_swap(request))

    async def build_order(self, request: QuoteRequest) -> SignedOrderPlan:
        """Собрать заявку, зафиксировав исход обращения."""
        return await self._observe(lambda: self._adapter.build_order(request))

    async def place_order(
        self, plan: SignedOrderPlan, signature: str, *, request_id: RequestId | None = None
    ) -> PlacedOrder:
        """Разместить заявку, зафиксировав исход обращения."""
        return await self._observe(
            lambda: self._adapter.place_order(plan, signature, request_id=request_id)
        )

    async def order_state(
        self,
        network_id: NetworkId,
        uid: str,
        *,
        operation: OperationType,
        request_id: RequestId | None = None,
    ) -> OrderState:
        """Узнать судьбу заявки, зафиксировав исход обращения."""
        return await self._observe(
            lambda: self._adapter.order_state(
                network_id, uid, operation=operation, request_id=request_id
            )
        )

    async def validate_fixed_route(self, request: QuoteRequest) -> RouteValidation:
        """Проверить зафиксированный маршрут, зафиксировав исход обращения.

        Несовпадение маршрута — бизнес-результат, а не сбой провайдера:
        успешный ответ учитывается как успех независимо от вердикта.
        """
        return await self._observe(lambda: self._adapter.validate_fixed_route(request))

    async def discover_capabilities(self) -> AdapterCapabilities:
        """Уточнить возможности через API."""
        return await self._observe(self._adapter.discover_capabilities)

    async def discover_fees(self, network_id: NetworkId) -> tuple[Fee, ...]:
        """Получить raw информацию о комиссиях."""
        return await self._observe(lambda: self._adapter.discover_fees(network_id))

    async def health_check(self) -> AdapterHealth:
        """Проверить доступность API.

        Наблюдение здесь не записывается: health check — явная проверка,
        её результат интерпретирует вызывающая сторона
        (``19_HEALTH_MONITORING.md`` §38-39). Обёртка учитывает исход
        рабочих обращений, а не проверок.
        """
        return await self._adapter.health_check()

    async def aclose(self) -> None:
        """Освободить ресурсы обёрнутого адаптера."""
        await self._adapter.aclose()

    async def _observe[T](self, call: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """Выполнить обращение и записать наблюдение.

        Ошибка, не свидетельствующая о недоступности, не записывается ни
        как отказ, ни как успех: она вообще не является наблюдением о
        доступности провайдера, и счётчики остаются как были.
        """
        try:
            result = await call()
        except MonikError as error:
            if is_availability_failure(error.info):
                self._recorder.record_provider_failure(self.provider_id, reason=error.info.code)
            raise
        self._recorder.record_provider_success(self.provider_id)
        return result
