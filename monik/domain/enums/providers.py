"""Идентификаторы поддерживаемых aggregator providers."""

from __future__ import annotations

from monik.domain.enums.base import DomainEnum


class ProviderId(DomainEnum):
    """Утверждённые production providers (``01_PROJECT_REQUIREMENTS.md`` §3).

    Добавление нового provider — отдельное архитектурное решение
    (``39_IMPLEMENTATION_PLAN.md`` §82), а не свободное расширение enum'а.
    """

    ONEINCH = "oneinch"
    ZERO_X = "zero_x"
    VELORA = "velora"
    UNISWAP = "uniswap"
    KYBERSWAP = "kyberswap"
    #: Исполняет подписанные офчейн заявки, а не транзакции
    #: (``the_main_rules.md``, правило 16).
    COW = "cow"


#: Провайдеры, чьи API отдают котировки публично. Ключ им не нужен, и
#: его отсутствие ошибкой конфигурации не является
#: (``the_main_rules.md``, правила 8 и 16).
#:
#: Признак объявлен здесь, а не в адаптере: проверка конфигурации
#: выполняется до того, как адаптеры созданы, и обращаться из неё к
#: инфраструктуре нельзя.
_PUBLIC_PROVIDERS = frozenset({ProviderId.VELORA, ProviderId.COW})


def requires_credentials(provider_id: ProviderId) -> bool:
    """Нужны ли провайдеру учётные данные.

    Раньше проверка требовала ссылку на секрет у каждого включённого
    провайдера, и публичному API приходилось задавать переменную с
    произвольным значением — то есть заводить секрет, которого нет.
    Правило 8 назвало это обходом и указало правильное решение: спрашивать
    у провайдера. Здесь оно и сделано.
    """
    return provider_id not in _PUBLIC_PROVIDERS
