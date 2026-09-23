"""Независимый рыночный курс токена.

Источник, не связанный с агрегаторами. Нужен затем, что котировка
агрегатора иногда описывает не рынок, а содержимое одного пула, и
проверить её больше нечем: обязательство считается по тому же маршруту,
что и обещание, и врёт вместе с ним.

Замеры 22–23.09 дали три таких случая подряд. CoW на Ethereum покупал
WETH по 2319 при рыночных 2750. Uniswap на той же сети прокладывал путь
USDT → «Dollars» → WETH → LINK и обещал 14 861 LINK за 100 USDT при
рыночных 8,15. Обёртка AVAX принимала деньги и возвращала 1,38 из 100.
Во всех трёх случаях расхождение с рынком видно сразу, а изнутри
агрегатора — никак (``the_main_rules.md``, правило 26).

Курс берётся в долларах у обеих сторон пары и делится: так сравнение не
зависит от того, насколько точно базовый токен держит паритет.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from monik.domain.enums.capability import CapabilityOperation
from monik.domain.enums.resources import RequestPriority
from monik.domain.errors import DataError, MonikError
from monik.domain.models.conversion import ConversionRate
from monik.domain.models.resource import ResourceKey, ResourceRequest
from monik.domain.models.token import Token
from monik.domain.value_objects.identifiers import RequestId
from monik.domain.value_objects.identity import NetworkId
from monik.domain.value_objects.timestamps import UtcDatetime
from monik.infrastructure.http import HttpClient, HttpRequest, classify_response
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger, log_fields
from monik.services.prices.providers import PRICE_RESOURCE_OWNER
from monik.services.resources import ResourceManager

__all__ = ["MARKET_PLATFORMS", "MarketPriceProvider"]

_LOGGER = get_logger("services.prices.market")

#: Как сеть называется у источника курсов. Его имена — его дело, и
#: перевод к нашим идентификаторам живёт здесь, у границы.
MARKET_PLATFORMS: dict[str, str] = {
    "ethereum": "ethereum",
    "polygon": "polygon-pos",
    "arbitrum": "arbitrum-one",
}


class MarketPriceProvider:
    """Курс пары по рыночным ценам обеих сторон.

    Значения кешируются: проверка вызывается редко — только у находки,
    дошедшей до открытия сделки, — но две подряд не должны стоить двух
    запросов. Лимит бесплатного API невелик, и тратить его на повтор
    одного и того же нельзя.
    """

    __slots__ = ("_cache", "_clock", "_endpoint", "_http", "_resources", "_timeout", "_ttl")

    def __init__(
        self,
        *,
        http: HttpClient,
        resources: ResourceManager,
        clock: Clock,
        endpoint: str,
        ttl_seconds: int = 300,
        timeout_seconds: float = 8.0,
    ) -> None:
        self._http = http
        self._resources = resources
        self._clock = clock
        self._endpoint = endpoint.rstrip("/")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._timeout = timedelta(seconds=timeout_seconds)
        #: Цена токена в долларах и момент её получения.
        self._cache: dict[str, tuple[Decimal, UtcDatetime]] = {}

    async def rate(self, from_token: Token, to_token: Token) -> ConversionRate | None:
        """Сколько ``to_token`` стоит один ``from_token`` по рынку.

        ``None`` означает «неизвестно» и не подменяется выдуманным
        значением (``CLAUDE.md`` §12): решение по такому ответу принимает
        вызывающая сторона.
        """
        platform = MARKET_PLATFORMS.get(str(from_token.network_id))
        if platform is None or from_token.network_id != to_token.network_id:
            return None
        prices = await self._usd_prices(
            from_token.network_id, platform, (str(from_token.address), str(to_token.address))
        )
        if prices is None:
            return None
        source = prices.get(str(from_token.address).lower())
        target = prices.get(str(to_token.address).lower())
        if source is None or target is None or target <= 0:
            return None
        now = self._clock.now()
        return ConversionRate(
            from_token=from_token.key,
            to_token=to_token.key,
            rate=source / target,
            source=PRICE_RESOURCE_OWNER,
            observed_at=now,
            expires_at=now + self._ttl,
        )

    async def _usd_prices(
        self, network_id: NetworkId, platform: str, addresses: tuple[str, ...]
    ) -> dict[str, Decimal] | None:
        """Долларовые цены адресов, по возможности из памяти."""
        now = self._clock.now()
        known: dict[str, Decimal] = {}
        missing: list[str] = []
        for address in addresses:
            cached = self._cache.get(address.lower())
            if cached is not None and cached[1] > now:
                known[address.lower()] = cached[0]
            else:
                missing.append(address.lower())
        if not missing:
            return known
        expires = now + self._ttl
        for address in missing:
            fetched = await self._fetch(network_id, platform, address)
            if fetched is None:
                return None
            self._cache[address] = (fetched, expires)
            known[address] = fetched
        return known

    async def _fetch(self, network_id: NetworkId, platform: str, address: str) -> Decimal | None:
        """Цена одного адреса.

        По одному, потому что бесплатный тариф источника больше одного
        адреса за запрос не принимает (проверено 23.09: несколько адресов
        дают отказ 400 с кодом 10012). Кеш делает это дешёвым: базовый
        токен пары спрашивается однажды на все проверки.
        """
        request_id = RequestId.generate()
        resource_request = ResourceRequest(
            request_id=request_id,
            key=ResourceKey(
                provider_id=PRICE_RESOURCE_OWNER,
                network_id=network_id,
                operation=CapabilityOperation.TOKEN_METADATA,
            ),
            priority=RequestPriority.MAINTENANCE,
            timeout=self._timeout,
            created_at=self._clock.now(),
            sequence=0,
            deduplication_key=f"market:{platform}:{address}",
        )

        async def call() -> Decimal | None:
            response = await self._http.send(
                HttpRequest(
                    method="GET",
                    url=f"{self._endpoint}/simple/token_price/{platform}",
                    params={"contract_addresses": address, "vs_currencies": "usd"},
                    request_id=request_id,
                    timeout_seconds=self._timeout.total_seconds(),
                )
            )
            classify_response(response, provider=PRICE_RESOURCE_OWNER)
            body = response.json()
            if not isinstance(body, dict):
                raise DataError(
                    "market price response is not a JSON object",
                    code="market_price_malformed",
                )
            quoted = body.get(address)
            if not isinstance(quoted, dict):
                return None
            raw = quoted.get("usd")
            if raw is None:
                return None
            if isinstance(raw, float):
                # Двоичное число теряет точность, а цена участвует в
                # сравнении с деньгами (``CLAUDE.md`` §11).
                raw = repr(raw)
            try:
                return Decimal(str(raw))
            except InvalidOperation:
                raise DataError(
                    f"market price for {address} is not a number",
                    code="market_price_invalid",
                ) from None

        try:
            return await self._resources.execute(resource_request, call)
        except MonikError as error:
            _LOGGER.info(
                "market price api unavailable",
                extra=log_fields(code=error.info.code, platform=platform),
            )
            return None
