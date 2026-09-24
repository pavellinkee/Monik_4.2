"""Находки, ожидающие решения подсистемы исполнения.

Проход и исполнение — **разные задачи**. Правило 11 требует этого прямо:
«подсистема исполнения не встроена в Level 1 и не вызывается им
напрямую — она получает найденную возможность и принимает решение сама».
Связывает их эта очередь: проход кладёт в неё найденное и заканчивается,
исполнение забирает и работает в своём сроке.

Разделение не формальное. Пока задача была одна, срок прохода был и
сроком сделки: 24.09 все три находки, прошедшие порог, погибли от
``task timed out`` — проход успевал, а на сборку обязательств и сверку
цены времени не оставалось. Ни одной попытки открыть сделку так и не
состоялось (``the_main_rules.md``, правило 27).

Хранится **последняя** пачка, а не все подряд. Находка живёт минутами, и
свежая всегда лучше накопленной: цена за это время уже другая, а
открывать по устаревшей — то же самое, что открывать вслепую.
"""

from __future__ import annotations

from datetime import timedelta

from monik.domain.value_objects.timestamps import UtcDatetime
from monik.services.level1.results import ScanResult
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger, log_fields

__all__ = ["FindingQueue"]

_LOGGER = get_logger("services.trading.pending")


class FindingQueue:
    """Последние находки прохода и срок их годности."""

    __slots__ = ("_clock", "_found_at", "_results", "_ttl")

    def __init__(self, *, clock: Clock, ttl_seconds: int) -> None:
        self._clock = clock
        self._ttl = timedelta(seconds=ttl_seconds)
        self._results: tuple[ScanResult, ...] = ()
        self._found_at: UtcDatetime | None = None

    @property
    def waiting(self) -> bool:
        """Есть ли что рассматривать. Устаревшее за ожидающее не считается."""
        return bool(self._results) and not self._expired(self._clock.now())

    def offer(self, results: tuple[ScanResult, ...]) -> None:
        """Передать находки прохода исполнению.

        Прежние вытесняются: свежая находка всегда лучше накопленной, а
        держать очередь из устаревших — значит однажды открыть сделку по
        цене получасовой давности.
        """
        if not results:
            return
        self._results = results
        self._found_at = self._clock.now()

    def take(self) -> tuple[ScanResult, ...]:
        """Забрать находки, если они ещё свежи.

        Устаревшие отбрасываются и об этом пишется в журнал: молчаливая
        потеря находки неотличима от её отсутствия, а различать их нужно.
        """
        if not self._results:
            return ()
        now = self._clock.now()
        results, found_at = self._results, self._found_at
        self._results, self._found_at = (), None
        if self._expired(now, found_at):
            waited = "?" if found_at is None else f"{(now - found_at).total_seconds():.1f}"
            _LOGGER.info(
                "findings expired before they could be considered",
                extra=log_fields(waited_seconds=waited, ttl_seconds=self._ttl.total_seconds()),
            )
            return ()
        return results

    def _expired(self, now: UtcDatetime, found_at: UtcDatetime | None = None) -> bool:
        moment = self._found_at if found_at is None else found_at
        return moment is None or now - moment > self._ttl
