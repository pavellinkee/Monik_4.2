"""Сверка цены агрегатора с независимым источником.

Решение о трате денег принимается по обязательству агрегатора. Этого
достаточно, пока обязательство описывает рынок, — но оно считается по
тому же маршруту, что и обещание, и врёт вместе с ним. Изнутри
агрегатора такую ложь не видно ничем.

Замеры 22–23.09 дали три случая подряд:

* CoW на Ethereum покупал WETH по 2319 при рыночных 2750;
* Uniswap там же вёл путь USDT → «Dollars» → WETH → LINK и обещал
  14 861 LINK за 100 USDT при рыночных 8,15 — предельная сумма такой
  котировки выдумана ровно так же, как и обещание;
* обёртка AVAX принимала 100 USDT и возвращала 1,38.

Поэтому перед тратой денег цена сверяется с рынком. Проверка стоит
запроса, поэтому делается **не на каждом проходе**, а только у находки,
дошедшей до открытия сделки: таких за сутки единицы, а источник ещё и
кеширует (``the_main_rules.md``, правило 26).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from monik.domain.models.conversion import ConversionRate
from monik.domain.models.token import Token
from monik.services.observability.logging import get_logger, log_fields

__all__ = ["MarketRateSource", "PriceGuard"]

_LOGGER = get_logger("services.trading.price_guard")


@runtime_checkable
class MarketRateSource(Protocol):
    """Независимый источник рыночного курса пары."""

    async def rate(self, from_token: Token, to_token: Token) -> ConversionRate | None:
        """Курс или ``None``, если он неизвестен."""
        ...


class PriceGuard:
    """Пропускает сделку, только если её цена похожа на рыночную."""

    __slots__ = ("_enabled", "_source", "_tolerance")

    def __init__(
        self,
        *,
        source: MarketRateSource | None,
        max_deviation_percent: Decimal,
        enabled: bool = True,
    ) -> None:
        self._source = source
        self._tolerance = max_deviation_percent
        self._enabled = enabled

    @property
    def active(self) -> bool:
        """Работает ли сверка. Без источника её нет, как её ни настраивай."""
        return self._enabled and self._source is not None

    async def allows(
        self,
        *,
        paying: Token,
        receiving: Token,
        raw_paid: int,
        raw_received: int,
    ) -> bool:
        """Похоже ли предложение на рынок.

        Отклонение считается в обе стороны. Слишком щедрое предложение
        подозрительнее скупого: рынок не раздаёт, и цифра выше рыночной
        означает не удачу, а пул, цену в котором назначили.

        Неизвестный рыночный курс проверку **не** проходит: подтверждать
        трату по непроверенным данным нельзя (``CLAUDE.md`` §12). Цена
        этого — остановка сделок, пока источник недоступен; она принята
        сознательно, потому что все три пойманных случая выглядели
        изнутри агрегатора безупречно.
        """
        if not self.active:
            return True
        if raw_paid <= 0 or raw_received <= 0:
            return False
        assert self._source is not None  # noqa: S101 - гарантировано self.active
        market = await self._source.rate(paying, receiving)
        if market is None or market.rate <= 0:
            _LOGGER.warning(
                "price check skipped the trade: the market rate is unknown",
                extra=log_fields(paying=str(paying.symbol), receiving=str(receiving.symbol)),
            )
            return False
        offered = (Decimal(raw_received).scaleb(-receiving.decimals)) / (
            Decimal(raw_paid).scaleb(-paying.decimals)
        )
        deviation = (offered / market.rate - 1) * 100
        if abs(deviation) <= self._tolerance:
            return True
        _LOGGER.warning(
            "price check refused the trade: the offer does not match the market",
            extra=log_fields(
                paying=str(paying.symbol),
                receiving=str(receiving.symbol),
                offered=str(offered),
                market=str(market.rate),
                deviation_percent=str(deviation),
                allowed_percent=str(self._tolerance),
            ),
        )
        return False
