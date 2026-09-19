"""Adapter CoW Swap Order Book API.

Контракт сверен с живым API и с контрактами протокола 18.09.2026
(``endpoints.py`` перечисляет, что именно проверено).

CoW Swap — первый провайдер Monik, который **не отдаёт транзакцию**.
Он принимает подписанную офчейн заявку, и исполняет её решатель в одной
из следующих партий (``the_main_rules.md``, правило 16). Особенности,
переведённые здесь в общие понятия системы:

* **вместо сборки calldata — сборка заявки.** ``build_swap`` остаётся
  неподдержанным и честно об этом сообщает; исполнение идёт через
  :meth:`build_order`, :meth:`place_order` и :meth:`order_state`;
* **обязательство задаём мы сами.** Uniswap называет минимум полем
  ``minimumAmount``, Velora принимает его параметром, а CoW Swap берёт
  его как ``buyAmount`` самой заявки: меньше решатель отдать не может,
  иначе расчёт не состоится. Симуляция узлом при этом невозможна —
  транзакции ещё нет, и защитой служит именно этот предел;
* **газа у такой ноги нет.** Стоимость исполнения удерживается из
  продаваемого токена и уже входит в названную сумму получения, поэтому
  расход газа равен нулю — это знание, а не пробел. Вычитать газ второй
  раз значило бы посчитать его дважды;
* **разрешение выдаётся не роутеру**, а контракту ``GPv2VaultRelayer``;
* отказ «нет маршрута» приходит телом с ``errorType`` ``NoLiquidity``.
"""

from __future__ import annotations

from typing import Any

from monik.config.secrets import SecretValue
from monik.config.sections.providers import ProviderConfig
from monik.domain.enums.capability import CapabilityOperation
from monik.domain.enums.health import AdapterState
from monik.domain.enums.operations import (
    OperationType,
    RoutingMode,
)
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import DataError, MonikError, UnsupportedError
from monik.domain.models.execution import (
    AllowanceKind,
    AllowanceRequirement,
    OrderOutcome,
    OrderState,
    PlacedOrder,
    SignedOrderPlan,
)
from monik.domain.models.fee import Fee
from monik.domain.models.quote import Quote
from monik.domain.models.route import Route, RouteStep
from monik.domain.value_objects.identifiers import RequestId
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.http import HttpResponse
from monik.infrastructure.providers.contract import (
    AdapterCapabilities,
    AdapterHealth,
    QuoteRequest,
    RouteValidation,
)
from monik.infrastructure.providers.cow import endpoints
from monik.infrastructure.providers.http_adapter import HttpProviderAdapter
from monik.infrastructure.providers.normalization import (
    build_quote,
    normalized_response,
    parse_base_units,
    require_field,
)

__all__ = ["CowAdapter"]

_PROVIDER = ProviderId.COW

#: Адрес счёта, от имени которого заявка составляется и подписывается.
#: Котировка строится под конкретного владельца, и подставлять чужой
#: адрес нельзя: подпись не сойдётся с полем ``from``.
OPTION_SWAPPER = "swapper"

#: Срок жизни заявки по умолчанию. Заявка не исполняется мгновенно:
#: решатель забирает её в одной из следующих партий, и слишком короткий
#: срок — это гарантированный промах. Переопределяется опцией провайдера.
DEFAULT_VALIDITY_SECONDS = 180

#: Опция срока жизни заявки.
OPTION_VALIDITY = "order_validity_seconds"

#: Маршрут CoW Swap не раскрывает: цену назначает решатель, а не
#: последовательность пулов. Проверить зафиксированный маршрут поэтому
#: невозможно, и адаптер об этом заявляет честно.
_SUPPORTS_FIXED_ROUTE = False

#: Обозначение маршрута в отпечатке. Состав пулов провайдер не сообщает,
#: и выдумывать его нельзя.
_ROUTE_PROTOCOL = "cow_solver"

#: Поле тела ошибки, называющее её вид.
_ERROR_TYPE_FIELD = "errorType"

#: Поле тела ошибки с пояснением.
_ERROR_DESCRIPTION_FIELD = "description"

#: Вид ошибки: маршрута нет.
_NO_ROUTE_TYPES = frozenset({"NoLiquidity", "NoLiquidityAvailable"})

#: Вид ошибки: сумма меньше той, из которой протокол смог бы удержать
#: стоимость исполнения. Маршрут при этом существует.
_ROUTE_REJECTED_TYPES = frozenset({"SellAmountDoesNotCoverFee", "TooSmallOrder"})

#: Виды ошибок, при которых повтор осмыслен.
_TEMPORARY_TYPES = frozenset({"PriceEstimationError", "InternalServerError"})

#: Ограничение длины диагностики.
_ERROR_DETAIL_LIMIT = 300

#: Состояния заявки в книге и их перевод в общий исход.
_OUTCOMES: dict[str, OrderOutcome] = {
    "open": OrderOutcome.OPEN,
    "presignaturePending": OrderOutcome.OPEN,
    "fulfilled": OrderOutcome.FILLED,
    "expired": OrderOutcome.EXPIRED,
    "cancelled": OrderOutcome.CANCELLED,
}


class CowAdapter(HttpProviderAdapter):
    """Реализация :class:`AggregatorAdapter` для CoW Swap."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        http: Any,
        resources: Any,
        clock: Any,
        api_key: SecretValue | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            _PROVIDER,
            config,
            http=http,
            resources=resources,
            clock=clock,
            api_key=api_key,
            base_url=base_url or config.base_url or endpoints.DEFAULT_BASE_URL,
        )
        self._capabilities = AdapterCapabilities(
            provider_id=_PROVIDER,
            supported_networks=frozenset(
                NetworkId(name) for name in endpoints.SUPPORTED_NETWORK_SLUGS
            ),
            routing_modes=frozenset({RoutingMode.CLASSIC}),
            supports_fixed_route=_SUPPORTS_FIXED_ROUTE,
            supports_fee_discovery=False,
            # Расход газа провайдер сообщает, но к нашей ноге он не
            # относится: платит его решатель, а с нас удержано в цене.
            supports_gas_estimate=True,
            supports_execution=True,
            # Исполняет подписанной заявкой, а не транзакцией. Без этого
            # признака подсистема исполнения пошла бы собирать calldata и
            # получила бы отказ: форма исполнения выбирается по нему.
            supports_signed_orders=True,
        )

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Заявленные возможности адаптера."""
        return self._capabilities

    def auth_headers(self) -> dict[str, str]:
        """Книга заявок работает без ключа доступа."""
        return {}

    # --- котировка --------------------------------------------------------

    async def get_quote(self, request: QuoteRequest) -> Quote:
        """Получить котировку из книги заявок."""
        slug = self._require_network(request.network_id)
        payload = await self.request_json(
            path=endpoints.quote_path(slug),
            network_id=request.network_id,
            operation=self._capability_operation(request),
            request_id=request.request_id,
            method="POST",
            json_body=self._quote_body(request),
            priority=request.priority,
            correlation_id=request.correlation_id,
            timeout=request.timeout,
            priority_at=request.priority_at,
        )
        with normalized_response(_PROVIDER):
            return self._to_quote(request, payload)

    async def validate_fixed_route(self, request: QuoteRequest) -> RouteValidation:
        """Маршрут CoW Swap не раскрывает и воспроизвести его нельзя."""
        raise UnsupportedError(
            "cow adapter cannot validate a fixed route: the solver, not a pool path, "
            "sets the price, and the route is never disclosed",
            code="provider_fixed_route_unsupported",
            provider_code=_PROVIDER.value,
        )

    # --- исполнение -------------------------------------------------------

    async def build_order(self, request: QuoteRequest) -> SignedOrderPlan:
        """Собрать заявку, которую остаётся подписать и разместить.

        Котировка берётся заново: заявка размещается по свежей цене, а не
        по той, на которой возможность была найдена минутой раньше.

        Предельная сумма получения — это и есть обязательство. Она
        выводится из свежей котировки с допуском проскальзывания: заявка
        с пределом выше рынка просто не исполнится, заявка без предела
        отдала бы решателю всё.
        """
        if request.slippage_bps is None:
            raise DataError(
                "cow order requires an explicit slippage tolerance: "
                "without it the guaranteed minimum is unknown",
                code="slippage_missing",
                provider_code=_PROVIDER.value,
            )
        slug = self._require_network(request.network_id)
        swapper = self._require_swapper()
        payload = await self.request_json(
            path=endpoints.quote_path(slug),
            network_id=request.network_id,
            operation=self._capability_operation(request),
            request_id=request.request_id,
            method="POST",
            json_body=self._quote_body(request),
            priority=request.priority,
            correlation_id=request.correlation_id,
            timeout=request.timeout,
            priority_at=request.priority_at,
        )
        with normalized_response(_PROVIDER):
            quote = self._to_quote(request, payload)
            chain_id = self._require_chain_id(request.network_id)
            minimum = max(quote.output_amount.raw * (10_000 - request.slippage_bps) // 10_000, 1)
            valid_to = self._valid_to()
            order = {
                "sellToken": str(request.input_token.address),
                "buyToken": str(request.output_token.address),
                "receiver": swapper,
                # Продаём ровно то, что просили: стоимость исполнения
                # протокол удерживает внутри цены, а не отдельной суммой.
                "sellAmount": str(request.input_amount.raw),
                "buyAmount": str(minimum),
                "validTo": valid_to,
                "appData": endpoints.APP_DATA_HASH,
                # Отдельной комиссии у заявки нет — она уже в цене.
                "feeAmount": "0",
                "kind": endpoints.KIND_SELL,
                "partiallyFillable": False,
                "sellTokenBalance": endpoints.SELL_TOKEN_BALANCE,
                "buyTokenBalance": endpoints.BUY_TOKEN_BALANCE,
            }
            return SignedOrderPlan(
                provider_id=_PROVIDER,
                network_id=request.network_id,
                chain_id=chain_id,
                typed_data=self._typed_data(chain_id, order),
                allowances=(
                    AllowanceRequirement(
                        kind=AllowanceKind.ERC20,
                        contract=str(request.input_token.address),
                        spender=endpoints.VAULT_RELAYER_CONTRACT,
                    ),
                ),
                quote=quote,
                min_output_raw=minimum,
                valid_to=valid_to,
            )

    async def place_order(
        self, plan: SignedOrderPlan, signature: str, *, request_id: RequestId | None = None
    ) -> PlacedOrder:
        """Разместить подписанную заявку в книге.

        Подпись выполняется владельцем ключа и передаётся сюда готовой:
        адаптер ключа не видит и видеть не должен.
        """
        slug = self._require_network(plan.network_id)
        order: dict[str, Any] = dict(plan.typed_data["message"])
        payload = await self.request_json(
            path=endpoints.order_path(slug),
            network_id=plan.network_id,
            operation=self._operation_of(plan.quote.operation),
            request_id=request_id or RequestId.generate(),
            method="POST",
            json_body=order
            | {
                "signature": signature,
                "signingScheme": endpoints.SIGNING_SCHEME,
                "from": order["receiver"],
            },
        )
        with normalized_response(_PROVIDER):
            if not isinstance(payload, str) or not payload:
                raise DataError(
                    "cow order book did not return an order identifier",
                    code="order_uid_missing",
                    provider_code=_PROVIDER.value,
                )
            return PlacedOrder(
                provider_id=_PROVIDER,
                network_id=plan.network_id,
                uid=payload,
                valid_to=plan.valid_to,
            )

    async def order_state(
        self,
        network_id: NetworkId,
        uid: str,
        *,
        operation: OperationType,
        request_id: RequestId | None = None,
    ) -> OrderState:
        """Узнать, что стало с размещённой заявкой.

        Исполненные суммы приходят вместе с состоянием, поэтому за ними
        отдельного запроса не требуется.
        """
        slug = self._require_network(network_id)
        payload = await self.request_json(
            path=endpoints.order_path(slug, uid),
            network_id=network_id,
            operation=self._operation_of(operation),
            request_id=request_id or RequestId.generate(),
        )
        with normalized_response(_PROVIDER):
            return self._to_order_state(payload)

    # --- обслуживание -----------------------------------------------------

    async def discover_capabilities(self) -> AdapterCapabilities:
        """Возможности заявлены статически и API не уточняются.

        Отдельного описания возможностей книга заявок не публикует, а
        проверять их котировкой значило бы тратить запрос впустую.
        """
        return self._capabilities

    async def discover_fees(self, network_id: NetworkId) -> tuple[Fee, ...]:
        """Отдельного описания комиссий нет.

        Компоненты не выдумываются: стоимость исполнения уже отражена в
        названной сумме получения.
        """
        return ()

    async def health_check(self) -> AdapterHealth:
        """Проверить доступность книги заявок."""
        name = next(iter(endpoints.SUPPORTED_NETWORK_SLUGS))
        slug = endpoints.SUPPORTED_NETWORK_SLUGS[name]
        try:
            await self.request_json(
                path=endpoints.order_path(slug, "0x00"),
                network_id=NetworkId(name),
                operation=CapabilityOperation.QUOTE_SELL,
                request_id=RequestId.generate(),
                deduplication_key=f"cow:health:{slug}",
            )
        except DataError:
            # Несуществующая заявка — ожидаемый ответ: он доказывает, что
            # книга отвечает. Сбоем это не является.
            return AdapterHealth(provider_id=_PROVIDER, state=AdapterState.READY)
        except MonikError as error:
            return AdapterHealth(
                provider_id=_PROVIDER,
                state=AdapterState.DEGRADED,
                detail=error.info.code,
            )
        return AdapterHealth(provider_id=_PROVIDER, state=AdapterState.READY)

    # --- перевод ответов --------------------------------------------------

    def _to_quote(self, request: QuoteRequest, payload: Any) -> Quote:
        """Перевести ответ котировки в нормализованную котировку."""
        if not isinstance(payload, dict):
            raise DataError(
                "cow quote response is not an object",
                code="response_malformed",
                provider_code=_PROVIDER.value,
            )
        quote = require_field(payload, "quote", provider=_PROVIDER)
        if not isinstance(quote, dict):
            raise DataError(
                "cow quote response carries no quote object",
                code="response_malformed",
                provider_code=_PROVIDER.value,
            )
        output = parse_base_units(
            require_field(quote, "buyAmount", provider=_PROVIDER),
            provider=_PROVIDER,
            field="buyAmount",
        )
        return build_quote(
            provider_id=_PROVIDER,
            request=request,
            output_raw=output,
            route=self._to_route(request),
            created_at=self._clock.now(),
            # Ноль — это знание, а не пробел: транзакции от нашего счёта
            # не будет, и газа мы не платим. Стоимость исполнения уже
            # удержана внутри названной суммы получения.
            estimated_gas_units=0,
            slippage_bps=request.slippage_bps,
            output_includes_fees=True,
        )

    @staticmethod
    def _to_route(request: QuoteRequest) -> Route:
        """Маршрут одним шагом: состав пулов провайдер не раскрывает."""
        return Route(
            provider_id=_PROVIDER,
            network_id=request.network_id,
            operation=request.operation,
            routing_mode=RoutingMode.CLASSIC,
            input_token=request.input_token.key,
            output_token=request.output_token.key,
            steps=(
                RouteStep(
                    input_token=request.input_token.key,
                    output_token=request.output_token.key,
                    protocol=_ROUTE_PROTOCOL,
                ),
            ),
        )

    def _to_order_state(self, payload: Any) -> OrderState:
        """Перевести ответ книги в общее состояние заявки."""
        if not isinstance(payload, dict):
            raise DataError(
                "cow order response is not an object",
                code="response_malformed",
                provider_code=_PROVIDER.value,
            )
        status = require_field(payload, "status", provider=_PROVIDER)
        outcome = _OUTCOMES.get(str(status))
        if outcome is None:
            raise DataError(
                f"cow order book reported an unknown status {status!r}",
                code="order_status_unknown",
                provider_code=_PROVIDER.value,
            )
        return OrderState(
            outcome=outcome,
            executed_input_raw=self._executed(payload, "executedSellAmount"),
            executed_output_raw=self._executed(payload, "executedBuyAmount"),
        )

    @staticmethod
    def _executed(payload: dict[str, Any], field: str) -> int:
        """Исполненная сумма. Отсутствие поля означает «ещё нисколько»."""
        value = payload.get(field)
        if value is None:
            return 0
        return parse_base_units(value, provider=_PROVIDER, field=field)

    # --- запрос -----------------------------------------------------------

    def _quote_body(self, request: QuoteRequest) -> dict[str, Any]:
        """Тело запроса котировки.

        ``sellAmountBeforeFee`` означает «продаём столько, сколько
        названо, а стоимость исполнения удержите изнутри». Именно так
        сумма круга и задана у остальных агрегаторов.
        """
        swapper = self._require_swapper()
        return {
            "sellToken": str(request.input_token.address),
            "buyToken": str(request.output_token.address),
            "from": swapper,
            "receiver": swapper,
            "kind": endpoints.KIND_SELL,
            "sellAmountBeforeFee": str(request.input_amount.raw),
            "partiallyFillable": False,
            "sellTokenBalance": endpoints.SELL_TOKEN_BALANCE,
            "buyTokenBalance": endpoints.BUY_TOKEN_BALANCE,
            "signingScheme": endpoints.SIGNING_SCHEME,
            "onchainOrder": False,
            "priceQuality": "optimal",
            "validFor": self._validity_seconds(),
        }

    @staticmethod
    def _typed_data(chain_id: int, order: dict[str, Any]) -> dict[str, Any]:
        """Типизированные данные EIP-712 для подписи заявки.

        Домен сверен со значением, которое контракт расчётов сообщает
        сам: имя ``Gnosis Protocol``, версия ``v2``.
        """
        return {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Order": [{"name": name, "type": kind} for name, kind in endpoints.ORDER_TYPE],
            },
            "primaryType": "Order",
            "domain": {
                "name": endpoints.DOMAIN_NAME,
                "version": endpoints.DOMAIN_VERSION,
                "chainId": chain_id,
                "verifyingContract": endpoints.SETTLEMENT_CONTRACT,
            },
            "message": order,
        }

    def _valid_to(self) -> int:
        """Момент, после которого заявка недействительна."""
        return int(self._clock.now().timestamp()) + self._validity_seconds()

    def _validity_seconds(self) -> int:
        """Срок жизни заявки в секундах."""
        configured = self._config.option(OPTION_VALIDITY)
        if configured is None:
            return DEFAULT_VALIDITY_SECONDS
        try:
            value = int(configured)
        except (TypeError, ValueError):
            raise DataError(
                f"providers[cow].options.{OPTION_VALIDITY} is not a whole number of seconds",
                code="provider_option_invalid",
                provider_code=_PROVIDER.value,
            ) from None
        if value <= 0:
            raise DataError(
                f"providers[cow].options.{OPTION_VALIDITY} must be positive",
                code="provider_option_invalid",
                provider_code=_PROVIDER.value,
            )
        return value

    def _require_swapper(self) -> str:
        """Адрес, от имени которого составляется заявка."""
        swapper = self._config.option(OPTION_SWAPPER)
        if swapper is None:
            raise UnsupportedError(
                "cow order requires the trading account address in providers[cow].options.swapper",
                code="provider_swapper_missing",
                provider_code=_PROVIDER.value,
            )
        return swapper

    def _require_network(self, network_id: NetworkId) -> str:
        """Обозначение сети в пути либо отказ."""
        slug = endpoints.network_slug_for(network_id)
        if slug is None:
            raise UnsupportedError(
                f"cow adapter does not support network {network_id}",
                code="provider_network_unsupported",
                provider_code=_PROVIDER.value,
            )
        return slug

    def _require_chain_id(self, network_id: NetworkId) -> int:
        """Идентификатор цепи: он входит в домен подписи."""
        chain_id = endpoints.chain_id_for(network_id)
        if chain_id is None:
            raise UnsupportedError(
                f"cow adapter does not know the chain id of network {network_id}, "
                "and the EIP-712 domain cannot be built without it",
                code="provider_network_unsupported",
                provider_code=_PROVIDER.value,
            )
        return chain_id

    def _capability_operation(self, request: QuoteRequest) -> CapabilityOperation:
        """Операция для Capability Registry."""
        return self._operation_of(request.operation)

    @staticmethod
    def _operation_of(operation: OperationType) -> CapabilityOperation:
        """Перевод стороны круга в понятие Capability Registry."""
        if operation is OperationType.SELL:
            return CapabilityOperation.QUOTE_SELL
        return CapabilityOperation.QUOTE_BUY

    # --- перевод отказов --------------------------------------------------

    def _error_type(self, response: HttpResponse) -> str | None:
        """Вид ошибки из тела ответа."""
        body = self.error_body(response)
        if body is None:
            return None
        value = body.get(_ERROR_TYPE_FIELD)
        return value if isinstance(value, str) else None

    def _error_text(self, response: HttpResponse) -> str:
        """Пояснение отказа, уже отредактированное."""
        body = self.error_body(response) or {}
        text = body.get(_ERROR_DESCRIPTION_FIELD)
        if not isinstance(text, str):
            text = str(body.get(_ERROR_TYPE_FIELD, ""))
        return self.redact_provider_text(text)[:_ERROR_DETAIL_LIMIT]

    def no_route_reason(self, response: HttpResponse) -> str | None:
        """Перевод отказа «нет ликвидности» в общее понятие."""
        kind = self._error_type(response)
        if kind is None or kind not in _NO_ROUTE_TYPES:
            return None
        return f"cow reports no liquidity for the requested pair: {self._error_text(response)}"

    def route_rejection_reason(self, response: HttpResponse) -> str | None:
        """Перевод отказа по слишком малой сумме.

        Маршрут существует, но сумма не покрывает стоимости исполнения.
        Смысл иной, чем у отсутствия маршрута, и смешивать их нельзя.
        """
        kind = self._error_type(response)
        if kind is None or kind not in _ROUTE_REJECTED_TYPES:
            return None
        return f"cow rejected the order size: {self._error_text(response)}"

    def temporary_failure_reason(self, response: HttpResponse) -> str | None:
        """Перевод собственных неполадок провайдера во временный отказ."""
        kind = self._error_type(response)
        if kind is None or kind not in _TEMPORARY_TYPES:
            return None
        return f"cow price estimation failed temporarily: {self._error_text(response)}"

    def error_detail(self, response: HttpResponse) -> str | None:
        """Диагностика неуспешного ответа."""
        kind = self._error_type(response)
        if kind is None:
            return None
        return f"{kind}: {self._error_text(response)}"
