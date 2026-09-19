"""Идентификаторы подписанных заявок у позиции.

CoW Swap исполняет не транзакцией, а подписанной офчейн заявкой
(``the_main_rules.md``, правило 16). У такой ноги нет хеша транзакции —
есть идентификатор заявки в книге, и по нему же узнаётся её судьба.

Столбцы добавлены отдельно от ``*_tx_hash``, а не вместо них: у круга
ноги могут исполняться по-разному — покупка заявкой, продажа
транзакцией, — и складывать в одно поле два разных идентификатора
значило бы потерять, чем именно нога исполнялась. После перезапуска
восстановить незавершённую ногу было бы нечем.
"""

from __future__ import annotations

from monik.infrastructure.db.migrations.base import Migration

__all__ = ["MIGRATION"]

_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE positions ADD COLUMN buy_order_uid TEXT",
    "ALTER TABLE positions ADD COLUMN sell_order_uid TEXT",
)

MIGRATION = Migration(version=7, name="signed_orders", statements=_STATEMENTS)
