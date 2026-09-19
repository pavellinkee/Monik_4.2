"""Миграции схемы Monik.

Миграции применяются строго последовательно и не могут быть пропущены
(``30_DATABASE_SCHEMA.md`` §16). Новая миграция добавляется отдельным
модулем и регистрируется в :data:`ALL_MIGRATIONS`.
"""

from monik.infrastructure.db.migrations.base import Migration
from monik.infrastructure.db.migrations.m0001_initial import MIGRATION as MIGRATION_0001
from monik.infrastructure.db.migrations.m0002_snapshots import MIGRATION as MIGRATION_0002
from monik.infrastructure.db.migrations.m0003_scan_mode import MIGRATION as MIGRATION_0003
from monik.infrastructure.db.migrations.m0004_positions import MIGRATION as MIGRATION_0004
from monik.infrastructure.db.migrations.m0005_position_costs import (
    MIGRATION as MIGRATION_0005,
)
from monik.infrastructure.db.migrations.m0006_gas_calibration import (
    MIGRATION as MIGRATION_0006,
)

#: Все миграции в порядке применения.
from monik.infrastructure.db.migrations.m0007_signed_orders import (
    MIGRATION as MIGRATION_0007,
)

ALL_MIGRATIONS: tuple[Migration, ...] = (
    MIGRATION_0001,
    MIGRATION_0002,
    MIGRATION_0003,
    MIGRATION_0004,
    MIGRATION_0005,
    MIGRATION_0006,
    MIGRATION_0007,
)

__all__ = ["ALL_MIGRATIONS", "Migration"]
