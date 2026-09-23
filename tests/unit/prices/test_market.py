"""Независимый рыночный курс.

Курс берётся в долларах у обеих сторон пары и делится: так сравнение не
зависит от того, насколько точно базовый токен держит паритет
(``the_main_rules.md``, правило 26).
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.http import FakeHttpClient, HttpResponse
from monik.services.observability import FakeClock
from monik.services.prices.market import MarketPriceProvider
from tests import factories as f

from ..providers.support import resource_manager

ENDPOINT = "https://prices.example/api/v3"

#: Цены, которые отдаёт стенд: USDT около паритета, AAVE дороже.
PRICES = {
    str(f.USDT.address).lower(): "0.9998",
    str(f.AAVE.address).lower(): "138.5",
}


class _Recorder:
    """Источник цен, запоминающий запросы."""

    def __init__(self, prices: dict[str, str] | None = None, status: int = 200) -> None:
        self._prices = PRICES if prices is None else prices
        self._status = status
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: Any) -> HttpResponse:
        self.requests.append(dict(request.params or {}))
        address = str((request.params or {}).get("contract_addresses", "")).lower()
        value = self._prices.get(address)
        body = {} if value is None else {address: {"usd": value}}
        return HttpResponse(status_code=self._status, text=json.dumps(body))


def _provider(recorder: _Recorder, clock: FakeClock, ttl: int = 300) -> MarketPriceProvider:
    return MarketPriceProvider(
        http=FakeHttpClient(handler=recorder),
        resources=resource_manager(clock),
        clock=clock,
        endpoint=ENDPOINT,
        ttl_seconds=ttl,
    )


class TestRate:
    async def test_the_rate_is_the_ratio_of_two_market_prices(self) -> None:
        clock = FakeClock(f.NOW)
        recorder = _Recorder()

        rate = await _provider(recorder, clock).rate(f.USDT, f.AAVE)

        assert rate is not None
        assert rate.rate == Decimal("0.9998") / Decimal("138.5")

    async def test_each_address_is_asked_separately(self) -> None:
        """Бесплатный тариф источника больше одного адреса не принимает.

        Проверено 23.09: несколько адресов в запросе дают отказ 400 с
        кодом 10012.
        """
        clock = FakeClock(f.NOW)
        recorder = _Recorder()

        await _provider(recorder, clock).rate(f.USDT, f.AAVE)

        asked = [r["contract_addresses"] for r in recorder.requests]
        assert len(asked) == 2
        assert all("," not in one for one in asked)

    async def test_an_unknown_price_is_not_invented(self) -> None:
        """``None`` означает «неизвестно» (``CLAUDE.md`` §12)."""
        clock = FakeClock(f.NOW)
        recorder = _Recorder(prices={str(f.USDT.address).lower(): "1.0"})

        assert await _provider(recorder, clock).rate(f.USDT, f.AAVE) is None

    async def test_an_unsupported_network_gives_nothing(self) -> None:
        """У сети без имени в источнике курса нет и быть не может."""
        clock = FakeClock(f.NOW)
        recorder = _Recorder()
        stranger = f.USDT.model_copy(update={"network_id": NetworkId("optimism")})

        rate = await _provider(recorder, clock).rate(stranger, f.AAVE)

        assert rate is None
        assert recorder.requests == [], "чужую сеть даже не спрашивали"


class TestCache:
    async def test_a_repeated_pair_costs_no_requests(self) -> None:
        """Проверка вызывается редко, но две подряд не стоят двух запросов."""
        clock = FakeClock(f.NOW)
        recorder = _Recorder()
        provider = _provider(recorder, clock)

        await provider.rate(f.USDT, f.AAVE)
        await provider.rate(f.USDT, f.AAVE)

        assert len(recorder.requests) == 2, "второй раз источник не спрашивали"

    async def test_a_stale_price_is_asked_again(self) -> None:
        """Устаревший курс не используется бесконечно."""
        clock = FakeClock(f.NOW)
        recorder = _Recorder()
        provider = _provider(recorder, clock, ttl=60)

        await provider.rate(f.USDT, f.AAVE)
        clock.advance(timedelta(seconds=61))
        await provider.rate(f.USDT, f.AAVE)

        assert len(recorder.requests) == 4


class TestFailure:
    async def test_an_unavailable_source_gives_nothing(self) -> None:
        """Отказ источника не превращается в выдуманный курс."""
        clock = FakeClock(f.NOW)
        recorder = _Recorder(status=500)

        assert await _provider(recorder, clock).rate(f.USDT, f.AAVE) is None
