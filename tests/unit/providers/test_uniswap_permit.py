"""Подписанное разрешение Permit2 в сборке обмена Uniswap.

Permit2 привязывает разрешение к адресу роутера, а Uniswap этот адрес
меняет: выданное прежнему к новому не относится, и обмен откатывается с
``AllowanceExpired(0)``. Именно так 21.09 не продалась `#T41`.

Разрешение, подписанное вместе с обменом, к адресу заранее не привязано
и смену роутера переживает (``the_main_rules.md``, правило 23).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from monik.config.secrets import SecretValue
from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import DataError
from monik.domain.value_objects.identifiers import RequestId
from monik.infrastructure.http import FakeHttpClient, HttpResponse
from monik.infrastructure.providers import QuoteRequest
from monik.infrastructure.providers.uniswap import UniswapAdapter, endpoints
from monik.services.observability import FakeClock
from monik.services.trading import TradingWallet
from tests import factories as f

from .support import provider_config, resource_manager, secret

SWAPPER = "0x0000000000000000000000000000000000000A11"
SPENDER = "0xDc264714F68d84CF29BC605589405E78bDBE7C9f"

#: Форма ответа снята с боевого API 21.09. Числа приходят **строками**, а
#: часть их лежит во вложенной структуре — на этом и ломалась подпись.
PERMIT_DATA: dict[str, Any] = {
    "domain": {
        "name": "Permit2",
        "chainId": 137,
        "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
    },
    "types": {
        "PermitSingle": [
            {"name": "details", "type": "PermitDetails"},
            {"name": "spender", "type": "address"},
            {"name": "sigDeadline", "type": "uint256"},
        ],
        "PermitDetails": [
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint160"},
            {"name": "expiration", "type": "uint48"},
            {"name": "nonce", "type": "uint48"},
        ],
    },
    "values": {
        "details": {
            "token": str(f.USDT.address),
            "amount": "1461501637330902918203684832716283019655932542975",
            "expiration": "1792584434",
            "nonce": "0",
        },
        "spender": SPENDER,
        "sigDeadline": "1789994234",
    },
}

QUOTE_BODY = {
    "input": {"amount": "100000000", "token": str(f.USDT.address)},
    "output": {
        "amount": "5140000000000000000",
        "minimumAmount": "5139486000000000000",
        "token": str(f.AAVE.address),
    },
    "gasUseEstimate": "230000",
    "route": [[{"type": "v3-pool", "address": "0xpool1", "fee": "500"}]],
}

SWAP_BODY = {
    "swap": {
        "to": SPENDER,
        "data": "0xdeadbeef",
        "value": "0",
        "gasLimit": "250000",
        "chainId": 137,
    }
}


def _wallet() -> TradingWallet:
    """Кошелёк на одноразовом ключе. Боевой ключ в тестах не участвует."""
    return TradingWallet(SecretValue("K", str(Account.create().key.hex())))


class _Recorder:
    """Клиент, отвечающий по пути и запоминающий отправленное."""

    def __init__(self, *, with_permit: bool) -> None:
        self.bodies: dict[str, Any] = {}
        self._with_permit = with_permit

    def __call__(self, request: Any) -> HttpResponse:
        path = request.url.rsplit(endpoints.DEFAULT_BASE_URL, 1)[-1]
        self.bodies[path] = request.json_body
        if path == endpoints.QUOTE_PATH:
            payload: dict[str, Any] = {"routing": "CLASSIC", "quote": QUOTE_BODY}
            if self._with_permit:
                payload["permitData"] = PERMIT_DATA
            return HttpResponse(status_code=200, text=json.dumps(payload), headers={})
        return HttpResponse(status_code=200, text=json.dumps(SWAP_BODY), headers={})


def _adapter(recorder: _Recorder, *, signer: TradingWallet | None) -> UniswapAdapter:
    clock = FakeClock(f.NOW)
    return UniswapAdapter(
        provider_config(ProviderId.UNISWAP, options={"swapper": SWAPPER}),
        http=FakeHttpClient(handler=recorder),
        resources=resource_manager(clock),
        clock=clock,
        api_key=secret(),
        signer=signer,
    )


def _request() -> QuoteRequest:
    return QuoteRequest(
        network_id=f.POLYGON,
        operation=OperationType.SELL,
        input_token=f.USDT,
        output_token=f.AAVE,
        input_amount=f.USDT.amount_from_base_units(100_000_000),
        request_id=RequestId.generate(),
        slippage_bps=1,
    )


class TestPermitIsSigned:
    async def test_the_signature_is_sent_with_the_swap(self) -> None:
        recorder = _Recorder(with_permit=True)
        wallet = _wallet()
        await _adapter(recorder, signer=wallet).build_swap(_request())

        body = recorder.bodies[endpoints.SWAP_PATH]
        assert body["permitData"] == PERMIT_DATA, "разрешение уходит как пришло"
        assert body["signature"].startswith("0x")

    async def test_the_signature_belongs_to_the_swapper(self) -> None:
        """Подпись должна быть нашей, иначе цепь её отвергнет.

        Проверка восстанавливает подписанта из подписи: сервер её не
        сверяет, а контракт сверит.
        """
        recorder = _Recorder(with_permit=True)
        wallet = _wallet()
        await _adapter(recorder, signer=wallet).build_swap(_request())

        signature = recorder.bodies[endpoints.SWAP_PATH]["signature"]
        message = encode_typed_data(
            full_message={
                "domain": PERMIT_DATA["domain"],
                "types": PERMIT_DATA["types"],
                "primaryType": "PermitSingle",
                "message": {
                    "details": {
                        "token": PERMIT_DATA["values"]["details"]["token"],
                        "amount": int(PERMIT_DATA["values"]["details"]["amount"]),
                        "expiration": int(PERMIT_DATA["values"]["details"]["expiration"]),
                        "nonce": int(PERMIT_DATA["values"]["details"]["nonce"]),
                    },
                    "spender": PERMIT_DATA["values"]["spender"],
                    "sigDeadline": int(PERMIT_DATA["values"]["sigDeadline"]),
                },
            }
        )
        assert Account.recover_message(message, signature=signature) == wallet.address

    async def test_without_a_permit_nothing_extra_is_sent(self) -> None:
        """Разрешение уже на счёте — API его не просит, и мы не шлём."""
        recorder = _Recorder(with_permit=False)
        await _adapter(recorder, signer=_wallet()).build_swap(_request())

        body = recorder.bodies[endpoints.SWAP_PATH]
        assert set(body) == {"quote"}

    async def test_a_required_permit_without_a_signer_is_refused(self) -> None:
        """Собрать обмен, который заведомо откатится, нельзя.

        Отказ громкий нарочно: молчаливая сборка дала бы транзакцию,
        которая сожгла бы газ и ничего не обменяла.
        """
        recorder = _Recorder(with_permit=True)

        with pytest.raises(DataError):
            await _adapter(recorder, signer=None).build_swap(_request())


class TestNestedNumbersAreCoerced:
    def test_nested_numbers_become_integers(self) -> None:
        """Числа приводятся по объявленному типу на любой глубине.

        ``eth_account`` числовые строки принимает и сам — подпись выходит
        верной и без приведения, это проверено замером. Приведение всё
        же доводится до конца, чтобы поведение было явным, а не зависело
        от необъявленной любезности библиотеки.
        """
        coerced = TradingWallet._coerce(PERMIT_DATA["values"], "PermitSingle", PERMIT_DATA["types"])

        assert coerced["details"]["amount"] == int(PERMIT_DATA["values"]["details"]["amount"])
        assert coerced["details"]["nonce"] == 0
        assert coerced["sigDeadline"] == int(PERMIT_DATA["values"]["sigDeadline"])
        assert coerced["details"]["token"] == PERMIT_DATA["values"]["details"]["token"], (
            "адрес остался строкой"
        )

    def test_the_signature_is_the_same_either_way(self) -> None:
        """Приведение подпись не меняет — и не должно.

        Проверка стоит здесь как страховка: если однажды библиотека
        перестанет принимать строки, упадёт этот тест, а не боевая
        сделка.
        """
        wallet = _wallet()
        base = {
            "domain": PERMIT_DATA["domain"],
            "types": PERMIT_DATA["types"],
            "primaryType": "PermitSingle",
        }
        as_strings = wallet.sign_typed_data({**base, "message": PERMIT_DATA["values"]})
        as_numbers = wallet.sign_typed_data(
            {
                **base,
                "message": TradingWallet._coerce(
                    PERMIT_DATA["values"], "PermitSingle", PERMIT_DATA["types"]
                ),
            }
        )

        assert as_strings == as_numbers
