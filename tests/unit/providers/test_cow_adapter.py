"""Адаптер CoW Swap: котировка, заявка и перевод отказов.

Формы ответов сняты с живого API 2026-09-18. Особенность провайдера, ради
которой он и нужен: он **не отдаёт транзакцию**. Обязательство здесь —
предельная сумма самой заявки, а газа у такой ноги нет: стоимость
исполнения удержана внутри цены (``the_main_rules.md``, правило 16).
"""

from __future__ import annotations

import json

import pytest

from monik.domain.enums.errors import Retryability
from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import (
    AuthenticationError,
    DataError,
    NoRouteError,
    ProviderError,
    RouteRejectedError,
    UnsupportedError,
)
from monik.domain.models.execution import AllowanceKind, OrderOutcome
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.http import FakeHttpClient, HttpResponse
from monik.infrastructure.providers import QuoteRequest
from monik.infrastructure.providers.cow import CowAdapter, endpoints
from monik.infrastructure.providers.uniswap import UniswapAdapter
from monik.services.observability import FakeClock
from monik.services.trading.orders import executes_with_orders
from tests import factories as f
from tests.unit.providers.support import provider_config, resource_manager, secret

SWAPPER = "0x" + "c6" * 20

#: Ответ ``/quote``. ``buyAmount`` — то, что мы получим, продав всю
#: названную сумму: стоимость исполнения уже внутри.
QUOTE_PAYLOAD = {
    "quote": {
        "sellToken": str(f.USDT.address),
        "buyToken": str(f.AAVE.address),
        "receiver": SWAPPER,
        "sellAmount": "99987724",
        "buyAmount": "5140000000000000000",
        "validTo": 1789758584,
        "appData": endpoints.APP_DATA_HASH,
        "feeAmount": "12276",
        "gasAmount": "320136",
        "gasPrice": "417548606216",
        "kind": "sell",
        "partiallyFillable": False,
        "sellTokenBalance": "erc20",
        "buyTokenBalance": "erc20",
        "signingScheme": "eip712",
    },
    "from": SWAPPER,
    "expiration": "2026-09-18T19:09:44.453213098Z",
    "id": 13859801,
    "verified": True,
    "protocolFeeBps": "0.3",
}


def _adapter(
    responses: list[HttpResponse] | None = None,
    *,
    options: dict[str, str] | None = None,
    http: FakeHttpClient | None = None,
) -> CowAdapter:
    clock = FakeClock(f.NOW)
    return CowAdapter(
        provider_config(
            ProviderId.COW,
            options=options if options is not None else {"swapper": SWAPPER},
        ),
        http=http
        or FakeHttpClient(
            responses
            if responses is not None
            else [HttpResponse(status_code=200, text=json.dumps(QUOTE_PAYLOAD))]
        ),
        resources=resource_manager(clock),
        clock=clock,
    )


def _request(**overrides: object) -> QuoteRequest:
    base: dict[str, object] = {
        "network_id": f.POLYGON,
        "operation": OperationType.BUY,
        "input_token": f.USDT,
        "output_token": f.AAVE,
        "input_amount": f.USDT.amount_from_base_units(100_000_000),
        "request_id": f.RequestId.generate(),
        "slippage_bps": 10,
    }
    base.update(overrides)
    return QuoteRequest(**base)  # type: ignore[arg-type]


class TestQuote:
    async def test_output_is_what_we_receive_for_the_whole_amount(self) -> None:
        """``buyAmount`` уже за вычетом стоимости исполнения."""
        quote = await _adapter().get_quote(_request())

        assert quote.output_amount.raw == 5_140_000_000_000_000_000
        assert quote.raw_output_amount_includes_fees is True

    async def test_gas_is_zero_and_not_unknown(self) -> None:
        """Ноль — это знание: транзакции от нашего счёта не будет.

        ``None`` означал бы «неизвестно» и запрещал бы считать круг;
        здесь же расход действительно нулевой, потому что стоимость
        исполнения удержана внутри цены.
        """
        quote = await _adapter().get_quote(_request())

        assert quote.estimated_gas_units == 0

    async def test_quote_is_requested_with_the_whole_amount(self) -> None:
        """Продаём ровно столько, сколько названо кругом."""
        http = FakeHttpClient([HttpResponse(status_code=200, text=json.dumps(QUOTE_PAYLOAD))])
        await _adapter(http=http).get_quote(_request())

        body = http.calls[0].request.json_body
        assert body["sellAmountBeforeFee"] == "100000000"
        assert body["kind"] == endpoints.KIND_SELL
        assert body["from"] == SWAPPER

    async def test_swapper_is_required(self) -> None:
        with pytest.raises(UnsupportedError):
            await _adapter(options={}).get_quote(_request())

    async def test_unsupported_network_is_refused(self) -> None:
        """Незаявленная сеть — отказ, а не молчаливая подстановка Polygon.

        Адаптер объявляет только те сети, поддержка которых проверена;
        одинаковый символ токена на другой сети означает другой контракт.
        """
        arbitrum = NetworkId("arbitrum")
        source = f.USDT.model_copy(update={"network_id": arbitrum})
        target = f.AAVE.model_copy(update={"network_id": arbitrum})

        with pytest.raises(UnsupportedError):
            await _adapter().get_quote(
                _request(
                    network_id=arbitrum,
                    input_token=source,
                    output_token=target,
                    input_amount=source.amount_from_base_units(100_000_000),
                )
            )


class TestOrder:
    async def test_limit_follows_the_requested_tolerance(self) -> None:
        """Предел заявки — и есть обязательство."""
        plan = await _adapter().build_order(_request(slippage_bps=10))

        assert plan.min_output_raw == 5_140_000_000_000_000_000 * 9_990 // 10_000

    async def test_order_sells_the_whole_amount_without_a_separate_fee(self) -> None:
        """Отдельной комиссии у заявки нет — она внутри цены."""
        plan = await _adapter().build_order(_request())
        order = plan.typed_data["message"]

        assert order["sellAmount"] == "100000000"
        assert order["feeAmount"] == "0"
        assert order["buyAmount"] == str(plan.min_output_raw)

    async def test_domain_matches_the_settlement_contract(self) -> None:
        """Домен сверен со значением, которое контракт сообщает сам."""
        plan = await _adapter().build_order(_request())
        domain = plan.typed_data["domain"]

        assert domain["name"] == "Gnosis Protocol"
        assert domain["version"] == "v2"
        assert domain["chainId"] == 137
        assert domain["verifyingContract"] == endpoints.SETTLEMENT_CONTRACT

    async def test_order_type_field_order_is_fixed(self) -> None:
        """Порядок полей входит в хеш типа: перестановка ломает подпись."""
        plan = await _adapter().build_order(_request())

        assert [field["name"] for field in plan.typed_data["types"]["Order"]] == [
            "sellToken",
            "buyToken",
            "receiver",
            "sellAmount",
            "buyAmount",
            "validTo",
            "appData",
            "feeAmount",
            "kind",
            "partiallyFillable",
            "sellTokenBalance",
            "buyTokenBalance",
        ]

    async def test_allowance_goes_to_the_vault_relayer(self) -> None:
        """Списывает не роутер и не контракт расчётов, а отдельный посредник."""
        plan = await _adapter().build_order(_request())

        assert len(plan.allowances) == 1
        allowance = plan.allowances[0]
        assert allowance.kind is AllowanceKind.ERC20
        assert allowance.contract == str(f.USDT.address)
        assert allowance.spender == endpoints.VAULT_RELAYER_CONTRACT
        assert allowance.spender != endpoints.SETTLEMENT_CONTRACT

    async def test_validity_is_bounded_and_configurable(self) -> None:
        adapter = _adapter(options={"swapper": SWAPPER, "order_validity_seconds": "60"})
        plan = await adapter.build_order(_request())

        assert plan.valid_to == int(f.NOW.timestamp()) + 60

    async def test_slippage_is_required(self) -> None:
        """Без допуска обязательство неизвестно, а не равно нулю."""
        with pytest.raises(DataError):
            await _adapter().build_order(_request(slippage_bps=None))

    async def test_building_a_transaction_is_refused(self) -> None:
        """Провайдер транзакций не отдаёт и не делает вид, что отдаёт."""
        with pytest.raises(UnsupportedError):
            await _adapter().build_swap(_request())

    async def test_fixed_route_cannot_be_validated(self) -> None:
        """Маршрут назначает решатель, и воспроизводить нечего."""
        with pytest.raises(UnsupportedError):
            await _adapter().validate_fixed_route(_request())


class TestOrderState:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("open", OrderOutcome.OPEN),
            ("presignaturePending", OrderOutcome.OPEN),
            ("fulfilled", OrderOutcome.FILLED),
            ("expired", OrderOutcome.EXPIRED),
            ("cancelled", OrderOutcome.CANCELLED),
        ],
    )
    async def test_status_is_translated(self, status: str, expected: OrderOutcome) -> None:
        adapter = _adapter(
            [
                HttpResponse(
                    status_code=200,
                    text=json.dumps(
                        {
                            "status": status,
                            "executedSellAmount": "100000000",
                            "executedBuyAmount": "5140000000000000000",
                        }
                    ),
                )
            ]
        )

        state = await adapter.order_state(f.POLYGON, "0xabc", operation=OperationType.BUY)

        assert state.outcome is expected
        assert state.is_final is (expected is not OrderOutcome.OPEN)
        assert state.executed_output_raw == 5_140_000_000_000_000_000

    async def test_missing_execution_means_nothing_executed_yet(self) -> None:
        adapter = _adapter([HttpResponse(status_code=200, text=json.dumps({"status": "open"}))])

        state = await adapter.order_state(f.POLYGON, "0xabc", operation=OperationType.BUY)

        assert state.executed_input_raw == 0
        assert state.executed_output_raw == 0

    async def test_unknown_status_is_a_data_error(self) -> None:
        """Незнакомое состояние не превращается в «наверное, открыта»."""
        adapter = _adapter(
            [HttpResponse(status_code=200, text=json.dumps({"status": "teleported"}))]
        )

        with pytest.raises(DataError):
            await adapter.order_state(f.POLYGON, "0xabc", operation=OperationType.BUY)


class TestRefusals:
    async def test_no_liquidity_becomes_no_route(self) -> None:
        adapter = _adapter(
            [
                HttpResponse(
                    status_code=404,
                    text=json.dumps({"errorType": "NoLiquidity", "description": "no route found"}),
                )
            ]
        )

        with pytest.raises(NoRouteError):
            await adapter.get_quote(_request())

    async def test_amount_too_small_is_a_rejected_route_not_a_missing_one(self) -> None:
        """Маршрут есть, но сумма не покрывает исполнения — смысл иной."""
        adapter = _adapter(
            [
                HttpResponse(
                    status_code=400,
                    text=json.dumps(
                        {
                            "errorType": "SellAmountDoesNotCoverFee",
                            "description": "sell amount does not cover the fee",
                        }
                    ),
                )
            ]
        )

        with pytest.raises(RouteRejectedError):
            await adapter.get_quote(_request())


class TestCapabilities:
    def test_the_adapter_declares_that_it_executes_with_orders(self) -> None:
        """Признак объявляет сам адаптер, и проверять надо именно его.

        Двойник в тестах исполнения объявляет признак вручную, поэтому
        забытый флаг у настоящего адаптера теми тестами не ловится: там
        проверяется путь, здесь — что провайдер на него попадёт.
        """
        capabilities = _adapter().capabilities

        assert capabilities.supports_execution is True
        assert capabilities.supports_signed_orders is True
        assert executes_with_orders(_adapter()) is True

    def test_transaction_providers_do_not_claim_orders(self) -> None:
        """Признак — свойство провайдера, а не значение по умолчанию."""
        clock = FakeClock(f.NOW)
        uniswap = UniswapAdapter(
            provider_config(ProviderId.UNISWAP, options={"swapper": SWAPPER}),
            http=FakeHttpClient([]),
            resources=resource_manager(clock),
            clock=clock,
            api_key=secret(),
        )

        assert uniswap.capabilities.supports_signed_orders is False
        assert executes_with_orders(uniswap) is False


class TestEdgeRefusal:
    """Отказ пограничного узла — не отвергнутый ключ.

    19.09 CloudFront закрыл доступ по частоте обращений, и отказ пришёл
    статусом 403. Классификация по умолчанию назвала это «провайдер
    отверг учётные данные», хотя у книги заявок учётных данных нет вовсе:
    оператор получил уведомление, отправляющее искать несуществующую
    причину.
    """

    @pytest.mark.parametrize("status", [401, 403, 429])
    async def test_edge_refusal_is_temporary_not_authentication(self, status: int) -> None:
        # Временный отказ вызывает повтор — двойник должен ответить
        # столько же раз, сколько Resource Manager попробует.
        adapter = _adapter(
            [
                HttpResponse(
                    status_code=status,
                    text="<HTML><H1>403 ERROR</H1>Request blocked.</HTML>",
                    headers={"Server": "CloudFront"},
                )
            ]
            * 3
        )

        with pytest.raises(ProviderError) as refusal:
            await adapter.get_quote(_request())

        assert refusal.value.info.code == "provider_temporary_failure"
        assert not isinstance(refusal.value, AuthenticationError)
        assert "rate limit" in str(refusal.value)

    async def test_the_refusing_server_is_named(self) -> None:
        """Оператору нужно видеть, кто отказал, а не гадать."""
        adapter = _adapter(
            [
                HttpResponse(
                    status_code=403,
                    text="blocked",
                    headers={"Server": "CloudFront"},
                )
            ]
            * 3
        )

        with pytest.raises(ProviderError) as refusal:
            await adapter.get_quote(_request())

        assert "CloudFront" in str(refusal.value)


class TestRefusalIsNotRetried:
    """Отказ по частоте обращений повтором не лечится.

    19.09 три повтора подряд уронили весь проход вместе с исправным
    Uniswap: повторы съели срок цикла. И сам повтор вреден — он
    добавляет нагрузку тому, кто уже закрыл доступ.
    """

    @pytest.mark.parametrize("status", [401, 403, 429])
    async def test_an_edge_refusal_is_not_retried(self, status: int) -> None:
        # Один ответ, а не три: повторов быть не должно, и лишние
        # заготовленные ответы скрыли бы их наличие.
        adapter = _adapter(
            [
                HttpResponse(
                    status_code=status,
                    text="blocked",
                    headers={"Server": "CloudFront"},
                )
            ]
        )

        with pytest.raises(ProviderError) as refusal:
            await adapter.get_quote(_request())

        assert refusal.value.info.retryability is Retryability.NON_RETRYABLE

    async def test_other_temporary_failures_are_still_retried(self) -> None:
        """Собственная неполадка провайдера повтором лечится."""
        adapter = _adapter(
            [
                HttpResponse(
                    status_code=500,
                    text=json.dumps(
                        {"errorType": "PriceEstimationError", "description": "try again"}
                    ),
                )
            ]
            * 3
        )

        with pytest.raises(ProviderError) as refusal:
            await adapter.get_quote(_request())

        assert refusal.value.info.retryability is Retryability.CONDITIONAL
