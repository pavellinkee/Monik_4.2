"""Очередь находок между проходом и исполнением.

Проход и исполнение — разные задачи (``the_main_rules.md``, правила 11
и 27). Очередь связывает их и следит за годностью: находка живёт минуты,
и открывать сделку по устаревшей — то же, что открывать вслепую.
"""

from __future__ import annotations

from datetime import timedelta

from monik.services.observability import FakeClock
from monik.services.trading.pending import FindingQueue
from tests import factories as f


def _results(tag: str = "a") -> tuple[str, ...]:
    """Находки как непрозрачная для очереди величина.

    Очередь не разбирает содержимое: её дело — свежесть и передача.
    """
    return (tag,)


class TestHandover:
    def test_what_was_offered_is_taken(self) -> None:
        clock = FakeClock(f.NOW)
        queue = FindingQueue(clock=clock, ttl_seconds=60)

        queue.offer(_results())  # type: ignore[arg-type]

        assert queue.take() == _results()

    def test_taking_empties_the_queue(self) -> None:
        """Одна находка рассматривается один раз."""
        clock = FakeClock(f.NOW)
        queue = FindingQueue(clock=clock, ttl_seconds=60)
        queue.offer(_results())  # type: ignore[arg-type]

        queue.take()

        assert queue.take() == ()

    def test_an_empty_offer_changes_nothing(self) -> None:
        clock = FakeClock(f.NOW)
        queue = FindingQueue(clock=clock, ttl_seconds=60)

        queue.offer(())

        assert queue.waiting is False
        assert queue.take() == ()


class TestFreshness:
    def test_a_fresh_finding_survives(self) -> None:
        clock = FakeClock(f.NOW)
        queue = FindingQueue(clock=clock, ttl_seconds=60)
        queue.offer(_results())  # type: ignore[arg-type]

        clock.advance(timedelta(seconds=59))

        assert queue.waiting is True
        assert queue.take() == _results()

    def test_a_stale_finding_is_dropped(self) -> None:
        """Цена за минуту уже другая, и открывать по ней нельзя."""
        clock = FakeClock(f.NOW)
        queue = FindingQueue(clock=clock, ttl_seconds=60)
        queue.offer(_results())  # type: ignore[arg-type]

        clock.advance(timedelta(seconds=61))

        assert queue.waiting is False
        assert queue.take() == ()

    def test_a_dropped_finding_does_not_linger(self) -> None:
        """Устаревшее выбрасывается, а не остаётся ждать следующего такта."""
        clock = FakeClock(f.NOW)
        queue = FindingQueue(clock=clock, ttl_seconds=60)
        queue.offer(_results())  # type: ignore[arg-type]
        clock.advance(timedelta(seconds=61))

        assert queue.take() == (), "устаревшее не отдано"
        assert queue.take() == (), "и не осталось лежать"
        assert queue.waiting is False


class TestSupersede:
    def test_a_newer_scan_replaces_the_older(self) -> None:
        """Свежая находка всегда лучше накопленной.

        Очередь из находок разной давности однажды привела бы к сделке
        по цене получасовой свежести.
        """
        clock = FakeClock(f.NOW)
        queue = FindingQueue(clock=clock, ttl_seconds=60)
        queue.offer(_results("старое"))  # type: ignore[arg-type]

        clock.advance(timedelta(seconds=25))
        queue.offer(_results("новое"))  # type: ignore[arg-type]

        assert queue.take() == _results("новое")

    def test_a_replacement_resets_the_clock(self) -> None:
        """Возраст считается от последней находки, а не от первой."""
        clock = FakeClock(f.NOW)
        queue = FindingQueue(clock=clock, ttl_seconds=60)
        queue.offer(_results("старое"))  # type: ignore[arg-type]
        clock.advance(timedelta(seconds=50))
        queue.offer(_results("новое"))  # type: ignore[arg-type]

        clock.advance(timedelta(seconds=30))

        assert queue.take() == _results("новое"), "новая находка ещё свежа"
