"""Оценка стоимости газа операции.

Gas обязателен для расчёта прибыльности
(``01_PROJECT_REQUIREMENTS.md`` §26) и учитывается отдельно от комиссий
агрегатора и протокола.

Неизвестный gas никогда не считается нулём
(``09_PROFIT_CALCULATOR.md`` §16): при недостатке данных возвращается
:class:`Gas` со статусом ``UNKNOWN`` и без стоимости.
"""

from __future__ import annotations

from decimal import Decimal

from monik.domain.enums.fees import FeeStatus
from monik.domain.models.gas import Gas, GasPrice
from monik.domain.models.token import TokenKey
from monik.domain.value_objects.identity import NetworkId
from monik.domain.value_objects.timestamps import UtcDatetime
from monik.services.gas.providers import GasPriceProvider
from monik.services.observability.clock import Clock

__all__ = ["GasEstimator"]

#: Множитель перевода wei в native token.
_WEI_IN_NATIVE = Decimal(10) ** 18


class GasEstimator:
    """Вычисляет стоимость газа как ``gas_units × gas_price``."""

    def __init__(
        self,
        clock: Clock,
        *,
        price_providers: tuple[GasPriceProvider, ...],
        native_tokens: dict[str, TokenKey],
        prefer_quoted_price: bool = False,
    ) -> None:
        if not price_providers:
            raise ValueError("at least one gas price provider is required")
        self._clock = clock
        self._providers = price_providers
        self._native_tokens = dict(native_tokens)
        self._prefer_quoted_price = prefer_quoted_price

    async def estimate(
        self,
        network_id: NetworkId,
        *,
        gas_units: int | None,
        quoted_price_wei: int | None = None,
        allow_remote_lookup: bool = True,
        source: str = "gas_estimator",
    ) -> Gas:
        """Оценить стоимость исполнения.

        ``gas_units`` приходит из route estimate адаптера. Если он или цена
        газа неизвестны, результат имеет статус ``UNKNOWN`` — подставлять
        ноль запрещено.

        ``quoted_price_wei`` — цена газа, которую провайдер прислал вместе
        с котировкой. Когда источник ``quote`` включён в конфигурации, она
        используется первой: значение уже получено, и обращаться за ним к
        узлу сети отдельным запросом незачем.

        ``allow_remote_lookup`` разрешает обратиться к источникам,
        которые делают внешний запрос. Level 1 вызывает метод с
        ``False``: этап поиска не тратит на цену газа ни одного запроса.
        Источники, отвечающие из конфигурации, при этом доступны — запрет
        касается обращений наружу, а не знания как такового. Level 2
        вызывает со значением по умолчанию: там стоимость сети
        обязательна, и узел спрашивается.
        """
        now = self._clock.now()
        native_token = self._native_tokens.get(str(network_id))
        if gas_units is None or native_token is None:
            return Gas(
                network_id=network_id,
                status=FeeStatus.UNKNOWN,
                observed_at=now,
                source=source,
            )

        # Цена сети идёт первой, котировка — запасным источником.
        # Замер 19.09: за единицу газа мы заплатили 277,2 и 278,3 gwei
        # (квитанции двух разрешений), узел в тот же момент называл
        # 279 gwei, а котировка Uniswap — 445. Котировка завышает цену
        # примерно в полтора раза: она рассчитана на то, чтобы
        # транзакция прошла наверняка, а не на то, чтобы предсказать
        # расход. Для решения о сделке нужна вторая величина.
        price = await self._first_available_price(network_id, allow_remote=allow_remote_lookup)
        if price is None:
            price = self._quoted_price(network_id, quoted_price_wei, now)
        if price is None:
            return Gas(
                network_id=network_id,
                status=FeeStatus.UNKNOWN,
                gas_units=gas_units,
                observed_at=now,
                source=source,
            )

        cost_native = (Decimal(gas_units) * Decimal(price.wei_per_gas)) / _WEI_IN_NATIVE
        return Gas(
            network_id=network_id,
            status=FeeStatus.KNOWN,
            gas_units=gas_units,
            gas_price=price,
            native_token=native_token,
            cost_native=cost_native,
            observed_at=now,
            source=source,
        )

    def _quoted_price(
        self,
        network_id: NetworkId,
        quoted_price_wei: int | None,
        now: UtcDatetime,
    ) -> GasPrice | None:
        """Цена газа из котировки, если она есть и источник разрешён."""
        if not self._prefer_quoted_price or quoted_price_wei is None:
            return None
        return GasPrice(
            network_id=network_id,
            wei_per_gas=quoted_price_wei,
            source="quote",
            observed_at=now,
        )

    async def _first_available_price(
        self, network_id: NetworkId, *, allow_remote: bool = True
    ) -> GasPrice | None:
        """Первая доступная свежая цена среди настроенных источников.

        Когда обращаться наружу нельзя, у источника всё равно
        спрашивается уже известное: запрет касается запроса, а не
        знания. Источник, который умеет ответить из памяти, отвечает.
        """
        now = self._clock.now()
        for provider in self._providers:
            if provider.requires_request and not allow_remote:
                cached = self._cached_price(provider, network_id)
                if cached is not None and cached.is_fresh(now):
                    return cached
                continue
            price = await provider.gas_price(network_id)
            if price is not None and price.is_fresh(now):
                return price
        return None

    @staticmethod
    def _cached_price(provider: GasPriceProvider, network_id: NetworkId) -> GasPrice | None:
        """Уже известная источнику цена, если он умеет её помнить."""
        remember = getattr(provider, "cached_price", None)
        if remember is None:
            return None
        cached = remember(network_id)
        return cached if isinstance(cached, GasPrice) else None
