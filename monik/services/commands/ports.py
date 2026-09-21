"""Порты обработчиков команд.

Данные читаются из репозиториев и уже собранных снимков: обработчик
показа ничего не спрашивает у провайдеров, и кнопка ``об`` в частности
не делает нового запроса (``CLAUDE.md`` §35).

Исключение одно и названо явно — :class:`PositionExitSource`. Оператор,
решающий продать зависшую сделку, спрашивает цену, а цены без запроса
не существует. Сам обработчик к агрегатору при этом не ходит: он
обращается к подсистеме исполнения, которой такие запросы принадлежат
по праву (``the_main_rules.md``, правила 11 и 22). Запрет
``15_NOTIFICATION_SYSTEM.md`` §6 этим не затронут: он адресован системе
уведомлений, а не подсистеме исполнения.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from monik.domain.enums.control import ScannerRunState
from monik.domain.enums.lifecycle import JobStatus
from monik.domain.models.job import ConfirmationResult, Level2Job
from monik.domain.models.opportunity import Opportunity
from monik.domain.models.scan import Scan
from monik.domain.value_objects.identifiers import KId, OpportunityId
from monik.services.opportunity.statistics import ConfirmationStatistics
from monik.services.trading.exits import ExitQuote

__all__ = [
    "BackupStatus",
    "PositionExitSource",
    "BackupStatusSource",
    "ComponentStatus",
    "JobReader",
    "NotificationReader",
    "OpportunityReader",
    "ProviderStatus",
    "ProviderStatusSource",
    "ScanReader",
    "ScannerControl",
    "StatsSnapshot",
    "StatsSource",
    "StatusSource",
    "TradingControl",
]


@dataclass(frozen=True, slots=True)
class ComponentStatus:
    """Состояние одной подсистемы для ``/status``."""

    name: str
    state: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class StatsSnapshot:
    """Агрегированная статистика для ``/stats``.

    Confirmation rate считается по формуле ``CLAUDE.md`` §27 и может быть
    ``N/A``: отсутствие решений нельзя показывать как ноль процентов.
    """

    confirmations: ConfirmationStatistics = field(default_factory=ConfirmationStatistics)
    scans_completed: int = 0
    opportunities_created: int = 0
    notifications_sent: int = 0


@runtime_checkable
class JobReader(Protocol):
    """Чтение Level 2 Job и сохранённых результатов проверки."""

    async def get(self, k_id: KId) -> Level2Job | None:
        """Найти Job по ``#K``."""
        ...

    async def list_by_status(self, status: JobStatus, *, limit: int) -> tuple[Level2Job, ...]:
        """Job'ы в указанном статусе."""
        ...

    async def load_confirmation(self, k_id: KId, revision: int) -> ConfirmationResult | None:
        """Сохранённый результат проверки."""
        ...


@runtime_checkable
class OpportunityReader(Protocol):
    """Чтение Opportunity."""

    async def get(self, opportunity_id: OpportunityId) -> Opportunity | None:
        """Найти возможность по идентификатору."""
        ...


@runtime_checkable
class NotificationReader(Protocol):
    """Чтение подготовленных текстов уведомления."""

    async def load_texts(self, notification_id: str) -> tuple[str | None, str | None]:
        """Тексты сообщения и кнопки ``об``."""
        ...


@runtime_checkable
class StatusSource(Protocol):
    """Снимок состояния подсистем."""

    def components(self) -> tuple[ComponentStatus, ...]:
        """Текущее состояние подсистем."""
        ...


@runtime_checkable
class StatsSource(Protocol):
    """Источник агрегированной статистики."""

    def snapshot(self) -> StatsSnapshot:
        """Текущая статистика."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """Состояние одного агрегатора и его очереди.

    Собирается из Health Monitoring и Resource Manager. Ни ключей, ни
    заголовков аутентификации здесь нет: наружу выходит только
    операционное состояние (``19_HEALTH_MONITORING.md`` §65).
    """

    provider: str
    health: str
    circuit_state: str
    requests_per_second: float
    max_concurrent: int
    active: int
    waiting: int
    reason: str | None = None
    #: Часы работы агрегатора, если они заданы. Без этой строки молчащий
    #: по расписанию провайдер выглядит как неисправный.
    schedule: str | None = None
    #: Находится ли он сейчас в своём окне.
    within_schedule: bool = True


@dataclass(frozen=True, slots=True)
class BackupStatus:
    """Состояние резервного копирования для отчёта пользователю."""

    enabled: bool
    last_run_at: str | None = None
    last_outcome: str | None = None
    copies: int = 0
    detail: str | None = None


@runtime_checkable
class ProviderStatusSource(Protocol):
    """Снимок состояния агрегаторов."""

    def providers(self) -> tuple[ProviderStatus, ...]:
        """Состояние всех настроенных агрегаторов."""
        ...


@runtime_checkable
class ScanReader(Protocol):
    """Чтение последних циклов Level 1."""

    async def recent(self, *, limit: int) -> tuple[Scan, ...]:
        """Последние циклы, сначала свежие."""
        ...


@runtime_checkable
class BackupStatusSource(Protocol):
    """Состояние механизма резервного копирования."""

    async def status(self) -> BackupStatus:
        """Текущее состояние резервных копий."""
        ...


@runtime_checkable
class ScannerControl(Protocol):
    """Управление сканированием.

    Порт намеренно узкий: подсистема команд не знает ни планировщика, ни
    сканеров — она только сообщает намерение оператора
    (``CLAUDE.md`` §35).
    """

    def state(self) -> ScannerRunState:
        """Текущее состояние сканирования."""
        ...

    def start(self) -> bool:
        """Разрешить сканирование. ``True``, если состояние изменилось."""
        ...

    def stop(self) -> bool:
        """Запретить новые циклы. ``True``, если состояние изменилось."""
        ...

    def request_restart(self) -> None:
        """Запросить перезапуск процесса."""
        ...


@runtime_checkable
class PositionExitSource(Protocol):
    """Выход из открытой сделки по решению оператора.

    Единственный порт команд, за которым стоит обращение к агрегаторам,
    и единственный, за которым стоит трата денег. Оба свойства
    намеренные: цены без запроса не существует, а закрыть зависшую
    сделку по текущей цене — это решение человека, ради которого
    подсистема исполнения его и уведомляет (``the_main_rules.md``,
    правила 11 и 22).
    """

    async def preview_exit(self, t_id: str) -> ExitQuote | None:
        """Во что обойдётся выход прямо сейчас.

        ``None`` — показывать нечего: такой открытой сделки нет, она уже
        в продаже, или предложения не дал никто.
        """
        ...

    async def sell_at_market(self, t_id: str) -> ExitQuote | None:
        """Продать по текущей цене. ``None`` — продать не удалось."""
        ...


@runtime_checkable
class TradingControl(Protocol):
    """Разрешение подсистемы исполнения открывать сделки.

    Отдельный порт, а не часть :class:`ScannerControl`: остановка
    сканирования и запрет тратить деньги — разные решения, и смешивать
    их в одной команде нельзя.
    """

    @property
    def allowed(self) -> bool:
        """Разрешена ли торговля конфигурацией."""
        ...

    def start(self) -> bool:
        """Разрешить открытие сделок."""
        ...

    def stop(self) -> bool:
        """Запретить открытие новых сделок."""
        ...

    def state(self) -> str:
        """Состояние для оператора."""
        ...
