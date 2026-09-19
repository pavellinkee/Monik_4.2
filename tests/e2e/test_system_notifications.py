"""E2E: операционные уведомления о состоянии приложения.

Проверяется сквозной путь: запуск → проверка готовности → сообщение в
Telegram, а также различение первого запуска, штатного перезапуска и
запуска после аварии.

Реальный Bot API не используется: транспорт заменён детерминированной
test implementation, credentials в тестах — заведомо недействительные
значения окружения.
"""

from __future__ import annotations

import copy
import pathlib
from datetime import timedelta
from typing import Any

import pytest

from monik.app.lifecycle import (
    TASK_NOTIFICATIONS,
    TASK_SYSTEM_HEALTH,
    Application,
    create_application,
)
from monik.app.startup_health import RUNTIME_STATE_KEY
from monik.config import parse_configuration
from monik.domain.enums.health import (
    ApplicationHealthStatus,
    ProviderHealthStatus,
    SupervisorState,
)
from monik.domain.enums.modes import ScanMode
from monik.domain.enums.notifications import DestinationKind, StartupKind
from monik.domain.enums.providers import ProviderId
from monik.domain.models.notification import NotificationDestination
from monik.infrastructure.db import Database
from monik.infrastructure.providers.contract import AggregatorAdapter
from monik.infrastructure.providers.fake import FakeAdapter
from monik.infrastructure.telegram import FakeTransport
from monik.services.notifications import SystemNotifier
from monik.services.observability import FakeClock
from tests import factories as f
from tests.component.level1.conftest import arbitrage_rule, level1_document
from tests.component.notifications.conftest import notification_env


def _destination() -> NotificationDestination:
    """Назначение доставки из конфигурации, а не из кода."""
    return NotificationDestination(
        destination_id="MONIK_TELEGRAM_CHAT_ID", kind=DestinationKind.TELEGRAM
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(f.NOW)


def _document(tmp_path: pathlib.Path, name: str, **overrides: Any) -> dict[str, Any]:
    document = copy.deepcopy(level1_document())
    document["gas"] = {"sources": ["static"], "static_wei_per_gas": {"polygon": 5_000_000_000}}
    document["database"] = {"path": str(tmp_path / name)}
    document["scheduler"] = {
        "tasks": {
            "scan_ur": {"mode": "interval", "interval_seconds": 300},
            TASK_NOTIFICATIONS: {"mode": "interval", "interval_seconds": 10},
        }
    }
    document["notifications"] = {
        "enabled": True,
        "telegram": {
            "enabled": True,
            "bot_token": {"env": "MONIK_TELEGRAM_BOT_TOKEN"},
            "chat_id": {"env": "MONIK_TELEGRAM_CHAT_ID"},
            "commands_enabled": False,
        },
    }
    document.update(overrides)
    return document


def _adapters(clock: FakeClock) -> dict[ProviderId, AggregatorAdapter]:
    return {
        ProviderId.ONEINCH: FakeAdapter(
            ProviderId.ONEINCH, clock, output_rule=arbitrage_rule("0.050", "20.00")
        ),
        ProviderId.ZERO_X: FakeAdapter(
            ProviderId.ZERO_X, clock, output_rule=arbitrage_rule("0.049", "20.30")
        ),
    }


async def _build(
    tmp_path: pathlib.Path, clock: FakeClock, name: str
) -> tuple[Application, Database, FakeTransport]:
    loaded = parse_configuration(_document(tmp_path, name), environ=notification_env())
    app, database = await create_application(loaded, clock=clock, adapters=_adapters(clock))
    assert app.container.system_notifier is not None, (
        "system notifier must be built when telegram is configured"
    )
    transport = FakeTransport()
    # Тот же notifier, но с детерминированным транспортом: обращения к
    # Bot API в тестах нет.
    app.container.system_notifier = SystemNotifier(
        loaded.config.notifications.system,
        transport=transport,
        destination=_destination(),
        clock=clock,
        state=app.container.repositories.metadata,
    )
    return app, database, transport


async def test_startup_sends_a_single_status_message(
    tmp_path: pathlib.Path, clock: FakeClock
) -> None:
    """После проверки готовности отправляется одно сообщение о запуске."""
    app, database, transport = await _build(tmp_path, clock, "startup.db")
    try:
        await app.startup()
        assert len(transport.sent) == 1
        text = transport.sent[0].text
        assert text.startswith("🟢")
        # Заголовок называет работающий проход, а не «Monik вообще»:
        # оператор запускает режим и хочет видеть в первой строке его.
        assert text.splitlines()[0] == "🟢 Monik Ur запущен"
        assert "Режим Ur" in text
        assert "oneinch" in text
        assert app.startup_kind is StartupKind.INITIAL
    finally:
        await app.shutdown()
        await database.close()


async def test_startup_probes_providers_before_reporting(
    tmp_path: pathlib.Path, clock: FakeClock
) -> None:
    """Состояние провайдеров известно к моменту отправки сообщения."""
    app, database, _ = await _build(tmp_path, clock, "probe.db")
    try:
        await app.startup()
        for provider_id in (ProviderId.ONEINCH, ProviderId.ZERO_X):
            assert app.container.health.provider(provider_id).status is (
                ProviderHealthStatus.HEALTHY
            )
    finally:
        await app.shutdown()
        await database.close()


async def test_graceful_shutdown_marks_a_restart(tmp_path: pathlib.Path, clock: FakeClock) -> None:
    """Штатная остановка и повторный запуск — перезапуск, а не авария."""
    first, database, _ = await _build(tmp_path, clock, "restart.db")
    await first.startup()
    await first.shutdown()
    await database.close()

    second, database2, _ = await _build(tmp_path, clock, "restart.db")
    try:
        await second.startup()
        assert second.startup_kind is StartupKind.RESTART
    finally:
        await second.shutdown()
        await database2.close()


async def test_missing_shutdown_marks_crash_recovery(
    tmp_path: pathlib.Path, clock: FakeClock
) -> None:
    """Отметка ``running`` от прошлого процесса означает аварию."""
    first, database, _ = await _build(tmp_path, clock, "crash.db")
    await first.startup()
    # Процесс «упал»: graceful shutdown не выполнялся.
    await database.close()

    # Пауза больше интервала: иначе сообщение подавила бы защита от
    # crash loop (она проверяется отдельным unit-тестом).
    clock.advance(timedelta(minutes=30))
    second, database2, transport = await _build(tmp_path, clock, "crash.db")
    try:
        await second.startup()
        assert second.startup_kind is StartupKind.CRASH_RECOVERY
        assert "аварийного завершения" in transport.sent[-1].text
    finally:
        await second.shutdown()
        await database2.close()


async def test_runtime_marker_is_persisted(tmp_path: pathlib.Path, clock: FakeClock) -> None:
    app, database, _ = await _build(tmp_path, clock, "marker.db")
    try:
        await app.startup()
        assert await app.container.repositories.metadata.get(RUNTIME_STATE_KEY) == "running"
    finally:
        await app.shutdown()
        await database.close()


async def test_successful_scans_do_not_create_notifications(
    tmp_path: pathlib.Path, clock: FakeClock
) -> None:
    """Нормальная работа не создаёт операционного спама."""
    app, database, transport = await _build(tmp_path, clock, "quiet.db")
    try:
        await app.startup()
        baseline = len(transport.sent)
        for _ in range(5):
            await app.container.level1.scan_all(ScanMode.UR)
            await app.container.system_notifier.notify_health(  # type: ignore[union-attr]
                app.container.health.application_health()
            )
        assert len(transport.sent) == baseline
    finally:
        await app.shutdown()
        await database.close()


async def test_health_task_is_scheduled(tmp_path: pathlib.Path, clock: FakeClock) -> None:
    """Уведомления о состоянии выполняются планировщиком, а не таймером."""
    app, database, _ = await _build(tmp_path, clock, "scheduled.db")
    try:
        await app.startup()
        outcome = await app.scheduler.trigger(TASK_SYSTEM_HEALTH)
        assert outcome is not None
    finally:
        await app.shutdown()
        await database.close()


async def test_degraded_subsystem_is_reported(tmp_path: pathlib.Path, clock: FakeClock) -> None:
    """Отказ подсистемы попадает в Telegram один раз, а не на каждый цикл."""
    app, database, transport = await _build(tmp_path, clock, "degraded.db")
    try:
        await app.startup()
        baseline = len(transport.sent)
        app.container.health.set_component(
            "database", ApplicationHealthStatus.UNAVAILABLE, reason="database_error"
        )
        notifier = app.container.system_notifier
        assert notifier is not None
        for _ in range(10):
            await notifier.notify_health(app.container.health.application_health())
        assert len(transport.sent) == baseline + 1
        assert transport.sent[-1].text.startswith("🔴")
    finally:
        await app.shutdown()
        await database.close()


async def test_status_command_reports_application_providers_and_last_scan(
    tmp_path: pathlib.Path, clock: FakeClock
) -> None:
    """``/status`` показывает состояние приложения, подсистем и провайдеров."""
    document = _document(tmp_path, "status.db")
    telegram = document["notifications"]["telegram"]  # type: ignore[index]
    telegram["commands_enabled"] = True  # type: ignore[index]
    loaded = parse_configuration(document, environ=notification_env())
    app, database = await create_application(loaded, clock=clock, adapters=_adapters(clock))
    try:
        await app.startup()
        await app.scheduler.trigger("scan_ur")
        assert app.container.commands is not None
        response = await app.container.commands.router.handle_text("/status")

        assert "application:" in response.text
        assert "level1: healthy (последний цикл" in response.text
        assert "provider:oneinch: healthy" in response.text
        for forbidden in (
            notification_env()["MONIK_TELEGRAM_BOT_TOKEN"],
            notification_env()["MONIK_TELEGRAM_CHAT_ID"],
        ):
            assert forbidden not in response.text
    finally:
        await app.shutdown()
        await database.close()


async def test_status_command_shows_degraded_provider_reason(
    tmp_path: pathlib.Path, clock: FakeClock
) -> None:
    """Причина деградации видна оператору и не содержит секретов."""
    document = _document(tmp_path, "status_degraded.db")
    telegram = document["notifications"]["telegram"]  # type: ignore[index]
    telegram["commands_enabled"] = True  # type: ignore[index]
    loaded = parse_configuration(document, environ=notification_env())
    app, database = await create_application(loaded, clock=clock, adapters=_adapters(clock))
    try:
        await app.startup()
        app.container.health.record_provider_probe(
            ProviderId.ONEINCH,
            ProviderHealthStatus.UNAVAILABLE,
            reason="http_authentication_failed",
        )
        assert app.container.commands is not None
        response = await app.container.commands.router.handle_text("/status")

        assert "provider:oneinch: unavailable (http_authentication_failed)" in response.text
    finally:
        await app.shutdown()
        await database.close()


def _stop_messages(transport: FakeTransport) -> list[str]:
    """Сообщения об остановке сканирования."""
    return [item.text for item in transport.sent if "Сканирование остановлено" in item.text]


class TestScannerStopNotification:
    """Каждая фактическая остановка сканера сообщается ровно один раз.

    Проверяются все пути остановки приложения: команда оператора,
    перезапрос, штатное завершение, критическая ошибка — и случаи, когда
    сообщать не о чем.
    """

    async def test_shutdown_reports_the_stop(
        self, tmp_path: pathlib.Path, clock: FakeClock
    ) -> None:
        app, database, transport = await _build(tmp_path, clock, "stop-shutdown.db")
        try:
            await app.startup()
            await app.shutdown()
        finally:
            await database.close()

        assert len(_stop_messages(transport)) == 1
        assert "завершает работу" in _stop_messages(transport)[0]

    async def test_operator_stop_is_reported_once(
        self, tmp_path: pathlib.Path, clock: FakeClock
    ) -> None:
        """Команда Telegram останавливает сканер — оператор узнаёт об этом."""
        app, database, transport = await _build(tmp_path, clock, "stop-operator.db")
        try:
            await app.startup()
            app.container.control.stop()
            await app._observe_scanner_state()
            await app._observe_scanner_state()
        finally:
            await app.shutdown()
            await database.close()

        messages = _stop_messages(transport)
        assert len(messages) == 1
        assert "оператором" in messages[0]

    async def test_already_stopped_scanner_does_not_repeat(
        self, tmp_path: pathlib.Path, clock: FakeClock
    ) -> None:
        app, database, transport = await _build(tmp_path, clock, "stop-repeat.db")
        try:
            await app.startup()
            app.container.control.stop()
            await app._observe_scanner_state()
            app.container.control.stop()
            await app._observe_scanner_state()
        finally:
            await app.shutdown()
            await database.close()

        assert len(_stop_messages(transport)) == 1

    async def test_restart_gives_one_message_for_two_stop_paths(
        self, tmp_path: pathlib.Path, clock: FakeClock
    ) -> None:
        """Перезапуск останавливает сканер и завершает процесс."""
        app, database, transport = await _build(tmp_path, clock, "stop-restart.db")
        try:
            await app.startup()
            app.container.control.request_restart()
            await app._observe_scanner_state()
        finally:
            await app.shutdown()
            await database.close()

        messages = _stop_messages(transport)
        assert len(messages) == 1
        assert "перезапуск" in messages[0]

    async def test_resumed_scanner_reports_the_next_stop(
        self, tmp_path: pathlib.Path, clock: FakeClock
    ) -> None:
        app, database, transport = await _build(tmp_path, clock, "stop-resume.db")
        try:
            await app.startup()
            app.container.control.stop()
            await app._observe_scanner_state()
            app.container.control.start()
            await app._observe_scanner_state()
            app.container.control.stop()
            await app._observe_scanner_state()
        finally:
            await app.shutdown()
            await database.close()

        assert len(_stop_messages(transport)) == 2

    async def test_failed_startup_reports_nothing(
        self, tmp_path: pathlib.Path, clock: FakeClock
    ) -> None:
        """Сканер не запускался — сообщать о его остановке нечего."""
        app, database, transport = await _build(tmp_path, clock, "stop-never-started.db")
        try:
            await app.shutdown()
        finally:
            await database.close()

        assert _stop_messages(transport) == []

    async def test_critical_failure_is_reported_as_such(
        self, tmp_path: pathlib.Path, clock: FakeClock
    ) -> None:
        app, database, transport = await _build(tmp_path, clock, "stop-safe.db")
        try:
            await app.startup()
            app.supervisor.state = SupervisorState.SAFE_STOP
            await app.shutdown()
        finally:
            await database.close()

        messages = _stop_messages(transport)
        assert len(messages) == 1
        assert messages[0].startswith("🔴")
        assert "критической ошибки" in messages[0]

    async def test_stop_message_keeps_no_secrets(
        self, tmp_path: pathlib.Path, clock: FakeClock
    ) -> None:
        app, database, transport = await _build(tmp_path, clock, "stop-secrets.db")
        try:
            await app.startup()
            await app.shutdown()
        finally:
            await database.close()

        text = _stop_messages(transport)[0]
        for marker in ("token", "api_key", "MONIK_", "chat_id"):
            assert marker.lower() not in text.lower()
