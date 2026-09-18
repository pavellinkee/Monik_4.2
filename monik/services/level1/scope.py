"""Определение границ одного цикла Level 1.

Scope полностью определяется конфигурацией и реестрами
(``02_LEVEL1_SCANNER.md`` §5, §68): списки сетей, токенов, сумм и
провайдеров в коде не зашиты. Изменение конфигурации применяется со
следующего цикла (``02_LEVEL1_SCANNER.md`` §69), поэтому scope
фиксируется на старте.

Цикл всегда принадлежит **одной** сети: BUY и SELL одной возможности
обязаны относиться к одной сети, а межсетевой арбитраж в текущий
workflow не входит (``10_LEVEL_1_SCANNER.md`` §38). Поэтому каждая
включённая сеть получает собственный scope, а не общий на всех: так
комбинация из разных сетей не может возникнуть даже случайно.
"""

from __future__ import annotations

from monik.config.root import Configuration
from monik.domain.enums.modes import ScanMode
from monik.domain.enums.providers import ProviderId
from monik.domain.models.scan import ScanScope
from monik.domain.models.token import Token
from monik.domain.value_objects.identity import NetworkId
from monik.services.observability.clock import Clock
from monik.services.registries.networks import NetworkRegistry
from monik.services.registries.providers import ProviderRegistry
from monik.services.registries.tokens import TokenRegistry

__all__ = ["ScopeBuilder"]


class ScopeBuilder:
    """Строит :class:`ScanScope` из актуальной конфигурации."""

    def __init__(
        self,
        configuration: Configuration,
        *,
        networks: NetworkRegistry,
        tokens: TokenRegistry,
        providers: ProviderRegistry,
        clock: Clock,
    ) -> None:
        self._configuration = configuration
        self._networks = networks
        self._tokens = tokens
        self._providers = providers
        self._clock = clock

    def scan_networks(self, mode: ScanMode | None = None) -> tuple[NetworkId, ...]:
        """Сети, которые сканируются в этом такте.

        Выключенная сеть не участвует в scan (``02_LEVEL1_SCANNER.md``
        §72), поэтому оператору достаточно снять ``enabled`` у сети.
        Режим может сузить набор своим списком, но не расширить его:
        выключенная сеть остаётся выключенной для всех.
        """
        enabled = tuple(network.network_id for network in self._networks.enabled())
        if mode is None:
            return enabled
        declared = self._configuration.scanner.modes.for_mode(mode).networks
        if declared is None:
            return enabled
        return tuple(network_id for network_id in enabled if declared.get(network_id, False))

    def build(self, network_id: NetworkId, mode: ScanMode) -> ScanScope | None:
        """Собрать scope прохода одной сети в заданном режиме.

        Отключённые сети, токены и провайдеры в scope не попадают
        (``02_LEVEL1_SCANNER.md`` §70-72). ``None`` означает, что проходу
        нечего делать: нет работающего провайдера этого режима или нет
        подходящих токенов. Пустой цикл не создаётся — он только засорял
        бы историю.
        """
        if not self._networks.is_enabled(network_id):
            return None
        providers = tuple(
            provider_id
            for provider_id in self.active_providers(network_id)
            if self._providers.participates_in(provider_id, mode)
        )
        tokens = self.mode_tokens(network_id, mode)
        if not providers or not tokens:
            return None
        return ScanScope(
            mode=mode,
            networks=(network_id,),
            providers=providers,
            tokens=tuple(token.key for token in tokens),
            raw_amounts=self._raw_amounts(self._tokens.base_token(network_id), mode),
        )

    def mode_tokens(self, network_id: NetworkId, mode: ScanMode) -> tuple[Token, ...]:
        """Токены, которые проверяет этот режим.

        Отбор описан здесь, а не в конфигурации: у каждого режима своя
        логика. ``ur`` берёт весь набор, ``fest`` — только помеченные
        ``usd_stable``, поэтому новый стабильный токен попадает в частый
        проход, как только получит метку, без правки списков.

        ``ann`` берёт весь включённый набор (``the_main_rules.md``,
        правило 15). Метка ``usd_stable`` его состав не ограничивает:
        расхождение цен между стейблкоинами меньше стоимости круга, и
        торговый проход, запертый в них, не может взять никакого порога.
        Чем торговать, решает включённость токена в конфигурации.
        """
        tokens = self.scan_tokens(network_id)
        if mode is ScanMode.FEST:
            return tuple(token for token in tokens if token.usd_stable)
        return tokens

    def active_providers(self, network_id: NetworkId) -> tuple[ProviderId, ...]:
        """Провайдеры, работающие с этой сетью прямо сейчас.

        Кроме включённости и заявленной сети учитываются часы работы:
        вне своего окна провайдер не опрашивается вовсе. Запрос не
        отправляется и не отклоняется — его просто не возникает, поэтому
        расписание не отражается ни на статистике отказов, ни на
        состоянии здоровья.
        """
        now = self._clock.now()
        return tuple(
            provider.provider_id
            for provider in self._providers.active(now)
            if self._providers.declares_network(provider.provider_id, network_id)
        )

    def scan_tokens(self, network_id: NetworkId) -> tuple[Token, ...]:
        """Промежуточные токены сети, ограниченные Top-N (§6)."""
        return self._tokens.scan_tokens(network_id)

    def base_token(self, network_id: NetworkId) -> Token:
        """Базовый токен сети: вход и выход round-trip (``10_LEVEL_1_SCANNER.md`` §37)."""
        return self._tokens.base_token(network_id)

    def _raw_amounts(self, base_token: Token, mode: ScanMode) -> tuple[int, ...]:
        """Суммы прохода в base units базового токена сети.

        Режимы ``ur`` и ``fest`` ищут **одной** суммой: стоимость поиска
        не должна расти вместе с числом сумм, которые предстоит проверить
        Level 2 (``the_main_rules.md``, правило 1).

        Режим ``ann`` проверяет **все** суммы сразу (правило 11): он не
        передаёт находку на второй этап, а исполняет её сам, и размер
        сделки — часть решения, а не последующая проверка. Суммы берутся
        его собственные, если заданы: торговый проход ограничен остатком
        счёта, а суммы проверки Level 2 — вопрос анализа.

        Пересчёт делается для каждой сети отдельно: знаки базового токена
        у сетей могут различаться.
        """
        scanner = self._configuration.scanner
        amounts = scanner.amounts_for(mode) if mode is ScanMode.ANN else (scanner.level1_amount,)
        return tuple(base_token.amount_from_decimal(str(amount)).raw for amount in amounts)
