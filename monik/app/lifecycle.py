"""Жизненный цикл приложения.

Последовательность запуска фиксирована ``CLAUDE.md`` §30:

1. загрузить configuration;
2. открыть SQLite;
3. проверить integrity;
4. выполнить migrations;
5. восстановить незавершённое состояние;
6. инициализировать adapters;
7. инициализировать Resource Manager;
8. инициализировать Scheduler;
9. инициализировать Telegram;
10. запустить workers.

Business logic здесь отсутствует: модуль только связывает готовые
подсистемы и управляет их запуском и остановкой.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from monik import APPLICATION_VERSION
from monik.app.container import Container, build_container
from monik.app.recovery import RecoveryReport, RecoveryService
from monik.app.startup_health import (
    detect_startup_kind,
    mark_running,
    mark_stopped,
    probe_providers,
)
from monik.app.supervisor import SupervisedWorker, Supervisor
from monik.app.trade_buttons import sell_buttons
from monik.config.loader import LoadedConfiguration
from monik.config.sections.scheduler import TaskScheduleConfig
from monik.domain.enums.control import ScannerStopReason
from monik.domain.enums.health import ApplicationHealthStatus, SupervisorState
from monik.domain.enums.modes import ScanMode
from monik.domain.enums.notifications import StartupKind
from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.domain.enums.resources import RequestPriority, search_priority
from monik.domain.enums.scheduler import TaskMode
from monik.infrastructure.db import Database, MigrationRunner
from monik.infrastructure.providers.contract import AggregatorAdapter
from monik.services.commands.parser import (
    CommandName,
    action_callback_data,
)
from monik.services.notifications import ModeSummary, StartupSummary
from monik.services.observability import MetricsRegistry
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger, log_fields
from monik.services.scheduler import (
    ExecutionOutcome,
    Scheduler,
    TaskHandler,
    TaskRegistry,
    TaskRunner,
)
from monik.services.updates import AptPendingUpdates, UpdateWatcher

__all__ = [
    "TASK_CAPABILITY_LOAD",
    "TASK_TOKEN_CHECK",
    "scan_task_name",
    "TASK_NOTIFICATIONS",
    "TASK_POSITIONS",
    "TASK_TRADE",
    "TASK_TRADE_REPORT",
    "TASK_BACKUP",
    "TASK_SYSTEM_HEALTH",
    "TASK_SYSTEM_UPDATES",
    "TASK_TELEGRAM_COMMANDS",
    "Application",
    "build_application",
    "create_application",
]

_LOGGER = get_logger("app.lifecycle")


#: Идентификаторы задач планировщика.
#: Идентификатор задачи режима: по одной задаче на режим.
def _plain_amount(value: Decimal) -> str:
    """Сумма в привычной записи, без хвостовых нулей и без степеней.

    ``normalize`` убирает хвостовые нули, но круглые числа превращает в
    показательную запись: 50 становится ``5E+1``. Оператору такая строка
    ничего не говорит, поэтому запись задаётся явно.
    """
    return format(value.normalize(), "f")


def scan_task_name(mode: ScanMode) -> str:
    """Имя задачи расписания для режима."""
    return f"scan_{mode.value}"


TASK_NOTIFICATIONS = "notification_delivery"
TASK_POSITIONS = "position_watch"
TASK_TRADE = "trade_execution"
TASK_TRADE_REPORT = "trade_report"
TASK_TELEGRAM_COMMANDS = "telegram_commands"
TASK_CAPABILITY_LOAD = "capability_load"
TASK_TOKEN_CHECK = "token_check"
TASK_SYSTEM_HEALTH = "system_health_notifications"
TASK_BACKUP = "backup"
TASK_SYSTEM_UPDATES = "system_updates"

#: День недели резервного копирования по умолчанию (ISO: суббота).
_SATURDAY = 6

#: Расписания по умолчанию. Пользовательская конфигурация имеет приоритет
#: (``14_SCHEDULER.md`` §58-59).
_DEFAULT_SCHEDULES: dict[str, TaskScheduleConfig] = {
    # Задач режимов здесь нет намеренно: их период известен только из
    # настройки самого режима (scanner.modes) и подставляется при
    # регистрации. Значение по умолчанию в этой таблице означало бы, что
    # период живёт в двух местах, и режим, не упомянутый в
    # scheduler.tasks, пошёл бы с чужим темпом.
    TASK_NOTIFICATIONS: TaskScheduleConfig(mode=TaskMode.INTERVAL, interval_seconds=10),
    TASK_POSITIONS: TaskScheduleConfig(mode=TaskMode.INTERVAL, interval_seconds=10),
    TASK_TELEGRAM_COMMANDS: TaskScheduleConfig(mode=TaskMode.INTERVAL, interval_seconds=5),
    TASK_CAPABILITY_LOAD: TaskScheduleConfig(mode=TaskMode.STARTUP),
    # Сверка адресов токенов: разовая, при старте. Опечатка в адресе
    # выглядит в работе как отсутствие ликвидности, и заметить её иначе
    # можно только вручную.
    TASK_TOKEN_CHECK: TaskScheduleConfig(mode=TaskMode.STARTUP),
    TASK_SYSTEM_HEALTH: TaskScheduleConfig(mode=TaskMode.INTERVAL, interval_seconds=60),
    # Резервная копия по умолчанию: суббота, 03:00. Ночь выходного дня —
    # время наименьшей нагрузки; конкретный день и час задаются
    # конфигурацией планировщика.
    TASK_BACKUP: TaskScheduleConfig(
        mode=TaskMode.WEEKLY, time="03:00", weekday=_SATURDAY, timezone="UTC"
    ),
    # Напоминание о доступных обновлениях: раз в сутки утром. Ничего не
    # устанавливает — только сообщает, что накопилось.
    TASK_SYSTEM_UPDATES: TaskScheduleConfig(
        mode=TaskMode.DAILY, time="09:00", timezone="UTC", interval_days=1
    ),
}


@dataclass
class Application:
    """Собранное приложение и его жизненный цикл."""

    container: Container
    scheduler: Scheduler
    supervisor: Supervisor
    recovery: RecoveryService
    shutdown_timeout: timedelta = timedelta(seconds=30)
    recovery_report: RecoveryReport | None = None
    startup_kind: StartupKind | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    #: Начатые такты планировщика. Такт живёт, пока выполняются его
    #: задачи, поэтому ссылки хранятся до завершения.
    _ticks: set[asyncio.Task[tuple[ExecutionOutcome, ...]]] = field(default_factory=set)
    #: Дошёл ли запуск до работающих воркеров. До этого момента сканер не
    #: работал, и сообщать о его остановке нечего.
    _started: bool = False
    #: Последнее наблюдённое состояние сканера. По переходу в остановленное
    #: отправляется одно уведомление.
    _scanner_running: bool = True

    async def startup(self) -> RecoveryReport:
        """Выполнить шаги 5-9 последовательности запуска.

        База уже открыта и мигрирована (шаги 2-4 выполняет
        :func:`create_application`), поэтому здесь восстанавливается
        состояние и инициализируются подсистемы.

        Операционное уведомление отправляется последним шагом — после
        проверки подсистем и доступности провайдеров: до неё объявлять
        запуск успешным нельзя (``19_HEALTH_MONITORING.md`` §69-70).
        """
        health = self.container.health
        state = self.container.repositories.metadata
        self.startup_kind = await detect_startup_kind(state)

        health.set_component("configuration", ApplicationHealthStatus.HEALTHY)
        health.set_component("database", ApplicationHealthStatus.HEALTHY)

        report = await self.recovery.recover()
        self.recovery_report = report

        await self.container.capabilities.load()
        health.set_component("resource_manager", ApplicationHealthStatus.HEALTHY)

        await self.scheduler.prepare()
        await self.scheduler.run_startup()
        health.set_component("scheduler", ApplicationHealthStatus.HEALTHY)

        for component in ("level1", "level2", "fees", "calculator", "notifications"):
            health.set_component(component, ApplicationHealthStatus.HEALTHY)

        await probe_providers(self.container.adapters, health=health)
        await mark_running(state, now=self.container.clock.now())
        self._started = True
        self._scanner_running = self.container.control.is_running
        await self._notify_startup(report)
        _LOGGER.info(
            "startup complete",
            extra=log_fields(
                recovered=report.total,
                startup_kind=self.startup_kind.value,
                status=health.application_health().status.value,
            ),
        )
        return report

    async def run(self) -> SupervisorState:
        """Запустить воркеры и работать до остановки."""
        self.supervisor.register(
            SupervisedWorker(name="scheduler_loop", run=self._scheduler_loop, critical=True)
        )
        await self.supervisor.start()
        return await self.supervisor.supervise()

    def request_stop(self) -> None:
        """Попросить приложение остановиться."""
        self._stop.set()

    async def shutdown(self) -> None:
        """Graceful shutdown: новые циклы не создаются (``14`` §49)."""
        self.request_stop()
        self.container.health.mark_stopping()
        # Отправляется до закрытия контейнера: после него транспорт
        # Telegram уже закрыт. Повтор подавляет сам notifier, поэтому
        # остановка, о которой уже сообщил цикл планировщика, второго
        # сообщения не создаёт.
        await self.notify_scanner_stopped(self._shutdown_reason())
        await mark_stopped(self.container.repositories.metadata, now=self.container.clock.now())
        await self.scheduler.shutdown()
        await self.container.level2_worker.cancel_all()
        try:
            await asyncio.wait_for(
                self.supervisor.shutdown(), timeout=self.shutdown_timeout.total_seconds()
            )
        except TimeoutError:
            _LOGGER.warning("shutdown timed out; workers were cancelled")
        await self.container.aclose()

    async def notify_scanner_stopped(self, reason: ScannerStopReason) -> bool:
        """Сообщить об остановке сканирования, если она действительно была.

        Единственная точка отправки: все пути остановки — команда
        оператора, перезапуск, сигнал, критическая ошибка — проходят либо
        через наблюдение в цикле планировщика, либо через
        :meth:`shutdown`. Повтор подавляет notifier, поэтому одно событие
        даёт одно сообщение.
        """
        notifier = self.container.system_notifier
        if notifier is None or not self._started:
            # Сканер не доработал до запуска воркеров: сообщать не о чем.
            return False
        return await notifier.notify_scanner_stopped(reason)

    def _shutdown_reason(self) -> ScannerStopReason:
        """Почему останавливается приложение."""
        if self.container.control.restart_requested:
            return ScannerStopReason.RESTART
        if self.supervisor.state is SupervisorState.SAFE_STOP:
            return ScannerStopReason.CRITICAL_FAILURE
        return ScannerStopReason.SHUTDOWN

    async def _observe_scanner_state(self) -> None:
        """Отследить переход сканера в остановленное состояние.

        Наблюдается фактическое состояние, а не вызовы: так уведомление
        не приходится дублировать в каждом обработчике команды, и
        повторная остановка уже остановленного сканера событием не
        является.
        """
        running = self.container.control.is_running
        if running == self._scanner_running:
            return
        self._scanner_running = running
        if running:
            notifier = self.container.system_notifier
            if notifier is not None:
                notifier.notify_scanner_resumed()
            return
        reason = (
            ScannerStopReason.RESTART
            if self.container.control.restart_requested
            else ScannerStopReason.OPERATOR
        )
        await self.notify_scanner_stopped(reason)

    async def _notify_startup(self, report: RecoveryReport) -> None:
        """Отправить итоговое сообщение о запуске, если канал настроен."""
        notifier = self.container.system_notifier
        if notifier is None or self.startup_kind is None:
            return
        config = self.container.configuration
        await notifier.notify_startup(
            StartupSummary(
                kind=self.startup_kind,
                version=APPLICATION_VERSION,
                environment=config.application.environment.value,
                networks=tuple(str(network.network_id) for network in config.enabled_networks),
                providers=tuple(
                    provider.provider_id.value for provider in config.enabled_providers
                ),
                health=self.container.health.application_health(),
                recovered=report.total,
                modes=self._running_modes(),
            )
        )

    def _running_modes(self) -> tuple[ModeSummary, ...]:
        """Что на самом деле работает, по одному описанию на режим.

        Состав берётся у сканера, а не из списка включённых сетей: сеть
        может быть объявлена и подготовлена, но не сканироваться ни одним
        режимом, и тогда она к работе отношения не имеет. Именно это
        расхождение и делало прежнее уведомление обманчивым.

        Правила отбора токенов у режимов разные, и повторять их здесь
        нельзя: они живут в сканере и спрашиваются у него.
        """
        config = self.container.configuration
        tokens = self.container.tokens
        summaries: list[ModeSummary] = []
        for mode in config.scanner.modes.enabled_modes():
            settings = config.scanner.modes.for_mode(mode)
            scopes = self.container.level1.scopes(mode)
            if not scopes:
                continue
            networks: list[str] = []
            providers: list[str] = []
            symbols: list[str] = []
            bases: list[str] = []
            amounts: list[str] = []
            for scope in scopes:
                networks.extend(str(network) for network in scope.networks)
                providers.extend(provider.value for provider in scope.providers)
                for key in scope.tokens:
                    token = tokens.get(key)
                    if token is not None:
                        symbols.append(str(token.symbol))
                for network in scope.networks:
                    base = tokens.base_token(network)
                    bases.append(str(base.symbol))
                    amounts.extend(
                        _plain_amount(base.amount_from_base_units(raw).as_decimal)
                        for raw in scope.raw_amounts
                    )
            summaries.append(
                ModeSummary(
                    name=mode.value,
                    networks=tuple(dict.fromkeys(networks)),
                    providers=tuple(dict.fromkeys(providers)),
                    tokens=tuple(dict.fromkeys(symbols)),
                    base_token=next(iter(dict.fromkeys(bases)), None),
                    amounts=tuple(dict.fromkeys(amounts)),
                    interval_seconds=settings.interval_seconds,
                    threshold_percent=str(config.profitability.threshold_for(mode)),
                )
            )
        return tuple(summaries)

    async def _scheduler_loop(self) -> None:
        """Периодически выполнять готовые задачи планировщика.

        Собственного расписания цикл не задаёт: моменты запуска определяет
        Scheduler (``14_SCHEDULER.md`` §3, §63).

        Такт не дожидается выполнения запущенных задач. Иначе цикл
        сканирования, идущий пятнадцать секунд, всё это время не давал бы
        планировщику принять команду оператора или отправить уведомление,
        а установка обновлений останавливала бы его на минуты. Расписания
        задач независимы (§21), и ожидание одной задачи не должно
        задерживать остальные.
        """
        interval = 1.0
        try:
            while not self._stop.is_set():
                await self._observe_scanner_state()
                if self.container.control.restart_requested:
                    # Перезапуск выполняет менеджер служб: приложение только
                    # корректно завершает текущую работу и выходит.
                    _LOGGER.warning("restart requested; stopping the application")
                    self.request_stop()
                    break
                self._dispatch_tick()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=interval)
                except TimeoutError:
                    continue
        finally:
            await self._drain_ticks()

    def _dispatch_tick(self) -> None:
        """Начать такт планировщика, не дожидаясь его задач.

        Ссылка на задачу сохраняется до её завершения: без этого сборщик
        мусора вправе уничтожить ещё выполняющуюся задачу.
        """
        tick = asyncio.ensure_future(self.scheduler.tick())
        self._ticks.add(tick)
        tick.add_done_callback(self._ticks.discard)

    async def _drain_ticks(self) -> None:
        """Дождаться тактов, начатых до остановки.

        Сами задачи отменяет :meth:`Scheduler.shutdown`; здесь снимаются
        только ожидающие их такты, чтобы остановка не оставляла за собой
        незавершённых задач.
        """
        pending = tuple(self._ticks)
        if not pending:
            return
        for tick in pending:
            tick.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def build_application(
    loaded: LoadedConfiguration,
    *,
    database: Database,
    clock: Clock,
    metrics: MetricsRegistry | None = None,
    adapters: dict[ProviderId, AggregatorAdapter] | None = None,
) -> Application:
    """Собрать приложение поверх открытой базы.

    ``adapters`` передаётся composition root'у: это позволяет запустить
    приложение на детерминированных test implementations, не подменяя
    собранные подсистемы после сборки.
    """
    container = build_container(
        loaded, database=database, clock=clock, metrics=metrics, adapters=adapters
    )
    registry = TaskRegistry()
    config = loaded.config

    registry.register(
        TASK_CAPABILITY_LOAD,
        _capability_task(container),
        config=config.scheduler,
        default=_DEFAULT_SCHEDULES[TASK_CAPABILITY_LOAD],
    )
    registry.register(
        TASK_TOKEN_CHECK,
        _token_check_task(container),
        config=config.scheduler,
        default=_DEFAULT_SCHEDULES[TASK_TOKEN_CHECK],
        priority=RequestPriority.MAINTENANCE,
    )
    # По задаче на включённый режим: у каждого свой темп и своя планка,
    # поэтому и расписание у каждого своё. Период берётся из настройки
    # режима и в расписании не дублируется — иначе задача, не упомянутая
    # в scheduler.tasks, молча пошла бы с чужим темпом.
    for mode in config.scanner.modes.enabled_modes():
        registry.register(
            scan_task_name(mode),
            _scan_task(container, mode),
            config=config.scheduler,
            default=TaskScheduleConfig(
                mode=TaskMode.INTERVAL,
                interval_seconds=config.scanner.modes.for_mode(mode).interval_seconds,
            ),
            # Правило приоритета принадлежит режиму (``the_main_rules.md``,
            # правило 13). Здесь оно только попадает в журнал: очередь
            # выстраивают сами запросы, а не задача расписания.
            priority=search_priority(mode, OperationType.BUY),
            timeout=timedelta(seconds=config.scanner.scan_timeout_for(mode)),
        )
    if container.watcher is not None:
        # Ведение открытых сделок не зависит от того, разрешено ли
        # открывать новые: остановка торговли не должна бросать уже
        # купленные токены.
        registry.register(
            TASK_POSITIONS,
            _positions_task(container),
            config=config.scheduler,
            default=TaskScheduleConfig(
                mode=TaskMode.INTERVAL,
                interval_seconds=config.trading.recheck_interval_seconds,
            ),
            # Задача одна на все режимы, поэтому её пометка — самая
            # низкая из подтверждающих. Настоящий приоритет запроса
            # берётся у режима возможности, а не отсюда.
            priority=RequestPriority.UR_LEVEL2,
        )
    if container.executor is not None:
        # Решение по находкам — своя задача со своим сроком: она не
        # делит время с поиском, а поиск её не прерывает
        # (``the_main_rules.md``, правило 27).
        registry.register(
            TASK_TRADE,
            _trade_task(container),
            config=config.scheduler,
            default=TaskScheduleConfig(
                mode=TaskMode.INTERVAL,
                interval_seconds=config.trading.trade_interval_seconds,
            ),
            priority=RequestPriority.ANN_BUY,
            # Срок покрывает всю сделку целиком: сборку обязательств,
            # сверку цены, отправку и ожидание квитанции. Прерывать
            # начатую сделку нельзя — деньги уже в пути.
            timeout=timedelta(seconds=config.trading.trade_timeout_seconds),
        )
    if container.reporter is not None:
        # Отчёт проверяется чаще, чем отправляется: он сам решает, истёк
        # ли промежуток. Иначе такт планировщика и период отчёта
        # пришлось бы держать равными, и период перестал бы быть
        # настройкой оператора.
        registry.register(
            TASK_TRADE_REPORT,
            _trade_report_task(container),
            config=config.scheduler,
            default=TaskScheduleConfig(mode=TaskMode.INTERVAL, interval_seconds=30),
            priority=RequestPriority.BACKGROUND,
        )
    registry.register(
        TASK_NOTIFICATIONS,
        _notification_task(container),
        config=config.scheduler,
        default=_DEFAULT_SCHEDULES[TASK_NOTIFICATIONS],
    )
    registry.register(
        TASK_BACKUP,
        _backup_task(container),
        config=config.scheduler,
        default=_DEFAULT_SCHEDULES[TASK_BACKUP],
        priority=RequestPriority.MAINTENANCE,
    )
    if container.system_notifier is not None:
        registry.register(
            TASK_SYSTEM_UPDATES,
            _system_updates_task(container),
            config=config.scheduler,
            default=_DEFAULT_SCHEDULES[TASK_SYSTEM_UPDATES],
            priority=RequestPriority.MAINTENANCE,
        )
        registry.register(
            TASK_SYSTEM_HEALTH,
            _system_health_task(container),
            config=config.scheduler,
            default=TaskScheduleConfig(
                mode=TaskMode.INTERVAL,
                interval_seconds=config.health.check_interval_seconds,
            ),
            priority=RequestPriority.BACKGROUND,
        )
    if container.commands is not None:
        registry.register(
            TASK_TELEGRAM_COMMANDS,
            _command_task(container),
            config=config.scheduler,
            default=_DEFAULT_SCHEDULES[TASK_TELEGRAM_COMMANDS],
            priority=RequestPriority.BACKGROUND,
        )

    scheduler = Scheduler(
        registry=registry,
        runner=TaskRunner(clock, container.metrics),
        clock=clock,
        log=container.repositories.scheduler,
    )
    supervisor = Supervisor(monitor=container.health, clock=clock, config=config.health)
    recovery = RecoveryService(
        jobs=container.repositories.jobs,
        opportunities=container.repositories.opportunities,
        notifications=container.repositories.notifications,
        clock=clock,
        transitions=container.transitions,
    )
    return Application(
        container=container,
        scheduler=scheduler,
        supervisor=supervisor,
        recovery=recovery,
        shutdown_timeout=timedelta(seconds=config.application.shutdown_timeout_seconds),
    )


async def create_application(
    loaded: LoadedConfiguration,
    *,
    clock: Clock,
    metrics: MetricsRegistry | None = None,
    adapters: dict[ProviderId, AggregatorAdapter] | None = None,
) -> tuple[Application, Database]:
    """Выполнить шаги 2-4 запуска и собрать приложение.

    SQLite открывается, проверяется её целостность и применяются
    migrations — до любых подсистем (``CLAUDE.md`` §30).
    """
    database = Database(loaded.config.database)
    await database.connect()
    await MigrationRunner(database).upgrade()
    application = build_application(
        loaded, database=database, clock=clock, metrics=metrics, adapters=adapters
    )
    return application, database


def _token_check_task(container: Container) -> TaskHandler:
    """Сверка настроенных адресов токенов со списками провайдеров.

    Выполняется один раз при старте и стоит одного запроса на провайдера,
    который умеет отдавать список целиком. Ничего не выключает: расхождение
    только показывается, решение остаётся за оператором.
    """

    async def run() -> None:
        if container.token_check is None:
            return
        await container.token_check.run()

    return run


def _capability_task(container: Container) -> TaskHandler:
    """Загрузка сохранённого состояния capability при старте.

    Полный discovery здесь не выполняется: он относится к maintenance
    (``08_CAPABILITY_REGISTRY.md`` §3-4).
    """

    async def run() -> None:
        await container.capabilities.load()
        if container.calibration is not None:
            # Поправка к оценке газа поднимается тем же шагом: она такое
            # же сохранённое состояние, накопленное прошлыми сделками.
            await container.calibration.load()

    return run


def _system_updates_task(container: Container) -> TaskHandler:
    """Напоминание о доступных, но не установленных обновлениях.

    Автоматически ставятся только обновления безопасности, поэтому
    остальные накапливаются. Задача ничего не устанавливает сама:
    решение и момент перезапуска остаются за оператором.

    К уведомлению добавляется кнопка установки — но только если нажать
    её будет кому и чем: нужен работающий канал команд и выданное
    системой право ставить обновления без пароля. Право выдаётся вне
    Monik и может измениться без перезапуска, поэтому проверяется при
    каждой проверке, а не однажды при сборке приложения.
    """
    source = AptPendingUpdates()

    async def run() -> None:
        await UpdateWatcher(
            source,
            container.system_notifier,
            apply_action=await _update_action(container),
        ).check()

    return run


async def _update_action(container: Container) -> str | None:
    """Действие кнопки установки обновлений, если оно доступно."""
    if container.commands is None or container.updater is None:
        return None
    if not await container.updater.available():
        return None
    return action_callback_data(CommandName.SYSTEM_UPDATE)


def _search_allowed(container: Container, mode: ScanMode) -> bool:
    """Идёт ли поиск в этом режиме прямо сейчас.

    Правило касается только торгового прохода: ``ur`` и ``fest`` ничего
    не исполняют, и торговый выключатель к ним отношения не имеет
    (``the_main_rules.md``, правило 11 — режимы независимы).

    Когда торговля **запрещена конфигурацией**, проход остаётся сухим:
    он ищет и записывает находки, не тратя денег. Это прямо разрешено
    правилом 11 — «можно искать, не торгуя», — и ломать его нельзя.

    Когда торговля **разрешена конфигурацией**, поиск следует за
    выключателем оператора: остановив торговлю, он останавливает и
    поиск. Иначе система продолжала бы опрашивать агрегаторов ради
    находок, которые заведомо никуда не пойдут.
    """
    if mode is not ScanMode.ANN:
        return True
    switch = container.trading
    if switch is None or not switch.allowed:
        return True
    return switch.is_open


async def _budget_allows(container: Container, mode: ScanMode) -> bool:
    """Есть ли на что торговать прямо сейчас.

    Пока весь остаток заперт в непроданной позиции, любая находка всё
    равно будет отброшена по остатку: исполнитель проверяет средства
    перед покупкой и до неё не доходит. Проход в этом положении тратит
    квоту агрегаторов на заведомо неисполнимое, поэтому он не делается.

    Сухой проход это не касается. Когда торговля **запрещена
    конфигурацией**, деньги не тратятся вовсе, и правило 11 прямо
    разрешает «искать, не торгуя»: остаток счёта такому проходу не указ.

    Стоимость самой проверки — один запрос остатка у узла сети и одно
    обращение к хранилищу сделок. К агрегаторам она не ходит.
    """
    if mode is not ScanMode.ANN:
        return True
    switch = container.trading
    executor = container.executor
    if switch is None or not switch.allowed or executor is None:
        return True
    scopes = container.level1.scopes(mode)
    if not scopes:
        # Проходу и так нечего делать; причину назовёт проверка
        # работающих провайдеров, и подменять её остатком незачем.
        return True
    for scope in scopes:
        for network_id in scope.networks:
            if await executor.has_room(network_id, scope.raw_amounts):
                return True
    return False


def _scan_task(container: Container, mode: ScanMode) -> TaskHandler:
    """Проход Level 1 в заданном режиме.

    После прохода обновляется отметка времени: она нужна команде
    ``/status``. Само состояние подсистемы при этом не меняется, поэтому
    успешные проходы не порождают уведомлений.
    """

    async def run() -> None:
        if not container.control.is_running:
            # Оператор остановил сканирование: новые циклы не начинаются,
            # но уже принятые проверки Level 2 доводятся до конца.
            _LOGGER.info(
                "scan skipped",
                extra=log_fields(mode=mode.value, state=container.control.state().value),
            )
            return
        if not _search_allowed(container, mode):
            # Торговый проход следует за выключателем торговли: оператор,
            # остановивший торговлю, остановил и поиск. Искать то, что
            # заведомо не будет исполнено, значит тратить квоту
            # агрегаторов и держать их под нагрузкой впустую.
            _LOGGER.info(
                "scan skipped: trading is stopped",
                extra=log_fields(mode=mode.value),
            )
            return
        if not await _budget_allows(container, mode):
            # Весь свободный остаток заперт в открытых сделках. Искать
            # то, что заведомо не купить, значит тратить квоту
            # агрегаторов впустую.
            _LOGGER.info(
                "scan skipped: the balance is committed to open trades",
                extra=log_fields(mode=mode.value),
            )
            return
        if not container.level1.has_active_providers():
            # Все агрегаторы вне своих часов работы. Это решение
            # оператора, а не сбой: проход просто не нужен.
            _LOGGER.info(
                "scan skipped: no provider is within its working hours",
                extra=log_fields(mode=mode.value),
            )
            return
        results = await container.level1.scan_all(mode)
        if not results:
            # Ни одна сеть не дала завершённого цикла: состояние подсистемы
            # не обновляется, причина уже записана в журнал.
            return
        if mode is ScanMode.ANN and container.findings is not None:
            # Находки торгового режима потребляет подсистема исполнения, а
            # не Level 2, и вызывается она **не отсюда**: проход только
            # кладёт найденное в очередь и заканчивается
            # (``the_main_rules.md``, правила 11 и 27). Пока это был один
            # вызов, срок прохода был и сроком сделки, и 24.09 все три
            # находки погибли от истечения срока.
            container.findings.offer(results)
        container.health.set_component(
            "level1",
            ApplicationHealthStatus.HEALTHY,
            reason=f"последний цикл {container.clock.now().isoformat(timespec='seconds')}",
        )

    return run


def _trade_task(container: Container) -> TaskHandler:
    """Решение по находкам прохода.

    Отдельная задача со своим сроком. Она не делит время с поиском, и
    поиск её не прерывает: начатую сделку останавливать нельзя — деньги
    уже в пути (``the_main_rules.md``, правило 27).

    Очередь проверяется часто, но пустая проверка ничего не стоит: она
    целиком в памяти. Пока идёт сделка, следующие такты пропускаются
    политикой наложения — задача не перебивает сама себя.
    """

    async def run() -> None:
        executor = container.executor
        queue = container.findings
        if executor is None or queue is None:
            return
        if container.trading is not None and not container.trading.is_open:
            # Торговля остановлена оператором. Находки при этом не
            # копятся: очередь держит только последнюю пачку.
            return
        results = queue.take()
        if not results:
            return
        await executor.consider(results)

    return run


def _trade_report_task(container: Container) -> TaskHandler:
    """Периодический отчёт оператору о работе торгового прохода."""

    async def run() -> None:
        reporter = container.reporter
        if reporter is None:
            return
        await reporter.tick()

    return run


def _positions_task(container: Container) -> TaskHandler:
    """Ведение открытых сделок режима ``ann``.

    Работает независимо от разрешения открывать новые сделки: купленный
    токен нужно довести до продажи в любом случае.
    """

    async def run() -> None:
        watcher = container.watcher
        if watcher is None:
            return
        for notice in await watcher.tick():
            notifier = container.system_notifier
            if notifier is not None:
                # Кнопка выхода по текущей цене: система сама в убыток
                # не закрывает, но сообщить о зависшей сделке и не дать
                # способа её закрыть — половина дела
                # (``the_main_rules.md``, правила 11 и 22).
                await notifier.notify_operator(
                    notice.describe(), buttons=sell_buttons(notice.position.t_id)
                )
            await watcher.mark_notified(notice.position)

    return run


def _notification_task(container: Container) -> TaskHandler:
    """Доставка уведомлений и фиксация её итога.

    Диспетчер отвечает за отправку и называет возможности, по которым
    вопрос доставки закрыт. Перевод возможности в notification-статус
    делает владелец её жизненного цикла: система доставки о статусах
    возможности не знает (``35_STATE_MACHINES.md`` §62-64).
    """

    async def run() -> None:
        report = await container.notifications.dispatch_pending()
        for opportunity_id in dict.fromkeys(report.settled):
            await container.opportunities.settle_delivery(opportunity_id)

    return run


def _backup_task(container: Container) -> TaskHandler:
    """Резервное копирование базы.

    Сбой копирования не останавливает сканер: обслуживающая задача имеет
    самый низкий приоритет и не блокирует работу (``05`` §20).
    """

    async def run() -> None:
        if container.backups is not None:
            await container.backups.run()

    return run


def _system_health_task(container: Container) -> TaskHandler:
    """Операционные уведомления об изменениях состояния.

    Дополнительных запросов к провайдерам задача не делает: она читает
    снимок Health Monitoring, который наполняется исходами обычных
    обращений (``19_HEALTH_MONITORING.md`` §43-44).
    """

    async def run() -> None:
        if container.system_notifier is not None:
            await container.system_notifier.notify_health(container.health.application_health())

    return run


def _command_task(container: Container) -> TaskHandler:
    async def run() -> None:
        if container.commands is not None:
            await container.commands.poll_once()

    return run
