"""Тесты Gas System."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from monik.domain.enums.fees import FeeStatus
from monik.domain.models.gas import GasPrice
from monik.infrastructure.http import FakeHttpClient, HttpResponse
from monik.services.gas import GasEstimator, RpcGasPriceProvider, StaticGasPriceProvider
from monik.services.gas.providers import GasPriceProvider
from monik.services.observability import FakeClock
from tests import factories as f
from tests.unit.providers.support import resource_manager

NATIVE_TOKENS = {str(f.POLYGON): f.WMATIC.key}


def _estimator(clock: FakeClock, *providers: object) -> GasEstimator:
    return GasEstimator(
        clock,
        price_providers=providers
        or (  # type: ignore[arg-type]
            StaticGasPriceProvider(clock, prices={str(f.POLYGON): 100_000_000_000}),
        ),
        native_tokens=NATIVE_TOKENS,
    )


class TestGasEstimator:
    async def test_computes_cost_from_units_and_price(self) -> None:
        clock = FakeClock(f.NOW)
        gas = await _estimator(clock).estimate(f.POLYGON, gas_units=200_000)
        assert gas.is_known
        # 200000 * 100 gwei = 0.02 native token
        assert gas.known_cost_native == Decimal("0.02")
        assert gas.native_token == f.WMATIC.key

    async def test_unknown_units_produce_unknown_gas(self) -> None:
        """Отсутствие gas units не превращается в ноль (09 §16)."""
        clock = FakeClock(f.NOW)
        gas = await _estimator(clock).estimate(f.POLYGON, gas_units=None)
        assert gas.status is FeeStatus.UNKNOWN
        assert gas.cost_native is None
        with pytest.raises(ValueError, match="must not be treated as zero"):
            _ = gas.known_cost_native

    async def test_unavailable_price_produces_unknown_gas(self) -> None:
        clock = FakeClock(f.NOW)
        estimator = GasEstimator(
            clock,
            price_providers=(StaticGasPriceProvider(clock, prices={}),),
            native_tokens=NATIVE_TOKENS,
        )
        gas = await estimator.estimate(f.POLYGON, gas_units=200_000)
        assert gas.status is FeeStatus.UNKNOWN
        assert gas.gas_units == 200_000
        assert gas.cost_native is None

    async def test_unknown_native_token_produces_unknown_gas(self) -> None:
        clock = FakeClock(f.NOW)
        estimator = GasEstimator(
            clock,
            price_providers=(StaticGasPriceProvider(clock, prices={str(f.POLYGON): 1}),),
            native_tokens={},
        )
        gas = await estimator.estimate(f.POLYGON, gas_units=1)
        assert gas.status is FeeStatus.UNKNOWN

    async def test_first_available_provider_wins(self) -> None:
        clock = FakeClock(f.NOW)
        empty = StaticGasPriceProvider(clock, prices={})
        available = StaticGasPriceProvider(clock, prices={str(f.POLYGON): 50_000_000_000})
        gas = await _estimator(clock, empty, available).estimate(f.POLYGON, gas_units=100_000)
        assert gas.known_cost_native == Decimal("0.005")

    def test_requires_at_least_one_provider(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            GasEstimator(FakeClock(f.NOW), price_providers=(), native_tokens=NATIVE_TOKENS)

    async def test_exact_arithmetic_is_used(self) -> None:
        """Финансовые значения не проходят через float (09 §3)."""
        clock = FakeClock(f.NOW)
        estimator = _estimator(clock, StaticGasPriceProvider(clock, prices={str(f.POLYGON): 1}))
        gas = await estimator.estimate(f.POLYGON, gas_units=1)
        assert gas.known_cost_native == Decimal("1") / Decimal(10) ** 18


class TestRpcGasPriceProvider:
    def _provider(self, clock: FakeClock, responses: list[HttpResponse]) -> RpcGasPriceProvider:
        return RpcGasPriceProvider(
            http=FakeHttpClient(responses),
            resources=resource_manager(clock),
            clock=clock,
            rpc_urls={str(f.POLYGON): ("https://polygon-rpc.com",)},
            freshness_seconds=60,
            # Надбавка принадлежит сети: у одной за место в блоке идёт
            # торг, у другой его нет вовсе.
            priority_fees_wei={str(f.POLYGON): 30_000_000_000},
        )

    def _result(self, value: object) -> HttpResponse:
        return HttpResponse(status_code=200, text=json.dumps({"result": value}))

    async def test_uses_eip1559_when_base_fee_available(self) -> None:
        clock = FakeClock(f.NOW)
        provider = self._provider(
            clock,
            [
                self._result("0x3b9aca00"),
                self._result({"baseFeePerGas": ["0x2540be400", "0x3b9aca00"]}),
            ],
        )
        price = await provider.gas_price(f.POLYGON)
        assert price is not None
        assert price.base_fee_wei == 1_000_000_000
        assert price.priority_fee_wei == 30_000_000_000
        assert price.wei_per_gas == 31_000_000_000

    async def test_falls_back_to_legacy_gas_price(self) -> None:
        clock = FakeClock(f.NOW)
        provider = self._provider(clock, [self._result("0x3b9aca00"), self._result(None)])
        price = await provider.gas_price(f.POLYGON)
        assert price is not None
        assert price.wei_per_gas == 1_000_000_000
        assert price.base_fee_wei is None

    async def test_unknown_network_returns_none(self) -> None:
        clock = FakeClock(f.NOW)
        provider = self._provider(clock, [])
        assert await provider.gas_price(f.NetworkId("arbitrum")) is None

    async def test_price_carries_freshness_window(self) -> None:
        clock = FakeClock(f.NOW)
        provider = self._provider(clock, [self._result("0x3b9aca00"), self._result(None)])
        price = await provider.gas_price(f.POLYGON)
        assert price is not None
        assert price.is_fresh(f.NOW)
        assert not price.is_fresh(f.NOW + timedelta(minutes=2))


class TestQuotedGasPrice:
    """Цена газа из котировки избавляет от запроса к узлу сети.

    Все три включённых агрегатора присылают стоимость исполнения вместе
    с ценой маршрута, поэтому отдельное обращение к RPC на этапе поиска
    не нужно. Узел остаётся запасным источником: провайдер может цену не
    сообщить, а публичные узлы закрываются без предупреждения.
    """

    @staticmethod
    def _estimator(providers: tuple[GasPriceProvider, ...], *, prefer: bool) -> GasEstimator:
        return GasEstimator(
            FakeClock(f.NOW),
            price_providers=providers,
            native_tokens={str(f.POLYGON): f.WMATIC.key},
            prefer_quoted_price=prefer,
        )

    async def test_the_network_price_wins_over_the_quoted_one(self) -> None:
        """Цена сети точнее котировки, и потому идёт первой.

        Замер 19.09: за единицу газа мы заплатили 277,2 и 278,3 gwei —
        это квитанции двух наших разрешений. Узел в тот же момент
        называл 279, а котировка Uniswap — 445. Котировка рассчитана на
        то, чтобы транзакция прошла наверняка, а не на то, чтобы
        предсказать расход; для решения о сделке нужна вторая величина.
        """
        node = _CountingProvider(wei_per_gas=100)
        estimator = self._estimator((node,), prefer=True)

        gas = await estimator.estimate(f.POLYGON, gas_units=1000, quoted_price_wei=7)

        assert gas.status is FeeStatus.KNOWN
        assert gas.gas_price is not None
        assert gas.gas_price.wei_per_gas == 100
        assert node.calls == 1

    async def test_the_quoted_price_covers_a_silent_network(self) -> None:
        """Котировка остаётся запасным источником, а не исчезает.

        Публичные узлы закрываются без предупреждения, и цена из
        котировки — это знание, которое уже получено. Терять его нельзя.
        """
        silent = _CountingProvider(wei_per_gas=None)
        estimator = self._estimator((silent,), prefer=True)

        gas = await estimator.estimate(f.POLYGON, gas_units=1000, quoted_price_wei=7)

        assert gas.status is FeeStatus.KNOWN
        assert gas.gas_price is not None
        assert gas.gas_price.wei_per_gas == 7
        assert gas.gas_price.source == "quote"

    async def test_node_is_used_when_the_provider_reports_no_price(self) -> None:
        node = _CountingProvider(wei_per_gas=100)
        estimator = self._estimator((node,), prefer=True)

        gas = await estimator.estimate(f.POLYGON, gas_units=1000, quoted_price_wei=None)

        assert gas.gas_price is not None
        assert gas.gas_price.wei_per_gas == 100
        assert node.calls == 1

    async def test_disabled_source_keeps_the_previous_behaviour(self) -> None:
        node = _CountingProvider(wei_per_gas=100)
        estimator = self._estimator((node,), prefer=False)

        gas = await estimator.estimate(f.POLYGON, gas_units=1000, quoted_price_wei=7)

        assert gas.gas_price is not None
        assert gas.gas_price.wei_per_gas == 100
        assert node.calls == 1

    async def test_unknown_gas_units_stay_unknown(self) -> None:
        """Цена без расхода стоимости не даёт: ноль подставлять нельзя."""
        estimator = self._estimator((_CountingProvider(wei_per_gas=100),), prefer=True)

        gas = await estimator.estimate(f.POLYGON, gas_units=None, quoted_price_wei=7)

        assert gas.status is FeeStatus.UNKNOWN
        assert gas.cost_native is None


class _CountingProvider:
    """Источник цены, считающий обращения к себе."""

    requires_request = True

    def __init__(self, *, wei_per_gas: int | None) -> None:
        self._wei_per_gas = wei_per_gas
        self.calls = 0

    async def gas_price(self, network_id: object) -> GasPrice | None:
        self.calls += 1
        if self._wei_per_gas is None:
            return None
        return GasPrice(
            network_id=f.POLYGON,
            wei_per_gas=self._wei_per_gas,
            source="test-node",
            observed_at=f.NOW,
        )


class TestNetworkPriorityFee:
    """Надбавка к базовой цене газа принадлежит сети.

    В одной сети за место в блоке идёт торг, и без заметной надбавки
    транзакция может не попасть в блок вовсе. В другой торга нет: все
    транзакции просят ноль и платят ровно базовую цену, которая там на
    три порядка ниже. Одно общее значение означало бы либо застрявшие
    транзакции, либо многократную переплату.
    """

    def _provider(self, clock: FakeClock, fees: dict[str, int]) -> RpcGasPriceProvider:
        return RpcGasPriceProvider(
            http=FakeHttpClient(
                [
                    HttpResponse(status_code=200, text=json.dumps({"result": "0x3b9aca00"})),
                    HttpResponse(
                        status_code=200,
                        text=json.dumps(
                            {"result": {"baseFeePerGas": ["0x2540be400", "0x3b9aca00"]}}
                        ),
                    ),
                ]
            ),
            resources=resource_manager(clock),
            clock=clock,
            rpc_urls={str(f.POLYGON): ("https://polygon-rpc.com",)},
            freshness_seconds=60,
            priority_fees_wei=fees,
        )

    async def test_network_without_a_fee_pays_only_the_base(self) -> None:
        clock = FakeClock(f.NOW)
        price = await self._provider(clock, {}).gas_price(f.POLYGON)

        assert price is not None
        assert price.priority_fee_wei == 0
        assert price.wei_per_gas == price.base_fee_wei

    async def test_fee_of_one_network_does_not_reach_another(self) -> None:
        """Иначе сеть без торга платила бы надбавку соседней."""
        clock = FakeClock(f.NOW)
        provider = self._provider(clock, {"arbitrum": 30_000_000_000})

        price = await provider.gas_price(f.POLYGON)

        assert price is not None
        assert price.priority_fee_wei == 0
