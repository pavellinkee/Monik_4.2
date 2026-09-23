"""Сверка цены агрегатора с независимым источником.

Решение о трате денег принимается по обязательству агрегатора, но оно
считается по тому же маршруту, что и обещание, и врёт вместе с ним.
Замеры 22–23.09 дали три таких случая подряд, и все три выглядели
изнутри агрегатора безупречно (``the_main_rules.md``, правило 26).
"""

from __future__ import annotations

from decimal import Decimal

from monik.domain.models.conversion import ConversionRate
from monik.domain.models.token import Token
from monik.services.trading.price_guard import PriceGuard
from tests import factories as f


class _Market:
    """Источник курса, заданный заранее.

    **Test implementation** (``CLAUDE.md`` §10): настоящий ходит в сеть,
    а здесь проверяется только решение по полученному курсу.
    """

    def __init__(self, rate: Decimal | None) -> None:
        self._rate = rate
        self.calls = 0

    async def rate(self, from_token: Token, to_token: Token) -> ConversionRate | None:
        self.calls += 1
        if self._rate is None:
            return None
        return ConversionRate(
            from_token=from_token.key,
            to_token=to_token.key,
            rate=self._rate,
            source="test",
            observed_at=f.NOW,
        )


def _guard(rate: Decimal | None, *, tolerance: str = "2", enabled: bool = True) -> PriceGuard:
    return PriceGuard(
        source=_Market(rate),
        max_deviation_percent=Decimal(tolerance),
        enabled=enabled,
    )


#: 395 USDT по 12,25 за LINK — примерно 32,24 LINK.
PAID = 395_000_000
HONEST = 32_244_897_959_183_673_469
MARKET = Decimal("0.081632653")  # LINK за один USDT


class TestHonestOffer:
    async def test_a_market_priced_offer_passes(self) -> None:
        guard = _guard(MARKET)

        assert await guard.allows(
            paying=f.USDT, receiving=f.AAVE, raw_paid=PAID, raw_received=HONEST
        )

    async def test_a_small_deviation_passes(self) -> None:
        """Допуск затем и нужен: точного совпадения не бывает."""
        guard = _guard(MARKET)

        assert await guard.allows(
            paying=f.USDT,
            receiving=f.AAVE,
            raw_paid=PAID,
            raw_received=int(HONEST * Decimal("1.015")),
        )


class TestPhantomOffer:
    async def test_a_generous_offer_is_refused(self) -> None:
        """Рынок не раздаёт.

        23.09 Uniswap обещал 14 861 LINK за 100 USDT при рыночных 8,15 —
        маршрут шёл через пул мусорного токена. Предельная сумма такой
        котировки выдумана ровно так же, как и обещание, поэтому
        проверка обязательства от неё не спасает.
        """
        guard = _guard(MARKET)

        assert not await guard.allows(
            paying=f.USDT, receiving=f.AAVE, raw_paid=PAID, raw_received=HONEST * 1820
        )

    async def test_a_stingy_offer_is_refused_too(self) -> None:
        """Отклонение считается в обе стороны."""
        guard = _guard(MARKET)

        assert not await guard.allows(
            paying=f.USDT,
            receiving=f.AAVE,
            raw_paid=PAID,
            raw_received=int(HONEST * Decimal("0.8")),
        )

    async def test_the_tolerance_is_configurable(self) -> None:
        wide = _guard(MARKET, tolerance="25")

        assert await wide.allows(
            paying=f.USDT,
            receiving=f.AAVE,
            raw_paid=PAID,
            raw_received=int(HONEST * Decimal("1.2")),
        )


class TestUnknownMarket:
    async def test_an_unknown_rate_refuses_the_trade(self) -> None:
        """Неизвестное нулём и «сойдёт» не считается (``CLAUDE.md`` §12).

        Цена этого — остановка сделок, пока источник недоступен. Она
        принята сознательно: все три пойманных случая выглядели изнутри
        агрегатора безупречно, и проверить их было больше нечем.
        """
        guard = _guard(None)

        assert not await guard.allows(
            paying=f.USDT, receiving=f.AAVE, raw_paid=PAID, raw_received=HONEST
        )

    async def test_an_empty_offer_is_refused(self) -> None:
        guard = _guard(MARKET)

        assert not await guard.allows(
            paying=f.USDT, receiving=f.AAVE, raw_paid=0, raw_received=HONEST
        )


class TestSwitch:
    async def test_a_disabled_guard_does_not_ask(self) -> None:
        """Выключенная сверка не тратит запрос."""
        market = _Market(MARKET)
        guard = PriceGuard(source=market, max_deviation_percent=Decimal(2), enabled=False)

        assert await guard.allows(
            paying=f.USDT, receiving=f.AAVE, raw_paid=PAID, raw_received=HONEST * 1820
        )
        assert market.calls == 0

    async def test_without_a_source_the_guard_is_not_active(self) -> None:
        """Без источника сверки нет, как её ни настраивай.

        Притворяться, что проверка работает, нельзя: оператор решал бы,
        опираясь на защиту, которой нет.
        """
        guard = PriceGuard(source=None, max_deviation_percent=Decimal(2))

        assert guard.active is False
        assert await guard.allows(
            paying=f.USDT, receiving=f.AAVE, raw_paid=PAID, raw_received=HONEST
        )
