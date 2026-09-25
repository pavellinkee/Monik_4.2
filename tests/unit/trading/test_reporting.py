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
from zoneinfo import ZoneInfo

from monik.domain.enums.providers import ProviderId
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.position import Position
from monik.domain.models.scan import ScanSummary
from monik.domain.value_objects.identifiers import TId
from monik.services.observability import FakeClock
from monik.services.trading.reporting import (
    TradeReporter,
    closing_text,
    money,
    percent,
    report_text,
)
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
        assert "+0.01" in text, "деньги — два знака"

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

        assert "Остаток: 108.62 USDT" in text


class _Positions:
    def __init__(self) -> None:
        self.closed: tuple[Position, ...] = ()
        self.open_items: tuple[Position, ...] = ()

    async def closed_between(self, *, since: object, until: object) -> tuple[Position, ...]:
        return self.closed

    async def open_positions(self) -> tuple[Position, ...]:
        return self.open_items


class _Scans:
    async def summary_between(self, *, since: object, until: object) -> ScanSummary:
        return ScanSummary(passes=3)


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.buttons: object = ()

    async def notify_operator(self, text: str, *, buttons: object = ()) -> bool:
        self.buttons = buttons
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


class TestOperatorTimezone:
    """Отчёт читает человек по своим часам.

    Внутри системы время живёт в UTC. Без перевода заголовок расходился
    бы и с часами оператора, и с журналом, который пояс уже учитывает
    (``the_main_rules.md``, правило 7).
    """

    def test_the_header_is_written_in_the_operator_timezone(self) -> None:
        text = report_text(
            since=SINCE,
            until=UNTIL,
            trading_state="идёт",
            closed=(),
            open_now=(),
            scans=ScanSummary(passes=60, best_net_roi=None),
            timezone=ZoneInfo("Europe/Lisbon"),
        )

        # SINCE задан в UTC; в Лиссабоне зимой это то же время, летом — на час больше.
        shift = SINCE.astimezone(ZoneInfo("Europe/Lisbon")).hour - SINCE.hour
        assert f"{12 + shift:02d}:00–{12 + shift:02d}:30" in text

    def test_without_a_timezone_the_header_stays_in_utc(self) -> None:
        """Пояс не задан — подменять его догадкой нельзя."""
        text = report_text(
            since=SINCE,
            until=UNTIL,
            trading_state="идёт",
            closed=(),
            open_now=(),
            scans=ScanSummary(passes=60, best_net_roi=None),
        )

        assert "12:00–12:30" in text

    async def test_the_reporter_carries_the_configured_timezone(self) -> None:
        """Пояс доходит от конфигурации до текста, а не теряется по пути."""
        clock = FakeClock(SINCE)
        notifier = _Notifier()
        reporter = _reporter(clock, notifier, timezone="Asia/Tokyo")
        await reporter.tick()
        clock.advance(timedelta(minutes=30))

        assert await reporter.tick() is True
        assert "21:00–21:30" in notifier.sent[0], "12:00 UTC — это 21:00 в Токио"


class TestClosingNotice:
    """Итог завершённой сделки приходит отдельным сообщением.

    Сделка может закрыться через час после того, как оператор о ней
    спрашивал, и узнавать об этом из получасовой сводки — значит
    узнавать поздно. 21.09 продажа `#T41` состоялась, а ответ на нажатие
    кнопки пропал вместе с упавшим обработчиком команды.
    """

    def test_the_result_is_named(self) -> None:
        text = closing_text(
            _position(raw_input=100_000_000, raw_returned=100_016_907, raw_gas_cost=16_375)
        )

        assert "#T16" in text
        assert "Вложено: 100.00 USDT" in text, "суммы — два знака"
        assert "Вернулось: 100.02 USDT" in text
        assert "Газ: 0.02 USDT" in text
        # Итог меньше копейки, и два знака превратили бы его в ноль:
        # заработок круга на стейблкоинах измеряется тысячными.
        assert "Итог: +0.001 USDT" in text

    def test_a_profitable_trade_is_marked_as_such(self) -> None:
        text = closing_text(
            _position(raw_input=100_000_000, raw_returned=100_016_907, raw_gas_cost=16_375)
        )

        assert text.startswith("✅")

    def test_a_losing_trade_is_not_dressed_up(self) -> None:
        """Убыток обозначается честно: оператор решает по нему."""
        text = closing_text(
            _position(raw_input=100_000_000, raw_returned=99_900_000, raw_gas_cost=16_375)
        )

        assert text.startswith("🔻")
        assert "Итог: -0.12 USDT" in text

    def test_an_unknown_result_is_not_a_zero(self) -> None:
        """Неизвестный итог нулём не считается (``CLAUDE.md`` §12)."""
        text = closing_text(
            _position(raw_input=100_000_000, raw_returned=100_016_907, raw_gas_cost=None)
        )

        assert "Итог: неизвестен" in text
        assert "+0.000000" not in text

    def test_the_route_and_duration_are_shown(self) -> None:
        text = closing_text(
            _position(raw_input=100_000_000, raw_returned=100_016_907, raw_gas_cost=16_375)
        )

        assert "Круг: cow → uniswap" in text
        assert "Длилась:" in text


class TestNumberFormat:
    """Числа в сообщении читает человек.

    Решение оператора 21.09: после запятой две цифры. Шесть знаков в
    сообщении читать невозможно, а решений по ним он не принимает.
    """

    def test_money_shows_two_digits(self) -> None:
        assert money(Decimal("100.000000")) == "100.00"
        assert money(Decimal("9.359258")) == "9.36"
        assert money(Decimal("-0.116375")) == "-0.12"

    def test_money_keeps_a_sign_when_asked(self) -> None:
        assert money(Decimal("0.12"), sign=True) == "+0.12"
        assert money(Decimal("-0.12"), sign=True) == "-0.12"

    def test_money_never_rounds_a_result_into_nothing(self) -> None:
        """Иначе «Итог: +0.00» приходил бы на каждую сделку.

        Заработок круга на стейблкоинах измеряется тысячными долями:
        округление до копеек превратило бы отчёт в рассказ ни о чём.
        """
        assert money(Decimal("0.000532"), sign=True) == "+0.001"
        assert money(Decimal("-0.0000001")) == "-0.0000001"

    def test_zero_stays_zero(self) -> None:
        """У нуля скрывать нечего."""
        assert money(Decimal("0")) == "0.00"

    def test_percent_keeps_more_digits_than_money(self) -> None:
        """Порог режима равен 0,01 %, и находка должна быть от него отличима."""
        assert percent(Decimal("0.0132"), sign=True) == "+0.0132"
        assert percent(Decimal("0.01")) == "0.0100"


class TestReportButtons:
    """Отчёт с открытой сделкой несёт кнопку её закрытия."""

    async def test_an_open_trade_puts_a_button_under_the_report(self) -> None:
        clock = FakeClock(SINCE)
        notifier = _Notifier()
        positions = _Positions()
        positions.open_items = (_position(status=PositionStatus.HOLDING, closed_at=None),)
        reporter = _reporter(
            clock,
            notifier,
            positions=positions,
            buttons=lambda items: tuple(
                ((f"кнопка {item.t_id}",),)
                for item in items  # type: ignore[misc]
            )[0:1],
        )
        await reporter.tick()
        clock.advance(timedelta(minutes=30))

        assert await reporter.tick() is True
        assert notifier.buttons, "кнопка приложена к отчёту"

    async def test_a_quiet_report_carries_nothing(self) -> None:
        """Закрывать нечего — и предлагать нечего."""
        clock = FakeClock(SINCE)
        notifier = _Notifier()
        reporter = _reporter(clock, notifier, buttons=lambda items: ())
        await reporter.tick()
        clock.advance(timedelta(minutes=30))

        assert await reporter.tick() is True
        assert not notifier.buttons


class TestUnfilledAttempts:
    """Несостоявшаяся попытка — не завершённая сделка.

    25.09 отчёт назвал неисполненную заявку «завершённой сделкой с
    неизвестным итогом», и это прочиталось как убыток, которого не было:
    деньги не двигались вовсе (``the_main_rules.md``, правило 29).
    """

    def _failed(self, **overrides: object) -> Position:
        base: dict[str, object] = {
            "status": PositionStatus.FAILED,
            "raw_acquired": None,
            "raw_returned": 50_000_000,
            "raw_gas_cost": None,
        }
        base.update(overrides)
        return _position(**base)

    def test_an_unfilled_attempt_is_not_a_completed_trade(self) -> None:
        text = report_text(
            since=SINCE,
            until=UNTIL,
            trading_state="идёт",
            closed=(self._failed(buy_order_uid="0x" + "ab" * 28),),
            open_now=(),
            scans=ScanSummary(passes=84, best_net_roi=Decimal("1.2498")),
        )

        assert "Сделок завершено: нет" in text
        assert "Сделок не состоялось: 1" in text
        assert "неизвестн" not in text, "итог не выдумывается и не называется неизвестным"

    def test_an_unfilled_attempt_does_not_enter_the_total(self) -> None:
        """Иначе сумма промежутка врала бы на несостоявшуюся сделку."""
        text = report_text(
            since=SINCE,
            until=UNTIL,
            trading_state="идёт",
            closed=(
                _position(raw_input=100_000_000, raw_returned=100_050_000, raw_gas_cost=6_000),
                self._failed(buy_order_uid="0x" + "ab" * 28),
            ),
            open_now=(),
            scans=ScanSummary(passes=84, best_net_roi=None),
        )

        assert "Сделок завершено: 1" in text
        assert "Итог промежутка: +0.04 USDT" in text
        assert "Сделок не состоялось: 1" in text

    def test_an_unfilled_order_names_its_reason(self) -> None:
        text = report_text(
            since=SINCE,
            until=UNTIL,
            trading_state="идёт",
            closed=(self._failed(buy_order_uid="0x" + "ab" * 28),),
            open_now=(),
            scans=ScanSummary(passes=1, best_net_roi=None),
        )

        assert "заявку никто не взял" in text
        assert "деньги не тронуты" in text

    def test_a_reverted_purchase_names_its_reason(self) -> None:
        """Откат сети стоил газа, и это не то же, что неисполненная заявка."""
        text = report_text(
            since=SINCE,
            until=UNTIL,
            trading_state="идёт",
            closed=(self._failed(buy_tx_hash="0x" + "cd" * 32),),
            open_now=(),
            scans=ScanSummary(passes=1, best_net_roi=None),
        )

        assert "покупку откатила сеть" in text
        assert "газ" in text

    def test_a_purchase_never_sent_names_its_reason(self) -> None:
        text = report_text(
            since=SINCE,
            until=UNTIL,
            trading_state="идёт",
            closed=(self._failed(),),
            open_now=(),
            scans=ScanSummary(passes=1, best_net_roi=None),
        )

        assert "не была отправлена" in text

    def test_a_real_unknown_result_is_still_reported(self) -> None:
        """У завершённой сделки неизвестный итог остаётся неизвестным.

        Это тревожный случай, и смешивать его с несостоявшейся попыткой
        нельзя: там считать нечего, а здесь посчитать не удалось.
        """
        text = report_text(
            since=SINCE,
            until=UNTIL,
            trading_state="идёт",
            closed=(_position(raw_returned=100_000_000, raw_gas_cost=None),),
            open_now=(),
            scans=ScanSummary(passes=1, best_net_roi=None),
        )

        assert "Сделок завершено: 1" in text
        assert "итог неизвестен" in text
        assert "Сделок с неизвестным итогом: 1" in text
