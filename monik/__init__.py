"""Monik — DEX arbitrage scanner.

Реализация утверждённой архитектуры из ``docs/architecture/``.
Слои и их границы описаны в ``docs/architecture/25_PROJECT_STRUCTURE.md``.

Версия приложения определяется **здесь и только здесь**. Все места, где
приложение сообщает свою версию — startup logs, ``--version``, Telegram
``/status``, уведомления о запуске и восстановлении, diagnostics — читают
:data:`APPLICATION_VERSION`, а не хранят собственную копию.

Источник версии (``24_DEPLOYMENT.md`` §6 допускает разные источники и
требует только однозначности): имя каталога состояния, в котором лежит
пакет. Состояния Monik живут отдельными каталогами вида ``monik_3.3`` или
``Monik_3.4``, поэтому копия, названная новым номером, сама сообщает новый
номер — без правки кода и без риска, что приложение представится прежней
версией.

Если каталог назван иначе — пакет установлен в ``site-packages``, запущен
из временного каталога, распакован под другим именем — используется
объявленное значение :data:`__version__`. Оно же остаётся версией пакета
для ``pyproject.toml``; совпадение проверяется тестом.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "APPLICATION_NAME",
    "APPLICATION_VERSION",
    "__version__",
    "resolve_version",
    "version_label",
]

#: Отображаемое имя приложения.
APPLICATION_NAME = "Monik"

#: Объявленная версия пакета. Используется как запасной источник, когда
#: имя каталога состояния не содержит номера, и обязана совпадать с
#: ``pyproject.toml``.
__version__ = "4.1.0"

#: Имя каталога состояния: ``monik_3.3``, ``Monik-3.4``, ``monik 3.4.1``.
#: Номер patch необязателен и по умолчанию равен нулю.
_STATE_DIRECTORY_RE = re.compile(r"^monik[ _-]?v?(\d+)\.(\d+)(?:\.(\d+))?$", re.IGNORECASE)


def _version_from_directory(name: str) -> str | None:
    """Извлечь версию из имени каталога состояния."""
    match = _STATE_DIRECTORY_RE.match(name.strip())
    if match is None:
        return None
    major, minor, patch = match.group(1), match.group(2), match.group(3)
    return f"{int(major)}.{int(minor)}.{int(patch or 0)}"


def resolve_version() -> str:
    """Версия текущего состояния Monik.

    Определяется по имени каталога, в котором лежит пакет; при неудаче
    возвращается объявленная :data:`__version__`.
    """
    try:
        root = Path(__file__).resolve().parent.parent
    except OSError:  # pragma: no cover - недоступная файловая система
        return __version__
    return _version_from_directory(root.name) or __version__


#: Версия, которую приложение сообщает о себе.
APPLICATION_VERSION = resolve_version()


def version_label() -> str:
    """Человекочитаемое обозначение версии, например ``Monik 3.3.0``."""
    return f"{APPLICATION_NAME} {APPLICATION_VERSION}"
