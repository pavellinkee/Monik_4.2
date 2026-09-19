"""Endpoints и параметры Velora Delta.

Контракт сверен с живым API и с контрактом протокола 19.09.2026:

* котировка, сборка заявки и её отправка выполнены живыми запросами;
* домен EIP-712 вычислен локально и сличён со значением, которое
  контракт Delta возвращает из ``DOMAIN_SEPARATOR()`` — совпадение
  точное при имени ``Portikus`` и версии ``2.0.0``;
* допуск проскальзывания задаётся параметром ``slippage`` **в базисных
  пунктах**: проверено сравнением предела заявки с обещанием котировки.

Delta — не тот же продукт, что Velora Market. Market маршрутизирует обмен
по пулам и отдаёт транзакцию; Delta принимает подписанное намерение,
которое исполняет агент. Поэтому это отдельный провайдер, а не режим
существующего (``the_main_rules.md``, правило 18).
"""

from __future__ import annotations

from monik.domain.value_objects.identity import NetworkId

__all__ = [
    "BUILD_PATH",
    "DEFAULT_BASE_URL",
    "DOMAIN_NAME",
    "DOMAIN_VERSION",
    "ORDERS_PATH",
    "PRICES_PATH",
    "SIDE_SELL",
    "SUPPORTED_CHAIN_IDS",
    "chain_id_for",
    "order_path",
]

#: Базовый URL. Тот же домен, что у Velora Market: продукты разные,
#: хозяин один.
DEFAULT_BASE_URL = "https://api.paraswap.io"

#: Сети, поддержка которых заявлена адаптером. Приём идентификатора
#: сети самим API подтверждением поддержки не считается.
SUPPORTED_CHAIN_IDS: dict[str, int] = {
    "polygon": 137,
}

#: Путь котировки. Сеть передаётся параметром ``chainId`` — не
#: ``network``, как у Market: та же компания, другой продукт, другое имя
#: параметра. Это и есть та особенность, ради которой нужен переходник.
PRICES_PATH = "/delta/prices"

#: Путь сборки заявки: превращает котировку в то, что нужно подписать.
BUILD_PATH = "/delta/orders/build"

#: Путь книги заявок.
ORDERS_PATH = "/delta/orders"

#: Направление: продаём известное количество, покупаем сколько дадут.
SIDE_SELL = "SELL"

#: Имя и версия домена EIP-712. Проверены сличением с
#: ``DOMAIN_SEPARATOR()`` самого контракта.
DOMAIN_NAME = "Portikus"
DOMAIN_VERSION = "2.0.0"


def chain_id_for(network_id: NetworkId) -> int | None:
    """Идентификатор цепи или ``None``, если сеть не заявлена."""
    return SUPPORTED_CHAIN_IDS.get(str(network_id))


def order_path(order_id: str) -> str:
    """Путь одной заявки. Идентификатор — UUID, выданный при отправке."""
    return f"{ORDERS_PATH}/{order_id}"
