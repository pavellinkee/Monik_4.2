"""Adapter KyberSwap Aggregator API.

KyberSwap отдаёт маршрут одним запросом ``GET /{сеть}/api/v1/routes``:
сумма на выходе, расход и цена газа и состав маршрута приходят вместе.
Второй шаг — ``POST /route/build`` — превращает выбранный маршрут в
вызов роутера. Он нужен режиму ``ann``, который сделки исполняет
(``the_main_rules.md``, правило 11).

Особенности провайдера, переведённые здесь в общие понятия:

* сеть указывается **в пути**, а не параметром запроса;
* отказы приходят собственными числовыми кодами в теле, а не только
  статусом HTTP: ``4008`` и ``4010`` означают отсутствие маршрута, а
  ``4011`` — что провайдер не знает токен;
* ``amountOut`` — сумма, которую получает владелец, партнёрская комиссия
  не задаётся и в ответе пуста;
* **гарантированного минимума в ответе нет.** Uniswap называет его полем
  ``minimumAmount``, KyberSwap — только зашивает в calldata, а наружу
  отдаёт лишь ожидаемый выход и заданный нами допуск. Минимум поэтому
  выводится здесь: ``amountOut × (10000 − допуск) / 10000``. Вывод
  проверен на живом API 2026-09-18 — полученное число найдено в самой
  calldata при допусках 1, 10 и 100 базисных пунктов;
* списание идёт **напрямую роутером**, без Permit2: разрешение нужно
  одно, а не два, как у Uniswap.
"""

from __future__ import annotations

from typing import Any

from monik.config.secrets import SecretValue
from monik.config.sections.providers import ProviderConfig
from monik.domain.enums.capability import CapabilityOperation
from monik.domain.enums.health import AdapterState
from monik.domain.enums.operations import (
    OperationType,
    RouteValidationOutcome,
    RoutingMode,
)
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import DataError, MonikError, UnsupportedError
from monik.domain.models.execution import AllowanceKind, AllowanceRequirement, SwapTransaction
from monik.domain.models.fee import Fee
from monik.domain.models.quote import Quote
from monik.domain.models.route import Route, RouteStep
from monik.domain.value_objects.identifiers import RequestId
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.http import HttpClient, HttpResponse
from monik.infrastructure.providers.contract import (
    AdapterCapabilities,
    AdapterHealth,
    QuoteRequest,
    RouteValidation,
    TypedDataSigner,
)
from monik.infrastructure.providers.http_adapter import HttpProviderAdapter
from monik.infrastructure.providers.kyberswap import endpoints
from monik.infrastructure.providers.normalization import (
    build_quote,
    normalized_response,
    parse_base_units,
    parse_optional_decimal,
    require_field,
)
from monik.services.observability.clock import Clock
from monik.services.resources import ResourceManager

__all__ = ["KyberSwapAdapter"]

_PROVIDER = ProviderId.KYBERSWAP

#: Маршрут возвращается вместе с идентификатором ``routeID`` и подписью
#: ``checksum``, но повторить по ним ту же цену API не обещает: проверка
#: Level 2 сравнивает отпечатки маршрута, а не восстанавливает его.
_SUPPORTS_FIXED_ROUTE = False

#: Документированные поля тела ошибки. Сырое тело в диагностику не
#: попадает (``22_SECURITY.md``).
_ERROR_FIELDS = ("code", "message")

#: Поле с числовым кодом отказа.
_ERROR_CODE_FIELD = "code"

#: Коды отказа, означающие отсутствие маршрута для пары и суммы.
#:
#: ``4011`` («токен не найден») стоит здесь же намеренно: для сканера это
#: не сбой и не временное состояние, а свойство пары — маршрут построить
#: невозможно, пока провайдер не узнает токен. Общая система видит
#: одинаковый отрицательный результат и перестаёт спрашивать после
#: нескольких попыток, вместо того чтобы повторять запрос каждый цикл.
_NO_ROUTE_CODES: dict[int, str] = {
    4008: "kyberswap found no route for the requested pair",
    4010: "kyberswap has no eligible pool for the requested pair",
    4011: "kyberswap does not know one of the requested tokens",
}

#: Ограничение длины диагностики.
_ERROR_DETAIL_LIMIT = 300


class KyberSwapAdapter(HttpProviderAdapter):
    """Реализация :class:`AggregatorAdapter` для KyberSwap."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        http: HttpClient,
        resources: ResourceManager,
        clock: Clock,
        api_key: SecretValue | None = None,
        base_url: str | None = None,
        signer: TypedDataSigner | None = None,
    ) -> None:
        super().__init__(
            _PROVIDER,
            config,
            http=http,
            resources=resources,
            clock=clock,
            api_key=api_key,
            base_url=base_url or config.base_url or endpoints.DEFAULT_BASE_URL,
            signer=signer,
        )
        self._capabilities = AdapterCapabilities(
            provider_id=_PROVIDER,
            supported_networks=frozenset(
                NetworkId(name) for name in endpoints.SUPPORTED_NETWORK_SLUGS
            ),
            routing_modes=frozenset({RoutingMode.CLASSIC}),
            supports_fixed_route=_SUPPORTS_FIXED_ROUTE,
            supports_fee_discovery=False,
            supports_gas_estimate=True,
            supports_execution=True,
        )

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Заявленные возможности адаптера."""
        return self._capabilities

    def auth_headers(self) -> dict[str, str]:
        """Заголовки KyberSwap.

        Ключ доступа API не требует. Документация предписывает называть
        себя заголовком ``x-client-id``; значение берётся из настройки
        провайдера, а без неё подставляется имя приложения. На цену и на
        лимит частоты значение не влияет.
        """
        client_id = self._api_key.get() if self._api_key is not None else None
        return {endpoints.CLIENT_ID_HEADER: client_id or endpoints.DEFAULT_CLIENT_ID}

    async def get_quote(self, request: QuoteRequest) -> Quote:
        """Получить котировку из ``/routes``."""
        slug = self._require_network(request.network_id)
        payload = await self.request_json(
            path=endpoints.routes_path(slug),
            network_id=request.network_id,
            operation=self._capability_operation(request),
            request_id=request.request_id,
            params=self._route_params(request),
            priority=request.priority,
            correlation_id=request.correlation_id,
            timeout=request.timeout,
            priority_at=request.priority_at,
        )
        with normalized_response(_PROVIDER):
            return self._to_quote(request, payload)

    async def build_swap(self, request: QuoteRequest) -> SwapTransaction:
        """Собрать вызов роутера по свежему маршруту.

        Два обращения: ``GET /routes`` находит маршрут, ``POST
        /route/build`` превращает его в calldata. Маршрут передаётся
        целиком и без изменений — провайдер проверяет его контрольной
        суммой, и правка любого поля делает маршрут негодным.

        Котировка берётся заново, а не переиспользуется: транзакция
        отправляется по свежей цене, а не по той, на которой возможность
        была найдена минутой раньше.
        """
        if request.slippage_bps is None:
            raise DataError(
                "kyberswap swap requires an explicit slippage tolerance: "
                "without it the guaranteed minimum is unknown",
                code="slippage_missing",
                provider_code=_PROVIDER.value,
            )
        slug = self._require_network(request.network_id)
        route_payload = await self.request_json(
            path=endpoints.routes_path(slug),
            network_id=request.network_id,
            operation=self._capability_operation(request),
            request_id=request.request_id,
            params=self._route_params(request),
            priority=request.priority,
            correlation_id=request.correlation_id,
            timeout=request.timeout,
            priority_at=request.priority_at,
        )
        with normalized_response(_PROVIDER):
            quote = self._to_quote(request, route_payload)
            summary = self._route_summary(route_payload)
        sender = self._require_swapper()
        build_payload = await self.request_json(
            path=endpoints.build_path(slug),
            network_id=request.network_id,
            operation=self._capability_operation(request),
            request_id=RequestId.generate(),
            method="POST",
            json_body={
                "routeSummary": summary,
                "sender": sender,
                "recipient": sender,
                "slippageTolerance": request.slippage_bps,
            },
            priority=request.priority,
            correlation_id=request.correlation_id,
            timeout=request.timeout,
            priority_at=request.priority_at,
        )
        with normalized_response(_PROVIDER):
            return self._to_swap(request, quote, build_payload)

    async def validate_fixed_route(self, request: QuoteRequest) -> RouteValidation:
        """Сравнить свежий маршрут с зафиксированным Level 1."""
        if request.fixed_route is None:
            raise DataError(
                "fixed route validation requires the route fixed by Level 1",
                code="fixed_route_missing",
                provider_code=_PROVIDER.value,
            )
        quote = await self.get_quote(request)
        observed = quote.route.fingerprint
        if quote.route.matches(request.fixed_route):
            return RouteValidation(
                outcome=RouteValidationOutcome.REPRODUCED,
                quote=quote,
                observed_fingerprint=observed,
            )
        return RouteValidation(
            outcome=RouteValidationOutcome.MISMATCH,
            observed_fingerprint=observed,
            detail="kyberswap returned a different route for the same pair",
        )

    async def discover_capabilities(self) -> AdapterCapabilities:
        """Подтвердить доступность API проверочной котировкой."""
        for name in endpoints.SUPPORTED_NETWORK_SLUGS:
            await self._probe(NetworkId(name), key=f"kyberswap:discovery:{name}")
        return self._capabilities

    async def discover_fees(self, network_id: NetworkId) -> tuple[Fee, ...]:
        """Отдельного endpoint'а комиссий нет.

        Комиссии пулов уже учтены в ``amountOut``; выдумывать компоненты
        запрещено (``06_AGGREGATOR_ADAPTERS.md`` §39).
        """
        return ()

    async def health_check(self) -> AdapterHealth:
        """Проверить доступность API минимальным **валидным** запросом.

        Неполный запрос API отвергает с ``400``, поэтому проверка
        выполняется полноценной котировкой на малую сумму. Своп при этом
        не исполняется.
        """
        name = next(iter(endpoints.SUPPORTED_NETWORK_SLUGS))
        try:
            await self._probe(NetworkId(name), key=f"kyberswap:health:{name}")
        except MonikError as error:
            return AdapterHealth(
                provider_id=_PROVIDER,
                state=AdapterState.DEGRADED,
                detail=error.info.code,
            )
        return AdapterHealth(provider_id=_PROVIDER, state=AdapterState.READY)

    def no_route_reason(self, response: HttpResponse) -> str | None:
        """Перевод собственных кодов отказа в отсутствие маршрута.

        KyberSwap отвечает статусом ``400`` и на отсутствие маршрута, и на
        некорректный запрос, поэтому распознаётся код в теле, а не статус
        целиком: неизвестный код остаётся ошибкой данных.
        """
        body = self.error_body(response)
        if body is None:
            return None
        code = body.get(_ERROR_CODE_FIELD)
        if not isinstance(code, int):
            return None
        return _NO_ROUTE_CODES.get(code)

    def error_detail(self, response: HttpResponse) -> str | None:
        """Диагностика отклонённого запроса.

        В сообщение попадают только документированные поля ответа,
        обрезанные по длине и пропущенные через редакцию секретов.
        """
        body = self.error_body(response)
        if body is None:
            return None
        parts = [
            f"{field}={body[field]}"
            for field in _ERROR_FIELDS
            if isinstance(body.get(field), str | int)
        ]
        if not parts:
            return None
        return self.redact_provider_text(" ".join(parts))[:_ERROR_DETAIL_LIMIT]

    # --- построение запроса ----------------------------------------------

    async def _probe(self, network_id: NetworkId, *, key: str) -> None:
        """Выполнить проверочную котировку сети."""
        slug = self._require_network(network_id)
        probe = endpoints.HEALTH_PROBES[str(network_id)]
        await self.request_json(
            path=endpoints.routes_path(slug),
            network_id=network_id,
            operation=CapabilityOperation.QUOTE_BUY,
            request_id=RequestId.generate(),
            params={
                "tokenIn": probe.token_in,
                "tokenOut": probe.token_out,
                "amountIn": probe.amount,
            },
            deduplication_key=key,
        )

    def _to_swap(self, request: QuoteRequest, quote: Quote, payload: Any) -> SwapTransaction:
        """Преобразовать ответ ``/route/build`` в транзакцию."""
        data = self._build_data(payload)
        output_raw = parse_base_units(
            require_field(data, "amountOut", provider=_PROVIDER),
            provider=_PROVIDER,
            field="amountOut",
        )
        bps = request.slippage_bps or 0
        return SwapTransaction(
            provider_id=_PROVIDER,
            network_id=request.network_id,
            chain_id=self._require_chain_id(request.network_id),
            to=str(require_field(data, "routerAddress", provider=_PROVIDER)),
            data=str(require_field(data, "data", provider=_PROVIDER)),
            value=parse_base_units(
                data.get("transactionValue", "0"), provider=_PROVIDER, field="transactionValue"
            ),
            gas_limit=parse_base_units(
                require_field(data, "gas", provider=_PROVIDER), provider=_PROVIDER, field="gas"
            ),
            # Одно разрешение вместо двух: роутер списывает токен сам,
            # без посредника вроде Permit2.
            allowances=(
                AllowanceRequirement(
                    kind=AllowanceKind.ERC20,
                    contract=str(request.input_token.address),
                    spender=str(require_field(data, "routerAddress", provider=_PROVIDER)),
                ),
            ),
            quote=quote,
            # Минимум провайдер наружу не отдаёт — только зашивает в
            # calldata. Выводим его из того же правила, по которому он
            # зашит, и не ниже единицы: ноль означал бы согласие получить
            # ничего.
            min_output_raw=max(output_raw * (10_000 - bps) // 10_000, 1),
        )

    @staticmethod
    def _build_data(payload: Any) -> dict[str, Any]:
        """Полезная часть ответа сборки."""
        data = require_field(payload, "data", provider=_PROVIDER)
        if not isinstance(data, dict):
            raise DataError(
                "kyberswap build response has no data object",
                code="provider_response_malformed",
                provider_code=_PROVIDER.value,
            )
        return data

    def _require_swapper(self) -> str:
        """Адрес, от имени которого собирается транзакция."""
        swapper = self._config.options.get("swapper")
        if not swapper:
            raise UnsupportedError(
                "kyberswap swap requires the trading account address in "
                "providers[kyberswap].options.swapper",
                code="provider_swapper_missing",
                provider_code=_PROVIDER.value,
            )
        return str(swapper)

    @staticmethod
    def _route_params(request: QuoteRequest) -> dict[str, str]:
        """Параметры запроса маршрута."""
        return {
            "tokenIn": str(request.input_token.address),
            "tokenOut": str(request.output_token.address),
            "amountIn": str(request.input_amount.raw),
        }

    def _require_network(self, network_id: NetworkId) -> str:
        slug = endpoints.network_slug_for(network_id)
        if slug is None:
            raise UnsupportedError(
                f"kyberswap adapter does not support network {network_id}",
                code="provider_network_unsupported",
                provider_code=_PROVIDER.value,
            )
        return slug

    def _require_chain_id(self, network_id: NetworkId) -> int:
        chain_id = endpoints.chain_id_for(network_id)
        if chain_id is None:
            raise UnsupportedError(
                f"kyberswap adapter does not know the chain id of {network_id}",
                code="provider_network_unsupported",
                provider_code=_PROVIDER.value,
            )
        return chain_id

    @staticmethod
    def _capability_operation(request: QuoteRequest) -> CapabilityOperation:
        return (
            CapabilityOperation.QUOTE_BUY
            if request.operation is OperationType.BUY
            else CapabilityOperation.QUOTE_SELL
        )

    # --- разбор ответа ----------------------------------------------------

    def _to_quote(self, request: QuoteRequest, payload: Any) -> Quote:
        """Преобразовать ``routeSummary`` в нормализованную котировку."""
        summary = self._route_summary(payload)
        output_raw = parse_base_units(
            require_field(summary, "amountOut", provider=_PROVIDER),
            provider=_PROVIDER,
            field="amountOut",
        )
        self._verify_input_amount(request, summary)
        gas_units = self._optional_units(summary, "gas")
        gas_price_wei = self._optional_units(summary, "gasPrice")
        gas_cost_usd = parse_optional_decimal(
            summary.get("gasUsd"), provider=_PROVIDER, field="gasUsd"
        )
        return build_quote(
            provider_id=_PROVIDER,
            request=request,
            output_raw=output_raw,
            route=self._to_route(request, summary),
            created_at=self._clock.now(),
            estimated_gas_units=gas_units,
            estimated_gas_price_wei=gas_price_wei,
            estimated_gas_cost_usd=gas_cost_usd,
            slippage_bps=request.slippage_bps,
            # Партнёрская комиссия не задаётся, и ``extraFee`` приходит
            # пустым: ``amountOut`` — то, что получит владелец.
            output_includes_fees=True,
        )

    @staticmethod
    def _route_summary(payload: Any) -> dict[str, Any]:
        """Достать ``routeSummary`` из ответа."""
        if not isinstance(payload, dict):
            raise DataError(
                "kyberswap response is not a JSON object",
                code="provider_response_malformed",
                provider_code=_PROVIDER.value,
            )
        data = require_field(payload, "data", provider=_PROVIDER)
        if not isinstance(data, dict):
            raise DataError(
                "kyberswap data is not a JSON object",
                code="provider_response_malformed",
                provider_code=_PROVIDER.value,
            )
        summary = require_field(data, "routeSummary", provider=_PROVIDER)
        if not isinstance(summary, dict):
            raise DataError(
                "kyberswap routeSummary is not a JSON object",
                code="provider_response_malformed",
                provider_code=_PROVIDER.value,
            )
        return summary

    @staticmethod
    def _optional_units(summary: dict[str, Any], field: str) -> int | None:
        """Необязательное целочисленное поле ответа."""
        value = summary.get(field)
        if value is None:
            return None
        return parse_base_units(value, provider=_PROVIDER, field=field)

    @staticmethod
    def _verify_input_amount(request: QuoteRequest, summary: dict[str, Any]) -> None:
        """Убедиться, что котировка относится к запрошенной сумме."""
        raw = summary.get("amountIn")
        if raw is None:
            return
        actual = parse_base_units(raw, provider=_PROVIDER, field="amountIn")
        if actual != request.input_amount.raw:
            raise DataError(
                "kyberswap returned a quote for a different source amount",
                code="provider_amount_mismatch",
                provider_code=_PROVIDER.value,
            )

    def _to_route(self, request: QuoteRequest, summary: dict[str, Any]) -> Route:
        """Собрать маршрут из ``route``."""
        return Route(
            provider_id=_PROVIDER,
            network_id=request.network_id,
            operation=request.operation,
            routing_mode=RoutingMode.CLASSIC,
            input_token=request.input_token.key,
            output_token=request.output_token.key,
            steps=self._parse_route(request, summary.get("route")),
        )

    def _parse_route(self, request: QuoteRequest, route: Any) -> tuple[RouteStep, ...]:
        """Извлечь источники ликвидности из вложенной структуры ``route``.

        KyberSwap описывает маршрут списком параллельных ветвей, каждая —
        последовательность шагов со своим ``exchange``. Названия
        нормализуются и сортируются, поэтому отпечаток не зависит от
        порядка элементов (``06_AGGREGATOR_ADAPTERS.md`` §83).
        """
        exchanges = sorted(set(self._collect_exchanges(route)))
        protocol = "+".join(exchanges) if exchanges else "kyberswap_aggregate"
        return (
            RouteStep(
                input_token=request.input_token.key,
                output_token=request.output_token.key,
                protocol=protocol,
            ),
        )

    def _collect_exchanges(self, node: Any) -> list[str]:
        """Рекурсивно собрать названия источников ликвидности."""
        if isinstance(node, dict):
            exchange = node.get("exchange")
            return [str(exchange)] if exchange else []
        if isinstance(node, list):
            found: list[str] = []
            for item in node:
                found.extend(self._collect_exchanges(item))
            return found
        return []
