"""Adapter Velora Delta.

Контракт сверен с живым API и с контрактом протокола 19.09.2026
(``endpoints.py`` перечисляет, что именно проверено).

Второй провайдер Monik, исполняющий **подписанным намерением**, а не
транзакцией (``the_main_rules.md``, правило 18). Общая форма исполнения
уже есть — :class:`SignedOrderPlan`, — и здесь остаются только
особенности самой Delta:

* **типизированные данные отдаёт сам API.** CoW Swap приходится собирать
  их из констант; Delta возвращает готовые ``domain``, ``types`` и
  значение заявки. Приводить их к общей форме — работа переходника:
  наружу выходит обычный EIP-712 с полем ``message``;
* **предел получения задаётся допуском в базисных пунктах.** Подменить
  сумму в котировке нельзя: она подписана полем ``hmac``, и правка
  отвергается. Поэтому обязательство выражается через ``slippage``, а
  затем сверяется с тем, что вернула сборка, — предел, который выставили
  не мы, принимать нельзя;
* **газа у ноги нет.** Заявку исполняет агент, и стоимость исполнения
  уже вычтена из названной суммы получения;
* **разрешение выдаётся контракту Delta**, и его адрес приходит вместе с
  котировкой, а не зашит в код.
"""

from __future__ import annotations

from typing import Any

from monik.config.secrets import SecretValue
from monik.config.sections.providers import ProviderConfig
from monik.domain.enums.capability import CapabilityOperation
from monik.domain.enums.health import AdapterState
from monik.domain.enums.operations import OperationType, RoutingMode
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
from monik.infrastructure.providers.http_adapter import HttpProviderAdapter
from monik.infrastructure.providers.normalization import (
    build_quote,
    normalized_response,
    parse_base_units,
    require_field,
)
from monik.infrastructure.providers.velora_delta import endpoints

__all__ = ["VeloraDeltaAdapter"]

_PROVIDER = ProviderId.VELORA_DELTA

#: Адрес счёта, от имени которого составляется и подписывается заявка.
OPTION_SWAPPER = "swapper"

#: Опция срока жизни заявки, в секундах.
OPTION_VALIDITY = "order_validity_seconds"

#: Срок жизни заявки по умолчанию. Задаётся **абсолютным временем**:
#: проверено живым API 19.09 — параметр ``deadline`` принимается и
#: соблюдается, а ``expiry`` и ``validFor`` молча игнорируются, оставляя
#: час по умолчанию.
#:
#: Срок здесь — это не только время жизни заявки, но и **срок
#: резервирования средств**: пока заявка жива, её сумма считается
#: занятой, иначе вторую заявку можно было бы подписать на те же деньги.
#: Часовой срок по умолчанию запирал капитал на час ради намерения,
#: которое ловит расхождение длиной в минуты.
DEFAULT_VALIDITY_SECONDS = 10

#: Маршрут Delta не раскрывает: цену назначает агент, а не путь по пулам.
_SUPPORTS_FIXED_ROUTE = False

#: Обозначение маршрута в отпечатке. Состав пулов провайдер не сообщает.
_ROUTE_PROTOCOL = "velora_delta_agent"

#: Поле тела ошибки, называющее её вид, и поле с пояснением.
_ERROR_TYPE_FIELD = "errorType"
_ERROR_DETAIL_FIELD = "details"

#: Вид ошибки: маршрута нет.
_NO_ROUTE_TYPES = frozenset({"NoRoutesFound", "NoRouteFound"})

#: Вид ошибки: сумма слишком мала, чтобы покрыть исполнение.
_ROUTE_REJECTED_TYPES = frozenset({"OrderValidationError", "AmountTooSmall"})

#: Ограничение длины диагностики.
_ERROR_DETAIL_LIMIT = 300

#: Состояния заявки и их перевод в общий исход.
#:
#: ⚠️ Список составлен по наблюдаемым ответам и документацией не
#: подтверждён: она в среде разработки недоступна. Незнакомое состояние
#: сознательно приводит к ошибке данных, а не к догадке: принять
#: неизвестное за исполнение значило бы объявить сделку закрытой, ничего
#: о ней не зная.
#:
#: ``RUNNING`` добавлено 19.09 по живому ответу на сделке ``#T3``: так
#: выглядит заявка, принятая книгой и ещё не взятая агентом. Замысел
#: сработал ровно как задуман — система остановилась и назвала значение
#: вместо того, чтобы его выдумать.
_OUTCOMES: dict[str, OrderOutcome] = {
    "PENDING": OrderOutcome.OPEN,
    "ACCEPTED": OrderOutcome.OPEN,
    "RUNNING": OrderOutcome.OPEN,
    "EXECUTING": OrderOutcome.OPEN,
    "EXECUTED": OrderOutcome.FILLED,
    "FULFILLED": OrderOutcome.FILLED,
    "EXPIRED": OrderOutcome.EXPIRED,
    "CANCELLED": OrderOutcome.CANCELLED,
    "FAILED": OrderOutcome.EXPIRED,
}


class VeloraDeltaAdapter(HttpProviderAdapter):
    """Реализация :class:`AggregatorAdapter` для Velora Delta."""

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
            supported_networks=frozenset(NetworkId(name) for name in endpoints.SUPPORTED_CHAIN_IDS),
            routing_modes=frozenset({RoutingMode.CLASSIC}),
            supports_fixed_route=_SUPPORTS_FIXED_ROUTE,
            supports_fee_discovery=False,
            supports_gas_estimate=True,
            supports_execution=True,
            supports_signed_orders=True,
        )

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Заявленные возможности адаптера."""
        return self._capabilities

    def auth_headers(self) -> dict[str, str]:
        """Delta работает без ключа доступа."""
        return {}

    # --- котировка --------------------------------------------------------

    async def get_quote(self, request: QuoteRequest) -> Quote:
        """Получить котировку намерения."""
        payload = await self._price(request)
        with normalized_response(_PROVIDER):
            return self._to_quote(request, payload)

    async def validate_fixed_route(self, request: QuoteRequest) -> RouteValidation:
        """Маршрут назначает агент, и воспроизводить нечего."""
        raise UnsupportedError(
            "velora delta cannot validate a fixed route: an agent, not a pool path, "
            "decides how the order is filled",
            code="provider_fixed_route_unsupported",
            provider_code=_PROVIDER.value,
        )

    # --- исполнение -------------------------------------------------------

    async def build_order(self, request: QuoteRequest) -> SignedOrderPlan:
        """Собрать намерение, которое остаётся подписать и отправить.

        Котировка берётся заново, и сборка выполняется по ней же: цена
        подписана полем ``hmac``, поэтому передаётся как есть.

        Обязательство выражается допуском в базисных пунктах, а затем
        **проверяется**: API вправе подставить собственный допуск, и
        принять предел, которого мы не просили, нельзя — по нему нас и
        исполнят.
        """
        if request.slippage_bps is None:
            raise DataError(
                "velora delta order requires an explicit slippage tolerance: "
                "without it the guaranteed minimum is unknown",
                code="slippage_missing",
                provider_code=_PROVIDER.value,
            )
        chain_id = self._require_chain_id(request.network_id)
        swapper = self._require_swapper()
        payload = await self._price(request)
        with normalized_response(_PROVIDER):
            quote = self._to_quote(request, payload)
            price = self._price_object(payload)
            spender = self._require_spender(payload)
        built = await self.request_json(
            path=endpoints.BUILD_PATH,
            network_id=request.network_id,
            operation=self._operation_of(request.operation),
            request_id=RequestId.generate(),
            method="POST",
            json_body={
                "price": price,
                "owner": swapper,
                "beneficiary": swapper,
                "chainId": chain_id,
                # Базисные пункты: проверено сравнением предела заявки с
                # обещанием котировки.
                "slippage": request.slippage_bps,
                # Абсолютное время: относительные параметры провайдер
                # принимает, но не соблюдает.
                "deadline": int(self._clock.now().timestamp()) + self._validity_seconds(),
            },
            priority=request.priority,
            correlation_id=request.correlation_id,
            timeout=request.timeout,
            priority_at=request.priority_at,
        )
        with normalized_response(_PROVIDER):
            typed_data, order = self._to_typed_data(built)
            granted = parse_base_units(
                order.get("destAmount"), provider=_PROVIDER, field="destAmount"
            )
            minimum = self._required_minimum(order, quote, request.slippage_bps)
            if granted < minimum:
                raise DataError(
                    "velora delta returned a weaker guarantee than requested: "
                    f"asked for at least {minimum}, the order commits to {granted}",
                    code="order_minimum_weakened",
                    provider_code=_PROVIDER.value,
                )
            return SignedOrderPlan(
                provider_id=_PROVIDER,
                network_id=request.network_id,
                chain_id=chain_id,
                typed_data=typed_data,
                allowances=(
                    AllowanceRequirement(
                        kind=AllowanceKind.ERC20,
                        contract=str(request.input_token.address),
                        spender=spender,
                    ),
                ),
                quote=quote,
                min_output_raw=granted,
                valid_to=self._deadline(order),
            )

    async def place_order(
        self, plan: SignedOrderPlan, signature: str, *, request_id: RequestId | None = None
    ) -> PlacedOrder:
        """Отправить подписанное намерение."""
        chain_id = self._require_chain_id(plan.network_id)
        order: dict[str, Any] = dict(plan.typed_data["message"])
        payload = await self.request_json(
            path=endpoints.ORDERS_PATH,
            network_id=plan.network_id,
            operation=self._operation_of(plan.quote.operation),
            request_id=request_id or RequestId.generate(),
            method="POST",
            json_body={
                "chainId": chain_id,
                "order": order,
                "signature": signature,
                "owner": order["owner"],
            },
        )
        with normalized_response(_PROVIDER):
            return PlacedOrder(
                provider_id=_PROVIDER,
                network_id=plan.network_id,
                uid=self._order_id(payload),
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
        """Узнать судьбу отправленного намерения."""
        payload = await self.request_json(
            path=endpoints.order_path(uid),
            network_id=network_id,
            operation=self._operation_of(operation),
            request_id=request_id or RequestId.generate(),
        )
        with normalized_response(_PROVIDER):
            return self._to_order_state(payload)

    # --- обслуживание -----------------------------------------------------

    async def discover_capabilities(self) -> AdapterCapabilities:
        """Отдельного описания возможностей Delta не публикует."""
        return self._capabilities

    async def discover_fees(self, network_id: NetworkId) -> tuple[Fee, ...]:
        """Стоимость исполнения уже отражена в названной сумме получения."""
        return ()

    async def health_check(self) -> AdapterHealth:
        """Проверить доступность API несуществующей заявкой.

        Отказ «такого идентификатора нет» доказывает, что книга отвечает,
        и сбоем не является.
        """
        name = next(iter(endpoints.SUPPORTED_CHAIN_IDS))
        try:
            await self.request_json(
                path=endpoints.order_path("00000000-0000-0000-0000-000000000000"),
                network_id=NetworkId(name),
                operation=CapabilityOperation.QUOTE_SELL,
                request_id=RequestId.generate(),
                deduplication_key=f"velora_delta:health:{name}",
            )
        except DataError:
            return AdapterHealth(provider_id=_PROVIDER, state=AdapterState.READY)
        except MonikError as error:
            return AdapterHealth(
                provider_id=_PROVIDER,
                state=AdapterState.DEGRADED,
                detail=error.info.code,
            )
        return AdapterHealth(provider_id=_PROVIDER, state=AdapterState.READY)

    # --- запросы и перевод ------------------------------------------------

    async def _price(self, request: QuoteRequest) -> Any:
        """Котировка намерения."""
        chain_id = self._require_chain_id(request.network_id)
        swapper = self._require_swapper()
        return await self.request_json(
            path=endpoints.PRICES_PATH,
            network_id=request.network_id,
            operation=self._operation_of(request.operation),
            request_id=request.request_id,
            params={
                "srcToken": str(request.input_token.address),
                "destToken": str(request.output_token.address),
                "amount": str(request.input_amount.raw),
                "srcDecimals": str(request.input_token.decimals),
                "destDecimals": str(request.output_token.decimals),
                "side": endpoints.SIDE_SELL,
                "chainId": str(chain_id),
                "userAddress": swapper,
            },
            priority=request.priority,
            correlation_id=request.correlation_id,
            timeout=request.timeout,
            priority_at=request.priority_at,
        )

    def _to_quote(self, request: QuoteRequest, payload: Any) -> Quote:
        """Перевести ответ котировки в нормализованную котировку."""
        price = self._price_object(payload)
        output = parse_base_units(
            require_field(price, "destAmount", provider=_PROVIDER),
            provider=_PROVIDER,
            field="destAmount",
        )
        return build_quote(
            provider_id=_PROVIDER,
            request=request,
            output_raw=output,
            route=self._to_route(request),
            created_at=self._clock.now(),
            # Ноль — знание, а не пробел: транзакции от нашего счёта не
            # будет, исполняет агент, и его расход уже вычтен из суммы.
            estimated_gas_units=0,
            slippage_bps=request.slippage_bps,
            output_includes_fees=True,
        )

    @staticmethod
    def _price_object(payload: Any) -> dict[str, Any]:
        """Котировка из ответа."""
        if not isinstance(payload, dict):
            raise DataError(
                "velora delta response is not an object",
                code="response_malformed",
                provider_code=_PROVIDER.value,
            )
        price = payload.get("price")
        if not isinstance(price, dict):
            raise DataError(
                "velora delta response carries no price object",
                code="response_malformed",
                provider_code=_PROVIDER.value,
            )
        return price

    @staticmethod
    def _require_spender(payload: Any) -> str:
        """Кому выдаётся разрешение. Адрес приходит вместе с котировкой."""
        spender = payload.get("deltaAddress") if isinstance(payload, dict) else None
        if not isinstance(spender, str) or len(spender) != 42:
            raise DataError(
                "velora delta response carries no contract address to approve",
                code="spender_missing",
                provider_code=_PROVIDER.value,
            )
        return spender

    @staticmethod
    def _to_route(request: QuoteRequest) -> Route:
        """Маршрут одним шагом: состав исполнения провайдер не раскрывает."""
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

    @staticmethod
    def _to_typed_data(built: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """Привести ответ сборки к обычной форме EIP-712.

        Провайдер отдаёт заявку под ключом ``value`` и не объявляет тип
        домена: и то и другое — его особенность. Наружу выходит обычная
        структура с ``message`` и полным набором типов, потому что
        подписывать её будет общий кошелёк, а не этот адаптер.
        """
        if not isinstance(built, dict):
            raise DataError(
                "velora delta build response is not an object",
                code="response_malformed",
                provider_code=_PROVIDER.value,
            )
        to_sign = built.get("toSign")
        if not isinstance(to_sign, dict):
            raise DataError(
                "velora delta build response carries nothing to sign",
                code="response_malformed",
                provider_code=_PROVIDER.value,
            )
        domain = to_sign.get("domain")
        types = to_sign.get("types")
        order = to_sign.get("value")
        if not (isinstance(domain, dict) and isinstance(types, dict) and isinstance(order, dict)):
            raise DataError(
                "velora delta build response is incomplete",
                code="response_malformed",
                provider_code=_PROVIDER.value,
            )
        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                **types,
            },
            "primaryType": "Order",
            "domain": domain,
            "message": order,
        }
        return typed_data, order

    @staticmethod
    def _required_minimum(order: dict[str, Any], quote: Quote, slippage_bps: int) -> int:
        """Каким предел обязан быть при запрошенном допуске.

        Считается от того же основания, которым пользуется провайдер, —
        от ``expectedAmount`` самой заявки. Считать от котировки нельзя:
        сборка выполняется отдельным запросом, её ожидание может
        отличаться от котировочного на единицы, и тогда годная заявка
        отвергалась бы из-за округления, а не из-за условий.
        """
        expected = order.get("expectedAmount")
        base = (
            parse_base_units(expected, provider=_PROVIDER, field="expectedAmount")
            if expected is not None
            else quote.output_amount.raw
        )
        return max(base * (10_000 - slippage_bps) // 10_000, 1)

    @staticmethod
    def _deadline(order: dict[str, Any]) -> int:
        """Момент, после которого намерение недействительно."""
        value = order.get("deadline")
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise DataError(
                "velora delta order carries no deadline",
                code="deadline_missing",
                provider_code=_PROVIDER.value,
            )
        return parse_base_units(value, provider=_PROVIDER, field="deadline")

    @staticmethod
    def _order_id(payload: Any) -> str:
        """Идентификатор отправленной заявки."""
        if isinstance(payload, str) and payload:
            return payload
        if isinstance(payload, dict):
            for field in ("id", "orderId", "uuid"):
                value = payload.get(field)
                if isinstance(value, str) and value:
                    return value
        raise DataError(
            "velora delta did not return an order identifier",
            code="order_uid_missing",
            provider_code=_PROVIDER.value,
        )

    def _to_order_state(self, payload: Any) -> OrderState:
        """Перевести ответ книги в общее состояние заявки."""
        if not isinstance(payload, dict):
            raise DataError(
                "velora delta order response is not an object",
                code="response_malformed",
                provider_code=_PROVIDER.value,
            )
        status = require_field(payload, "status", provider=_PROVIDER)
        outcome = _OUTCOMES.get(str(status).upper())
        if outcome is None:
            raise DataError(
                f"velora delta reported an unknown order status {status!r}",
                code="order_status_unknown",
                provider_code=_PROVIDER.value,
            )
        return OrderState(
            outcome=self._settle_stale(payload, outcome),
            executed_input_raw=self._executed(payload, "executedSrcAmount"),
            executed_output_raw=self._executed(payload, "executedDestAmount"),
        )

    def _settle_stale(self, payload: dict[str, Any], reported: OrderOutcome) -> OrderOutcome:
        """Уточнить исход, когда учёт провайдера отстаёт от цепи.

        19.09 на сделке ``#T3`` заявка осталась в состоянии ``RUNNING``
        и после истечения своего срока: провайдер не переписал статус.
        Верить ему на слово нельзя — позиция зависла бы навсегда, а
        вместе с ней и резерв средств под неё.

        Источник истины здесь — не учёт провайдера, а то, что проверяет
        контракт. Срок заявки в него зашит: после него расчёт невозможен,
        сколько бы ни говорила книга. А состоявшийся расчёт виден
        списком транзакций — он либо есть, либо нет.
        """
        if reported.is_final:
            return reported
        if payload.get("transactions"):
            # Расчёт состоялся, а статус ещё не переписан. Факт цепи
            # весомее записи в книге.
            return OrderOutcome.FILLED
        if self._deadline_passed(payload):
            return OrderOutcome.EXPIRED
        return reported

    def _deadline_passed(self, payload: dict[str, Any]) -> bool:
        """Истёк ли срок, зашитый в саму заявку."""
        order = payload.get("order")
        if not isinstance(order, dict):
            return False
        deadline = order.get("deadline")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, str)):
            return False
        try:
            moment = int(deadline)
        except ValueError:
            return False
        return moment <= int(self._clock.now().timestamp())

    @staticmethod
    def _executed(payload: dict[str, Any], field: str) -> int:
        """Исполненная сумма. Отсутствие поля означает «ещё нисколько»."""
        value = payload.get(field)
        if value is None:
            return 0
        return parse_base_units(value, provider=_PROVIDER, field=field)

    def _validity_seconds(self) -> int:
        """Сколько живёт заявка и, вместе с ней, резерв средств."""
        configured = self._config.option(OPTION_VALIDITY)
        if configured is None:
            return DEFAULT_VALIDITY_SECONDS
        try:
            value = int(configured)
        except (TypeError, ValueError):
            raise DataError(
                f"providers[velora_delta].options.{OPTION_VALIDITY} "
                "is not a whole number of seconds",
                code="provider_option_invalid",
                provider_code=_PROVIDER.value,
            ) from None
        if value <= 0:
            raise DataError(
                f"providers[velora_delta].options.{OPTION_VALIDITY} must be positive",
                code="provider_option_invalid",
                provider_code=_PROVIDER.value,
            )
        return value

    def _require_swapper(self) -> str:
        """Адрес, от имени которого составляется заявка."""
        swapper = self._config.option(OPTION_SWAPPER)
        if swapper is None:
            raise UnsupportedError(
                "velora delta order requires the trading account address in "
                "providers[velora_delta].options.swapper",
                code="provider_swapper_missing",
                provider_code=_PROVIDER.value,
            )
        return swapper

    def _require_chain_id(self, network_id: NetworkId) -> int:
        """Идентификатор цепи: он входит и в запрос, и в домен подписи."""
        chain_id = endpoints.chain_id_for(network_id)
        if chain_id is None:
            raise UnsupportedError(
                f"velora delta adapter does not support network {network_id}",
                code="provider_network_unsupported",
                provider_code=_PROVIDER.value,
            )
        return chain_id

    @staticmethod
    def _operation_of(operation: OperationType) -> CapabilityOperation:
        """Перевод стороны круга в понятие Capability Registry."""
        if operation is OperationType.SELL:
            return CapabilityOperation.QUOTE_SELL
        return CapabilityOperation.QUOTE_BUY

    # --- перевод отказов --------------------------------------------------

    def _error_type(self, response: HttpResponse) -> str | None:
        body = self.error_body(response)
        if body is None:
            return None
        value = body.get(_ERROR_TYPE_FIELD)
        return value if isinstance(value, str) else None

    def _error_text(self, response: HttpResponse) -> str:
        body = self.error_body(response) or {}
        text = body.get(_ERROR_DETAIL_FIELD)
        if not isinstance(text, str):
            text = str(body.get(_ERROR_TYPE_FIELD, ""))
        return self.redact_provider_text(text)[:_ERROR_DETAIL_LIMIT]

    def no_route_reason(self, response: HttpResponse) -> str | None:
        """Перевод отказа «маршрута нет» в общее понятие."""
        kind = self._error_type(response)
        if kind is None or kind not in _NO_ROUTE_TYPES:
            return None
        return f"velora delta reports no route: {self._error_text(response)}"

    def route_rejection_reason(self, response: HttpResponse) -> str | None:
        """Перевод отказа по условиям заявки."""
        kind = self._error_type(response)
        if kind is None or kind not in _ROUTE_REJECTED_TYPES:
            return None
        return f"velora delta rejected the order: {self._error_text(response)}"

    def error_detail(self, response: HttpResponse) -> str | None:
        """Диагностика неуспешного ответа."""
        kind = self._error_type(response)
        if kind is None:
            return None
        return f"{kind}: {self._error_text(response)}"
