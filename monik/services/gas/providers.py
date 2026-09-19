"""Источники цены газа.

Решение D-4: ``GasPriceProvider`` — независимая абстракция. Конкретный
источник (RPC, внешний gas API, статическое значение для тестов)
подключается конфигурацией и заменяется без изменения бизнес-логики.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

from monik.domain.enums.capability import CapabilityOperation
from monik.domain.enums.resources import RequestPriority
from monik.domain.errors import DataError
from monik.domain.models.gas import GasPrice
from monik.domain.models.resource import ResourceKey, ResourceRequest
from monik.domain.value_objects.identifiers import RequestId
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.http import HttpClient, HttpRequest, classify_response
from monik.services.observability.clock import Clock
from monik.services.resources import ResourceManager
from monik.services.rpc import RpcEndpoints, call_with_failover

__all__ = [
    "RPC_RESOURCE_OWNER",
    "GasPriceProvider",
    "RpcGasPriceProvider",
    "StaticGasPriceProvider",
]

#: Имя ресурса blockchain RPC в Resource Manager.
RPC_RESOURCE_OWNER = "rpc"


@runtime_checkable
class GasPriceProvider(Protocol):
    """Источник текущей цены газа сети."""

    #: Требует ли источник обращения к внешней системе. Этап поиска
    #: Level 1 не тратит на цену газа ни одного запроса, поэтому
    #: источники с ``True`` на нём не опрашиваются; источники, которые
    #: отвечают из конфигурации или из памяти, доступны всегда.
    requires_request: bool

    async def gas_price(self, network_id: NetworkId) -> GasPrice | None:
        """Текущая цена газа или ``None``, если она недоступна.

        ``None`` означает «неизвестно» и **не** эквивалентно нулю
        (``09_PROFIT_CALCULATOR.md`` §16).
        """
        ...


class StaticGasPriceProvider:
    """Фиксированная цена газа.

    **Test implementation** (``CLAUDE.md`` §10, §46): применяется в тестах и
    как явно настроенный fallback, а не как источник production-данных.
    """

    #: Значение берётся из конфигурации: запроса не требует.
    requires_request = False

    def __init__(self, clock: Clock, *, prices: dict[str, int]) -> None:
        self._clock = clock
        self._prices = dict(prices)

    async def gas_price(self, network_id: NetworkId) -> GasPrice | None:
        """Настроенная цена сети."""
        wei = self._prices.get(str(network_id))
        if wei is None:
            return None
        return GasPrice(
            network_id=network_id,
            wei_per_gas=wei,
            source="static",
            observed_at=self._clock.now(),
        )


class RpcGasPriceProvider:
    """Цена газа из JSON-RPC узла сети.

    Поддерживает EIP-1559: при доступности ``baseFeePerGas`` итоговая цена
    складывается из base fee и priority fee. Внешний вызов выполняется через
    Resource Manager (``CLAUDE.md`` §14).
    """

    #: Обращается к узлу сети: на этапе поиска не опрашивается.
    requires_request = True

    def __init__(
        self,
        *,
        http: HttpClient,
        resources: ResourceManager,
        clock: Clock,
        rpc_urls: dict[str, tuple[str, ...]],
        freshness_seconds: int,
        priority_fees_wei: dict[str, int] | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._http = http
        self._resources = resources
        self._clock = clock
        self._endpoints = RpcEndpoints(rpc_urls)
        self._freshness = timedelta(seconds=freshness_seconds)
        #: Надбавка по сетям: свойство сети, а не приложения.
        self._priority_fees_wei = dict(priority_fees_wei or {})
        self._timeout = timedelta(seconds=timeout_seconds)
        #: Последняя known цена по сетям. Пока она свежа, обращения к
        #: узлу не происходит вовсе: цена газа меняется от блока к блоку
        #: незначительно, а спрашивать её в каждом проходе значило бы
        #: тратить запрос на то, что уже известно.
        self._cache: dict[str, GasPrice] = {}

    def cached_price(self, network_id: NetworkId) -> GasPrice | None:
        """Известная цена, если она ещё свежа. Запроса не делает.

        Нужна там, где обращаться наружу нельзя, но знание допустимо:
        запрет на запрос и незнание — разные вещи.
        """
        cached = self._cache.get(str(network_id))
        if cached is None or not cached.is_fresh(self._clock.now()):
            return None
        return cached

    async def gas_price(self, network_id: NetworkId) -> GasPrice | None:
        """Цена газа сети: из кеша, пока он свеж, иначе у узла."""
        if not self._endpoints.supports(network_id):
            return None
        cached = self.cached_price(network_id)
        if cached is not None:
            return cached
        payload = await self._call(network_id, "eth_gasPrice")
        legacy = self._parse_quantity(payload, field="eth_gasPrice")
        if legacy is None:
            return None
        base_fee = await self._base_fee(network_id)
        now = self._clock.now()
        if base_fee is None:
            return self._remember(
                GasPrice(
                    network_id=network_id,
                    wei_per_gas=legacy,
                    source="rpc:eth_gasPrice",
                    observed_at=now,
                    expires_at=now + self._freshness,
                )
            )
        tip = self._priority_fees_wei.get(str(network_id), 0)
        return self._remember(
            GasPrice(
                network_id=network_id,
                wei_per_gas=base_fee + tip,
                base_fee_wei=base_fee,
                priority_fee_wei=tip,
                source="rpc:eth_feeHistory",
                observed_at=now,
                expires_at=now + self._freshness,
            )
        )

    def _remember(self, price: GasPrice) -> GasPrice:
        """Запомнить цену до истечения свежести."""
        self._cache[str(price.network_id)] = price
        return price

    async def _base_fee(self, network_id: NetworkId) -> int | None:
        """Base fee последнего блока, если сеть поддерживает EIP-1559."""
        payload = await self._call(network_id, "eth_feeHistory", ["0x1", "latest", []])
        if not isinstance(payload, dict):
            return None
        base_fees = payload.get("baseFeePerGas")
        if not isinstance(base_fees, list) or not base_fees:
            return None
        return self._parse_hex(str(base_fees[-1]), field="baseFeePerGas")

    async def _call(
        self,
        network_id: NetworkId,
        method: str,
        params: list[Any] | None = None,
    ) -> Any:
        """Выполнить JSON-RPC вызов через Resource Manager."""
        request_id = RequestId.generate()
        resource_request = ResourceRequest(
            request_id=request_id,
            key=ResourceKey(
                # RPC — самостоятельный внешний ресурс, а не агрегатор
                # (``01_PROJECT_REQUIREMENTS.md`` §34).
                provider_id=RPC_RESOURCE_OWNER,
                network_id=network_id,
                operation=CapabilityOperation.GAS_ESTIMATE,
            ),
            priority=RequestPriority.MAINTENANCE,
            timeout=self._timeout,
            created_at=self._clock.now(),
            sequence=0,
            deduplication_key=f"rpc:{network_id}:{method}",
        )

        async def ask(url: str) -> Any:
            response = await self._http.send(
                HttpRequest(
                    method="POST",
                    url=url,
                    json_body={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                        "params": params or [],
                    },
                    request_id=request_id,
                    timeout_seconds=self._timeout.total_seconds(),
                )
            )
            classify_response(response, provider="rpc")
            body = response.json()
            if not isinstance(body, dict):
                raise DataError("rpc response is not a JSON object", code="rpc_response_malformed")
            return body.get("result")

        async def call() -> Any:
            # Перебор узлов внутри одного обращения к Resource Manager:
            # для вызывающей стороны это один запрос, им и должен
            # считаться для ограничения частоты и предохранителя.
            return await call_with_failover(
                self._endpoints.for_network(network_id),
                ask,
                network_id=network_id,
                method=method,
            )

        return await self._resources.execute(resource_request, call)

    def _parse_quantity(self, payload: Any, *, field: str) -> int | None:
        if payload is None:
            return None
        return self._parse_hex(str(payload), field=field)

    @staticmethod
    def _parse_hex(value: str, *, field: str) -> int:
        """Разобрать шестнадцатеричное значение JSON-RPC."""
        try:
            return int(value, 16)
        except ValueError as exc:
            raise DataError(
                f"rpc returned a malformed quantity in {field!r}",
                code="rpc_quantity_invalid",
            ) from exc
