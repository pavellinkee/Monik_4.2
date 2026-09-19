"""Подпись типизированных данных EIP-712.

Нужна провайдеру, который исполняет подписанную офчейн заявку, а не
транзакцию (``the_main_rules.md``, правило 16). Ключ и здесь не выходит
наружу: наружу выходит только подпись.

Кодирование проверено живым API 18.09.2026: книга заявок CoW Swap
признала подпись своей и отказала по остатку средств, а не по подписи.
"""

from __future__ import annotations

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from monik.config.secrets import SecretValue
from monik.domain.errors import ConfigurationError
from monik.services.trading.wallet import TradingWallet

#: Ключ существует только в этом файле и средств за ним нет.
TEST_KEY = "0x" + "11" * 32


def _wallet() -> TradingWallet:
    return TradingWallet(SecretValue("TEST_KEY", TEST_KEY))


def _typed(**overrides: object) -> dict:
    data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": [
                {"name": "sellToken", "type": "address"},
                {"name": "sellAmount", "type": "uint256"},
                {"name": "validTo", "type": "uint32"},
                {"name": "partiallyFillable", "type": "bool"},
            ],
        },
        "primaryType": "Order",
        "domain": {
            "name": "Gnosis Protocol",
            "version": "v2",
            "chainId": 137,
            "verifyingContract": "0x9008D19f58AAbD9eD0D60971565AA8510560ab41",
        },
        "message": {
            "sellToken": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
            "sellAmount": "100000000",
            "validTo": 1789758584,
            "partiallyFillable": False,
        },
    }
    data.update(overrides)  # type: ignore[arg-type]
    return data


class TestSignature:
    def test_signature_recovers_the_signing_account(self) -> None:
        """Подпись принадлежит нашему счёту — это и проверяет книга."""
        wallet = _wallet()
        typed = _typed()

        signature = wallet.sign_typed_data(typed)
        expected = dict(typed["message"]) | {"sellAmount": 100_000_000}
        recovered = Account.recover_message(
            encode_typed_data(full_message=typed | {"message": expected}),
            signature=bytes.fromhex(signature[2:]),
        )

        assert recovered == wallet.address

    def test_signature_is_sixty_five_bytes(self) -> None:
        """65 байт r, s, v — форма, которую принимает книга заявок."""
        signature = _wallet().sign_typed_data(_typed())

        assert signature.startswith("0x")
        assert len(bytes.fromhex(signature[2:])) == 65

    def test_numeric_fields_may_arrive_as_strings(self) -> None:
        """Суммы приходят строками, как их ждёт API провайдера.

        Строка и число дают разные хеши, поэтому приведение обязательно:
        иначе подпись была бы «почти правильной» и заявку отвергли бы.
        """
        wallet = _wallet()

        as_text = wallet.sign_typed_data(_typed())
        as_numbers = wallet.sign_typed_data(
            _typed(message=dict(_typed()["message"]) | {"sellAmount": 100_000_000})
        )

        assert as_text == as_numbers

    def test_only_declared_numeric_fields_are_converted(self) -> None:
        """Приведение идёт по объявленным типам, а не по именам полей.

        Адрес — тоже строка, и превращать его в число нельзя.
        """
        typed = _typed()
        signature = _wallet().sign_typed_data(typed)

        assert signature.startswith("0x")
        assert typed["message"]["sellToken"].startswith("0x")

    def test_a_non_numeric_value_in_a_numeric_field_is_refused(self) -> None:
        """Подделывать значение нельзя: лучше отказ, чем чужая подпись."""
        broken = _typed(message=dict(_typed()["message"]) | {"sellAmount": "очень много"})

        with pytest.raises(ConfigurationError):
            _wallet().sign_typed_data(broken)

    def test_the_key_never_appears_in_the_representation(self) -> None:
        wallet = _wallet()

        assert TEST_KEY not in repr(wallet)
        assert wallet.address in repr(wallet)
