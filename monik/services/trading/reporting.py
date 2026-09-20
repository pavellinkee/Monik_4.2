"""Периодический отчёт оператору о работе торгового прохода.

Отчёт отвечает на три вопроса, которые оператор задаёт себе, глядя на
молчащий телефон: работает ли торговля вообще, что произошло за
промежуток и насколько близко подходили находки. Без третьего пункта
тихий отчёт был бы бесполезен: «сделок нет» одинаково выглядит и когда
система ищет, и когда она встала.

Отчёт ничего не вычисляет заново: итог сделки берётся у самой сделки,
лучшая находка — у записи прохода. Считать здесь значило бы завести
вторую арифметику рядом с той, по которой принимались решения.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta, tzinfo
from decimal import Decimal
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from monik.domain.models.position import Position
from monik.domain.models.scan import ScanSummary
from monik.domain.value_objects.timestamps import UtcDatetime
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger, log_fields

__all__ = ["OperatorNotifier", "TradeReporter", "report_text"]

_LOGGER = get_logger("services.trading.reporting")


@runtime_checkable
class ClosedPositions(Protocol):
    """Источник завершённых сделок."""

    async def closed_between(
        self, *, since: UtcDatetime, until: UtcDatetime
    ) -> tuple[Position, ...]:
        """Сделки, завершённые в промежутке."""
        ...

    async def open_positions(self) -> tuple[Position, ...]:
        """Все незакрытые сделки."""
        ...


@runtime_checkable
class OperatorNotifier(Protocol):
    """Канал сообщений оператору."""

    async def notify_operator(self, text: str) -> bool:
        """Отправить произвольное сообщение."""
        ...


@runtime_checkable
class ScanSummarySource(Protocol):
    """Источник сводки по проходам."""

    async def summary_between(self, *, since: UtcDatetime, until: UtcDatetime) -> ScanSummary:
        """Сводка проходов за промежуток."""
        ...


def _local(moment: UtcDatetime, timezone: tzinfo | None) -> str:
    """Время в поясе оператора.

    Внутри системы время живёт в UTC, а читает отчёт человек по своим
    часам. Без перевода заголовок расходился бы и с часами оператора, и
    с журналом, который пояс уже учитывает (``the_main_rules.md``,
    правило 7).
    """
    return (moment if timezone is None else moment.astimezone(timezone)).strftime("%H:%M")


def report_text(
    *,
    since: UtcDatetime,
    until: UtcDatetime,
    trading_state: str,
    closed: tuple[Position, ...],
    open_now: tuple[Position, ...],
    scans: ScanSummary,
    threshold_percent: Decimal | None = None,
    balance: Decimal | None = None,
    balance_symbol: str = "",
    timezone: tzinfo | None = None,
) -> str:
    """Собрать текст отчёта.

    Отдельная функция, а не метод: текст проверяется без базы, часов и
    сети, и проверять его так проще, чем собирать окружение.
    """
    lines = [
        f"📊 Отчёт ann за {_local(since, timezone)}–{_local(until, timezone)}",
        f"Торговля: {trading_state}",
    ]
    if closed:
        lines.append(f"Сделок завершено: {len(closed)}")
        total = Decimal(0)
        unknown = 0
        for position in closed:
            result = position.net_result
            if result is None:
                unknown += 1
                shown = "итог неизвестен"
            else:
                total += result
                shown = f"{result:+f}"
            lines.append(
                f"  {position.t_id} {position.buy_provider_id.value}→"
                f"{position.sell_provider_id.value} {position.target_token.symbol} "
                f"{position.input_amount:f} → {shown}"
            )
        lines.append(f"Итог промежутка: {total:+f} {closed[0].base_token.symbol}")
        if unknown:
            # Неизвестный итог не превращается в ноль: он бы тихо
            # улучшил сводку (``CLAUDE.md`` §12).
            lines.append(f"Сделок с неизвестным итогом: {unknown}")
    else:
        lines.append("Сделок завершено: нет")
    if open_now:
        lines.append(f"Открыто сейчас: {len(open_now)}")
        for position in open_now:
            lines.append(
                f"  {position.t_id} {position.target_token.symbol} "
                f"{position.input_amount:f} — {position.status.value}"
            )
    else:
        lines.append("Открыто сейчас: нет")
    found = f"Проходов: {scans.passes}"
    if scans.best_net_roi is not None:
        found += f", лучшая находка {scans.best_net_roi:+f} %"
        if threshold_percent is not None:
            found += f" при пороге {threshold_percent:f} %"
    lines.append(found)
    if balance is not None:
        lines.append(f"Остаток: {balance:f} {balance_symbol}")
    return "\n".join(lines)


class TradeReporter:
    """Отправляет отчёт оператору раз в заданный промежуток."""

    __slots__ = (
        "_balance",
        "_clock",
        "_interval",
        "_notify",
        "_positions",
        "_scans",
        "_sent_until",
        "_threshold",
        "_timezone",
        "_trading_state",
    )

    def __init__(
        self,
        *,
        positions: ClosedPositions,
        scans: ScanSummarySource,
        notify: OperatorNotifier,
        clock: Clock,
        interval_seconds: int,
        trading_state: Callable[[], str],
        threshold_percent: Decimal | None = None,
        balance: Callable[[], Awaitable[tuple[Decimal, str] | None]] | None = None,
        timezone: str | None = None,
    ) -> None:
        self._positions = positions
        self._scans = scans
        self._notify = notify
        self._clock = clock
        self._interval = timedelta(seconds=interval_seconds)
        self._trading_state = trading_state
        self._threshold = threshold_percent
        self._balance = balance
        #: Пояс, в котором оператор читает время. Без него заголовок
        #: остался бы в UTC и расходился бы с его часами.
        self._timezone = ZoneInfo(timezone) if timezone else None
        #: Конец последнего отчётного промежутка. Промежутки стыкуются
        #: встык: иначе сделка, завершившаяся между ними, не попала бы
        #: ни в один отчёт.
        self._sent_until: UtcDatetime | None = None

    async def tick(self) -> bool:
        """Отправить отчёт, если промежуток истёк.

        Конец промежутка берётся по **плановой границе**, а не по моменту
        проверки. Разница не косметическая: задачу опрашивает планировщик
        со своим шагом, и граница почти никогда не совпадает с тактом.
        Записав концом момент проверки, мы отсчитывали бы следующий срок
        от уже опоздавшего — и опоздание переносилось бы вперёд вместо
        того, чтобы погаситься. Так отчёт уезжал на десятки секунд в
        сутки. По плановой границе лишние секунды просто попадают в
        следующий промежуток, а расписание не плывёт.
        """
        now = self._clock.now()
        if self._sent_until is None:
            self._sent_until = now
            return False
        if now - self._sent_until < self._interval:
            return False
        since = self._sent_until
        until = since + self._interval
        if now - until >= self._interval:
            # Отстали больше чем на промежуток: такт задержался надолго.
            # Догонять пачкой сообщений нельзя — оператор получил бы
            # очередь отчётов за прошедшее. Всё пропущенное уходит одним
            # отчётом, после чего расписание снова идёт по плану.
            until = now
        self._sent_until = until
        try:
            text = await self._compose(since, until)
        except Exception as error:  # noqa: BLE001 - отчёт не должен ронять работу
            _LOGGER.error(
                "trade report could not be composed",
                extra=log_fields(error=type(error).__name__, detail=str(error)),
            )
            return False
        sent = await self._notify.notify_operator(text)
        _LOGGER.info(
            "trade report sent" if sent else "trade report not delivered",
            extra=log_fields(since=since.isoformat(), until=until.isoformat()),
        )
        return bool(sent)

    async def _compose(self, since: UtcDatetime, until: UtcDatetime) -> str:
        closed = await self._positions.closed_between(since=since, until=until)
        open_now = await self._positions.open_positions()
        scans = await self._scans.summary_between(since=since, until=until)
        balance: Decimal | None = None
        symbol = ""
        if self._balance is not None:
            amount = await self._balance()
            if amount is not None:
                balance, symbol = amount
        return report_text(
            since=since,
            until=until,
            trading_state=self._trading_state(),
            closed=closed,
            open_now=open_now,
            scans=scans,
            threshold_percent=self._threshold,
            balance=balance,
            balance_symbol=symbol,
            timezone=self._timezone,
        )
