"""Обработчики команд Telegram.

Ответ формируется **только** из сохранённых данных и уже собранных
снимков (``CLAUDE.md`` §35): обработчик не обращается к провайдерам
котировок и не выполняет расчётов.

Управление сканером — единственное исключение из «только чтение»: оно
выполняется через узкий порт :class:`ScannerControl`, который лишь
сообщает намерение оператора. Планировщика и сканеров подсистема команд
по-прежнему не знает.

Действия, прерывающие работу сканера, требуют явного подтверждения:
случайное нажатие не должно останавливать production.

Секреты в ответы не попадают: показывается только операционное состояние
(``19_HEALTH_MONITORING.md`` §65).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from monik.domain.enums.control import ScannerRunState
from monik.domain.enums.lifecycle import JobStatus
from monik.domain.errors import DomainValidationError
from monik.domain.models.job import ConfirmationResult, Level2Job
from monik.domain.value_objects.identifiers import KId
from monik.services.commands.parser import (
    DESTRUCTIVE_COMMANDS,
    CommandName,
    ParsedCommand,
    action_callback_data,
    confirm_callback_data,
    parse_callback,
    parse_command,
    provider_callback_data,
    sell_confirm_callback_data,
)
from monik.services.commands.ports import (
    BackupStatusSource,
    JobReader,
    NotificationReader,
    PositionExitSource,
    ProviderStatus,
    ProviderStatusSource,
    ScannerControl,
    ScanReader,
    StatsSource,
    StatusSource,
    TradingControl,
)
from monik.services.notifications.ports import MessageButton
from monik.services.observability.logging import get_logger, log_fields
from monik.services.trading.exits import ExitQuote
from monik.services.updates.ports import SystemUpdater

__all__ = ["COMMAND_HELP", "CommandResponse", "CommandRouter"]

_LOGGER = get_logger("services.commands")

#: Сколько активных Job показывать в ``/level2``.
_ACTIVE_JOB_LIMIT = 20

#: Сколько последних циклов показывать в ``/scans``.
_RECENT_SCAN_LIMIT = 5

#: Статусы, считающиеся активными для ``/level2``.
_ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)

#: Назначение каждой команды. Используется в ``/help`` и в
#: ``docs/telegram_commands.md``: описание живёт в одном месте.
COMMAND_HELP: tuple[tuple[CommandName, str], ...] = (
    (CommandName.MENU, "показать кнопки управления"),
    (CommandName.HELP, "список команд"),
    (CommandName.STATUS, "состояние приложения целиком"),
    (CommandName.PROVIDERS, "состояние агрегаторов и их очередей"),
    (CommandName.SCANS, "последние циклы Level 1"),
    (CommandName.LEVEL2, "активные проверки Level 2"),
    (CommandName.DETAILS, "результат проверки по идентификатору, например /details K1234"),
    (CommandName.STATS, "накопленная статистика с момента запуска"),
    (CommandName.BACKUP, "состояние резервного копирования"),
    (CommandName.START_SCANNER, "разрешить сканирование"),
    (CommandName.STOP_SCANNER, "остановить сканирование (с подтверждением)"),
    (CommandName.START_TRADING, "разрешить сделки режима ann (с подтверждением)"),
    (CommandName.STOP_TRADING, "запретить новые сделки; открытые доводятся"),
    (CommandName.RESTART, "перезапустить приложение (с подтверждением)"),
    (
        CommandName.SYSTEM_UPDATE,
        "установить обновления системы и перезапустить приложение (с подтверждением)",
    ),
)

#: Подписи кнопок меню.
_BUTTON_LABELS: dict[CommandName, str] = {
    CommandName.START_SCANNER: "▶️ Запустить",
    CommandName.STOP_SCANNER: "⏸ Остановить",
    CommandName.RESTART: "🔄 Перезапустить",
    CommandName.START_TRADING: "💰 Запустить торговлю",
    CommandName.STOP_TRADING: "🚫 Остановить торговлю",
    CommandName.STATUS: "📊 Статус",
    CommandName.PROVIDERS: "🔌 Статус агрегатора",
    CommandName.STATS: "📈 Статистика",
    CommandName.LEVEL2: "🧪 Level 2",
    CommandName.SCANS: "🕔 Последние сканы",
    CommandName.BACKUP: "💾 Резервные копии",
    CommandName.HELP: "❓ Помощь",
    CommandName.SYSTEM_UPDATE: "⬇️ Обновить и перезапустить",
}

#: Человекочитаемое состояние сканера.
_RUN_STATE_LABELS: dict[ScannerRunState, str] = {
    ScannerRunState.RUNNING: "сканирование идёт",
    ScannerRunState.PAUSED: "сканирование остановлено оператором",
    ScannerRunState.RESTARTING: "запрошен перезапуск",
}

#: Что именно подтверждает пользователь.
_CONFIRMATION_PROMPTS: dict[CommandName, str] = {
    CommandName.STOP_SCANNER: (
        "Остановить сканирование?\n"
        "Новые циклы Level 1 запускаться не будут. "
        "Уже принятые проверки Level 2 завершатся."
    ),
    CommandName.RESTART: (
        "Перезапустить приложение?\nТекущий цикл будет корректно завершён, процесс перезапустится."
    ),
    CommandName.START_TRADING: (
        "Разрешить режиму ann совершать сделки?\n"
        "С этого момента найденная возможность приведёт к настоящей покупке "
        "на средства торгового счёта. Уже открытые сделки ведутся всегда, "
        "независимо от этого разрешения."
    ),
    CommandName.SYSTEM_UPDATE: (
        "Установить обновления системы и перезапустить Monik?\n"
        "Установка займёт несколько минут, всё это время сканер не отвечает на команды. "
        "После установки приложение перезапустится само."
    ),
}


@dataclass(frozen=True, slots=True)
class CommandResponse:
    """Текст ответа, кнопки и признак успешной обработки."""

    text: str
    handled: bool = True
    buttons: tuple[tuple[MessageButton, ...], ...] = field(default=())


class CommandRouter:
    """Маршрутизирует команды и нажатия кнопок."""

    def __init__(
        self,
        *,
        jobs: JobReader,
        notifications: NotificationReader,
        status: StatusSource,
        stats: StatsSource,
        providers: ProviderStatusSource | None = None,
        scans: ScanReader | None = None,
        control: ScannerControl | None = None,
        trading: TradingControl | None = None,
        backups: BackupStatusSource | None = None,
        updater: SystemUpdater | None = None,
        exits: PositionExitSource | None = None,
        application: str | None = None,
        environment: str | None = None,
    ) -> None:
        self._jobs = jobs
        self._notifications = notifications
        self._status = status
        self._stats = stats
        self._providers = providers
        self._scans = scans
        self._control = control
        self._trading = trading
        self._backups = backups
        self._updater = updater
        self._exits = exits
        self._application = application
        self._environment = environment

    async def handle_text(self, text: str) -> CommandResponse:
        """Обработать текстовую команду."""
        command = parse_command(text)
        if command.error is not None:
            return CommandResponse(text=command.error, handled=False)
        if command.name in DESTRUCTIVE_COMMANDS:
            # Текстовая команда опасного действия сама его не выполняет.
            return self._confirmation_request(command.name)
        return await self._dispatch(command)

    async def handle_callback(self, data: str) -> CommandResponse:
        """Обработать нажатие кнопки.

        Текст ``об`` берётся из сохранённого уведомления: новых внешних
        запросов не выполняется (``CLAUDE.md`` §35).
        """
        callback = parse_callback(data)
        if callback.position_id is not None:
            return await self._sell_response(callback.position_id, confirmed=callback.confirmed)
        if callback.notification_id is not None:
            _, details = await self._notifications.load_texts(callback.notification_id)
            if details is None:
                return CommandResponse(text="детали недоступны", handled=False)
            return CommandResponse(text=details)
        if callback.command is None:
            return CommandResponse(text="неизвестное действие", handled=False)
        if callback.command.name in DESTRUCTIVE_COMMANDS and not callback.confirmed:
            return self._confirmation_request(callback.command.name)
        return await self._dispatch(callback.command)

    # --- обработчики ------------------------------------------------------

    async def _dispatch(self, command: ParsedCommand) -> CommandResponse:
        if command.name is CommandName.DETAILS:
            return await self._details(command.argument or "")
        if command.name is CommandName.LEVEL2:
            return await self._level2()
        if command.name is CommandName.STATUS:
            return self._status_response()
        if command.name is CommandName.STATS:
            return self._stats_response()
        if command.name is CommandName.MENU:
            return self._menu_response()
        if command.name is CommandName.HELP:
            return self._help_response()
        if command.name is CommandName.PROVIDERS:
            return self._providers_response(command.argument)
        if command.name is CommandName.SCANS:
            return await self._scans_response()
        if command.name is CommandName.BACKUP:
            return await self._backup_response()
        if command.name is CommandName.SYSTEM_UPDATE:
            return await self._system_update_response()
        if command.name in {CommandName.START_TRADING, CommandName.STOP_TRADING}:
            return self._trading_response(command.name)
        if command.name in {
            CommandName.START_SCANNER,
            CommandName.STOP_SCANNER,
            CommandName.RESTART,
        }:
            return self._control_response(command.name)
        return CommandResponse(text="неизвестная команда", handled=False)

    # --- управление -------------------------------------------------------

    async def _sell_response(self, t_id: str, *, confirmed: bool) -> CommandResponse:
        """Продажа зависшей сделки по текущей цене.

        Два шага: сначала показать, по чём продадут, и спросить, потом
        продать. Одним шагом нельзя — деньги тратятся по нажатию, и
        промах пальцем не должен стоить сделки.

        Цена перед продажей спрашивается заново: между подтверждением и
        продажей проходят секунды, и уйти сделка должна по тому, что
        агрегатор обязуется отдать **сейчас**, а не по показанному.
        """
        if self._exits is None:
            return CommandResponse(text="продажа по команде недоступна", handled=False)
        if not confirmed:
            offer = await self._exits.preview_exit(t_id)
            if offer is None:
                return CommandResponse(text=f"{t_id}: продать сейчас не у кого", handled=False)
            return CommandResponse(
                text=sell_prompt(offer),
                buttons=(
                    (
                        MessageButton(
                            label="✅ Да", callback_data=sell_confirm_callback_data(t_id)
                        ),
                        MessageButton(
                            label="❌ Нет", callback_data=action_callback_data(CommandName.MENU)
                        ),
                    ),
                ),
            )
        sold = await self._exits.sell_at_market(t_id)
        if sold is None:
            # Сообщить об успехе выхода, которого не было, хуже, чем
            # сообщить о неудаче: оператор ушёл бы, считая деньги
            # свободными.
            _LOGGER.warning("manual exit failed", extra=log_fields(t_id=t_id))
            return CommandResponse(
                text=(
                    f"{t_id}: продать не удалось — предложение не исполнилось.\n"
                    "Сделка осталась в ожидании, причина в журнале."
                ),
                handled=False,
            )
        return CommandResponse(text=sold_text(sold))

    def _confirmation_request(self, command: CommandName) -> CommandResponse:
        """Спросить подтверждение перед прерыванием работы."""
        return CommandResponse(
            text=_CONFIRMATION_PROMPTS[command],
            buttons=(
                (
                    MessageButton(label="✅ Да", callback_data=confirm_callback_data(command)),
                    MessageButton(
                        label="↩️ Отмена", callback_data=action_callback_data(CommandName.MENU)
                    ),
                ),
            ),
        )

    async def _system_update_response(self) -> CommandResponse:
        """Установить обновления системы и перезапустить приложение.

        Перезапуск запрашивается только после удачной установки: если
        поставить не удалось, перезапуск ничего не изменит, а сканер
        потеряет текущий цикл напрасно.

        Ответ формируется до запроса перезапуска, поэтому оператор
        узнаёт результат даже тогда, когда процесс завершится сразу.
        """
        if self._updater is None:
            return CommandResponse(text="обновление системы недоступно", handled=False)
        result = await self._updater.apply()
        if not result.applied:
            _LOGGER.warning(
                "system update command failed",
                extra=log_fields(detail=result.detail),
            )
            return CommandResponse(
                text=f"⚠️ Обновить не удалось: {result.detail}",
                buttons=self._menu_buttons(),
            )
        packages = "" if result.packages is None else f" Обновлено пакетов: {result.packages}."
        _LOGGER.warning(
            "system update command executed",
            extra=log_fields(operation=CommandName.SYSTEM_UPDATE.value, packages=result.packages),
        )
        if self._control is None:
            return CommandResponse(
                text=f"✅ Обновления установлены.{packages} Перезапустите приложение вручную.",
                buttons=self._menu_buttons(),
            )
        self._control.request_restart()
        return CommandResponse(
            text=f"✅ Обновления установлены.{packages} Приложение перезапускается.",
        )

    def _trading_response(self, command: CommandName) -> CommandResponse:
        """Разрешить или запретить открытие сделок.

        Запрет касается только новых сделок: уже купленное доводится до
        продажи в любом случае, иначе остановка бросала бы деньги.
        """
        switch = self._trading
        if switch is None:
            return CommandResponse(text="торговая подсистема не собрана", handled=False)
        if not switch.allowed:
            return CommandResponse(
                text="Торговля запрещена конфигурацией (trading.execution_enabled).",
                buttons=self._menu_buttons(),
            )
        if command is CommandName.START_TRADING:
            changed = switch.start()
            text = (
                "💰 Торговля запущена. Следующая найденная возможность будет исполнена."
                if changed
                else "Торговля уже идёт"
            )
        else:
            changed = switch.stop()
            text = (
                "🚫 Новые сделки не открываются. Открытые доводятся до продажи."
                if changed
                else "Торговля и так не запущена"
            )
        _LOGGER.warning(
            "trading control command executed",
            extra=log_fields(operation=command.value, state=switch.state()),
        )
        return CommandResponse(text=text, buttons=self._menu_buttons())

    def _control_response(self, command: CommandName) -> CommandResponse:
        """Выполнить подтверждённое управляющее действие."""
        if self._control is None:
            return CommandResponse(text="управление сканером недоступно", handled=False)
        if command is CommandName.START_SCANNER:
            changed = self._control.start()
            text = "▶️ Сканирование запущено" if changed else "Сканирование уже идёт"
        elif command is CommandName.STOP_SCANNER:
            changed = self._control.stop()
            text = (
                "⏸ Сканирование остановлено. Принятые проверки Level 2 завершатся."
                if changed
                else "Сканирование уже остановлено"
            )
        else:
            self._control.request_restart()
            text = "🔄 Перезапуск запрошен. Приложение поднимется автоматически."
        _LOGGER.warning(
            "scanner control command executed",
            extra=log_fields(operation=command.value, state=self._control.state().value),
        )
        return CommandResponse(text=text, buttons=self._menu_buttons())

    # --- информация -------------------------------------------------------

    def _menu_response(self) -> CommandResponse:
        state = self._control.state() if self._control is not None else None
        header = "Управление Monik"
        if state is not None:
            header = f"{header}\n{_RUN_STATE_LABELS[state]}"
        return CommandResponse(text=header, buttons=self._menu_buttons())

    def _menu_buttons(self) -> tuple[tuple[MessageButton, ...], ...]:
        """Кнопки меню. Управляющие показываются только при наличии порта."""
        rows: list[tuple[MessageButton, ...]] = []
        if self._control is not None:
            rows.append(
                tuple(
                    _button(command)
                    for command in (
                        CommandName.START_SCANNER,
                        CommandName.STOP_SCANNER,
                        CommandName.RESTART,
                    )
                )
            )
        rows.append((_button(CommandName.STATUS), _button(CommandName.PROVIDERS)))
        rows.append((_button(CommandName.STATS), _button(CommandName.LEVEL2)))
        tail = [_button(CommandName.SCANS)]
        if self._backups is not None:
            tail.append(_button(CommandName.BACKUP))
        tail.append(_button(CommandName.HELP))
        rows.append(tuple(tail))
        return tuple(rows)

    def _help_response(self) -> CommandResponse:
        lines = ["Команды Monik:"]
        lines.extend(f"/{command.value} — {purpose}" for command, purpose in COMMAND_HELP)
        lines.append("")
        lines.append("Остановка, перезапуск и обновление системы запрашивают подтверждение.")
        return CommandResponse(text="\n".join(lines), buttons=self._menu_buttons())

    async def _details(self, raw_k_id: str) -> CommandResponse:
        """``/details K1234`` — сохранённый результат проверки."""
        try:
            k_id = KId(raw_k_id)
        except (ValueError, DomainValidationError):
            return CommandResponse(text=f"некорректный идентификатор: {raw_k_id}", handled=False)

        job = await self._jobs.get(k_id)
        if job is None:
            return CommandResponse(text=f"{k_id} не найден", handled=False)
        result = await self._jobs.load_confirmation(k_id, job.attempt_count)
        if result is None:
            return CommandResponse(
                text=f"{k_id}: статус {job.status.value}, результат проверки ещё не сохранён"
            )
        return CommandResponse(text=_details_text(result, job.status))

    async def _level2(self) -> CommandResponse:
        """``/level2`` — активные Job'ы."""
        active: list[Level2Job] = []
        for status in _ACTIVE_STATUSES:
            active.extend(await self._jobs.list_by_status(status, limit=_ACTIVE_JOB_LIMIT))
        if not active:
            return CommandResponse(text="активных Level 2 задач нет")
        lines = ["Активные Level 2 задачи:"]
        lines.extend(
            f"{job.k_id} · {job.status.value} · попыток {job.attempt_count}"
            for job in sorted(active, key=lambda item: item.created_at)
        )
        return CommandResponse(text="\n".join(lines))

    def _status_response(self) -> CommandResponse:
        """``/status`` — состояние приложения."""
        components = self._status.components()
        if not components:
            return CommandResponse(text="состояние подсистем недоступно", handled=False)
        lines = ["Состояние Monik"]
        if self._application:
            lines.append(f"Версия: {self._application}")
        if self._environment:
            lines.append(f"Окружение: {self._environment}")
        if self._control is not None:
            lines.append(f"Сканер: {_RUN_STATE_LABELS[self._control.state()]}")
        lines.append("")
        lines.append("Подсистемы:")
        lines.extend(
            f"{item.name}: {item.state}" + (f" ({item.detail})" if item.detail else "")
            for item in components
        )
        if self._providers is not None:
            queues = self._providers.providers()
            if queues:
                lines.append("")
                lines.append("Очереди агрегаторов:")
                lines.extend(_provider_line(item) for item in queues)
        return CommandResponse(text="\n".join(lines), buttons=self._menu_buttons())

    def _providers_response(self, requested: str | None) -> CommandResponse:
        """``/providers`` — состояние агрегаторов, при аргументе — одного."""
        if self._providers is None:
            return CommandResponse(text="состояние агрегаторов недоступно", handled=False)
        statuses = self._providers.providers()
        if not statuses:
            return CommandResponse(text="агрегаторы не настроены", handled=False)
        if requested is not None:
            wanted = requested.strip().lower()
            selected = [item for item in statuses if item.provider == wanted]
            if not selected:
                names = ", ".join(item.provider for item in statuses)
                return CommandResponse(
                    text=f"неизвестный агрегатор: {requested}. Доступны: {names}",
                    handled=False,
                )
            return CommandResponse(text=_provider_details(selected[0]))
        lines = ["Состояние агрегаторов:"]
        lines.extend(_provider_line(item) for item in statuses)
        return CommandResponse(
            text="\n".join(lines),
            buttons=(
                tuple(
                    MessageButton(
                        label=item.provider, callback_data=provider_callback_data(item.provider)
                    )
                    for item in statuses
                ),
            ),
        )

    async def _scans_response(self) -> CommandResponse:
        """``/scans`` — последние циклы Level 1."""
        if self._scans is None:
            return CommandResponse(text="история циклов недоступна", handled=False)
        scans = await self._scans.recent(limit=_RECENT_SCAN_LIMIT)
        if not scans:
            return CommandResponse(text="циклов пока не было")
        lines = ["Последние циклы Level 1:"]
        for scan in scans:
            finished = (
                scan.finished_at.isoformat(timespec="seconds")
                if scan.finished_at
                else "выполняется"
            )
            statistics = scan.statistics
            lines.append(
                f"{finished} · {scan.status.value} · "
                f"запросов {statistics.quote_requests} · "
                f"успешно {statistics.successful_quotes} · "
                f"возможностей {statistics.opportunities_created}"
            )
        return CommandResponse(text="\n".join(lines))

    async def _backup_response(self) -> CommandResponse:
        """``/backup`` — состояние резервного копирования."""
        if self._backups is None:
            return CommandResponse(text="резервное копирование не настроено", handled=False)
        status = await self._backups.status()
        if not status.enabled:
            return CommandResponse(text="резервное копирование выключено")
        lines = ["Резервное копирование:", f"Копий сохранено: {status.copies}"]
        if status.last_run_at:
            lines.append(f"Последняя копия: {status.last_run_at}")
        else:
            lines.append("Последняя копия: ещё не создавалась")
        if status.last_outcome:
            lines.append(f"Результат: {status.last_outcome}")
        if status.detail:
            lines.append(status.detail)
        return CommandResponse(text="\n".join(lines))

    def _stats_response(self) -> CommandResponse:
        """``/stats`` — накопленная статистика."""
        snapshot = self._stats.snapshot()
        rate = snapshot.confirmations.confirmation_rate
        _LOGGER.info("stats requested", extra=log_fields(decided=snapshot.confirmations.decided))
        lines = [
            "Статистика с момента запуска:",
            f"Циклов Level 1: {snapshot.scans_completed}",
            f"Возможностей создано: {snapshot.opportunities_created}",
            f"Уведомлений отправлено: {snapshot.notifications_sent}",
            f"Подтверждено сумм: {snapshot.confirmations.confirmed}",
            f"Не подтверждено сумм: {snapshot.confirmations.unconfirmed}",
            f"Неопределённых сумм: {snapshot.confirmations.partial}",
            # PARTIAL исключается из расчёта; отсутствие решений даёт N/A
            # (``CLAUDE.md`` §27).
            f"Confirmation rate: {'N/A' if rate is None else f'{rate:.2f}%'}",
        ]
        return CommandResponse(text="\n".join(lines))


def _button(command: CommandName) -> MessageButton:
    return MessageButton(label=_BUTTON_LABELS[command], callback_data=action_callback_data(command))


def _provider_line(item: ProviderStatus) -> str:
    """Одна строка состояния агрегатора.

    Часы работы показываются, только если они заданы: иначе молчащий по
    расписанию агрегатор выглядит как неисправный.
    """
    line = (
        f"{item.provider}: {item.health}"
        f" · очередь {item.active}/{item.max_concurrent}"
        f" (ожидают {item.waiting})"
        f" · {item.requests_per_second} зап/с"
    )
    if item.schedule is not None:
        mark = "работает" if item.within_schedule else "перерыв"
        line = f"{line} · {item.schedule} ({mark})"
    return line


def _provider_details(item: ProviderStatus) -> str:
    """Подробное состояние одного агрегатора."""
    lines = [
        f"Агрегатор {item.provider}",
        f"Состояние: {item.health}",
        f"Circuit breaker: {item.circuit_state}",
        f"Лимит частоты: {item.requests_per_second} запросов в секунду",
        f"Одновременных запросов: {item.active} из {item.max_concurrent}",
        f"Ожидают в очереди: {item.waiting}",
    ]
    if item.reason:
        lines.append(f"Причина: {item.reason}")
    return "\n".join(lines)


def _details_text(result: ConfirmationResult, job_status: JobStatus) -> str:
    """Текст ответа ``/details`` из сохранённого результата."""
    lines = [
        f"{result.k_id} · {job_status.value} · проверка #{result.revision}",
        f"Итог: {result.job_status.value}",
        f"Подтверждено: {result.confirmed_count} · "
        f"не подтверждено: {result.unconfirmed_count} · "
        f"неопределённых: {result.partial_count}",
    ]
    for amount in result.amount_results:
        line = f"— {amount.input_amount.as_decimal}: {amount.status.value}"
        if amount.rejection_reason:
            line += f" ({amount.rejection_reason})"
        lines.append(line)
    if result.failure_reason:
        lines.append(f"Причина: {result.failure_reason}")
    return "\n".join(lines)


def sell_prompt(offer: ExitQuote) -> str:
    """Вопрос оператору перед продажей по текущей цене.

    Числа — по обязательству агрегатора: человек решает, расставаться ли
    с деньгами, и видеть он должен то, что ему гарантированно отдадут.
    """
    lines = [
        f"{offer.t_id}: продать сейчас за {offer.output} {offer.symbol}?",
        f"Итог сделки: {offer.profit:+} {offer.symbol}",
        f"Продавец: {offer.provider}",
    ]
    if not offer.costs_known:
        # Умолчать нельзя: без газа итог завышен (``CLAUDE.md`` §12).
        lines.append("Стоимость газа неизвестна — итог показан без неё")
    return "\n".join(lines)


def sold_text(offer: ExitQuote) -> str:
    """Чем кончилась продажа по команде оператора."""
    return (
        f"{offer.t_id}: продана за {offer.output} {offer.symbol} "
        f"у {offer.provider}, итог {offer.profit:+} {offer.symbol}"
    )
