"""Тесты загрузки, валидации и нормализации конфигурации."""

from __future__ import annotations

import pathlib
from decimal import Decimal
from typing import Any

import pytest

from monik.config import (
    Configuration,
    load_configuration,
    parse_configuration,
)
from monik.config.sections import Environment, GasSource, PriceSource
from monik.domain.enums import NotificationMode, OverlapPolicy, ProviderId, ThresholdMetric
from monik.domain.enums.modes import ScanMode
from monik.domain.errors import ConfigurationError
from monik.domain.value_objects.identity import NetworkId
from monik.services.observability.redaction import SecretRegistry

from .conftest import AAVE_ADDRESS, USDT_ADDRESS


def _load(document: dict[str, Any], env: dict[str, str]) -> Configuration:
    return parse_configuration(document, environ=env).config


class TestValidConfiguration:
    def test_loads_minimal_document(self, document: dict[str, Any], env: dict[str, str]) -> None:
        config = _load(document, env)
        assert config.application.environment is Environment.DEVELOPMENT
        assert len(config.enabled_networks) == 1
        assert len(config.enabled_providers) == 2

    def test_applies_architecture_defaults(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        """Дефолты соответствуют утверждённым значениям."""
        config = _load(document, env)
        assert config.scanner.modes.ur.interval_seconds == 300
        assert config.scanner.level1.overlap_policy is OverlapPolicy.SKIP
        assert config.scanner.level1.top_tokens == 30
        assert config.scanner.level2.max_parallel == 20
        assert config.resources.retry.max_attempts == 3
        assert config.profitability.threshold_for(ScanMode.UR) == Decimal("1.00")
        assert config.profitability.threshold_metric is ThresholdMetric.NET_ROI
        assert config.notifications.mode is NotificationMode.A
        # Цена газа из котировки — первый источник: значение приходит
        # вместе с ценой маршрута и лишнего запроса не стоит.
        assert config.gas.sources == (
            GasSource.QUOTE,
            GasSource.ADAPTER_ESTIMATE,
            GasSource.RPC,
        )
        assert config.prices.sources == (PriceSource.AGGREGATOR_QUOTE,)

    def test_amounts_are_exact_decimals(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        """Финансовые значения не приводятся к float (17 §20)."""
        document["scanner"]["amounts"] = ["0.000001", "1234.567891"]
        config = _load(document, env)
        assert config.scanner.amounts == (Decimal("0.000001"), Decimal("1234.567891"))
        assert all(isinstance(amount, Decimal) for amount in config.scanner.amounts)

    def test_provider_pairs_exclude_same_provider_by_default(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        """Кросс-провайдерная модель по умолчанию (02 §18)."""
        pairs = _load(document, env).provider_pairs()
        assert (ProviderId.ONEINCH, ProviderId.ZERO_X) in pairs
        assert all(buy is not sell for buy, sell in pairs)

    def test_scan_tokens_exclude_base_token_and_respect_top_n(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["scanner"]["level1"] = {"top_tokens": 1}
        config = _load(document, env)
        symbols = [token.symbol for token in config.scan_tokens(NetworkId("polygon"))]
        assert symbols == ["AAVE"]

    def test_version_is_deterministic(self, document: dict[str, Any], env: dict[str, str]) -> None:
        assert _load(document, env).version == _load(document, env).version

    def test_version_changes_with_content(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        first = _load(document, env).version
        document["scanner"]["amounts"] = ["100", "500", "1000"]
        assert _load(document, env).version != first

    def test_configuration_is_immutable(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        """Configuration неизменяема в пределах runtime (17 §50-51)."""
        config = _load(document, env)
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            config.scanner = config.scanner  # type: ignore[misc]


class TestInvalidConfiguration:
    def test_missing_required_section_fails(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        del document["scanner"]
        with pytest.raises(ConfigurationError, match="scanner"):
            _load(document, env)

    def test_unknown_field_is_rejected(self, document: dict[str, Any], env: dict[str, str]) -> None:
        """Опечатка не игнорируется молча (17 §11)."""
        document["scanner"]["unkown_option"] = 1
        with pytest.raises(ConfigurationError, match="unkown_option"):
            _load(document, env)

    def test_wrong_type_is_rejected(self, document: dict[str, Any], env: dict[str, str]) -> None:
        document["networks"][0]["chain_id"] = "not-a-number"
        with pytest.raises(ConfigurationError, match="chain_id"):
            _load(document, env)

    def test_out_of_range_value_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["scanner"]["level2"] = {"max_parallel": 0}
        with pytest.raises(ConfigurationError, match="max_parallel"):
            _load(document, env)

    def test_invalid_enum_is_rejected(self, document: dict[str, Any], env: dict[str, str]) -> None:
        document["application"]["environment"] = "staging"
        with pytest.raises(ConfigurationError, match="environment"):
            _load(document, env)

    def test_invalid_timezone_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["application"]["timezone"] = "Mars/Olympus"
        with pytest.raises(ConfigurationError, match="timezone"):
            _load(document, env)

    def test_invalid_time_format_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["fees"] = {"refresh_time": "25:00"}
        with pytest.raises(ConfigurationError, match="refresh_time"):
            _load(document, env)

    def test_negative_amount_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["scanner"]["amounts"] = ["-100"]
        with pytest.raises(ConfigurationError, match="amounts"):
            _load(document, env)

    def test_amount_not_representable_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        """Молчаливое округление сумм запрещено (17 §22)."""
        document["scanner"]["amounts"] = ["0.1234567"]
        with pytest.raises(ConfigurationError, match="not representable"):
            _load(document, env)

    def test_duplicate_amounts_are_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["scanner"]["amounts"] = ["100", "100"]
        with pytest.raises(ConfigurationError, match="unique"):
            _load(document, env)

    def test_invalid_token_address_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["tokens"][0]["address"] = "not-an-address"
        with pytest.raises(ConfigurationError, match="address"):
            _load(document, env)

    def test_non_https_urls_are_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["networks"][0]["rpc_url"] = "http://polygon-rpc.com"
        with pytest.raises(ConfigurationError, match="https"):
            _load(document, env)

    def test_path_traversal_in_database_path_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["database"] = {"path": "../../etc/monik.db"}
        with pytest.raises(ConfigurationError, match="traversal"):
            _load(document, env)


class TestSafetyInvariantsCannotBeDisabled:
    @pytest.mark.parametrize(
        ("section", "field"),
        [
            ("fees", "treat_unknown_as_zero"),
            ("gas", "treat_unknown_as_zero"),
        ],
    )
    def test_unknown_cost_cannot_be_treated_as_zero(
        self, document: dict[str, Any], env: dict[str, str], section: str, field: str
    ) -> None:
        """UNKNOWN никогда не равен нулю (CLAUDE.md §12, §23)."""
        document[section] = {field: True}
        with pytest.raises(ConfigurationError, match="not"):
            _load(document, env)

    def test_route_confirmation_cannot_be_disabled(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        """Level 2 обязан проверять зафиксированный маршрут (11 §18)."""
        document["scanner"]["level2"] = {"require_route_confirmation": False}
        with pytest.raises(ConfigurationError, match="require_route_confirmation"):
            _load(document, env)

    def test_retry_after_cannot_be_ignored(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["resources"] = {"retry": {"respect_retry_after": False}}
        with pytest.raises(ConfigurationError, match="respect_retry_after"):
            _load(document, env)

    def test_foreign_keys_cannot_be_disabled(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["database"] = {"foreign_keys_enabled": False}
        with pytest.raises(ConfigurationError, match="foreign_keys_enabled"):
            _load(document, env)

    def test_unknown_cost_blocking_cannot_be_disabled(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["profitability"] = {"treat_unknown_cost_as_blocking": False}
        with pytest.raises(ConfigurationError, match="treat_unknown_cost_as_blocking"):
            _load(document, env)


class TestCrossFieldValidation:
    def test_token_on_disabled_network_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["tokens"].append(
            {
                "network_id": "ethereum",
                "address": AAVE_ADDRESS,
                "symbol": "AAVE",
                "decimals": 18,
            }
        )
        with pytest.raises(ConfigurationError, match="not enabled"):
            _load(document, env)

    def test_provider_without_enabled_network_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["networks"].append(
            {
                "network_id": "ethereum",
                "name": "Ethereum",
                "chain_id": 1,
                "native_token_symbol": "ETH",
                "wrapped_native_address": AAVE_ADDRESS,
                "base_token_address": AAVE_ADDRESS,
                "enabled": False,
            }
        )
        document["providers"][0]["supported_networks"] = ["ethereum"]
        with pytest.raises(ConfigurationError, match="supports none"):
            _load(document, env)

    def test_provider_referencing_unknown_network_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["providers"][0]["supported_networks"] = ["polygon", "arbitrum"]
        with pytest.raises(ConfigurationError, match="unknown networks"):
            _load(document, env)

    def test_route_policy_referencing_disabled_provider_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["routes"] = {"allowed_pairs": [{"buy": "oneinch", "sell": "velora"}]}
        with pytest.raises(ConfigurationError, match="not enabled"):
            _load(document, env)

    def test_single_provider_without_same_provider_leaves_no_pairs(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["providers"] = [document["providers"][0]]
        with pytest.raises(ConfigurationError, match="no usable provider pair"):
            _load(document, env)

    def test_unknown_base_token_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["networks"][0]["base_token_address"] = AAVE_ADDRESS
        document["tokens"] = [document["tokens"][0]]
        with pytest.raises(ConfigurationError, match="unknown or disabled"):
            _load(document, env)

    def test_base_token_only_leaves_nothing_to_scan(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["tokens"] = [document["tokens"][0]]
        with pytest.raises(ConfigurationError, match="besides the base token"):
            _load(document, env)

    def test_no_enabled_provider_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        for provider in document["providers"]:
            provider["enabled"] = False
        with pytest.raises(ConfigurationError, match="at least one provider"):
            _load(document, env)

    def test_duplicate_token_identity_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["tokens"].append(dict(document["tokens"][0]))
        with pytest.raises(ConfigurationError, match="duplicate token"):
            _load(document, env)

    def test_mode_without_a_threshold_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        """Режим без порога — незаданная политика, а не значение по умолчанию."""
        document["profitability"] = {"thresholds": {"ur": "0.10"}}
        with pytest.raises(ConfigurationError, match="threshold is not set"):
            _load(document, env)

    def test_the_scan_deadline_follows_the_mode_interval(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        """Срок прохода выводится из интервала режима, а не из общего.

        Прежде общий срок, превышающий самый частый режим, отвергал всю
        конфигурацию. Теперь режим берёт свой интервал, и наложение
        невозможно по построению: срок равен интервалу
        (``the_main_rules.md``, правило 28).
        """
        document["scanner"]["modes"] = {"fest": {"enabled": True, "interval_seconds": 60}}
        document["scanner"]["level1"] = {"scan_timeout_seconds": 120}

        configuration = _load(document, env)

        assert configuration.scanner.scan_timeout_for(ScanMode.FEST) == 60

    def test_an_own_scan_deadline_may_not_exceed_its_interval(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        """Явно заданный срок по-прежнему обязан укладываться в интервал."""
        document["scanner"]["modes"] = {
            "fest": {"enabled": True, "interval_seconds": 60, "scan_timeout_seconds": 120}
        }
        with pytest.raises(ConfigurationError, match="must not exceed the mode interval"):
            _load(document, env)

    def test_all_modes_disabled_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["scanner"]["modes"] = {"ur": {"enabled": False}, "fest": {"enabled": False}}
        with pytest.raises(ConfigurationError, match="at least one scan mode"):
            _load(document, env)


class TestProductionSafety:
    def _production(self, document: dict[str, Any]) -> dict[str, Any]:
        document["application"]["environment"] = "production"
        return document

    def test_debug_logging_is_rejected_in_production(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document = self._production(document)
        document["logging"] = {"level": "DEBUG"}
        with pytest.raises(ConfigurationError, match="DEBUG logging"):
            _load(document, env)

    def test_provider_without_credentials_is_rejected_in_production(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document = self._production(document)
        del document["providers"][0]["api_key"]
        with pytest.raises(ConfigurationError, match="without a credentials reference"):
            _load(document, env)

    def test_integrity_check_cannot_be_disabled_in_production(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document = self._production(document)
        document["database"] = {"integrity_check_on_startup": False}
        with pytest.raises(ConfigurationError, match="integrity_check_on_startup"):
            _load(document, env)

    def test_valid_production_configuration_loads(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        config = _load(self._production(document), env)
        assert config.application.is_production


class TestFileLoading:
    def test_missing_file_is_reported(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            load_configuration(tmp_path / "absent.yaml", environ={})

    def test_malformed_yaml_is_reported(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("application: [unclosed\n", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="not valid YAML"):
            load_configuration(path, environ={})

    def test_non_mapping_document_is_reported(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "list.yaml"
        path.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="mapping"):
            load_configuration(path, environ={})

    def test_shipped_example_is_valid(self, repo_root: pathlib.Path) -> None:
        """config/config.example.yaml обязан оставаться валидным."""
        loaded = load_configuration(
            repo_root / "config" / "config.example.yaml",
            environ={
                "MONIK_ONEINCH_API_KEY": "example-key-value-1",
                "MONIK_ZEROX_API_KEY": "example-key-value-2",
            },
            registry=SecretRegistry(),
        )
        assert loaded.config.networks[0].base_token_address == USDT_ADDRESS.lower()
        assert len(loaded.secrets) == 2


class TestScheduledIntervalSource:
    """Период задачи задаётся настройкой её подсистемы, а не расписанием.

    Одно и то же значение в двух местах опасно: работает одно, а
    расхождение остаётся незамеченным. Так уже случилось с периодом
    Level 1 — настройка в разделе scanner не влияла ни на что.
    """

    def test_schedule_takes_the_period_from_the_subsystem(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["scanner"]["modes"] = {"ur": {"interval_seconds": 300}}
        document["scanner"]["level1"] = {"scan_timeout_seconds": 240}
        document["scheduler"] = {
            "enabled": True,
            "tasks": {"scan_ur": {"mode": "interval", "overlap_policy": "skip"}},
        }
        config = _load(document, env)

        assert config.scheduler.tasks["scan_ur"].interval_seconds == 300

    def test_matching_value_in_the_schedule_is_accepted(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["scanner"]["modes"] = {"ur": {"interval_seconds": 300}}
        document["scanner"]["level1"] = {"scan_timeout_seconds": 240}
        document["scheduler"] = {
            "enabled": True,
            "tasks": {
                "scan_ur": {
                    "mode": "interval",
                    "interval_seconds": 300,
                    "overlap_policy": "skip",
                }
            },
        }
        config = _load(document, env)

        assert config.scheduler.tasks["scan_ur"].interval_seconds == 300

    def test_divergent_value_stops_the_start(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        """Молчаливое расхождение опаснее отсутствия настройки."""
        document["scanner"]["modes"] = {"ur": {"interval_seconds": 300}}
        document["scanner"]["level1"] = {"scan_timeout_seconds": 240}
        document["scheduler"] = {
            "enabled": True,
            "tasks": {
                "scan_ur": {
                    "mode": "interval",
                    "interval_seconds": 600,
                    "overlap_policy": "skip",
                }
            },
        }
        with pytest.raises(ConfigurationError, match="interval"):
            _load(document, env)

    def test_other_interval_tasks_keep_their_own_period(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        """Подстановка касается только задач с собственной настройкой."""
        document["scheduler"] = {
            "enabled": True,
            "tasks": {"custom_task": {"mode": "interval", "interval_seconds": 120}},
        }
        config = _load(document, env)

        assert config.scheduler.tasks["custom_task"].interval_seconds == 120

    def test_interval_task_without_any_period_is_rejected(
        self, document: dict[str, Any], env: dict[str, str]
    ) -> None:
        document["scheduler"] = {
            "enabled": True,
            "tasks": {"custom_task": {"mode": "interval"}},
        }
        with pytest.raises(ConfigurationError, match="interval"):
            _load(document, env)


class TestModeWiring:
    """Добавление режима не должно требовать правок в центральных списках.

    Дважды подряд служба не поднималась ровно по этой причине: сборщик
    секретов и таблица расписаний перечисляли известные места руками, и
    новую подсистему в них не внесли. Тесты фиксируют, что оба места
    теперь строятся из самого набора.
    """

    def test_mode_period_lives_only_in_the_mode_settings(self) -> None:
        """Период режима не дублируется в таблице расписаний.

        Дубль означал бы, что режим, не упомянутый в scheduler.tasks,
        молча идёт с чужим темпом: ann так и пошёл раз в пять минут
        вместо тридцати секунд.
        """
        from monik.app.lifecycle import _DEFAULT_SCHEDULES, scan_task_name

        for mode in ScanMode:
            assert scan_task_name(mode) not in _DEFAULT_SCHEDULES

    def test_every_mode_has_an_interval_source(self) -> None:
        """Период задачи режима берётся из настройки этого режима."""
        from monik.config.root import _INTERVAL_SOURCES

        for mode in ScanMode:
            assert f"scan_{mode.value}" in _INTERVAL_SOURCES
