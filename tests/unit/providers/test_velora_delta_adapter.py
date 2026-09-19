"""Адаптер Velora Delta: котировка, намерение и перевод отказов.

Формы ответов сняты с живого API 19.09.2026. Особенность провайдера:
он исполняет **подписанным намерением**, типизированные данные для него
отдаёт сам, а предел получения выводит из допуска в базисных пунктах —
подменить сумму в котировке нельзя, она подписана ``hmac``.
"""

from __future__ import annotations

import json

import pytest

from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import DataError, NoRouteError, RouteRejectedError, UnsupportedError
from monik.domain.models.execution import AllowanceKind, OrderOutcome
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.http import FakeHttpClient, HttpResponse
from monik.infrastructure.providers import QuoteRequest
from monik.infrastructure.providers.velora_delta import VeloraDeltaAdapter, endpoints
from monik.services.observability import FakeClock
from monik.services.trading.orders import executes_with_orders
from tests import factories as f
from tests.unit.providers.support import provider_config, resource_manager

SWAPPER = "0x" + "c6" * 20
DELTA = "0x0000000000bbf5c5fd284e657f01bd000933c96d"

#: Ответ ``/delta/prices``. ``destAmount`` уже за вычетом исполнения:
#: разница с ``destAmountBeforeFee`` равна расходу агента на газ.
PRICE_PAYLOAD = {
    "price": {
        "srcToken": str(f.USDT.address),
        "destToken": str(f.AAVE.address),
        "srcAmount": "100000000",
        "destAmount": "5140000000000000000",
        "destAmountBeforeFee": "5141000000000000000",
        "gasCost": "704700",
        "partner": "anon",
        "hmac": "558224cb2a7e77bbdde3f7badfde0056ee19241b",
    },
    "deltaAddress": DELTA,
}


def _built(dest_amount: str = "5134860000000000000") -> dict:
    """Ответ ``/delta/orders/build``: готовые типизированные данные."""
    return {
        "toSign": {
            "domain": {
                "name": endpoints.DOMAIN_NAME,
                "version": endpoints.DOMAIN_VERSION,
                "chainId": 137,
                "verifyingContract": DELTA,
            },
            "types": {
                "Order": [
                    {"name": "owner", "type": "address"},
                    {"name": "srcAmount", "type": "uint256"},
                    {"name": "destAmount", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "bridge", "type": "Bridge"},
                ],
                "Bridge": [{"name": "destinationChainId", "type": "uint256"}],
            },
            "value": {
                "owner": SWAPPER,
                "srcAmount": "100000000",
                "destAmount": dest_amount,
                "expectedAmount": "5140000000000000000",
                "deadline": 1789826680,
                "bridge": {"destinationChainId": 0},
            },
        }
    }


def _adapter(
    responses: list[HttpResponse] | None = None, *, options: dict[str, str] | None = None
) -> VeloraDeltaAdapter:
    clock = FakeClock(f.NOW)
    return VeloraDeltaAdapter(
        provider_config(
            ProviderId.VELORA_DELTA,
            options=options if options is not None else {"swapper": SWAPPER},
        ),
        http=FakeHttpClient(
            responses
            if responses is not None
            else [HttpResponse(status_code=200, text=json.dumps(PRICE_PAYLOAD))]
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
    async def test_output_is_already_net_of_execution(self) -> None:
        quote = await _adapter().get_quote(_request())

        assert quote.output_amount.raw == 5_140_000_000_000_000_000
        assert quote.raw_output_amount_includes_fees is True

    async def test_gas_is_zero_and_not_unknown(self) -> None:
        """Исполняет агент, и его расход уже вычтен из суммы получения."""
        quote = await _adapter().get_quote(_request())

        assert quote.estimated_gas_units == 0

    async def test_the_network_is_named_by_chain_id(self) -> None:
        """Market берёт ``network``, Delta — ``chainId``: это разные API."""
        http = FakeHttpClient([HttpResponse(status_code=200, text=json.dumps(PRICE_PAYLOAD))])
        adapter = VeloraDeltaAdapter(
            provider_config(ProviderId.VELORA_DELTA, options={"swapper": SWAPPER}),
            http=http,
            resources=resource_manager(FakeClock(f.NOW)),
            clock=FakeClock(f.NOW),
        )
        await adapter.get_quote(_request())

        params = http.calls[0].request.params
        assert params["chainId"] == "137"
        assert "network" not in params

    async def test_swapper_is_required(self) -> None:
        with pytest.raises(UnsupportedError):
            await _adapter(options={}).get_quote(_request())

    async def test_unsupported_network_is_refused(self) -> None:
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
    @staticmethod
    def _pair(built: dict) -> VeloraDeltaAdapter:
        return _adapter(
            [
                HttpResponse(status_code=200, text=json.dumps(PRICE_PAYLOAD)),
                HttpResponse(status_code=200, text=json.dumps(built)),
            ]
        )

    async def test_the_tolerance_is_sent_in_basis_points(self) -> None:
        """Проверено живым API: ``slippage`` измеряется в базисных пунктах."""
        http = FakeHttpClient(
            [
                HttpResponse(status_code=200, text=json.dumps(PRICE_PAYLOAD)),
                HttpResponse(status_code=200, text=json.dumps(_built())),
            ]
        )
        adapter = VeloraDeltaAdapter(
            provider_config(ProviderId.VELORA_DELTA, options={"swapper": SWAPPER}),
            http=http,
            resources=resource_manager(FakeClock(f.NOW)),
            clock=FakeClock(f.NOW),
        )
        await adapter.build_order(_request(slippage_bps=10))

        assert http.calls[1].request.json_body["slippage"] == 10

    async def test_the_price_is_passed_untouched(self) -> None:
        """Котировка подписана ``hmac``: любая правка делает её негодной."""
        http = FakeHttpClient(
            [
                HttpResponse(status_code=200, text=json.dumps(PRICE_PAYLOAD)),
                HttpResponse(status_code=200, text=json.dumps(_built())),
            ]
        )
        adapter = VeloraDeltaAdapter(
            provider_config(ProviderId.VELORA_DELTA, options={"swapper": SWAPPER}),
            http=http,
            resources=resource_manager(FakeClock(f.NOW)),
            clock=FakeClock(f.NOW),
        )
        await adapter.build_order(_request())

        assert http.calls[1].request.json_body["price"] == PRICE_PAYLOAD["price"]

    async def test_a_weakened_guarantee_is_refused(self) -> None:
        """Предел, которого мы не просили, принимать нельзя.

        Сборка вправе подставить собственный допуск — живой API так и
        делает, когда допуск не назван. Исполнят нас именно по нему,
        поэтому молча согласиться значило бы отдать разницу.
        """
        weakened = _built(dest_amount=str(5_140_000_000_000_000_000 * 99 // 100))

        with pytest.raises(DataError) as refusal:
            await self._pair(weakened).build_order(_request(slippage_bps=1))

        assert refusal.value.info.code == "order_minimum_weakened"

    async def test_the_guarantee_becomes_the_commitment(self) -> None:
        plan = await self._pair(_built()).build_order(_request(slippage_bps=10))

        assert plan.min_output_raw == 5_140_000_000_000_000_000 * 9_990 // 10_000
        assert plan.min_output_raw <= plan.quote.output_amount.raw

    async def test_typed_data_is_normalised_for_the_wallet(self) -> None:
        """Провайдер называет заявку ``value``; наружу выходит ``message``."""
        plan = await self._pair(_built()).build_order(_request())

        assert plan.typed_data["primaryType"] == "Order"
        assert plan.typed_data["message"]["owner"] == SWAPPER
        assert "EIP712Domain" in plan.typed_data["types"]
        assert plan.typed_data["types"]["Bridge"] == [
            {"name": "destinationChainId", "type": "uint256"}
        ]

    async def test_domain_matches_the_protocol_contract(self) -> None:
        """Домен сверен со значением, которое контракт сообщает сам."""
        plan = await self._pair(_built()).build_order(_request())
        domain = plan.typed_data["domain"]

        assert domain["name"] == "Portikus"
        assert domain["version"] == "2.0.0"
        assert domain["verifyingContract"] == DELTA

    async def test_allowance_goes_to_the_address_from_the_quote(self) -> None:
        """Адрес приходит в ответе и в коде не зашит."""
        plan = await self._pair(_built()).build_order(_request())

        assert len(plan.allowances) == 1
        allowance = plan.allowances[0]
        assert allowance.kind is AllowanceKind.ERC20
        assert allowance.contract == str(f.USDT.address)
        assert allowance.spender == DELTA

    async def test_slippage_is_required(self) -> None:
        with pytest.raises(DataError):
            await self._pair(_built()).build_order(_request(slippage_bps=None))

    async def test_building_a_transaction_is_refused(self) -> None:
        with pytest.raises(UnsupportedError):
            await _adapter().build_swap(_request())

    def test_the_adapter_declares_that_it_executes_with_orders(self) -> None:
        assert _adapter().capabilities.supports_signed_orders is True
        assert executes_with_orders(_adapter()) is True


class TestOrderState:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("PENDING", OrderOutcome.OPEN),
            ("EXECUTING", OrderOutcome.OPEN),
            ("EXECUTED", OrderOutcome.FILLED),
            ("EXPIRED", OrderOutcome.EXPIRED),
            ("CANCELLED", OrderOutcome.CANCELLED),
        ],
    )
    async def test_status_is_translated(self, status: str, expected: OrderOutcome) -> None:
        adapter = _adapter([HttpResponse(status_code=200, text=json.dumps({"status": status}))])

        state = await adapter.order_state(f.POLYGON, "uuid", operation=OperationType.BUY)

        assert state.outcome is expected

    async def test_an_unknown_status_is_never_guessed(self) -> None:
        """Список состояний документацией не подтверждён.

        Принять незнакомое значение за исполнение значило бы объявить
        сделку закрытой, ничего о ней не зная. Ошибка данных называет
        само значение, и первый же настоящий ответ дополнит список.
        """
        adapter = _adapter(
            [HttpResponse(status_code=200, text=json.dumps({"status": "TELEPORTED"}))]
        )

        with pytest.raises(DataError) as refusal:
            await adapter.order_state(f.POLYGON, "uuid", operation=OperationType.BUY)

        assert "TELEPORTED" in str(refusal.value)


class TestRefusals:
    async def test_no_route_becomes_no_route(self) -> None:
        adapter = _adapter(
            [
                HttpResponse(
                    status_code=404,
                    text=json.dumps({"errorType": "NoRoutesFound", "details": "no route"}),
                )
            ]
        )

        with pytest.raises(NoRouteError):
            await adapter.get_quote(_request())

    async def test_a_rejected_order_is_not_a_missing_route(self) -> None:
        adapter = _adapter(
            [
                HttpResponse(
                    status_code=400,
                    text=json.dumps(
                        {"errorType": "OrderValidationError", "details": "amount too small"}
                    ),
                )
            ]
        )

        with pytest.raises(RouteRejectedError):
            await adapter.get_quote(_request())


class TestObservedStatus:
    async def test_the_running_state_is_not_final(self) -> None:
        """``RUNNING`` наблюдено вживую на сделке #T3 19.09.

        Так выглядит заявка, принятая книгой и ещё не взятая агентом:
        ``transactions`` пуст, средства на счету не тронуты. Считать её
        исполненной нельзя, неудачной — тоже.
        """
        adapter = _adapter(
            [
                HttpResponse(
                    status_code=200,
                    text=json.dumps({"status": "RUNNING", "transactions": []}),
                )
            ]
        )

        state = await adapter.order_state(f.POLYGON, "uuid", operation=OperationType.BUY)

        assert state.outcome is OrderOutcome.OPEN
        assert state.is_final is False
        assert state.executed_output_raw == 0


class TestOrderLifetime:
    """Срок заявки — он же срок резервирования средств.

    Пока заявка жива, её сумма считается занятой: иначе вторую заявку
    можно было бы подписать на те же деньги. Час по умолчанию запер
    100 USDT на сделке #T3 19.09 и остановил торговлю целиком.
    """

    @staticmethod
    def _adapter_with(validity: str | None) -> VeloraDeltaAdapter:
        options = {"swapper": SWAPPER}
        if validity is not None:
            options["order_validity_seconds"] = validity
        return _adapter(
            [
                HttpResponse(status_code=200, text=json.dumps(PRICE_PAYLOAD)),
                HttpResponse(status_code=200, text=json.dumps(_built())),
            ],
            options=options,
        )

    async def test_the_deadline_is_absolute_and_ours(self) -> None:
        """Относительные параметры провайдер принимает, но не соблюдает.

        Проверено живым API 19.09: ``expiry`` и ``validFor`` молча
        оставляют час, работает только ``deadline`` абсолютным временем.
        """
        http = FakeHttpClient(
            [
                HttpResponse(status_code=200, text=json.dumps(PRICE_PAYLOAD)),
                HttpResponse(status_code=200, text=json.dumps(_built())),
            ]
        )
        adapter = VeloraDeltaAdapter(
            provider_config(
                ProviderId.VELORA_DELTA,
                options={"swapper": SWAPPER, "order_validity_seconds": "10"},
            ),
            http=http,
            resources=resource_manager(FakeClock(f.NOW)),
            clock=FakeClock(f.NOW),
        )
        await adapter.build_order(_request())

        body = http.calls[1].request.json_body
        assert body["deadline"] == int(f.NOW.timestamp()) + 10
        assert "expiry" not in body
        assert "validFor" not in body

    async def test_a_broken_setting_is_refused(self) -> None:
        with pytest.raises(DataError):
            await self._adapter_with("через час").build_order(_request())

    async def test_a_non_positive_setting_is_refused(self) -> None:
        with pytest.raises(DataError):
            await self._adapter_with("0").build_order(_request())
