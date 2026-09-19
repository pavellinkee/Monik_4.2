"""Учётные данные требуются только тем провайдерам, которым они нужны.

Проверка ``production`` раньше требовала ссылку на секрет у каждого
включённого провайдера. Публичному API приходилось задавать переменную
с произвольным значением — заводить секрет, которого нет. Правило 8
назвало это обходом и указало правильное решение; здесь оно проверяется.
"""

from __future__ import annotations

import pytest

from monik.domain.enums.providers import ProviderId, requires_credentials


class TestCredentialRequirement:
    @pytest.mark.parametrize(
        "provider_id",
        [ProviderId.ONEINCH, ProviderId.ZERO_X, ProviderId.UNISWAP, ProviderId.KYBERSWAP],
    )
    def test_providers_with_keys_still_require_them(self, provider_id: ProviderId) -> None:
        assert requires_credentials(provider_id) is True

    @pytest.mark.parametrize("provider_id", [ProviderId.VELORA, ProviderId.COW])
    def test_public_apis_do_not_require_credentials(self, provider_id: ProviderId) -> None:
        """Публичный API ключа не требует, и выдумывать его не нужно."""
        assert requires_credentials(provider_id) is False

    def test_every_provider_is_classified(self) -> None:
        """Новый провайдер обязан получить ответ, а не значение по умолчанию."""
        for provider_id in ProviderId:
            assert isinstance(requires_credentials(provider_id), bool)
