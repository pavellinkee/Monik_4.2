"""Режим ann — торговый проход.

Отличается от ``ur`` и ``fest`` тремя вещами (``the_main_rules.md``,
правило 11): сканирует **все** суммы сразу, выбирает лучшую находку по
**заработку в базовом токене**, а не по доходности в процентах, и может
работать в собственном подмножестве сетей.

Само исполнение сделок — отдельная подсистема; пока её нет, найденное
режимом только записывается в журнал и возможностей не создаёт.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from monik.config import parse_configuration
from monik.domain.enums.modes import ScanMode
from monik.domain.enums.resources import RequestPriority
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.db import Database
from monik.services.observability import FakeClock
from tests.component.level1.conftest import Level1Harness, build_harness, level1_document
from tests.component.level1.test_multi_network import two_network_document
from tests.unit.config.conftest import VALID_ENV

POLYGON = NetworkId("polygon")
ARBITRUM = NetworkId("arbitrum")


def _stable_document(**overrides: Any) -> dict[str, Any]:
    """Набор, в котором есть стабильные и нестабильные токены."""
    document = level1_document()
    for token in document["tokens"]:
        if token["symbol"] in {"USDT", "AAVE"}:
            token["usd_stable"] = True
    document["tokens"].append(
        {
            "network_id": "polygon",
            "address": "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",
            "symbol": "WETH",
            "decimals": 18,
            "rank": 4,
        }
    )
    document["scanner"]["amounts"] = ["50", "100"]
    document["scanner"]["modes"] = {"ann": {"enabled": True, "interval_seconds": 30}}
    document["scanner"]["level1"] = {"amount": "50", "scan_timeout_seconds": 30}
    document["profitability"] = {"thresholds": {"ur": "-100", "fest": "-100", "ann": "-100"}}
    document.update(overrides)
    return document


def _harness(document: dict[str, Any], database: Database, clock: FakeClock) -> Level1Harness:
    configuration = parse_configuration(document, environ=dict(VALID_ENV)).config
    return build_harness(configuration, database, clock)


class TestScope:
    def test_ann_scans_every_configured_amount(self, database: Database, clock: FakeClock) -> None:
        """Все суммы сразу: размер сделки — часть решения, а не проверка."""
        harness = _harness(_stable_document(), database, clock)

        ann = harness.scanner.scopes(ScanMode.ANN)[0]
        ur = harness.scanner.scopes(ScanMode.UR)[0]

        assert len(ann.raw_amounts) == 2
        assert ann.raw_amounts == (50_000_000, 100_000_000)
        assert len(ur.raw_amounts) == 1, "ur по-прежнему ищет одной суммой"

    def test_ann_takes_every_enabled_token(self, database: Database, clock: FakeClock) -> None:
        """Торговый проход не ограничен стейблкоинами (правило 15).

        Состав задаёт конфигурация: включён токен — режим его берёт.
        Метка ``usd_stable`` отбор ``ann`` не сужает, иначе волатильный
        токен, который оператор включил намеренно, остался бы невидимым.
        """
        harness = _harness(_stable_document(), database, clock)

        scope = harness.scanner.scopes(ScanMode.ANN)[0]
        symbols = {harness.tokens.require(key).symbol for key in scope.tokens}

        assert symbols == {"AAVE", "WETH"}, "включённый волатильный токен входит в проход"

    def test_fest_still_takes_only_stable_tokens(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Правило 15 меняет только ``ann``: отбор ``fest`` прежний."""
        document = _stable_document()
        document["scanner"]["modes"]["fest"] = {"enabled": True, "interval_seconds": 30}
        harness = _harness(document, database, clock)

        scope = harness.scanner.scopes(ScanMode.FEST)[0]
        symbols = {harness.tokens.require(key).symbol for key in scope.tokens}

        assert symbols == {"AAVE"}, "WETH не помечен usd_stable и в частый проход не входит"

    def test_declared_but_switched_off_network_is_not_scanned(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Торговый проход начинают в одной сети, пока остальные наблюдаются."""
        document = two_network_document()
        document["scanner"]["modes"] = {
            "ann": {
                "enabled": True,
                "interval_seconds": 30,
                "networks": {"polygon": False, "arbitrum": True},
            }
        }
        document["scanner"]["level1"] = {"scan_timeout_seconds": 30}
        document["profitability"] = {"thresholds": {"ur": "-100", "fest": "-100", "ann": "-100"}}
        harness = _harness(document, database, clock)

        assert [s.networks for s in harness.scanner.scopes(ScanMode.ANN)] == [(ARBITRUM,)]
        assert [s.networks for s in harness.scanner.scopes(ScanMode.UR)] == [
            (POLYGON,),
            (ARBITRUM,),
        ]

    def test_mode_cannot_enable_a_disabled_network(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Режим сужает набор сетей, но не включает выключенную."""
        document = two_network_document()
        document["networks"][1]["enabled"] = False
        document["tokens"] = [t for t in document["tokens"] if t["network_id"] != "arbitrum"]
        document["scanner"]["modes"] = {
            "ann": {
                "enabled": True,
                "interval_seconds": 30,
                "networks": {"polygon": True, "arbitrum": True},
            }
        }
        document["scanner"]["level1"] = {"scan_timeout_seconds": 30}
        document["profitability"] = {"thresholds": {"ur": "-100", "fest": "-100", "ann": "-100"}}
        harness = _harness(document, database, clock)

        # Проверяется именно сеть: Arbitrum выключен в networks, и
        # разрешение режима его не воскрешает. Раньше здесь ожидался
        # пустой список сцен — но пустым он был из-за отбора по
        # ``usd_stable``, а не из-за сети, и после правила 15 такая
        # проверка перестала бы говорить о сетях вовсе.
        assert [scope.networks for scope in harness.scanner.scopes(ScanMode.ANN)] == [(POLYGON,)]


class TestSelection:
    async def test_no_opportunity_is_created_without_a_consumer(
        self, database: Database, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Возможность без потребителя не создаётся, но проход выполняется.

        Иначе находка осталась бы в состоянии CREATED навсегда. Журнал при
        этом показывает, какая сделка состоялась бы, — это и есть сухой
        прогон режима до появления подсистемы исполнения.
        """
        configuration = parse_configuration(_stable_document(), environ=dict(VALID_ENV)).config
        harness = build_harness(configuration, database, clock)
        harness.scanner._dispatch_modes = frozenset({ScanMode.UR, ScanMode.FEST})

        with caplog.at_level("INFO", logger="monik.services.level1.scanner"):
            result = (await harness.scanner.scan_all(ScanMode.ANN))[0]

        assert result.opportunities == ()
        assert not harness.dispatcher.submitted
        observed = [
            r for r in caplog.records if r.getMessage() == "opportunity observed without a consumer"
        ]
        assert observed, "проход обязан сообщить, что он нашёл бы"
        fields = observed[0].monik_fields
        assert fields["mode"] == "ann"
        assert Decimal(fields["net_profit"]) > 0

    async def test_the_reported_amount_is_the_most_profitable_one(
        self, database: Database, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Выбирается сумма с наибольшим заработком, а не с наибольшим процентом.

        Курс у тестовых адаптеров одинаков для всех сумм, поэтому больший
        заработок даёт большая сумма — на ней и должен остановиться выбор.
        """
        configuration = parse_configuration(_stable_document(), environ=dict(VALID_ENV)).config
        harness = build_harness(configuration, database, clock)
        harness.scanner._dispatch_modes = frozenset({ScanMode.UR})

        with caplog.at_level("INFO", logger="monik.services.level1.scanner"):
            await harness.scanner.scan_all(ScanMode.ANN)

        fields = (
            next(
                r
                for r in caplog.records
                if r.getMessage() == "opportunity observed without a consumer"
            )
        ).monik_fields
        # Лучшая по заработку сумма — большая из проверенных.
        assert Decimal(fields["amount"]) == Decimal(100)


class TestModeAmounts:
    """Суммы торгового прохода задаются отдельно от общих.

    Суммы ``scanner.amounts`` — это вопрос анализа: ими Level 2
    проверяет найденную возможность, и от денег на кошельке они не
    зависят. Суммы режима ``ann`` — наоборот, ограничены остатком счёта:
    сделка на сумму, которой нет, не состоится. Один общий список
    заставлял бы менять одно ради другого.
    """

    def test_mode_amounts_replace_the_common_list(
        self, database: Database, clock: FakeClock
    ) -> None:
        document = _stable_document()
        document["scanner"]["amounts"] = ["50", "100", "300", "500"]
        document["scanner"]["modes"] = {
            "ann": {"enabled": True, "interval_seconds": 30, "amounts": ["50", "100"]}
        }
        harness = _harness(document, database, clock)

        ann = harness.scanner.scopes(ScanMode.ANN)[0]

        assert ann.raw_amounts == (50_000_000, 100_000_000)

    def test_common_list_is_used_when_the_mode_has_none(
        self, database: Database, clock: FakeClock
    ) -> None:
        document = _stable_document()
        document["scanner"]["amounts"] = ["50", "100", "300"]
        document["scanner"]["modes"] = {"ann": {"enabled": True, "interval_seconds": 30}}
        harness = _harness(document, database, clock)

        ann = harness.scanner.scopes(ScanMode.ANN)[0]

        assert ann.raw_amounts == (50_000_000, 100_000_000, 300_000_000)

    def test_empty_mode_amounts_are_rejected(self) -> None:
        """Пустой список — не «как обычно», а молча выключенный режим."""
        document = _stable_document()
        document["scanner"]["modes"] = {
            "ann": {"enabled": True, "interval_seconds": 30, "amounts": []}
        }

        with pytest.raises(Exception, match="mode amounts must not be empty"):
            parse_configuration(document, environ=dict(VALID_ENV))


class TestModeTimeout:
    """Срок прохода принадлежит режиму и следует за его интервалом.

    Оператор задаёт частоту, а срок ей соответствует: поставит 10 —
    проход получит 10, поставит 45 — получит 45
    (``the_main_rules.md``, правило 28). Так наложение проходов
    становится невозможным по построению, а не по проверке.
    """

    def test_the_deadline_follows_the_interval(self, database: Database, clock: FakeClock) -> None:
        """Без собственного значения срок равен интервалу режима.

        Прежде он брался из общей настройки, и подняв интервал, оператор
        получал прежний срок: два числа приходилось держать
        согласованными руками.
        """
        document = _stable_document()
        document["scanner"]["level1"] = {"amount": "50", "scan_timeout_seconds": 240}
        document["scanner"]["modes"] = {
            "ur": {"enabled": True, "interval_seconds": 300},
            "ann": {"enabled": True, "interval_seconds": 45},
        }
        configuration = parse_configuration(document, environ=dict(VALID_ENV)).config

        assert configuration.scanner.scan_timeout_for(ScanMode.ANN) == 45
        assert configuration.scanner.scan_timeout_for(ScanMode.UR) == 300

    def test_an_own_deadline_still_wins(self, database: Database, clock: FakeClock) -> None:
        """Явно заданный срок остаётся главнее вывода из интервала."""
        document = _stable_document()
        document["scanner"]["level1"] = {"amount": "50", "scan_timeout_seconds": 240}
        document["scanner"]["modes"] = {
            "ur": {"enabled": True, "interval_seconds": 300},
            "ann": {"enabled": True, "interval_seconds": 45, "scan_timeout_seconds": 10},
        }
        configuration = parse_configuration(document, environ=dict(VALID_ENV)).config

        assert configuration.scanner.scan_timeout_for(ScanMode.ANN) == 10

    def test_deadline_longer_than_the_mode_interval_is_rejected(self) -> None:
        document = _stable_document()
        document["scanner"]["modes"] = {
            "ann": {"enabled": True, "interval_seconds": 10, "scan_timeout_seconds": 30}
        }

        with pytest.raises(Exception, match="must not exceed the mode interval"):
            parse_configuration(document, environ=dict(VALID_ENV))

    def test_a_large_common_deadline_no_longer_binds_a_fast_mode(self) -> None:
        """Общий срок больше не диктует режиму с интервалом.

        Прежде такая конфигурация отвергалась целиком. Теперь быстрый
        режим просто берёт свой интервал, и наложиться его проходы не
        могут: срок равен интервалу.
        """
        document = _stable_document()
        document["scanner"]["level1"] = {"amount": "50", "scan_timeout_seconds": 240}
        document["scanner"]["modes"] = {"ann": {"enabled": True, "interval_seconds": 10}}

        configuration = parse_configuration(document, environ=dict(VALID_ENV)).config

        assert configuration.scanner.scan_timeout_for(ScanMode.ANN) == 10


class TestModePriorities:
    """Правило приоритета принадлежит режиму, а не системе целиком.

    ``the_main_rules.md``, правило 13. У ``ur`` и ``fest`` работа делится
    на поиск и подтверждение, у ``ann`` — на сканирование, покупку и
    продажу. Общего порядка для них не существует, поэтому каждый режим
    ставит свой.
    """

    def _document(self) -> dict[str, Any]:
        document = _stable_document()
        document["scanner"]["modes"] = {
            "ur": {"enabled": True, "interval_seconds": 300},
            "ann": {"enabled": True, "interval_seconds": 30},
        }
        document["scanner"]["level1"] = {"amount": "50", "scan_timeout_seconds": 30}
        return document

    async def test_trading_scan_asks_with_its_own_priority(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Сканирование ann не называется ни Level 1, ни Level 2."""
        harness = _harness(self._document(), database, clock)

        await harness.scanner.scan_all(ScanMode.ANN)

        asked = {
            request.priority
            for adapter in harness.adapters.values()
            for request in adapter.quote_calls
        }
        assert asked == {RequestPriority.ANN_SCAN}

    async def test_search_mode_asks_with_its_own_priorities(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Внутри ur порядок прежний, но метки теперь его собственные."""
        harness = _harness(self._document(), database, clock)

        await harness.scanner.scan_all(ScanMode.UR)

        asked = {
            request.priority
            for adapter in harness.adapters.values()
            for request in adapter.quote_calls
        }
        assert asked <= {RequestPriority.UR_LEVEL1_BUY, RequestPriority.UR_LEVEL1_SELL}
        assert RequestPriority.ANN_SCAN not in asked

    async def test_frequent_mode_asks_with_its_own_priorities(
        self, database: Database, clock: FakeClock
    ) -> None:
        """fest помечает свои запросы своими метками, не общими с ur."""
        document = self._document()
        document["scanner"]["modes"]["fest"] = {"enabled": True, "interval_seconds": 30}
        harness = _harness(document, database, clock)

        await harness.scanner.scan_all(ScanMode.FEST)

        asked = {
            request.priority
            for adapter in harness.adapters.values()
            for request in adapter.quote_calls
        }
        assert asked <= {RequestPriority.FEST_LEVEL1_BUY, RequestPriority.FEST_LEVEL1_SELL}

    async def test_trading_scan_outranks_every_search_request(
        self, database: Database, clock: FakeClock
    ) -> None:
        """Между режимами ann выше fest, а fest выше ur.

        Даже сканирование торгового режима — самое низкое из его трёх
        занятий — обслуживается раньше подтверждения Level 2 у ur.
        """
        assert RequestPriority.ANN_SCAN.rank < RequestPriority.FEST_LEVEL2.rank
        assert RequestPriority.FEST_LEVEL1_BUY.rank < RequestPriority.UR_LEVEL2.rank
