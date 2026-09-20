"""Периодический отчёт оператору.

Отчёт отвечает на три вопроса, которые оператор задаёт себе, глядя на
молчащий телефон: работает ли торговля, что произошло за промежуток и
насколько близко подходили находки. Тихий отчёт без третьего пункта
бесполезен: «сделок нет» одинаково выглядит и когда система ищет, и
когда она встала.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from monik.domain.enums.providers import ProviderId
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.position import Position
from monik.domain.models.scan import ScanSummary
from monik.domain.value_objects.identifiers import TId
from monik.services.observability import FakeClock
from monik.services.trading.reporting import TradeReporter, report_text
from tests import factories as f

SINCE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
UNTIL = SINCE + timedelta(minutes=30)


def _position(**overrides: object) -> Position:
    base: dict[str, object] = {
        "t_id": TId.from_sequence(16),
        "network_id": f.POLYGON,
        "status": PositionStatus.CLOSED,
        "base_token": f.USDT,
        "target_token": f.AAVE,
        "buy_provider_id": ProviderId.COW,
        "sell_provider_id": ProviderId.UNISWAP,
        "raw_input": 50_000_000,
        "raw_acquired": 50_005_079,
        "raw_returned": 50_013_702,
        "raw_gas_cost": 618,
        "opened_at": SINCE,
        "closed_at": SINCE + timedelta(seconds=25),
    }
    base.update(overrides)
    return Position(**base)  # type: ignore[arg-type]


class TestText:
    def test_a_closed_trade_is_reported_with_its_own_result(self) -> None:
        """Итог берётся у сделки, а не пересчитывается заново."""
        text = report_text(
            since=SINCE,
            until=UNTIL,
            trading_state="идёт",
            closed=(_position(),),
            open_now=(),
            scans=ScanSummary(passes=60, best_net_roi=Decimal("0.0132")),
        )

        assert "12:00–12:30" in text
        assert "Торговля: идёт" in text
        assert "#T16" in text
        assert "cow→uniswap" in text
        assert "+0.013084" in text

    def test_a_quiet_period_still_says_how_close_it_got(self) -> None:
        """Иначе «сделок нет» не отличить от «система встала»."""
        text = report_text(
            since=SINCE,
            until=UNTIL,
            trading_state="идёт",
            closed=(),
            open_now=(),
            scans=ScanSummary(passes=60, best_net_roi=Decimal("0.004")),
            threshold_percent=Decimal("0.01"),
        )

        assert "Сделок завершено: нет" in text
        assert "Проходов: 60" in text
        assert "+0.004" in text
        assert "0.01" in text

    def test_an_unknown_result_is_not_counted_as_zero(self) -> None:
        """Неизвестный итог тихо улучшил бы сводку (``CLAUDE.md`` §12)."""
        text = report_text(
            since=SINCE,
            until=UNTIL,
            trading_state="идёт",
            closed=(_position(raw_gas_cost=None),),
            open_now=(),
            scans=ScanSummary(passes=1),
        )

        assert "итог неизвестен" in text
        assert "Сделок с неизвестным итогом: 1" in text

    def test_open_positions_are_named(self) -> None:
        text = report_text(
            since=SINCE,
            until=UNTIL,
            trading_state="остановлена",
            closed=(),
            open_now=(_position(status=PositionStatus.HOLDING, closed_at=None),),
            scans=ScanSummary(passes=0),
        )

        assert "Открыто сейчас: 1" in text
        assert "holding" in text

    def test_the_balance_is_shown_when_known(self) -> None:
        text = report_text(
            since=SINCE,
            until=UNTIL,
            trading_state="идёт",
            closed=(),
            open_now=(),
            scans=ScanSummary(passes=1),
            balance=Decimal("108.617565"),
            balance_symbol="USDT",
        )

        assert "Остаток: 108.617565 USDT" in text


class _Positions:
    def __init__(self) -> None:
        self.closed: tuple[Position, ...] = ()

    async def closed_between(self, *, since: object, until: object) -> tuple[Position, ...]:
        return self.closed

    async def open_positions(self) -> tuple[Position, ...]:
        return ()


class _Scans:
    async def summary_between(self, *, since: object, until: object) -> ScanSummary:
        return ScanSummary(passes=3)


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def notify_operator(self, text: str) -> bool:
        self.sent.append(text)
        return True


def _reporter(clock: FakeClock, notifier: _Notifier, **overrides: object) -> TradeReporter:
    base: dict[str, object] = {
        "positions": _Positions(),
        "scans": _Scans(),
        "notify": notifier,
        "clock": clock,
        "interval_seconds": 1_800,
        "trading_state": lambda: "идёт",
    }
    base.update(overrides)
    return TradeReporter(**base)  # type: ignore[arg-type]


class TestSchedule:
    async def test_nothing_is_sent_before_the_period_elapses(self) -> None:
        clock = FakeClock(SINCE)
        notifier = _Notifier()
        reporter = _reporter(clock, notifier)

        assert await reporter.tick() is False
        clock.advance(timedelta(minutes=29))
        assert await reporter.tick() is False
        assert notifier.sent == []

    async def test_the_report_is_sent_once_the_period_elapses(self) -> None:
        clock = FakeClock(SINCE)
        notifier = _Notifier()
        reporter = _reporter(clock, notifier)
        await reporter.tick()

        clock.advance(timedelta(minutes=30))

        assert await reporter.tick() is True
        assert len(notifier.sent) == 1

    async def test_periods_join_without_a_gap(self) -> None:
        """Сделка между отчётами не должна пропасть ни из одного."""
        clock = FakeClock(SINCE)
        notifier = _Notifier()
        reporter = _reporter(clock, notifier)
        await reporter.tick()
        clock.advance(timedelta(minutes=30))
        await reporter.tick()
        clock.advance(timedelta(minutes=30))
        await reporter.tick()

        assert len(notifier.sent) == 2
        assert "12:00–12:30" in notifier.sent[0]
        assert "12:30–13:00" in notifier.sent[1]

    async def test_a_broken_report_does_not_stop_the_work(self) -> None:
        """Отчёт — сообщение человеку, а не часть торговли."""

        class _Broken:
            async def closed_between(self, *, since: object, until: object) -> tuple[Position, ...]:
                raise RuntimeError("база недоступна")

            async def open_positions(self) -> tuple[Position, ...]:
                return ()

        clock = FakeClock(SINCE)
        notifier = _Notifier()
        reporter = _reporter(clock, notifier, positions=_Broken())
        await reporter.tick()
        clock.advance(timedelta(minutes=30))

        assert await reporter.tick() is False
        assert notifier.sent == []


class TestDrift:
    """Расписание отчёта не должно уезжать.

    Задачу опрашивает планировщик со своим шагом, и плановая граница
    почти никогда не совпадает с тактом. Если считать концом промежутка
    момент проверки, следующий срок отсчитывается от уже опоздавшего, и
    опоздание переносится вперёд вместо того, чтобы погаситься.
    """

    async def test_a_late_check_keeps_the_planned_boundary(self) -> None:
        """Промежуток берётся по плану, а не по моменту проверки.

        Проверяется сама граница, а не заголовок: тот округляется до
        минут и разницу в секундах спрятал бы.
        """

        class _Recording(_Positions):
            def __init__(self) -> None:
                super().__init__()
                self.window: tuple[object, object] | None = None

            async def closed_between(self, *, since: object, until: object) -> tuple[Position, ...]:
                self.window = (since, until)
                return ()

        clock = FakeClock(SINCE)
        notifier = _Notifier()
        positions = _Recording()
        reporter = _reporter(clock, notifier, positions=positions)
        await reporter.tick()

        clock.advance(timedelta(minutes=30, seconds=20))

        assert await reporter.tick() is True
        assert positions.window == (SINCE, SINCE + timedelta(minutes=30)), (
            "лишние двадцать секунд в промежуток не вошли"
        )

    async def test_the_lateness_does_not_accumulate(self) -> None:
        """Опоздание одного такта не сдвигает все последующие.

        Прежде конец промежутка записывался моментом проверки, и каждый
        следующий срок отсчитывался от опоздавшего: за сутки отчёт
        уезжал на десятки минут.
        """
        clock = FakeClock(SINCE)
        notifier = _Notifier()
        reporter = _reporter(clock, notifier)
        await reporter.tick()

        # Каждый такт опаздывает на 20 секунд, но граница остаётся на месте.
        clock.advance(timedelta(minutes=30, seconds=20))
        await reporter.tick()
        clock.advance(timedelta(minutes=29, seconds=40))
        await reporter.tick()
        clock.advance(timedelta(minutes=30))
        await reporter.tick()

        assert [text.splitlines()[0] for text in notifier.sent] == [
            "📊 Отчёт ann за 12:00–12:30",
            "📊 Отчёт ann за 12:30–13:00",
            "📊 Отчёт ann за 13:00–13:30",
        ]

    async def test_a_long_stall_is_covered_by_a_single_report(self) -> None:
        """Пропущенное уходит одним отчётом, а не очередью сообщений.

        Догонять пачкой нельзя: оператор получил бы подряд несколько
        отчётов за прошедшее, и телефон зазвонил бы шесть раз вместо
        одного.
        """
        clock = FakeClock(SINCE)
        notifier = _Notifier()
        reporter = _reporter(clock, notifier)
        await reporter.tick()

        clock.advance(timedelta(hours=3))
        assert await reporter.tick() is True
        assert await reporter.tick() is False, "очереди догоняющих отчётов нет"

        assert len(notifier.sent) == 1
        assert "12:00–15:00" in notifier.sent[0], "промежуток покрыт целиком"

    async def test_the_schedule_resumes_after_a_stall(self) -> None:
        """После пропуска расписание снова идёт по плану."""
        clock = FakeClock(SINCE)
        notifier = _Notifier()
        reporter = _reporter(clock, notifier)
        await reporter.tick()
        clock.advance(timedelta(hours=3))
        await reporter.tick()

        clock.advance(timedelta(minutes=30))

        assert await reporter.tick() is True
        assert "15:00–15:30" in notifier.sent[1]
