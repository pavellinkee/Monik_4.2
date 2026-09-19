"""Общие условия для всех тестов.

Здесь живёт только то, что обязано действовать на весь набор. Частные
приспособления остаются рядом со своими тестами.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

#: Журнал приложения. ``configure_logging`` настраивает именно его и
#: выставляет ``propagate = False``, чтобы записи не уходили в чужой
#: корневой журнал. Для приложения это правильно, для набора тестов —
#: ловушка: ``caplog`` вешает свой обработчик на корень, и любой тест,
#: однажды вызвавший настройку, глушил чтение журнала во **всех**
#: последующих. Падения при этом зависели от порядка запуска, а не от
#: кода, и выглядели необъяснимо.
_APPLICATION_LOGGER = "monik"


@pytest.fixture(autouse=True)
def _restore_application_logging() -> Iterator[None]:
    """Вернуть журналу приложения исходное состояние после каждого теста.

    Тест вправе настраивать журналирование — это часть поведения
    приложения, и проверять её нужно. Но последствия такой настройки не
    должны переживать сам тест.
    """
    logger = logging.getLogger(_APPLICATION_LOGGER)
    handlers = list(logger.handlers)
    level = logger.level
    propagate = logger.propagate
    try:
        yield
    finally:
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate
