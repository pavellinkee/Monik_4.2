"""Тексты операционных уведомлений.

Формат сообщений централизован (``15_NOTIFICATION_SYSTEM.md`` §47):
разные подсистемы не создают собственных hard-coded форматов.

В текст попадает только операционное состояние. Ни ключи провайдеров, ни
bot token, ни chat id, ни тела ответов API здесь не появляются
(``19_HEALTH_MONITORING.md`` §65, ``15_NOTIFICATION_SYSTEM.md`` §70-71).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from monik.domain.enums.control import ScannerStopReason
from monik.domain.enums.health import ApplicationHealthStatus
from monik.domain.enums.notifications import StartupKind, SystemAlertSeverity
from monik.domain.models.health import ApplicationHealth

__all__ = [
    "ModeSummary",
    "UPDATE_BUTTON_LABEL",
    "StartupSummary",
    "aggregated_text",
    "pending_updates_text",
    "recovery_text",
    "scanner_stopped_text",
    "severity_for_component",
    "severity_for_provider",
    "startup_text",
    "transition_text",
]

#: Надпись на кнопке установки обновлений. Кнопка появляется только
#: тогда, когда установка действительно доступна приложению.
UPDATE_BUTTON_LABEL = "⬇️ Обновить и перезапустить"

#: Маркер важности в начале сообщения.
_MARKERS: dict[SystemAlertSeverity, str] = {
    SystemAlertSeverity.INFO: "🟢",
    SystemAlertSeverity.WARNING: "🟠",
    SystemAlertSeverity.CRITICAL: "🔴",
}

#: Заголовок сообщения о запуске для каждого вида запуска.
_STARTUP_HEADLINES: dict[StartupKind, str] = {
    StartupKind.INITIAL: "Monik запущен",
    StartupKind.RESTART: "Monik перезапущен",
    StartupKind.CRASH_RECOVERY: "Monik запущен после аварийного завершения",
}

#: Состояния приложения, при которых запуск считается неполным.
_STARTUP_SUFFIX: dict[ApplicationHealthStatus, str] = {
    ApplicationHealthStatus.DEGRADED: " с предупреждениями",
    ApplicationHealthStatus.UNAVAILABLE: " с критическими ошибками",
}

#: Заголовок сообщения об остановке сканирования и его важность.
#: Остановка оператором штатна, аварийное завершение — нет.
_STOP_HEADLINES: dict[ScannerStopReason, tuple[SystemAlertSeverity, str]] = {
    ScannerStopReason.OPERATOR: (
        SystemAlertSeverity.WARNING,
        "Сканирование остановлено оператором",
    ),
    ScannerStopReason.RESTART: (
        SystemAlertSeverity.WARNING,
        "Сканирование остановлено: запрошен перезапуск",
    ),
    ScannerStopReason.SHUTDOWN: (
        SystemAlertSeverity.WARNING,
        "Сканирование остановлено: приложение завершает работу",
    ),
    ScannerStopReason.CRITICAL_FAILURE: (
        SystemAlertSeverity.CRITICAL,
        "Сканирование остановлено из-за критической ошибки",
    ),
}

#: Пояснение, что именно происходит с уже принятой работой.
_STOP_DETAILS: dict[ScannerStopReason, str] = {
    ScannerStopReason.OPERATOR: (
        "Новые циклы Level 1 не начинаются. Принятые проверки Level 2 будут завершены."
    ),
    ScannerStopReason.RESTART: "Процесс поднимет менеджер служб.",
    ScannerStopReason.SHUTDOWN: "Текущая работа завершена корректно.",
    ScannerStopReason.CRITICAL_FAILURE: "Требуется вмешательство оператора.",
}

#: Подсистемы, состояние которых показывается в сообщении о запуске.
_STARTUP_COMPONENTS = ("level1", "level2", "notifications", "database", "scheduler")


@dataclass(frozen=True, slots=True)
class ModeSummary:
    """Что именно работает в одном режиме прохода.

    Уведомление о запуске описывает работу, а не объявленные
    возможности. Сеть, включённая в конфигурации, но не сканируемая ни
    одним режимом, в работе не участвует, и называть её работающей —
    значит вводить оператора в заблуждение. Поэтому состав берётся у
    самого сканера, а не из списка включённых сетей.
    """

    name: str
    networks: tuple[str, ...]
    providers: tuple[str, ...]
    #: Промежуточные токены круга. Базовый в них не входит: круг
    #: начинается и заканчивается им, и в списке целей его нет.
    tokens: tuple[str, ...]
    #: Базовый токен круга. Без него список выше читается неверно:
    #: оператор видит два токена там, где работают три.
    base_token: str | None = None
    #: Суммы, которыми идёт проход.
    amounts: tuple[str, ...] = ()
    interval_seconds: int | None = None
    threshold_percent: str | None = None

    @property
    def title(self) -> str:
        """Имя режима для заголовка: ``ann`` → ``Ann``."""
        return self.name.capitalize()


@dataclass(frozen=True, slots=True)
class StartupSummary:
    """Данные для сообщения о завершении запуска.

    Собираются composition root'ом из конфигурации и снимка Health
    Monitoring: сама Notification System состояние не вычисляет.
    """

    kind: StartupKind
    version: str
    environment: str
    #: Сканируемые сети. Их может быть несколько: круг замыкается
    #: внутри сети, но сетей в работе столько, сколько включил оператор.
    networks: tuple[str, ...]
    providers: tuple[str, ...]
    health: ApplicationHealth
    recovered: int = 0
    #: Работающие режимы прохода. Пусто — сканирование не идёт вовсе.
    modes: tuple[ModeSummary, ...] = ()


def _network_label(networks: tuple[str, ...]) -> str:
    """Заголовок строки сетей: одна сеть — «Сеть», несколько — «Сети»."""
    return "Сеть" if len(networks) == 1 else "Сети"


def severity_for_provider(status: str) -> SystemAlertSeverity:
    """Важность сообщения о состоянии провайдера."""
    if status in {"healthy", "recovering"}:
        return SystemAlertSeverity.INFO
    if status == "unavailable":
        return SystemAlertSeverity.CRITICAL
    return SystemAlertSeverity.WARNING


def severity_for_component(status: str) -> SystemAlertSeverity:
    """Важность сообщения о состоянии подсистемы."""
    if status == ApplicationHealthStatus.UNAVAILABLE.value:
        return SystemAlertSeverity.CRITICAL
    if status in {
        ApplicationHealthStatus.HEALTHY.value,
        ApplicationHealthStatus.STARTING.value,
        ApplicationHealthStatus.STOPPING.value,
    }:
        return SystemAlertSeverity.INFO
    return SystemAlertSeverity.WARNING


def startup_text(summary: StartupSummary) -> str:
    """Сообщение о завершении запуска.

    Отправляется только после проверки готовности: до неё состояние
    подсистем неизвестно, и объявлять запуск успешным нельзя
    (``19_HEALTH_MONITORING.md`` §70).
    """
    status = summary.health.status
    severity = _startup_severity(status)
    headline = _startup_headline(summary) + _STARTUP_SUFFIX.get(status, "")
    lines = [
        f"{_MARKERS[severity]} {headline}",
        f"Версия: {summary.version}",
        f"Окружение: {summary.environment}",
    ]
    lines.extend(_mode_lines(summary))
    lines.extend(_component_lines(summary.health))
    lines.extend(_provider_lines(summary.health))
    if summary.recovered:
        lines.append(f"Восстановлено незавершённых записей: {summary.recovered}")
    lines.append(f"Общее состояние: {status.value}")
    return "\n".join(lines)


def _startup_headline(summary: StartupSummary) -> str:
    """Заголовок с именем работающего режима.

    Оператор запускает не «Monik вообще», а конкретный проход, и первая
    строка уведомления должна называть именно его. Когда режимов
    несколько, перечисляются все; когда не работает ни один — остаётся
    прежний общий заголовок, потому что называть нечего.
    """
    base = _STARTUP_HEADLINES[summary.kind]
    if not summary.modes:
        return base
    names = ", ".join(mode.title for mode in summary.modes)
    verb = base.removeprefix("Monik").strip()
    return f"Monik {names} {verb}"


def _mode_lines(summary: StartupSummary) -> list[str]:
    """Описание каждого работающего режима.

    Перечисляется то, что относится к самому проходу: где он идёт, у
    кого спрашивает и что проверяет. Объявленные, но не участвующие в
    работе сети и агрегаторы сюда не попадают.
    """
    if not summary.modes:
        return ["Сканирование: ни один режим не включён"]
    lines: list[str] = []
    for mode in summary.modes:
        lines.append(f"Режим {mode.title}")
        lines.append(
            f"  {_network_label(mode.networks)}: "
            f"{', '.join(mode.networks) if mode.networks else 'нет'}"
        )
        lines.append(f"  Агрегаторы: {', '.join(mode.providers) if mode.providers else 'нет'}")
        if mode.tokens:
            circle = ", ".join(mode.tokens)
            lines.append(
                f"  Круг: {mode.base_token} → {circle} → {mode.base_token}"
                if mode.base_token
                else f"  Токены: {circle}"
            )
        if mode.amounts:
            lines.append(f"  Суммы: {', '.join(mode.amounts)}")
        pace: list[str] = []
        if mode.interval_seconds is not None:
            pace.append(f"проход раз в {mode.interval_seconds} с")
        if mode.threshold_percent is not None:
            pace.append(f"порог {mode.threshold_percent} %")
        if pace:
            lines.append(f"  {', '.join(pace)}")
    return lines


def scanner_stopped_text(reason: ScannerStopReason, *, detail: str | None = None) -> str:
    """Сообщение о фактической остановке сканирования.

    Формат тот же, что у остальных операционных уведомлений: маркер
    важности, заголовок, пояснение (``15_NOTIFICATION_SYSTEM.md`` §47).
    """
    severity, headline = _STOP_HEADLINES[reason]
    lines = [f"{_MARKERS[severity]} {headline}", _STOP_DETAILS[reason]]
    if detail:
        lines.append(detail)
    return "\n".join(lines)


def transition_text(
    subject: str, status: str, *, severity: SystemAlertSeverity, reason: str | None = None
) -> str:
    """Сообщение о смене состояния подсистемы или провайдера."""
    text = f"{_MARKERS[severity]} {subject}: {status}"
    if reason:
        return f"{text} ({reason})"
    return text


def pending_updates_text(
    updates: Sequence[str],
    *,
    apply_command: str,
    limit: int = 15,
    with_button: bool = False,
) -> str:
    """Напоминание о доступных, но не установленных обновлениях.

    Автоматически ставятся только обновления безопасности, поэтому
    остальные накапливаются и ждут решения оператора. Сообщение
    перечисляет их и называет команду применения; список обрезается,
    чтобы уведомление оставалось читаемым.
    """
    lines = [
        f"{_MARKERS[SystemAlertSeverity.INFO]} Доступны обновления системы: {len(updates)}",
        "Автоматически ставятся только обновления безопасности; эти ждут решения.",
        "",
    ]
    lines.extend(f"· {item}" for item in updates[:limit])
    if len(updates) > limit:
        lines.append(f"… и ещё {len(updates) - limit}")
    if with_button:
        # Кнопка делает то же самое, поэтому команда остаётся запасным
        # путём, а не основным способом.
        lines.extend(
            (
                "",
                "Кнопка ниже установит обновления и перезапустит сканер.",
                "Вручную:",
                apply_command,
            )
        )
    else:
        lines.extend(
            (
                "",
                "Применить вручную:",
                apply_command,
                "После установки сканер нужно перезапустить.",
            )
        )
    return "\n".join(lines)


def recovery_text(subject: str) -> str:
    """Сообщение о восстановлении (``19_HEALTH_MONITORING.md`` §48)."""
    return f"{_MARKERS[SystemAlertSeverity.INFO]} {subject}: восстановлен"


def aggregated_text(subject: str, status: str, *, errors: int) -> str:
    """Периодическое напоминание о незакрытом состоянии.

    Сообщение отправляется не на каждую ошибку, а по накопленному счётчику
    (``28_OBSERVABILITY.md`` §59-60).
    """
    if errors > 0:
        return f"⚠️ {subject}: всё ещё {status} — {errors} ошибок с прошлого уведомления"
    return f"⚠️ {subject}: всё ещё {status}"


def _startup_severity(status: ApplicationHealthStatus) -> SystemAlertSeverity:
    if status is ApplicationHealthStatus.UNAVAILABLE:
        return SystemAlertSeverity.CRITICAL
    if status is ApplicationHealthStatus.DEGRADED:
        return SystemAlertSeverity.WARNING
    return SystemAlertSeverity.INFO


def _component_lines(health: ApplicationHealth) -> list[str]:
    known = {item.component: item.status.value for item in health.components}
    return [
        f"{component}: {known[component]}"
        for component in _STARTUP_COMPONENTS
        if component in known
    ]


def _provider_lines(health: ApplicationHealth) -> list[str]:
    return [f"provider {item.provider_id.value}: {item.status.value}" for item in health.providers]
