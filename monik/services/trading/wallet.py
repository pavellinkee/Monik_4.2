"""Торговый счёт: ключ и адрес.

Ключ живёт ровно в одном месте — в файле секретов службы — и попадает
сюда через тот же механизм ссылок на environment, что и ключи
агрегаторов (``17_CONFIGURATION.md`` §26, ``32_SECURITY.md`` §3).

Наружу класс отдаёт **только адрес**. Сам ключ не возвращается ни одним
методом, не попадает в ``repr`` и не может быть случайно записан в лог:
подпись выполняется внутри.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount
from eth_utils.address import to_checksum_address

from monik.config.secrets import SecretValue
from monik.domain.errors import ConfigurationError

__all__ = ["TradingWallet"]


class TradingWallet:
    """Счёт, от имени которого отправляются сделки."""

    __slots__ = ("_account",)

    def __init__(self, private_key: SecretValue) -> None:
        try:
            account: LocalAccount = Account.from_key(private_key.get())
        except (ValueError, TypeError):
            # Текст исключения библиотеки может содержать сам ключ,
            # поэтому наружу он не выпускается.
            raise ConfigurationError(
                "trading private key is not a valid secp256k1 key",
                code="trading_key_invalid",
            ) from None
        self._account = account

    @property
    def address(self) -> str:
        """Публичный адрес счёта в формате EIP-55."""
        return str(self._account.address)

    def sign_transaction(self, transaction: dict[str, Any]) -> bytes:
        """Подписать транзакцию и вернуть её в виде байтов для отправки.

        Адреса приводятся к контрольному регистру EIP-55. Внутри Monik
        адрес канонически хранится в нижнем регистре — так он сравнивается
        без оговорок, — а библиотека подписи принимает только запись с
        контрольной суммой. Это различие форматов и остаётся здесь, у
        границы с библиотекой, а не расползается по вызывающему коду.
        """
        prepared = dict(transaction)
        for field in ("to", "from"):
            value = prepared.get(field)
            if isinstance(value, str):
                prepared[field] = to_checksum_address(value)
        signed = self._account.sign_transaction(prepared)
        return bytes(signed.raw_transaction)

    def sign_typed_data(self, typed_data: Mapping[str, Any]) -> str:
        """Подписать типизированные данные EIP-712 и вернуть подпись.

        Нужна провайдеру, который исполняет не транзакцию, а подписанную
        офчейн заявку (``the_main_rules.md``, правило 16). Ключ и здесь
        наружу не выходит: подпись выполняется внутри, а возвращается
        только её результат.

        Числовые поля заявки провайдеры передают строками — так их ждёт
        их API, — а подпись требует целых: строка и число дают разные
        хеши, и заявка с «почти правильной» подписью была бы отвергнута.
        Приведение выполняется здесь, у границы с библиотекой, по
        объявленным в самих данных типам, а не по списку имён полей:
        иначе каждое новое числовое поле пришлось бы дописывать руками.
        """
        prepared = dict(typed_data)
        message = prepared.get("message")
        types = prepared.get("types")
        primary = prepared.get("primaryType")
        if isinstance(message, Mapping) and isinstance(types, Mapping) and isinstance(primary, str):
            prepared["message"] = self._coerce(message, primary, types)
        signed = self._account.sign_message(encode_typed_data(full_message=prepared))
        return "0x" + bytes(signed.signature).hex()

    @classmethod
    def _coerce(cls, value: Any, type_name: str, types: Mapping[str, Any]) -> Any:
        """Привести значение к объявленному типу EIP-712.

        Обход рекурсивный, потому что структура бывает вложенной:
        ``PermitSingle`` Permit2 держит числа внутри ``PermitDetails``,
        и плоское приведение до них не добиралось.

        Замер 21.09 показал, что ``eth_account`` числовые строки
        принимает и сам, то есть подпись вложенной структуры выходила
        верной и без этого. Приведение всё же доведено до конца
        сознательно: оно делает поведение явным вместо того, чтобы
        зависеть от необъявленной любезности библиотеки. Ровно по этой
        причине приведение здесь и появилось изначально.

        Тип берётся из самих данных, а не из списка имён: иначе каждое
        новое поле пришлось бы дописывать руками.
        """
        if type_name.endswith("]"):
            inner = type_name[: type_name.rindex("[")]
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return [cls._coerce(item, inner, types) for item in value]
            return value
        fields = types.get(type_name)
        if isinstance(fields, Sequence) and not isinstance(fields, (str, bytes)):
            if not isinstance(value, Mapping):
                return value
            prepared = dict(value)
            for field in fields:
                if not isinstance(field, Mapping):
                    continue
                name = field.get("name")
                declared = field.get("type")
                if isinstance(name, str) and isinstance(declared, str) and name in prepared:
                    prepared[name] = cls._coerce(prepared[name], declared, types)
            return prepared
        if type_name.startswith(("uint", "int")) and isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                raise ConfigurationError(
                    f"typed data field of type {type_name!r} is not a number: {value!r}",
                    code="typed_data_field_invalid",
                ) from None
        return value

    def __repr__(self) -> str:
        """Представление без секрета: только адрес."""
        return f"TradingWallet(address={self.address})"
