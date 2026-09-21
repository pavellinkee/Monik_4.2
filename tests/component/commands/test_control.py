"""Управление Monik из Telegram: меню, кнопки и подтверждения.

Требования — ``docs/telegram_commands.md``. Опасные действия выполняются
только после явного подтверждения, а ответы не содержат секретов
(``19_HEALTH_MONITORING.md`` §65).
"""

from __future__ import annotations

import pathlib
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from monik.app.control import ScannerSwitch
from monik.config.sections.database import DatabaseConfig
from monik.domain.enums.control import ScannerRunState
from monik.domain.enums.lifecycle import ScanStatus
from monik.domain.models.scan import Scan, ScanScope, ScanStatistics
from monik.infrastructure.db import Database, MigrationRunner
from monik.repositories.sqlite import SqliteJobRepository, SqliteNotificationRepository
from monik.services.commands import (
    BackupStatus,
    CommandName,
    CommandRouter,
    ProviderStatus,
    parse_command,
)
from monik.services.commands.parser import (
    action_callback_data,
    confirm_callback_data,
    sell_callback_data,
    sell_confirm_callback_data,
)
from monik.services.trading.exits import ExitQuote
from monik.services.updates.ports import SystemUpdateResult
from tests import factories as f

from .test_commands import StaticStats, StaticStatus

SECRETS = (
    "sk-live-должен-остаться-невидимым",
    "1234567890:AAH-bot-token",
    "0xprivatekey",
)


@pytest.fixture
async def database(tmp_path: pathlib.Path) -> AsyncIterator[Database]:
    instance = Database(DatabaseConfig(path=str(tmp_path / "control.db"), busy_timeout_seconds=1.0))
    await instance.connect()
    await MigrationRunner(instance).upgrade()
    try:
        yield instance
    finally:
        await instance.close()


class StaticProviders:
    """Снимок очередей агрегаторов."""

    def __init__(self, statuses: tuple[ProviderStatus, ...] | None = None) -> None:
        self._statuses = (
            statuses
            if statuses is not None
            else (
                ProviderStatus(
                    provider="zero_x",
                    health="healthy",
                    circuit_state="closed",
                    requests_per_second=4.9,
                    max_concurrent=4,
                    active=1,
                    waiting=0,
                ),
                ProviderStatus(
                    provider="uniswap",
                    health="degraded",
                    circuit_state="open",
                    requests_per_second=5.9,
                    max_concurrent=2,
                    active=0,
                    waiting=3,
                    reason="provider_rate_limited",
                ),
            )
        )

    def providers(self) -> tuple[ProviderStatus, ...]:
        return self._statuses


class StaticScans:
    """Последние циклы Level 1."""

    def __init__(self, scans: tuple[Scan, ...] = ()) -> None:
        self._scans = scans

    async def recent(self, *, limit: int) -> tuple[Scan, ...]:
        return self._scans[:limit]


class StaticBackups:
    def __init__(self, status: BackupStatus | None = None) -> None:
        self._status = status or BackupStatus(
            enabled=True,
            last_run_at="2026-01-03T03:00:00+00:00",
            last_outcome="success",
            copies=8,
        )

    async def status(self) -> BackupStatus:
        return self._status


class StaticUpdater:
    """Установка обновлений с заданным исходом."""

    def __init__(self, *, applied: bool = True, packages: int | None = 2) -> None:
        self._result = SystemUpdateResult(
            applied=applied,
            detail="обновления установлены" if applied else "нет права выполнять установку",
            packages=packages if applied else None,
        )
        self.calls = 0

    async def available(self) -> bool:
        return True

    async def apply(self) -> SystemUpdateResult:
        self.calls += 1
        return self._result


def build_router(database: Database, **overrides: object) -> CommandRouter:
    return CommandRouter(
        jobs=SqliteJobRepository(database),
        notifications=SqliteNotificationRepository(database),
        status=overrides.get("status") or StaticStatus(),  # type: ignore[arg-type]
        stats=overrides.get("stats") or StaticStats(),  # type: ignore[arg-type]
        providers=overrides.get("providers", StaticProviders()),  # type: ignore[arg-type]
        scans=overrides.get("scans", StaticScans()),  # type: ignore[arg-type]
        control=overrides.get("control", ScannerSwitch()),  # type: ignore[arg-type]
        backups=overrides.get("backups", StaticBackups()),  # type: ignore[arg-type]
        updater=overrides.get("updater", StaticUpdater()),  # type: ignore[arg-type]
        exits=overrides.get("exits"),  # type: ignore[arg-type]
        application=overrides.get("application", "Monik 3.2.0"),  # type: ignore[arg-type]
        environment=overrides.get("environment", "production"),  # type: ignore[arg-type]
    )


def _finished_scan() -> Scan:
    return Scan(
        scan_id=f.ScanId("33333333-3333-4333-8333-333333333333"),
        status=ScanStatus.COMPLETE,
        scope=ScanScope(
            networks=(f.POLYGON,),
            providers=(f.ProviderId.ZERO_X,),
            tokens=(f.AAVE.key,),
            raw_amounts=(100_000_000,),
        ),
        statistics=ScanStatistics(
            quote_requests=240, successful_quotes=238, opportunities_created=2
        ),
        started_at=f.NOW,
        finished_at=f.NOW,
    )


# --- разбор новых команд ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/menu", CommandName.MENU),
        ("/help", CommandName.HELP),
        ("/start", CommandName.START_SCANNER),
        ("/stop", CommandName.STOP_SCANNER),
        ("/restart", CommandName.RESTART),
        ("/providers", CommandName.PROVIDERS),
        ("/providers uniswap", CommandName.PROVIDERS),
        ("/scans", CommandName.SCANS),
        ("/backup", CommandName.BACKUP),
    ],
)
def test_control_commands_are_parsed(text: str, expected: CommandName) -> None:
    assert parse_command(text).name is expected


# --- меню и помощь ---------------------------------------------------------


async def test_menu_offers_the_required_buttons(database: Database) -> None:
    """Минимальный набор кнопок из задания."""
    response = await build_router(database).handle_text("/menu")

    labels = [button.label for row in response.buttons for button in row]
    assert "▶️ Запустить" in labels
    assert "⏸ Остановить" in labels
    assert "🔄 Перезапустить" in labels
    assert "📊 Статус" in labels
    assert "🔌 Статус агрегатора" in labels


async def test_help_lists_every_documented_command(database: Database) -> None:
    response = await build_router(database).handle_text("/help")

    for command in ("/menu", "/status", "/providers", "/scans", "/backup", "/restart"):
        assert command in response.text


async def test_menu_hides_control_buttons_without_a_port(database: Database) -> None:
    """Без порта управления кнопки, которые ничего не сделают, не показываются."""
    response = await build_router(database, control=None).handle_text("/menu")

    labels = [button.label for row in response.buttons for button in row]
    assert "▶️ Запустить" not in labels
    assert "📊 Статус" in labels


# --- подтверждение опасных действий ---------------------------------------


@pytest.mark.parametrize("text", ["/stop", "/restart"])
async def test_destructive_command_asks_for_confirmation(database: Database, text: str) -> None:
    """Команда сама действие не выполняет."""
    control = ScannerSwitch()
    response = await build_router(database, control=control).handle_text(text)

    assert "?" in response.text
    assert [button.label for row in response.buttons for button in row] == ["✅ Да", "↩️ Отмена"]
    assert control.state() is ScannerRunState.RUNNING
    assert not control.restart_requested


async def test_stop_requires_the_confirmation_button(database: Database) -> None:
    control = ScannerSwitch()
    router = build_router(database, control=control)

    await router.handle_callback(action_callback_data(CommandName.STOP_SCANNER))
    assert control.is_running, "нажатие кнопки меню ещё не подтверждение"

    await router.handle_callback(confirm_callback_data(CommandName.STOP_SCANNER))
    assert control.state() is ScannerRunState.PAUSED


async def test_cancel_returns_to_the_menu_without_changing_anything(database: Database) -> None:
    control = ScannerSwitch()
    router = build_router(database, control=control)

    await router.handle_text("/stop")
    response = await router.handle_callback(action_callback_data(CommandName.MENU))

    assert control.is_running
    assert response.buttons


async def test_restart_is_requested_only_after_confirmation(database: Database) -> None:
    control = ScannerSwitch()
    router = build_router(database, control=control)

    await router.handle_text("/restart")
    assert not control.restart_requested

    await router.handle_callback(confirm_callback_data(CommandName.RESTART))
    assert control.restart_requested
    assert control.state() is ScannerRunState.RESTARTING


async def test_start_resumes_scanning(database: Database) -> None:
    control = ScannerSwitch()
    control.stop()
    router = build_router(database, control=control)

    response = await router.handle_callback(action_callback_data(CommandName.START_SCANNER))

    assert control.is_running
    assert response.handled


async def test_repeated_start_is_reported_as_no_change(database: Database) -> None:
    router = build_router(database, control=ScannerSwitch())

    response = await router.handle_callback(action_callback_data(CommandName.START_SCANNER))

    assert "уже" in response.text


# --- состояние -------------------------------------------------------------


async def test_status_shows_version_environment_and_queues(database: Database) -> None:
    response = await build_router(database).handle_text("/status")

    assert "Monik 3.2.0" in response.text
    assert "production" in response.text
    assert "сканирование идёт" in response.text
    assert "zero_x" in response.text
    assert "4.9 зап/с" in response.text


async def test_status_reflects_a_stopped_scanner(database: Database) -> None:
    control = ScannerSwitch()
    control.stop()

    response = await build_router(database, control=control).handle_text("/status")

    assert "остановлено оператором" in response.text


async def test_providers_offer_a_button_per_aggregator(database: Database) -> None:
    response = await build_router(database).handle_text("/providers")

    labels = [button.label for row in response.buttons for button in row]
    assert labels == ["zero_x", "uniswap"]


async def test_one_provider_reports_the_reason_for_degradation(database: Database) -> None:
    response = await build_router(database).handle_text("/providers uniswap")

    assert "Агрегатор uniswap" in response.text
    assert "open" in response.text
    assert "provider_rate_limited" in response.text


async def test_unknown_provider_lists_the_available_ones(database: Database) -> None:
    response = await build_router(database).handle_text("/providers oneinch")

    assert response.handled is False
    assert "zero_x" in response.text


async def test_scans_report_the_last_cycles(database: Database) -> None:
    router = build_router(database, scans=StaticScans((_finished_scan(),)))

    response = await router.handle_text("/scans")

    assert "запросов 240" in response.text
    assert "возможностей 2" in response.text


async def test_scans_report_an_empty_history(database: Database) -> None:
    response = await build_router(database).handle_text("/scans")

    assert "циклов пока не было" in response.text


async def test_backup_reports_the_last_run(database: Database) -> None:
    response = await build_router(database).handle_text("/backup")

    assert "Копий сохранено: 8" in response.text
    assert "success" in response.text


async def test_backup_reports_being_switched_off(database: Database) -> None:
    router = build_router(database, backups=StaticBackups(BackupStatus(enabled=False)))

    response = await router.handle_text("/backup")

    assert "выключено" in response.text


# --- секреты ---------------------------------------------------------------


async def test_no_command_reveals_secrets(database: Database) -> None:
    """Ни один ответ не содержит ключей, токенов и приватных ключей."""
    router = build_router(database)

    texts = []
    for text in (
        "/menu",
        "/help",
        "/status",
        "/providers",
        "/providers zero_x",
        "/scans",
        "/backup",
        "/stats",
        "/level2",
        "/stop",
        "/restart",
    ):
        texts.append((await router.handle_text(text)).text)

    joined = "\n".join(texts)
    for secret in SECRETS:
        assert secret not in joined
    for marker in ("api_key", "api key", "token", "MONIK_", "private"):
        assert marker.lower() not in joined.lower()


async def test_system_update_is_applied_only_after_confirmation(database: Database) -> None:
    """Кнопка обновления опасна так же, как остановка: сначала вопрос."""
    updater = StaticUpdater()
    control = ScannerSwitch()
    router = build_router(database, updater=updater, control=control)

    prompt = await router.handle_callback(action_callback_data(CommandName.SYSTEM_UPDATE))
    assert updater.calls == 0
    assert not control.restart_requested
    assert prompt.buttons

    response = await router.handle_callback(confirm_callback_data(CommandName.SYSTEM_UPDATE))

    assert updater.calls == 1
    assert control.restart_requested
    assert "установлены" in response.text


async def test_failed_update_does_not_restart_the_scanner(database: Database) -> None:
    """Перезапуск после неудачной установки только теряет цикл."""
    updater = StaticUpdater(applied=False)
    control = ScannerSwitch()
    router = build_router(database, updater=updater, control=control)

    response = await router.handle_callback(confirm_callback_data(CommandName.SYSTEM_UPDATE))

    assert not control.restart_requested
    assert "не удалось" in response.text


async def test_update_is_reported_as_unavailable_without_the_port(database: Database) -> None:
    router = build_router(database, updater=None)

    response = await router.handle_callback(confirm_callback_data(CommandName.SYSTEM_UPDATE))

    assert not response.handled


async def test_text_command_also_asks_for_confirmation(database: Database) -> None:
    updater = StaticUpdater()
    router = build_router(database, updater=updater)

    await router.handle_text("/update")

    assert updater.calls == 0


class StaticExits:
    """Выход из сделки, заданный заранее.

    **Test implementation** (``CLAUDE.md`` §10). Настоящий выход
    спрашивает цену у агрегаторов; здесь важно только поведение кнопки.
    """

    def __init__(self, offer: ExitQuote | None) -> None:
        self._offer = offer
        self.previewed: list[str] = []
        self.sold: list[str] = []

    async def preview_exit(self, t_id: str) -> ExitQuote | None:
        self.previewed.append(t_id)
        return self._offer

    async def sell_at_market(self, t_id: str) -> ExitQuote | None:
        self.sold.append(t_id)
        return self._offer


def _offer(**overrides: object) -> ExitQuote:
    base: dict[str, object] = {
        "t_id": "#T41",
        "provider": "cow",
        "output": Decimal("99.674145"),
        "profit": Decimal("-0.325855"),
        "symbol": "USDT",
    }
    base.update(overrides)
    return ExitQuote(**base)  # type: ignore[arg-type]


class TestManualExit:
    """Кнопка «продать по текущей цене» под зависшей сделкой.

    Система сама в убыток не закрывает, но сообщить о застрявших деньгах
    и не дать способа их вернуть — половина дела. Решение принимает
    человек, и двух шагов оно стоит: промах пальцем не должен стоить
    сделки.
    """

    async def test_the_button_asks_before_selling(self, database: Database) -> None:
        exits = StaticExits(_offer())
        router = build_router(database, exits=exits)

        response = await router.handle_callback(sell_callback_data("#T41"))

        assert exits.sold == [], "до подтверждения ничего не продано"
        assert "#T41" in response.text
        labels = [button.label for row in response.buttons for button in row]
        assert labels == ["✅ Да", "❌ Нет"]

    async def test_the_question_names_the_price(self, database: Database) -> None:
        """Оператор решает по числам, а не вслепую."""
        router = build_router(database, exits=StaticExits(_offer()))

        response = await router.handle_callback(sell_callback_data("#T41"))

        assert "99.67 USDT" in response.text, "названа цена продажи, два знака"
        assert "-0.33 USDT" in response.text, "назван итог сделки"
        assert "cow" in response.text, "назван продавец"

    async def test_an_unknown_gas_cost_is_not_hidden(self, database: Database) -> None:
        """Без газа итог завышен, и умолчать об этом нельзя."""
        router = build_router(database, exits=StaticExits(_offer(costs_known=False)))

        response = await router.handle_callback(sell_callback_data("#T41"))

        assert "газа неизвестна" in response.text

    async def test_confirmation_sells(self, database: Database) -> None:
        exits = StaticExits(_offer())
        router = build_router(database, exits=exits)

        response = await router.handle_callback(sell_confirm_callback_data("#T41"))

        assert exits.sold == ["#T41"], "продана именно эта сделка"
        assert "продана" in response.text
        assert "99.67 USDT" in response.text

    async def test_the_button_carries_its_own_trade(self, database: Database) -> None:
        """Уведомлений может висеть несколько, и нажатое относится к своему."""
        exits = StaticExits(_offer(t_id="#T42"))
        router = build_router(database, exits=exits)

        await router.handle_callback(sell_confirm_callback_data("#T42"))

        assert exits.sold == ["#T42"]

    async def test_nothing_to_sell_is_said_plainly(self, database: Database) -> None:
        """Предложения нет — так и сказать, а не молчать."""
        exits = StaticExits(None)
        router = build_router(database, exits=exits)

        response = await router.handle_callback(sell_callback_data("#T41"))

        assert response.handled is False
        assert "#T41" in response.text
        assert exits.sold == []

    async def test_a_failed_sale_is_reported(self, database: Database) -> None:
        router = build_router(database, exits=StaticExits(None))

        response = await router.handle_callback(sell_confirm_callback_data("#T41"))

        assert response.handled is False
        assert "не удалось" in response.text

    async def test_without_the_subsystem_the_button_refuses(self, database: Database) -> None:
        """Торговля может быть не собрана вовсе — обещать нечего."""
        router = build_router(database)

        response = await router.handle_callback(sell_callback_data("#T41"))

        assert response.handled is False
