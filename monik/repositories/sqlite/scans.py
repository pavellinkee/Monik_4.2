"""Хранилище циклов Level 1."""

from __future__ import annotations

import json
from decimal import Decimal

import aiosqlite

from monik.domain.enums.lifecycle import ScanStatus
from monik.domain.models.scan import Scan, ScanScope, ScanStatistics, ScanSummary
from monik.domain.value_objects.identifiers import ScanId
from monik.domain.value_objects.timestamps import UtcDatetime
from monik.infrastructure.db.connection import Database
from monik.infrastructure.db.types import from_timestamp, to_json, to_timestamp
from monik.repositories.sqlite.mapping import column, load_model, optional_column

__all__ = ["SqliteScanRepository"]

_COLUMNS = "scan_id, status, scope_json, statistics_json, started_at, finished_at"


class SqliteScanRepository:
    """Сохраняет метаданные и статистику циклов Level 1.

    Полный поток quotes не сохраняется — только агрегированные счётчики
    (``30_DATABASE_SCHEMA.md`` §43-44).
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(self, scan: Scan) -> None:
        """Сохранить новый цикл."""
        await self._database.execute(
            f"INSERT INTO scans ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
            self._values(scan),
        )

    async def update(self, scan: Scan) -> None:
        """Обновить состояние цикла."""
        await self._database.execute(
            "UPDATE scans SET status = ?, scope_json = ?, statistics_json = ?, "
            "started_at = ?, finished_at = ? WHERE scan_id = ?",
            (
                scan.status.value,
                to_json(scan.scope.model_dump(mode="json")),
                to_json(scan.statistics.model_dump(mode="json")),
                to_timestamp(scan.started_at),
                to_timestamp(scan.finished_at) if scan.finished_at else None,
                str(scan.scan_id),
            ),
        )

    async def get(self, scan_id: ScanId) -> Scan | None:
        """Найти цикл по идентификатору."""
        row = await self._database.fetch_one(
            f"SELECT {_COLUMNS} FROM scans WHERE scan_id = ?", (str(scan_id),)
        )
        return self._to_domain(row) if row else None

    async def recent(self, *, limit: int) -> tuple[Scan, ...]:
        """Последние циклы, начиная с самого свежего."""
        rows = await self._database.fetch_all(
            f"SELECT {_COLUMNS} FROM scans ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        return tuple(self._to_domain(row) for row in rows)

    async def summary_between(self, *, since: UtcDatetime, until: UtcDatetime) -> ScanSummary:
        """Сводка проходов за промежуток: сколько и какой был лучший.

        Лучшая доходность не вычисляется заново, а берётся из записи
        прохода: там лежит ровно то число, по которому принималось
        решение. Пересчёт здесь завёл бы вторую арифметику рядом с той.
        """
        rows = await self._database.fetch_all(
            "SELECT statistics_json FROM scans WHERE finished_at IS NOT NULL "
            "AND finished_at >= ? AND finished_at < ?",
            (to_timestamp(since), to_timestamp(until)),
        )
        best: Decimal | None = None
        for row in rows:
            value = _best_net_roi(column(row, "statistics_json"))
            if value is not None and (best is None or value > best):
                best = value
        return ScanSummary(passes=len(rows), best_net_roi=best)

    async def delete_finished_before(self, moment: UtcDatetime) -> int:
        """Удалить завершённые циклы старше указанного момента.

        Активные циклы не удаляются: cleanup не должен нарушать recovery
        (``30_DATABASE_SCHEMA.md`` §71).
        """
        rows = await self._database.fetch_all(
            "SELECT scan_id FROM scans WHERE finished_at IS NOT NULL AND finished_at < ? "
            "AND status != ?",
            (to_timestamp(moment), ScanStatus.RUNNING.value),
        )
        if not rows:
            return 0
        await self._database.execute(
            "DELETE FROM scans WHERE finished_at IS NOT NULL AND finished_at < ? AND status != ?",
            (to_timestamp(moment), ScanStatus.RUNNING.value),
        )
        return len(rows)

    @staticmethod
    def _values(scan: Scan) -> tuple[object, ...]:
        return (
            str(scan.scan_id),
            scan.status.value,
            to_json(scan.scope.model_dump(mode="json")),
            to_json(scan.statistics.model_dump(mode="json")),
            to_timestamp(scan.started_at),
            to_timestamp(scan.finished_at) if scan.finished_at else None,
        )

    @staticmethod
    def _to_domain(row: aiosqlite.Row) -> Scan:
        finished_at = optional_column(row, "finished_at")
        return Scan(
            scan_id=ScanId(str(column(row, "scan_id"))),
            status=ScanStatus(str(column(row, "status"))),
            scope=load_model(ScanScope, column(row, "scope_json")),
            statistics=load_model(ScanStatistics, column(row, "statistics_json")),
            started_at=from_timestamp(str(column(row, "started_at"))),
            finished_at=from_timestamp(str(finished_at)) if finished_at else None,
        )


def _best_net_roi(raw: object) -> Decimal | None:
    """Лучшая чистая доходность из статистики прохода.

    Статистика хранится как есть, одной записью, и разбирать её здесь —
    работа отображения, а не вычисление. Повреждённая или неполная
    запись не мешает отчёту: такой проход просто не участвует в
    сравнении.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    best = payload.get("best_combination")
    if not isinstance(best, dict):
        return None
    roi = best.get("net_roi")
    value = roi.get("value") if isinstance(roi, dict) else roi
    if not isinstance(value, (str, int)):
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None
