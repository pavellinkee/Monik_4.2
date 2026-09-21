"""Adapter Velora (ParaSwap) Market API.

⚠️ **API contract NOT verified against live endpoint** (решение D-3).

Velora использует двухшаговую модель ``/prices`` → ``/transactions``.
Второй шаг нужен режиму ``ann``, который сделки исполняет
(``the_main_rules.md``, правило 11).

Особенности провайдера, переведённые здесь в общие понятия:

* **минимум задаём мы сами.** Uniswap называет его полем
  ``minimumAmount``, KyberSwap выводит из допуска, а Velora принимает
  его параметром ``destAmount`` при сборке: сколько назвали, ниже того
  роутер и не отдаст. Это самая честная форма из трёх — обязательство
  не выводится и не угадывается, а задаётся;
* списание идёт через отдельный контракт ``tokenTransferProxy``, а не
  через сам роутер: разрешение выдаётся ему, и адрес приходит в ответе
  вместе с маршрутом;
* расход газа приходит в маршруте полем ``gasCost``.
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
from monik.infrastructure.providers.normalization import (
    build_quote,
    normalized_response,
    parse_base_units,
    parse_optional_decimal,
    require_field,
)
from monik.infrastructure.providers.velora import endpoints
from monik.services.observability.clock import Clock
from monik.services.resources import ResourceManager

__all__ = ["VeloraAdapter"]

_PROVIDER = ProviderId.VELORA

#: Адрес счёта, от имени которого собирается транзакция. Опция
#: провайдера: котировка и calldata строятся под конкретного
#: отправителя, и подставлять произвольный адрес нельзя.
OPTION_SWAPPER = "swapper"

#: ``priceRoute`` возвращается ответом и передаётся в ``/transactions``
#: как есть. Для проверки Level 2 адаптер сравнивает отпечатки маршрута:
#: подставлять другой маршрут запрещено (``06_AGGREGATOR_ADAPTERS.md`` §52).
_SUPPORTS_FIXED_ROUTE = False

#: Документированные поля тела ошибки Market API. Сырое тело в
#: диагностику не попадает (``22_SECURITY.md``).
_ERROR_FIELDS = ("error", "message", "detail")

#: Итог маршрута до партнёрской комиссии.
_OUTPUT_FIELD = "destAmount"

#: Итог маршрута после партнёрской комиссии — то, что получит владелец.
#: Присутствует, когда комиссия есть; иначе API поле опускает.
_OUTPUT_AFTER_FEE_FIELD = "destAmountAfterFee"

#: Поле тела ошибки, называющее её причину.
_ERROR_REASON_FIELD = "error"

#: Причина отказа ``404``: маршрута с достаточной ликвидностью нет. Это
#: штатный отрицательный ответ, а не сбой.
_NO_ROUTE_REASON = "No routes found with enough liquidity"

#: Статус, которым приходит отсутствие маршрута.
_NO_ROUTE_STATUS = 404

#: Причина отказа ``400``: маршрут существует, но ожидаемая потеря выше
#: допустимого Market API влияния на цену. Смысл иной, чем у отсутствия
#: маршрута, поэтому и категория ошибки другая.
_ROUTE_REJECTED_REASON = "ESTIMATED_LOSS_GREATER_THAN_MAX_IMPACT"

#: Статус, которым приходит отвергнутый маршрут.
_ROUTE_REJECTED_STATUS = 400

#: Ограничение длины диагностики.
_ERROR_DETAIL_LIMIT = 300


class VeloraAdapter(HttpProviderAdapter):
    """Реализация :class:`AggregatorAdapter` для Velora."""

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
                NetworkId(name) for name in endpoints.SUPPORTED_NETWORK_IDS
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
        """Заголовки Velora.

        Market API не требует ключа для получения котировок; partner-ключ,
        если он задан, передаётся отдельным заголовком.
        """
        if self._api_key is None:
            return {}
        return {"X-Partner": self._api_key.get()}

    async def get_quote(self, request: QuoteRequest) -> Quote:
        """Получить котировку из ``/prices``."""
        network = self._require_network(request.network_id)
        payload = await self.request_json(
            path=endpoints.PRICES_PATH,
            network_id=request.network_id,
            operation=self._capability_operation(request),
            request_id=request.request_id,
            params=self._price_params(request, network),
            priority=request.priority,
            correlation_id=request.correlation_id,
            timeout=request.timeout,
            priority_at=request.priority_at,
        )
        with normalized_response(_PROVIDER):
            return self._to_quote(request, payload)

    async def build_swap(self, request: QuoteRequest) -> SwapTransaction:
        """Собрать вызов роутера по свежему маршруту.

        Два обращения: ``/prices`` находит маршрут, ``/transactions``
        превращает его в calldata. Маршрут передаётся целиком и без
        изменений — провайдер подписывает его полем ``hmac``.

        Котировка берётся заново, а не переиспользуется: транзакция
        отправляется по свежей цене, а не по той, на которой возможность
        была найдена минутой раньше.
        """
        if request.slippage_bps is None:
            raise DataError(
                "velora swap requires an explicit slippage tolerance: "
                "without it the guaranteed minimum is unknown",
                code="slippage_missing",
                provider_code=_PROVIDER.value,
            )
        network = self._require_network(request.network_id)
        payload = await self.request_json(
            path=endpoints.PRICES_PATH,
            network_id=request.network_id,
            operation=self._capability_operation(request),
            request_id=request.request_id,
            params=self._price_params(request, network),
            priority=request.priority,
            correlation_id=request.correlation_id,
            timeout=request.timeout,
            priority_at=request.priority_at,
        )
        with normalized_response(_PROVIDER):
            quote = self._to_quote(request, payload)
            price_route = self._price_route(payload)
        minimum = max(quote.output_amount.raw * (10_000 - request.slippage_bps) // 10_000, 1)
        built = await self.request_json(
            path=endpoints.transactions_path(network),
            network_id=request.network_id,
            operation=self._capability_operation(request),
            request_id=RequestId.generate(),
            method="POST",
            json_body={
                "srcToken": str(request.input_token.address),
                "destToken": str(request.output_token.address),
                "srcDecimals": request.input_token.decimals,
                "destDecimals": request.output_token.decimals,
                "srcAmount": str(request.input_amount.raw),
                # Минимум задаётся нами и становится обязательством
                # роутера: ниже него обмен не исполнится.
                "destAmount": str(minimum),
                "priceRoute": price_route,
                "userAddress": self._require_swapper(),
            },
            priority=request.priority,
            correlation_id=request.correlation_id,
            timeout=request.timeout,
            priority_at=request.priority_at,
        )
        with normalized_response(_PROVIDER):
            return self._to_swap(request, quote, price_route, built, minimum)

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
            detail="velora returned a different priceRoute for the same pair",
        )

    async def discover_capabilities(self) -> AdapterCapabilities:
        """Подтвердить доступность API списком токенов сети."""
        for name, network in endpoints.SUPPORTED_NETWORK_IDS.items():
            await self.request_json(
                path=endpoints.tokens_path(network),
                network_id=NetworkId(name),
                operation=CapabilityOperation.TOKEN_METADATA,
                request_id=RequestId.generate(),
                deduplication_key=f"velora:tokens:{network}",
            )
        return self._capabilities

    async def discover_fees(self, network_id: NetworkId) -> tuple[Fee, ...]:
        """Отдельного endpoint'а комиссий нет.

        Компоненты не выдумываются (``06_AGGREGATOR_ADAPTERS.md`` §39):
        стоимость маршрута отражена в ``destAmount``.
        """
        return ()

    async def health_check(self) -> AdapterHealth:
        """Проверить доступность API."""
        name, network = next(iter(endpoints.SUPPORTED_NETWORK_IDS.items()))
        try:
            await self.request_json(
                path=endpoints.tokens_path(network),
                network_id=NetworkId(name),
                operation=CapabilityOperation.TOKEN_METADATA,
                request_id=RequestId.generate(),
                deduplication_key=f"velora:health:{network}",
            )
        except MonikError as error:
            return AdapterHealth(
                provider_id=_PROVIDER,
                state=AdapterState.DEGRADED,
                detail=error.info.code,
            )
        return AdapterHealth(provider_id=_PROVIDER, state=AdapterState.READY)

    def _error_reason(self, response: HttpResponse, status: int) -> str | None:
        """Причина отказа из тела ошибки, если статус совпал."""
        if response.status_code != status:
            return None
        body = self.error_body(response)
        if body is None:
            return None
        reason = body.get(_ERROR_REASON_FIELD)
        return reason if isinstance(reason, str) else None

    def no_route_reason(self, response: HttpResponse) -> str | None:
        """Перевод отказа ``404`` об отсутствии ликвидности.

        Market API сообщает об этом статусом ``404`` с собственным
        текстом причины. По смыслу это тот же отрицательный результат,
        что ``liquidityAvailable=false`` у 0x и ``NoRouteFoundError`` у
        Uniswap, и система должна видеть его одинаково.

        Прочие ошибки ``404`` остаются ошибками данных: распознаётся
        документированная причина, а не статус целиком.
        """
        reason = self._error_reason(response, _NO_ROUTE_STATUS)
        if reason is None or _NO_ROUTE_REASON.lower() not in reason.lower():
            return None
        return "velora reports no route with enough liquidity for the requested pair"

    def route_rejection_reason(self, response: HttpResponse) -> str | None:
        """Перевод отказа ``400`` о превышении допустимого влияния на цену.

        Маршрут для пары существует, но Market API отказался его
        предлагать: ожидаемая потеря выше допустимой. От отсутствия
        маршрута это отличается причиной, поэтому и категория другая.
        """
        reason = self._error_reason(response, _ROUTE_REJECTED_STATUS)
        if reason is None or _ROUTE_REJECTED_REASON.lower() not in reason.lower():
            return None
        return "velora rejected the route: estimated loss exceeds the allowed price impact"

    def error_detail(self, response: HttpResponse) -> str | None:
        """Диагностика отклонённого запроса Market API.

        Без неё ошибка 4xx сообщает только код статуса, и причина отказа
        (неизвестный токен, отсутствующий маршрут, некорректный параметр)
        теряется. В сообщение попадают только документированные поля
        ошибки, обрезанные по длине и пропущенные через редакцию секретов.
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

    @staticmethod
    def _price_route(payload: Any) -> dict[str, Any]:
        """Маршрут из ответа ``/prices``, целиком и без изменений."""
        route = require_field(payload, "priceRoute", provider=_PROVIDER)
        if not isinstance(route, dict):
            raise DataError(
                "velora priceRoute is not a JSON object",
                code="provider_response_malformed",
                provider_code=_PROVIDER.value,
            )
        return route

    def _to_swap(
        self,
        request: QuoteRequest,
        quote: Quote,
        price_route: dict[str, Any],
        built: Any,
        minimum: int,
    ) -> SwapTransaction:
        """Преобразовать ответ ``/transactions`` в транзакцию."""
        if not isinstance(built, dict):
            raise DataError(
                "velora transaction response is not a JSON object",
                code="provider_response_malformed",
                provider_code=_PROVIDER.value,
            )
        return SwapTransaction(
            provider_id=_PROVIDER,
            network_id=request.network_id,
            chain_id=self._require_network(request.network_id),
            to=str(require_field(built, "to", provider=_PROVIDER)),
            data=str(require_field(built, "data", provider=_PROVIDER)),
            value=parse_base_units(built.get("value", "0"), provider=_PROVIDER, field="value"),
            # Предел газа приходит маршрутом: ответ сборки его не несёт,
            # потому что проверка на стороне провайдера отключена.
            gas_limit=parse_base_units(
                require_field(price_route, "gasCost", provider=_PROVIDER),
                provider=_PROVIDER,
                field="gasCost",
            ),
            # Списывает не роутер, а отдельный контракт-посредник, и
            # разрешение выдаётся ему. Адрес приходит вместе с маршрутом:
            # угадывать его нельзя, он меняется вместе с версией роутера.
            allowances=(
                AllowanceRequirement(
                    kind=AllowanceKind.ERC20,
                    contract=str(request.input_token.address),
                    spender=str(
                        require_field(price_route, "tokenTransferProxy", provider=_PROVIDER)
                    ),
                ),
            ),
            quote=quote,
            min_output_raw=minimum,
        )

    def _require_swapper(self) -> str:
        """Адрес, от имени которого собирается транзакция."""
        swapper = self._config.option(OPTION_SWAPPER)
        if swapper is None:
            raise UnsupportedError(
                "velora swap requires the trading account address in "
                "providers[velora].options.swapper",
                code="provider_swapper_missing",
                provider_code=_PROVIDER.value,
            )
        return swapper

    @staticmethod
    def _price_params(request: QuoteRequest, network: int) -> dict[str, str]:
        """Параметры запроса котировки.

        ``srcDecimals``/``destDecimals`` берутся из Token Registry
        (``09_PROFIT_CALCULATOR.md`` §5), а не выводятся из символа.
        """
        return {
            "srcToken": str(request.input_token.address),
            "destToken": str(request.output_token.address),
            "srcDecimals": str(request.input_token.decimals),
            "destDecimals": str(request.output_token.decimals),
            "amount": str(request.input_amount.raw),
            "side": "SELL",
            "network": str(network),
            "version": endpoints.API_VERSION,
        }

    def _require_network(self, network_id: NetworkId) -> int:
        network = endpoints.network_id_for(network_id)
        if network is None:
            raise UnsupportedError(
                f"velora adapter does not support network {network_id}",
                code="provider_network_unsupported",
                provider_code=_PROVIDER.value,
            )
        return network

    @staticmethod
    def _capability_operation(request: QuoteRequest) -> CapabilityOperation:
        return (
            CapabilityOperation.QUOTE_BUY
            if request.operation is OperationType.BUY
            else CapabilityOperation.QUOTE_SELL
        )

    # --- разбор ответа ----------------------------------------------------

    def _to_quote(self, request: QuoteRequest, payload: Any) -> Quote:
        """Преобразовать ``priceRoute`` в нормализованную котировку."""
        if not isinstance(payload, dict):
            raise DataError(
                "velora response is not a JSON object",
                code="provider_response_malformed",
                provider_code=_PROVIDER.value,
            )
        price_route = require_field(payload, "priceRoute", provider=_PROVIDER)
        if not isinstance(price_route, dict):
            raise DataError(
                "velora priceRoute is not a JSON object",
                code="provider_response_malformed",
                provider_code=_PROVIDER.value,
            )
        # ``destAmount`` — сумма ДО партнёрской комиссии, а получит
        # владелец ``destAmountAfterFee`` (подтверждено документацией
        # Market API и живыми ответами: разница равна ``partnerFee``).
        # Считать выходом сумму до комиссии значит завышать результат, а
        # завышать запрещено (``CLAUDE.md`` §12). Поле необязательное:
        # когда комиссии нет, API его опускает, и берётся ``destAmount``.
        output_field = (
            _OUTPUT_AFTER_FEE_FIELD
            if price_route.get(_OUTPUT_AFTER_FEE_FIELD) is not None
            else _OUTPUT_FIELD
        )
        output_raw = parse_base_units(
            require_field(price_route, output_field, provider=_PROVIDER),
            provider=_PROVIDER,
            field=output_field,
        )
        self._verify_src_amount(request, price_route)
        # Market API присылает стоимость исполнения в долларах вместе с
        # котировкой: курс native token отдельным запросом не нужен.
        gas_cost_usd = parse_optional_decimal(
            price_route.get("gasCostUSD"), provider=_PROVIDER, field="gasCostUSD"
        )
        gas = price_route.get("gasCost")
        gas_units = (
            parse_base_units(gas, provider=_PROVIDER, field="gasCost") if gas is not None else None
        )
        return build_quote(
            provider_id=_PROVIDER,
            request=request,
            output_raw=output_raw,
            route=self._to_route(request, price_route),
            created_at=self._clock.now(),
            estimated_gas_units=gas_units,
            estimated_gas_cost_usd=gas_cost_usd,
            slippage_bps=request.slippage_bps,
            provider_metadata=(("api_version", endpoints.API_VERSION),),
            # ``destAmount`` — итог маршрута с учётом его издержек.
            output_includes_fees=True,
        )

    @staticmethod
    def _verify_src_amount(request: QuoteRequest, price_route: dict[str, Any]) -> None:
        """Убедиться, что котировка относится к запрошенной сумме."""
        raw = price_route.get("srcAmount")
        if raw is None:
            return
        actual = parse_base_units(raw, provider=_PROVIDER, field="srcAmount")
        if actual != request.input_amount.raw:
            raise DataError(
                "velora returned a quote for a different source amount",
                code="provider_amount_mismatch",
                provider_code=_PROVIDER.value,
            )

    def _to_route(self, request: QuoteRequest, price_route: dict[str, Any]) -> Route:
        """Собрать маршрут из ``bestRoute``."""
        return Route(
            provider_id=_PROVIDER,
            network_id=request.network_id,
            operation=request.operation,
            routing_mode=RoutingMode.CLASSIC,
            input_token=request.input_token.key,
            output_token=request.output_token.key,
            steps=self._parse_best_route(request, price_route.get("bestRoute")),
            provider_parameters=(("api_version", endpoints.API_VERSION),),
        )

    def _parse_best_route(self, request: QuoteRequest, best_route: Any) -> tuple[RouteStep, ...]:
        """Извлечь названия обменников из вложенной структуры ``bestRoute``.

        Velora описывает маршрут списком percent-разбиений со вложенными
        ``swaps`` и ``swapExchanges``. Названия нормализуются и сортируются,
        поэтому отпечаток не зависит от порядка элементов
        (``06_AGGREGATOR_ADAPTERS.md`` §83).
        """
        exchanges = sorted(set(self._collect_exchanges(best_route)))
        protocol = "+".join(exchanges) if exchanges else "velora_aggregate"
        return (
            RouteStep(
                input_token=request.input_token.key,
                output_token=request.output_token.key,
                protocol=protocol,
            ),
        )

    def _collect_exchanges(self, node: Any) -> list[str]:
        """Рекурсивно собрать названия обменников."""
        if isinstance(node, dict):
            found: list[str] = []
            exchange = node.get("exchange")
            if exchange:
                found.append(str(exchange))
            for value in node.values():
                if isinstance(value, list | dict):
                    found.extend(self._collect_exchanges(value))
            return found
        if isinstance(node, list):
            collected: list[str] = []
            for item in node:
                collected.extend(self._collect_exchanges(item))
            return collected
        return []
