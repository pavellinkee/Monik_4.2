"""Тесты стабильности доменных enum'ов.

Значения enum'ов попадают в persistent state, поэтому изменение значения —
breaking change, требующий migration (36 §76-78).
"""

from __future__ import annotations

import pytest

from monik.domain import enums
from monik.domain.enums import (
    AmountConfirmationStatus,
    CapabilityStatus,
    DomainEnum,
    JobStatus,
    NotificationMode,
    OperationType,
    OpportunityStatus,
    ProviderId,
    RequestPriority,
    ScanMode,
)
from monik.domain.enums.resources import confirmation_priority, search_priority

#: Значения, зафиксированные архитектурой. Изменять только вместе с migration.
FROZEN_VALUES: dict[str, set[str]] = {
    # KyberSwap и CoW Swap добавлены решениями оператора
    # (``the_main_rules.md``, правила 9 и 16): архитектурный §3
    # перечисляет четырёх провайдеров, набор расширен сознательно и
    # каждое расширение записано как источник истины.
    "ProviderId": {"oneinch", "zero_x", "velora", "uniswap", "kyberswap", "cow"},
    "OperationType": {"buy", "sell"},
    "JobStatus": {
        "queued",
        "running",
        "confirmed",
        "rejected",
        "failed",
        "expired",
        "cancelled",
    },
    "AmountConfirmationStatus": {"confirmed", "unconfirmed", "partial"},
    "NotificationStatus": {
        "queued",
        "sending",
        "retry_wait",
        "sent",
        "failed",
        "cancelled",
    },
    "NotificationMode": {"A", "B"},
    "CircuitState": {"closed", "open", "half_open"},
}


def _enum_types() -> list[type[DomainEnum]]:
    found = []
    for name in enums.__all__:
        attribute = getattr(enums, name)
        if isinstance(attribute, type) and issubclass(attribute, DomainEnum):
            if attribute is not DomainEnum:
                found.append(attribute)
    return found


@pytest.mark.parametrize("enum_name", sorted(FROZEN_VALUES))
def test_frozen_values_are_unchanged(enum_name: str) -> None:
    enum_type = getattr(enums, enum_name)
    assert {member.value for member in enum_type} == FROZEN_VALUES[enum_name]


@pytest.mark.parametrize("enum_type", _enum_types(), ids=lambda t: t.__name__)
def test_values_are_unique_and_non_empty(enum_type: type[DomainEnum]) -> None:
    values = [member.value for member in enum_type]
    assert len(values) == len(set(values))
    assert all(value for value in values)


@pytest.mark.parametrize("enum_type", _enum_types(), ids=lambda t: t.__name__)
def test_is_serializable_as_plain_string(enum_type: type[DomainEnum]) -> None:
    member = next(iter(enum_type))
    assert isinstance(member.value, str)
    assert str(member) == member.value
    assert enum_type(member.value) is member


def test_provider_set_matches_approved_providers() -> None:
    """Набор провайдеров утверждён: ``01 §3`` плюс правила 9 и 16.

    Архитектурный документ перечисляет четырёх провайдеров. KyberSwap и
    CoW Swap добавлены отдельными решениями оператора, записанными в
    ``the_main_rules.md``; набор по-прежнему закрыт и не расширяется
    свободно.
    """
    assert {p.value for p in ProviderId} == {
        "oneinch",
        "zero_x",
        "velora",
        "uniswap",
        "kyberswap",
        "cow",
    }


def test_each_search_mode_keeps_the_order_it_had() -> None:
    """Внутри ``ur`` и ``fest`` порядок прежний (``CLAUDE.md`` §15).

    Появление торгового режима и порядка между режимами не должно было
    его тронуть: правило приоритета принадлежит режиму
    (``the_main_rules.md``, правило 13).
    """
    for level2, sell, buy in (
        (
            RequestPriority.FEST_LEVEL2,
            RequestPriority.FEST_LEVEL1_SELL,
            RequestPriority.FEST_LEVEL1_BUY,
        ),
        (
            RequestPriority.UR_LEVEL2,
            RequestPriority.UR_LEVEL1_SELL,
            RequestPriority.UR_LEVEL1_BUY,
        ),
    ):
        assert level2.rank < sell.rank < buy.rank


def test_trading_mode_orders_sell_before_buy_before_scanning() -> None:
    """У ``ann`` нет Level 1 и Level 2 — есть продажа, покупка и поиск.

    Порядок обратен их последовательности во времени: за продажей стоят
    уже потраченные деньги, покупка их только тратит, а сканирование,
    уступив очередь, теряет один цикл.
    """
    ann = [
        RequestPriority.ANN_SELL,
        RequestPriority.ANN_BUY,
        RequestPriority.ANN_SCAN,
    ]

    assert sorted(ann, key=lambda p: p.rank) == ann


def test_modes_are_ordered_ann_then_fest_then_ur() -> None:
    """Правило между режимами сильнее правила внутри режима.

    Вся работа ``ann`` обслуживается раньше любой работы ``fest``, вся
    работа ``fest`` — раньше любой работы ``ur``. Даже подтверждение
    Level 2 более низкого режима уступает сканированию более высокого.
    """
    ann = (RequestPriority.ANN_SELL, RequestPriority.ANN_BUY, RequestPriority.ANN_SCAN)
    fest = (
        RequestPriority.FEST_LEVEL2,
        RequestPriority.FEST_LEVEL1_SELL,
        RequestPriority.FEST_LEVEL1_BUY,
    )
    ur = (
        RequestPriority.UR_LEVEL2,
        RequestPriority.UR_LEVEL1_SELL,
        RequestPriority.UR_LEVEL1_BUY,
    )

    assert max(p.rank for p in ann) < min(p.rank for p in fest)
    assert max(p.rank for p in fest) < min(p.rank for p in ur)


def test_work_outside_the_modes_comes_last() -> None:
    """Обслуживание и доставка уведомлений уступают любому режиму."""
    modes = [p for p in RequestPriority if p.value.startswith(("ann_", "fest_", "ur_"))]

    assert max(p.rank for p in modes) < RequestPriority.MAINTENANCE.rank
    assert RequestPriority.MAINTENANCE.rank < RequestPriority.BACKGROUND.rank


def test_search_priority_is_chosen_by_mode_and_direction() -> None:
    """``ann`` ног не различает: сканирование там одно занятие целиком."""
    assert search_priority(ScanMode.UR, OperationType.SELL) is RequestPriority.UR_LEVEL1_SELL
    assert search_priority(ScanMode.FEST, OperationType.BUY) is RequestPriority.FEST_LEVEL1_BUY
    assert search_priority(ScanMode.ANN, OperationType.BUY) is RequestPriority.ANN_SCAN
    assert search_priority(ScanMode.ANN, OperationType.SELL) is RequestPriority.ANN_SCAN


def test_trading_mode_has_no_confirmation_priority() -> None:
    """``ann`` исполняет находку сам и до Level 2 не доходит.

    Подставлять ему чужой приоритет нельзя: у режима, которого на втором
    этапе не бывает, его просто нет.
    """
    assert confirmation_priority(ScanMode.UR) is RequestPriority.UR_LEVEL2
    assert confirmation_priority(ScanMode.FEST) is RequestPriority.FEST_LEVEL2

    with pytest.raises(ValueError, match="no level 2 confirmation"):
        confirmation_priority(ScanMode.ANN)


def test_priority_ranks_are_unique() -> None:
    ranks = [priority.rank for priority in RequestPriority]
    assert len(ranks) == len(set(ranks))


def test_opportunity_lifecycle_covers_verification_and_notification() -> None:
    """Решение D-1: единый lifecycle Opportunity."""
    values = {status.value for status in OpportunityStatus}
    assert {"created", "verifying"} <= values
    assert {"confirmed", "partial", "unprofitable", "route_unavailable"} <= values
    assert {"notified", "notified_partial", "notified_failed"} <= values


def test_capability_has_distinct_unknown_and_unsupported() -> None:
    assert CapabilityStatus.UNKNOWN is not CapabilityStatus.UNSUPPORTED


def test_partial_is_distinct_from_confirmed() -> None:
    assert AmountConfirmationStatus.PARTIAL is not AmountConfirmationStatus.CONFIRMED


def test_notification_mode_values_are_uppercase_letters() -> None:
    assert {mode.value for mode in NotificationMode} == {"A", "B"}


def test_job_status_repr_is_readable() -> None:
    assert repr(JobStatus.QUEUED) == "JobStatus.QUEUED"
