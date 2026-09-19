"""Общая основа адаптеров, работающих по HTTP.

Здесь сосредоточена инфраструктурная обвязка, одинаковая для всех
провайдеров: выполнение запроса через Resource Manager, применение
credentials, нормализация HTTP-статусов и таймаутов.

Provider-specific детали (endpoints, параметры, разбор ответа, правила
комиссий) остаются в модуле конкретного адаптера
(``06_AGGREGATOR_ADAPTERS.md`` §3, §27-28).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from monik.config.secrets import SecretValue
from monik.config.sections.providers import ProviderConfig
from monik.domain.enums.capability import CapabilityOperation
from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.domain.enums.resources import RequestPriority
from monik.domain.errors import (
    AuthenticationError,
    NoRouteError,
    ProviderError,
    RouteRejectedError,
    UnsupportedError,
)
from monik.domain.models.execution import (
    OrderState,
    PlacedOrder,
    SignedOrderPlan,
    SwapTransaction,
)
from monik.domain.models.resource import ResourceKey, ResourceRequest
from monik.domain.value_objects.identifiers import CorrelationId, RequestId
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.http import HttpClient, HttpRequest, HttpResponse, classify_response
from monik.infrastructure.providers.contract import QuoteRequest
from monik.services.observability.clock import Clock
from monik.services.observability.redaction import REDACTED, redact_text
from monik.services.resources import ResourceManager

__all__ = ["HttpProviderAdapter"]


class HttpProviderAdapter:
    """Базовая реализация адаптера поверх HTTP.

    Все внешние вызовы выполняются через Resource Manager
    (``06_AGGREGATOR_ADAPTERS.md`` §31): собственных повторов, очередей и
    ограничений частоты адаптер не создаёт.
    """

    def __init__(
        self,
        provider_id: ProviderId,
        config: ProviderConfig,
        *,
        http: HttpClient,
        resources: ResourceManager,
        clock: Clock,
        api_key: SecretValue | None = None,
        base_url: str,
    ) -> None:
        self._provider_id = provider_id
        self._config = config
        self._http = http
        self._resources = resources
        self._clock = clock
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    @property
    def provider_id(self) -> ProviderId:
        """Идентификатор провайдера."""
        return self._provider_id

    @property
    def base_url(self) -> str:
        """Базовый URL API."""
        return self._base_url

    async def build_swap(self, request: QuoteRequest) -> SwapTransaction:
        """Сборка транзакции по умолчанию не поддерживается.

        Адаптер, умеющий исполнять, переопределяет метод и объявляет
        ``supports_execution``. Отказ здесь честнее «почти готового»
        вызова: провайдер, который не отдаёт транзакцию, годится для
        поиска, но не для торговли (``06_AGGREGATOR_ADAPTERS.md`` §15).
        """
        raise UnsupportedError(
            f"{self._provider_id.value} adapter cannot build swap transactions",
            code="provider_execution_unsupported",
            provider_code=self._provider_id.value,
        )

    async def build_order(self, request: QuoteRequest) -> SignedOrderPlan:
        """Заявками этот провайдер не исполняет.

        Форма исполнения — свойство провайдера, а не выбор вызывающего.
        Отказ честнее подделки: тот, кто отдаёт транзакцию, заявку
        собрать не может (``the_main_rules.md``, правило 16).
        """
        raise UnsupportedError(
            f"{self._provider_id.value} adapter executes transactions, not signed orders",
            code="provider_orders_unsupported",
            provider_code=self._provider_id.value,
        )

    async def place_order(
        self, plan: SignedOrderPlan, signature: str, *, request_id: RequestId | None = None
    ) -> PlacedOrder:
        """Книги заявок у этого провайдера нет."""
        raise UnsupportedError(
            f"{self._provider_id.value} adapter has no order book",
            code="provider_orders_unsupported",
            provider_code=self._provider_id.value,
        )

    async def order_state(
        self,
        network_id: NetworkId,
        uid: str,
        *,
        operation: OperationType,
        request_id: RequestId | None = None,
    ) -> OrderState:
        """Книги заявок у этого провайдера нет."""
        raise UnsupportedError(
            f"{self._provider_id.value} adapter has no order book",
            code="provider_orders_unsupported",
            provider_code=self._provider_id.value,
        )

    async def aclose(self) -> None:
        """Освободить ресурсы HTTP-клиента."""
        await self._http.aclose()

    # --- выполнение запросов ---------------------------------------------

    def auth_headers(self) -> dict[str, str]:
        """Заголовки аутентификации.

        Значение секрета берётся только здесь и не логируется
        (``06_AGGREGATOR_ADAPTERS.md`` §14).
        """
        if self._api_key is None:
            return {}
        return {"Authorization": f"Bearer {self._api_key.get()}"}

    def error_detail(self, response: HttpResponse) -> str | None:
        """Provider-специфичная диагностика неуспешного ответа.

        База ничего не знает о формате ошибок конкретного API и поэтому
        ничего не извлекает. Адаптер, который умеет разбирать свой формат,
        переопределяет метод и возвращает **уже отредактированный** текст
        (см. :meth:`redact_provider_text`): сырое тело ответа и заголовки в
        ошибку не попадают (``06_AGGREGATOR_ADAPTERS.md`` §14,
        ``22_SECURITY.md``).
        """
        return None

    def error_body(self, response: HttpResponse) -> dict[str, Any] | None:
        """Тело неуспешного ответа, разобранное как JSON-объект.

        Общая часть разбора: каждый провайдер описывает ошибку своими
        полями, но все они передают её объектом JSON. Адаптеру остаётся
        только назвать свои поля, а не повторять разбор.
        """
        try:
            body = json.loads(response.text)
        except (ValueError, TypeError):
            return None
        if not isinstance(body, dict):
            return None
        return body

    def no_route_reason(self, response: HttpResponse) -> str | None:
        """Provider-специфичное распознавание штатного «маршрута нет».

        Отсутствие маршрута — отрицательный бизнес-результат, а не сбой
        (``06_AGGREGATOR_ADAPTERS.md`` §75-77), но сообщают о нём
        провайдеры по-разному: один отдельным полем успешного ответа,
        другой статусом ``404`` со своим кодом, третий статусом ``400``.
        База не знает ни одной из этих форм и поэтому не распознаёт
        ничего; адаптер переопределяет метод и переводит свою форму в
        общее понятие системы.

        Возвращается пояснение для :class:`NoRouteError` либо ``None``,
        если ответ к отсутствию маршрута отношения не имеет — тогда он
        классифицируется как обычно.
        """
        return None

    def route_rejection_reason(self, response: HttpResponse) -> str | None:
        """Provider-специфичное распознавание отвергнутого маршрута.

        Отличается от :meth:`no_route_reason` причиной: маршрут для пары
        существует, но провайдер отказался его предлагать по собственному
        правилу — например, ожидаемая потеря выше допустимого им влияния
        на цену. Итог для системы такой же штатный, но сведения разные, и
        смешивать их нельзя.

        База не знает ни одной провайдерской формы такого отказа; адаптер
        переопределяет метод и возвращает пояснение для
        :class:`RouteRejectedError` либо ``None``.
        """
        return None

    def temporary_failure_reason(self, response: HttpResponse) -> str | None:
        """Provider-специфичное распознавание временного сбоя.

        Часть провайдеров сообщает о собственных неполадках обычным 4xx —
        статусом, который по умолчанию означает ошибку запроса и повтора
        не вызывает. Если провайдер прямо говорит, что повтор может
        удаться, отказ обязан стать временным: иначе котировка теряется
        там, где хватило бы повтора (``CLAUDE.md`` §31-32).

        База не знает ни одной такой формы; адаптер переопределяет метод
        и возвращает пояснение для :class:`ProviderError` либо ``None``.
        """
        return None

    def redact_provider_text(self, text: str) -> str:
        """Скрыть секреты в тексте, полученном от провайдера.

        Помимо общего реестра секретов вычёркивается ключ именно этого
        адаптера: ответ API может процитировать переданный ключ, и такой
        текст не должен попасть ни в ошибку, ни в лог, ни в уведомление
        (``22_SECURITY.md``).
        """
        result = text
        if self._api_key is not None:
            result = result.replace(self._api_key.get(), REDACTED)
        return redact_text(result)

    def require_credentials(self) -> SecretValue:
        """Убедиться, что credentials заданы."""
        if self._api_key is None:
            raise AuthenticationError(
                f"{self._provider_id.value} adapter has no credentials configured",
                code="provider_credentials_missing",
                provider_code=self._provider_id.value,
            )
        return self._api_key

    async def request_json(
        self,
        *,
        path: str,
        network_id: NetworkId,
        operation: CapabilityOperation,
        request_id: RequestId,
        method: str = "GET",
        params: dict[str, str] | None = None,
        json_body: Any = None,
        priority: RequestPriority = RequestPriority.UR_LEVEL1_BUY,
        correlation_id: CorrelationId | None = None,
        timeout: timedelta | None = None,
        deduplication_key: str | None = None,
        batch_units: int = 1,
        priority_at: datetime | None = None,
    ) -> Any:
        """Выполнить запрос и вернуть разобранный JSON."""
        response = await self.request(
            path=path,
            network_id=network_id,
            operation=operation,
            request_id=request_id,
            method=method,
            params=params,
            json_body=json_body,
            priority=priority,
            correlation_id=correlation_id,
            timeout=timeout,
            deduplication_key=deduplication_key,
            batch_units=batch_units,
            priority_at=priority_at,
        )
        return response.json()

    async def request(
        self,
        *,
        path: str,
        network_id: NetworkId,
        operation: CapabilityOperation,
        request_id: RequestId,
        method: str = "GET",
        params: dict[str, str] | None = None,
        json_body: Any = None,
        priority: RequestPriority = RequestPriority.UR_LEVEL1_BUY,
        correlation_id: CorrelationId | None = None,
        timeout: timedelta | None = None,
        deduplication_key: str | None = None,
        batch_units: int = 1,
        priority_at: datetime | None = None,
    ) -> HttpResponse:
        """Выполнить запрос через Resource Manager.

        Метод и тело задаются адаптером: одни провайдеры принимают
        параметры в query, другие — в теле POST-запроса.
        """
        url = f"{self._base_url}{path}"
        effective_timeout = timeout or timedelta(seconds=self._config.request_timeout_seconds)
        resource_request = ResourceRequest(
            request_id=request_id,
            key=ResourceKey(
                provider_id=self._provider_id,
                network_id=network_id,
                operation=operation,
            ),
            priority=priority,
            timeout=effective_timeout,
            created_at=self._clock.now(),
            sequence=0,
            correlation_id=correlation_id,
            deduplication_key=deduplication_key,
            batch_units=batch_units,
            priority_at=priority_at,
        )

        async def call() -> HttpResponse:
            response = await self._http.send(
                HttpRequest(
                    method=method,
                    url=url,
                    headers=self.auth_headers(),
                    params=params or {},
                    json_body=json_body,
                    request_id=request_id,
                    timeout_seconds=effective_timeout.total_seconds(),
                )
            )
            if not response.is_success:
                # Отказ «маршрута нет» распознаётся раньше классификации
                # статуса: для части провайдеров он приходит обычной
                # ошибкой 4xx, и без перевода штатный отрицательный ответ
                # стал бы ошибкой данных — с повтором, вкладом в
                # статистику отказов и шумом в логах.
                reason = self.no_route_reason(response)
                if reason is not None:
                    raise NoRouteError(
                        reason,
                        code="provider_no_route",
                        provider_code=self._provider_id.value,
                        http_status=response.status_code,
                        request_id=response.request_id,
                    )
                rejection = self.route_rejection_reason(response)
                if rejection is not None:
                    raise RouteRejectedError(
                        rejection,
                        code="provider_route_rejected",
                        provider_code=self._provider_id.value,
                        http_status=response.status_code,
                        request_id=response.request_id,
                    )
                temporary = self.temporary_failure_reason(response)
                if temporary is not None:
                    raise ProviderError(
                        temporary,
                        code="provider_temporary_failure",
                        provider_code=self._provider_id.value,
                        http_status=response.status_code,
                        request_id=response.request_id,
                    )
            classify_response(
                response,
                provider=self._provider_id.value,
                detail=self.error_detail(response) if not response.is_success else None,
            )
            return response

        return await self._resources.execute(resource_request, call)
