"""Endpoints и константы CoW Swap Order Book API.

Контракт сверен с живым API и с самими контрактами 18.09.2026:

* домен EIP-712 вычислен локально и сличён со значением, которое
  ``GPv2Settlement.domainSeparator()`` сообщает в Polygon, — совпадение
  точное;
* строка типа ``Order`` взята из ``GPv2Order.sol`` (``TYPE_HASH``);
* адреса контрактов проверены наличием кода в сети.

CoW Swap не отдаёт транзакцию: он принимает подписанную офчейн заявку
(``the_main_rules.md``, правило 16). Поэтому здесь нет пути сборки
calldata — вместо него путь книги заявок и путь состояния заявки.
"""

from __future__ import annotations

from monik.domain.value_objects.identity import NetworkId

__all__ = [
    "APP_DATA_HASH",
    "BUY_TOKEN_BALANCE",
    "DEFAULT_BASE_URL",
    "DOMAIN_NAME",
    "DOMAIN_VERSION",
    "KIND_SELL",
    "ORDERS_PATH",
    "ORDER_TYPE",
    "QUOTE_PATH",
    "SELL_TOKEN_BALANCE",
    "SETTLEMENT_CONTRACT",
    "SIGNING_SCHEME",
    "SUPPORTED_CHAIN_IDS",
    "SUPPORTED_NETWORK_SLUGS",
    "chain_id_for",
    "VAULT_RELAYER_CONTRACT",
    "network_slug_for",
    "order_path",
    "quote_path",
]

#: Базовый URL книги заявок. Сеть входит в путь, а не в параметры.
DEFAULT_BASE_URL = "https://api.cow.fi"

#: Сети, поддержка которых заявлена адаптером, и их обозначения в пути.
#: Сейчас объявлен только Polygon: остальные сети CoW Swap существуют, но
#: непроверенную поддержку адаптер не заявляет.
SUPPORTED_NETWORK_SLUGS: dict[str, str] = {
    "polygon": "polygon",
    # У CoW основная сеть Ethereum называется в адресе ``mainnet``, а не
    # ``ethereum``: это и есть переходник между нашим идентификатором и
    # их. Проверено живым запросом 2026-09-22.
    "ethereum": "mainnet",
}

#: Идентификаторы цепей заявленных сетей. Входят в домен подписи, и
#: ошибка здесь сделала бы подпись недействительной.
SUPPORTED_CHAIN_IDS: dict[str, int] = {
    "polygon": 137,
    "ethereum": 1,
}

#: Контракт расчётов. Он же ``verifyingContract`` домена EIP-712 и
#: получатель всех расчётов протокола. Адрес одинаков во всех сетях.
SETTLEMENT_CONTRACT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"

#: Контракт, списывающий продаваемый токен. Разрешение выдаётся ему, а
#: не контракту расчётов и не роутеру: это и есть переходник к общей
#: модели разрешений Monik.
VAULT_RELAYER_CONTRACT = "0xC92E8bdf79f0507f65a392b0ab4667716BFE0110"

#: Имя и версия домена EIP-712. Проверены сличением с
#: ``GPv2Settlement.domainSeparator()``.
DOMAIN_NAME = "Gnosis Protocol"
DOMAIN_VERSION = "v2"

#: Тип подписываемой заявки. Порядок полей значим: он входит в хеш типа.
ORDER_TYPE: tuple[tuple[str, str], ...] = (
    ("sellToken", "address"),
    ("buyToken", "address"),
    ("receiver", "address"),
    ("sellAmount", "uint256"),
    ("buyAmount", "uint256"),
    ("validTo", "uint32"),
    ("appData", "bytes32"),
    ("feeAmount", "uint256"),
    ("kind", "string"),
    ("partiallyFillable", "bool"),
    ("sellTokenBalance", "string"),
    ("buyTokenBalance", "string"),
)

#: Направление заявки. Monik всегда продаёт известное количество и
#: покупает столько, сколько дадут, — это соответствует ``EXACT_INPUT``
#: остальных агрегаторов.
KIND_SELL = "sell"

#: Источник продаваемого токена и назначение купленного. Оба — обычный
#: баланс ERC-20; иные значения относятся к Balancer Vault и не нужны.
SELL_TOKEN_BALANCE = "erc20"
BUY_TOKEN_BALANCE = "erc20"

#: Схема подписи: типизированные данные EIP-712.
SIGNING_SCHEME = "eip712"

#: Метаданные заявки. Monik ничего к заявке не прикладывает, и пустое
#: значение — нулевой хеш — единственное честное: выдумывать содержимое
#: ради заполненного поля не нужно.
APP_DATA_HASH = "0x" + "00" * 32

#: Путь котировки. Сеть подставляется в начало пути.
QUOTE_PATH = "/api/v1/quote"

#: Путь книги заявок.
ORDERS_PATH = "/api/v1/orders"


def network_slug_for(network_id: NetworkId) -> str | None:
    """Обозначение сети в пути или ``None``, если сеть не заявлена."""
    return SUPPORTED_NETWORK_SLUGS.get(str(network_id))


def quote_path(slug: str) -> str:
    """Путь запроса котировки в конкретной сети."""
    return f"/{slug}{QUOTE_PATH}"


def order_path(slug: str, uid: str | None = None) -> str:
    """Путь книги заявок либо одной заявки в ней."""
    if uid is None:
        return f"/{slug}{ORDERS_PATH}"
    return f"/{slug}{ORDERS_PATH}/{uid}"


def chain_id_for(network_id: NetworkId) -> int | None:
    """Идентификатор цепи или ``None``, если сеть не заявлена."""
    return SUPPORTED_CHAIN_IDS.get(str(network_id))
