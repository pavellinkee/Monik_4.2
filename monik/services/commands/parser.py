"""Разбор входящих команд Telegram.

Набор команд описан в ``docs/telegram_commands.md``. Помимо команд
``CLAUDE.md`` §36 (``/details``, ``/level2``, ``/status``, ``/stats``)
поддерживается управление сканером и просмотр состояния агрегаторов.

Некорректный ввод не приводит к ошибке подсистемы: он превращается в
явный результат разбора, который обработчик показывает пользователю.

Нажатия кнопок разбираются здесь же: кнопка выполняет то же действие, что
и команда, поэтому второй модели ввода не создаётся.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "DESTRUCTIVE_COMMANDS",
    "CommandName",
    "ParsedCallback",
    "ParsedCommand",
    "action_callback_data",
    "confirm_callback_data",
    "details_callback_data",
    "parse_callback",
    "parse_command",
    "provider_callback_data",
    "sell_callback_data",
    "sell_confirm_callback_data",
]

#: Префикс callback-данных кнопки ``об``.
DETAILS_CALLBACK_PREFIX = "details"

#: Префикс кнопки, повторяющей обычную команду.
ACTION_CALLBACK_PREFIX = "do"

#: Префикс кнопки подтверждения опасного действия.
CONFIRM_CALLBACK_PREFIX = "confirm"

#: Префикс кнопки выбора конкретного агрегатора.
PROVIDER_CALLBACK_PREFIX = "provider"

#: Префикс кнопки «продать по текущей цене». Несёт номер сделки:
#: уведомлений о затянувшемся ожидании может висеть несколько, и кнопка
#: обязана относиться к своей.
SELL_CALLBACK_PREFIX = "sell"

#: Префикс подтверждения этой продажи. Отдельный от общего
#: ``confirm``: тот подтверждает команду без аргумента, а здесь
#: подтверждается действие над конкретной сделкой.
SELL_CONFIRM_CALLBACK_PREFIX = "sellok"


class CommandName(StrEnum):
    """Поддерживаемая команда."""

    #: Показать меню кнопок.
    MENU = "menu"
    #: Список команд с описанием.
    HELP = "help"
    #: Разрешить сканирование.
    START_SCANNER = "start"
    #: Запретить новые циклы сканирования.
    STOP_SCANNER = "stop"
    #: Перезапустить процесс.
    RESTART = "restart"
    #: Разрешить подсистеме исполнения открывать сделки. Ведение уже
    #: открытых от этой команды не зависит.
    START_TRADING = "trade_on"
    #: Запретить открытие новых сделок. Купленное продолжает вестись.
    STOP_TRADING = "trade_off"
    #: Сводное состояние приложения.
    STATUS = "status"
    #: Состояние агрегаторов и их очередей.
    PROVIDERS = "providers"
    #: Последние циклы Level 1.
    SCANS = "scans"
    #: Активные проверки Level 2.
    LEVEL2 = "level2"
    #: Сохранённый результат проверки по ``#K``.
    DETAILS = "details"
    #: Накопленная статистика.
    STATS = "stats"
    #: Состояние резервного копирования.
    BACKUP = "backup"
    #: Установить обновления системы и перезапустить приложение.
    SYSTEM_UPDATE = "update"
    #: Нераспознанная команда.
    UNKNOWN = "unknown"


#: Команды без обязательного аргумента.
_SIMPLE_COMMANDS = (
    CommandName.MENU,
    CommandName.HELP,
    CommandName.START_SCANNER,
    CommandName.STOP_SCANNER,
    CommandName.RESTART,
    CommandName.START_TRADING,
    CommandName.STOP_TRADING,
    CommandName.STATUS,
    CommandName.SCANS,
    CommandName.LEVEL2,
    CommandName.STATS,
    CommandName.BACKUP,
    CommandName.SYSTEM_UPDATE,
)

#: Действия, прерывающие работу сканера. Выполняются только после явного
#: подтверждения (``docs/telegram_commands.md``).
DESTRUCTIVE_COMMANDS = frozenset(
    {
        CommandName.STOP_SCANNER,
        CommandName.RESTART,
        CommandName.SYSTEM_UPDATE,
        # Разрешение тратить деньги подтверждается отдельно.
        CommandName.START_TRADING,
    }
)


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """Результат разбора текста команды."""

    name: CommandName
    argument: str | None = None
    error: str | None = None

    @property
    def is_valid(self) -> bool:
        """Распознана ли команда полностью."""
        return self.name is not CommandName.UNKNOWN and self.error is None


@dataclass(frozen=True, slots=True)
class ParsedCallback:
    """Результат разбора нажатия кнопки.

    Кнопка либо открывает сохранённое уведомление (``details``), либо
    выполняет команду — в том числе подтверждённую.
    """

    notification_id: str | None = None
    command: ParsedCommand | None = None
    confirmed: bool = False
    #: Номер сделки, которую оператор хочет продать по текущей цене.
    position_id: str | None = None

    @property
    def is_known(self) -> bool:
        """Распознано ли нажатие."""
        return (
            self.notification_id is not None
            or self.command is not None
            or self.position_id is not None
        )


def details_callback_data(notification_id: str) -> str:
    """Данные кнопки ``об`` для конкретного уведомления."""
    return f"{DETAILS_CALLBACK_PREFIX}:{notification_id}"


def sell_callback_data(t_id: str) -> str:
    """Данные кнопки «продать по текущей цене»."""
    return f"{SELL_CALLBACK_PREFIX}:{t_id}"


def sell_confirm_callback_data(t_id: str) -> str:
    """Данные кнопки, подтверждающей продажу конкретной сделки."""
    return f"{SELL_CONFIRM_CALLBACK_PREFIX}:{t_id}"


def action_callback_data(command: CommandName) -> str:
    """Данные кнопки, повторяющей команду."""
    return f"{ACTION_CALLBACK_PREFIX}:{command.value}"


def confirm_callback_data(command: CommandName) -> str:
    """Данные кнопки подтверждения опасного действия."""
    return f"{CONFIRM_CALLBACK_PREFIX}:{command.value}"


def provider_callback_data(provider: str) -> str:
    """Данные кнопки выбора агрегатора."""
    return f"{PROVIDER_CALLBACK_PREFIX}:{provider}"


def parse_command(text: str) -> ParsedCommand:
    """Разобрать текст сообщения.

    Бот может получать команду с суффиксом ``@botname``: он отбрасывается,
    потому что не является частью имени команды.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return ParsedCommand(name=CommandName.UNKNOWN, error="not a command")
    parts = stripped.split()
    raw_name = parts[0][1:].split("@", maxsplit=1)[0].lower()
    argument = parts[1] if len(parts) > 1 else None

    if raw_name == CommandName.DETAILS.value:
        if argument is None:
            return ParsedCommand(
                name=CommandName.DETAILS, error="команда /details требует идентификатор K"
            )
        return ParsedCommand(name=CommandName.DETAILS, argument=argument)
    if raw_name == CommandName.PROVIDERS.value:
        # Аргумент необязателен: без него показываются все агрегаторы.
        return ParsedCommand(name=CommandName.PROVIDERS, argument=argument)
    for command in _SIMPLE_COMMANDS:
        if raw_name == command.value:
            return ParsedCommand(name=command)
    return ParsedCommand(name=CommandName.UNKNOWN, error=f"неизвестная команда /{raw_name}")


def parse_callback(data: str) -> ParsedCallback:
    """Разобрать данные нажатой кнопки."""
    prefix, _, payload = data.partition(":")
    payload = payload.strip()
    if not payload:
        return ParsedCallback()
    if prefix == DETAILS_CALLBACK_PREFIX:
        return ParsedCallback(notification_id=payload)
    if prefix in {SELL_CALLBACK_PREFIX, SELL_CONFIRM_CALLBACK_PREFIX}:
        return ParsedCallback(
            position_id=payload,
            confirmed=prefix == SELL_CONFIRM_CALLBACK_PREFIX,
        )
    if prefix == PROVIDER_CALLBACK_PREFIX:
        return ParsedCallback(command=ParsedCommand(name=CommandName.PROVIDERS, argument=payload))
    if prefix in {ACTION_CALLBACK_PREFIX, CONFIRM_CALLBACK_PREFIX}:
        command = _command_by_value(payload)
        if command is None:
            return ParsedCallback()
        return ParsedCallback(
            command=ParsedCommand(name=command),
            confirmed=prefix == CONFIRM_CALLBACK_PREFIX,
        )
    return ParsedCallback()


def _command_by_value(value: str) -> CommandName | None:
    for command in CommandName:
        if command is not CommandName.UNKNOWN and command.value == value:
            return command
    return None
