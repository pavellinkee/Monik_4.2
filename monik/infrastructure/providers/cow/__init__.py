"""CoW Swap adapter.

Исполнение — подписанная офчейн заявка, а не транзакция
(``the_main_rules.md``, правило 16).
"""

from monik.infrastructure.providers.cow.adapter import CowAdapter

__all__ = ["CowAdapter"]
