"""Composition root приложения.

Все зависимости собираются здесь **явно** (``25_PROJECT_STRUCTURE.md`` §8):
глобальных изменяемых singletons нет, каждая подсистема получает свои
зависимости через конструктор.

Порядок сборки соответствует ``CLAUDE.md`` §30: configuration → SQLite →
adapters → Resource Manager → подсистемы → Scheduler → Telegram → workers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

from monik import version_label
from monik.app.control import ScannerSwitch, TradingSwitch
from monik.config.loader import LoadedConfiguration
from monik.config.root import Configuration
from monik.config.secrets import SecretValue
from monik.config.sections.fees import GasSource, PriceSource
from monik.config.sections.providers import ProviderConfig
from monik.domain.enums.lifecycle import AmountConfirmationStatus
from monik.domain.enums.modes import ScanMode
from monik.domain.enums.notifications import DestinationKind
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import ConfigurationError
from monik.domain.models.job import ConfirmationResult
from monik.domain.models.notification import NotificationDestination
from monik.domain.models.opportunity import Opportunity
from monik.domain.models.resource import ResourceKey
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.db import Database
from monik.infrastructure.http import HttpClient, HttpxClient, UrlPolicy
from monik.infrastructure.providers.contract import AggregatorAdapter
from monik.infrastructure.providers.cow import CowAdapter
from monik.infrastructure.providers.cow import endpoints as cow_endpoints
from monik.infrastructure.providers.health_tracking import HealthTrackingAdapter
from monik.infrastructure.providers.kyberswap import KyberSwapAdapter
from monik.infrastructure.providers.kyberswap import endpoints as kyberswap_endpoints
from monik.infrastructure.providers.oneinch import OneInchAdapter
from monik.infrastructure.providers.oneinch import endpoints as oneinch_endpoints
from monik.infrastructure.providers.uniswap import UniswapAdapter
from monik.infrastructure.providers.uniswap import endpoints as uniswap_endpoints
from monik.infrastructure.providers.velora import VeloraAdapter
from monik.infrastructure.providers.velora import endpoints as velora_endpoints
from monik.infrastructure.providers.zero_x import ZeroXAdapter
from monik.infrastructure.providers.zero_x import endpoints as zero_x_endpoints
from monik.infrastructure.telegram.adapter import TelegramNotificationAdapter
from monik.infrastructure.telegram.polling import TelegramUpdateSource
from monik.repositories.sqlite import (
    SqliteCapabilityRepository,
    SqliteConfirmationRepository,
    SqliteFeeRepository,
    SqliteIdSequenceRepository,
    SqliteJobRepository,
    SqliteMetadataRepository,
    SqliteNotificationRepository,
    SqliteOpportunityRepository,
    SqliteScanRepository,
    SqliteSchedulerRepository,
    SqliteStateTransitionRepository,
)
from monik.repositories.sqlite.gas_calibration import SqliteGasCalibrationRepository
from monik.repositories.sqlite.positions import SqlitePositionRepository
from monik.services.backup import BackupService
from monik.services.calculator import ProfitCalculator
from monik.services.commands import (
    BackupStatus,
    CommandRouter,
    CommandService,
    ComponentStatus,
    ProviderStatus,
    StatsSnapshot,
)
from monik.services.fees.policy import FeePolicy, QuoteInclusiveFeePolicy
from monik.services.fees.service import FeeService
from monik.services.gas.calibration import GasCalibration
from monik.services.gas.estimator import GasEstimator
from monik.services.gas.providers import (
    GasPriceProvider,
    RpcGasPriceProvider,
    StaticGasPriceProvider,
)
from monik.services.health.monitor import HealthMonitor
from monik.services.level1 import (
    CombinationFilter,
    Level1Scanner,
    PreliminaryEvaluator,
    ScopeBuilder,
)
from monik.services.level1.no_route import NoRouteMemory
from monik.services.level2 import (
    AmountVerifier,
    ConfirmationHandler,
    Level2Financials,
    Level2Scanner,
    Level2Worker,
    RouteVerifier,
)
from monik.services.notifications import (
    MessageFormatter,
    NotificationDispatcher,
    SystemNotifier,
)
from monik.services.observability import MetricsRegistry, TransitionRecorder, names
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger
from monik.services.opportunity import OpportunityService
from monik.services.opportunity.statistics import ConfirmationStatistics
from monik.services.prices.conversion import ConversionService
from monik.services.prices.providers import (
    AggregatorQuotePriceProvider,
    HttpPriceProvider,
    TokenPriceProvider,
)
from monik.services.registries import (
    CapabilityRegistry,
    NetworkRegistry,
    OnchainTokenMetadata,
    ProviderRegistry,
    TokenAddressCheck,
    TokenRegistry,
)
from monik.services.resources import ResourceLimits, ResourceManager
from monik.services.trading import (
    ChainAccount,
    PositionWatcher,
    TradeExecutor,
    TradingWallet,
    TransactionSender,
)
from monik.services.trading.costs import GasCostConverter
from monik.services.trading.orders import SignedOrderRunner
from monik.services.updates import AptSystemUpdater, SystemUpdater

__all__ = ["Container", "Repositories", "build_container"]

_LOGGER = get_logger("app.container")

#: Фабрика HTTP-клиентов: каждая подсистема получает собственный клиент.
HttpClientFactory = Callable[[], HttpClient]

#: Базовые URL провайдеров по умолчанию: нужны для allowlist ещё до
#: создания адаптера.
_DEFAULT_BASE_URLS: dict[ProviderId, str] = {
    ProviderId.ONEINCH: oneinch_endpoints.DEFAULT_BASE_URL,
    ProviderId.ZERO_X: zero_x_endpoints.DEFAULT_BASE_URL,
    ProviderId.VELORA: velora_endpoints.DEFAULT_BASE_URL,
    ProviderId.UNISWAP: uniswap_endpoints.DEFAULT_BASE_URL,
    ProviderId.KYBERSWAP: kyberswap_endpoints.DEFAULT_BASE_URL,
    ProviderId.COW: cow_endpoints.DEFAULT_BASE_URL,
}

#: Соответствие идентификатора провайдера его адаптеру.
_ADAPTERS: dict[
    ProviderId,
    type[
        OneInchAdapter
        | ZeroXAdapter
        | VeloraAdapter
        | UniswapAdapter
        | KyberSwapAdapter
        | CowAdapter
    ],
]
_ADAPTERS = {
    ProviderId.ONEINCH: OneInchAdapter,
    ProviderId.ZERO_X: ZeroXAdapter,
    ProviderId.VELORA: VeloraAdapter,
    ProviderId.UNISWAP: UniswapAdapter,
    ProviderId.KYBERSWAP: KyberSwapAdapter,
    ProviderId.COW: CowAdapter,
}


@dataclass
class Container:
    """Собранные подсистемы приложения."""

    configuration: Configuration
    clock: Clock
    database: Database
    metrics: MetricsRegistry
    health: HealthMonitor
    resources: ResourceManager
    http_clients: tuple[HttpClient, ...]
    adapters: dict[ProviderId, AggregatorAdapter]
    networks: NetworkRegistry
    tokens: TokenRegistry
    providers: ProviderRegistry
    capabilities: CapabilityRegistry
    fees: FeeService
    gas: GasEstimator
    conversion: ConversionService
    calculator: ProfitCalculator
    level1: Level1Scanner
    level2: Level2Scanner
    level2_worker: Level2Worker
    opportunities: OpportunityService
    notifications: NotificationDispatcher
    transitions: TransitionRecorder
    repositories: Repositories
    commands: CommandService | None = None
    telegram: TelegramNotificationAdapter | None = None
    formatter: MessageFormatter | None = None
    system_notifier: SystemNotifier | None = None
    control: ScannerSwitch = field(default_factory=ScannerSwitch)
    backups: BackupService | None = None
    #: Установка системных обновлений по команде оператора. ``None``
    #: означает, что приложение её не выполняет.
    updater: SystemUpdater | None = None
    #: Сверка адресов токенов с сетью. Выполняется один раз при старте.
    token_check: TokenAddressCheck | None = None
    #: Поправка к оценке расхода газа. Накопленное поднимается при
    #: старте: иначе каждый перезапуск возвращал бы Monik к завышенной
    #: оценке, пока замеры не накопятся заново.
    calibration: GasCalibration | None = None
    #: Торговый счёт режима ann. ``None`` — ключ не настроен, и
    #: подсистема исполнения собрана быть не может.
    wallet: TradingWallet | None = None
    chain_account: ChainAccount | None = None
    #: Разрешение тратить деньги: конфигурация плюс команда оператора.
    trading: TradingSwitch | None = None
    executor: TradeExecutor | None = None
    watcher: PositionWatcher | None = None

    async def aclose(self) -> None:
        """Освободить внешние ресурсы."""
        for adapter in self.adapters.values():
            await adapter.aclose()
        if self.telegram is not None:
            await self.telegram.aclose()
        for client in self.http_clients:
            await client.aclose()


@dataclass
class Repositories:
    """Репозитории, используемые несколькими подсистемами."""

    jobs: SqliteJobRepository
    opportunities: SqliteOpportunityRepository
    notifications: SqliteNotificationRepository
    scans: SqliteScanRepository
    sequences: SqliteIdSequenceRepository
    scheduler: SqliteSchedulerRepository
    metadata: SqliteMetadataRepository
    transitions: SqliteStateTransitionRepository
    capabilities: SqliteCapabilityRepository
    fees: SqliteFeeRepository
    confirmations: SqliteConfirmationRepository
    _clients: list[HttpClient] = field(default_factory=list)


def build_container(
    loaded: LoadedConfiguration,
    *,
    database: Database,
    clock: Clock,
    metrics: MetricsRegistry | None = None,
    adapters: dict[ProviderId, AggregatorAdapter] | None = None,
) -> Container:
    """Собрать приложение из валидированной конфигурации.

    ``adapters`` позволяет composition root подставить готовый набор
    адаптеров (например детерминированные test implementations), не меняя
    остальную сборку.
    """
    config = loaded.config
    registry = metrics or MetricsRegistry()
    health = HealthMonitor(config.health, clock)
    control = ScannerSwitch()
    url_policy = UrlPolicy(_allowed_hosts(loaded))
    clients: list[HttpClient] = []

    def http_client() -> HttpClient:
        client = HttpxClient(config.http, url_policy)
        clients.append(client)
        return client

    resources = ResourceManager(config.resources, clock)
    _register_provider_limits(config, resources)
    built_adapters = (
        dict(adapters)
        if adapters is not None
        else _build_adapters(loaded, http_client=http_client, resources=resources, clock=clock)
    )
    # Исход каждого обращения фиксируется Health Monitoring: без этого
    # состояние провайдера навсегда осталось бы ``UNKNOWN``
    # (``19_HEALTH_MONITORING.md`` §43).
    provider_adapters: dict[ProviderId, AggregatorAdapter] = {
        provider_id: HealthTrackingAdapter(adapter, health)
        for provider_id, adapter in built_adapters.items()
    }

    networks = NetworkRegistry(config)
    tokens = TokenRegistry(config)
    providers = ProviderRegistry(config)
    repositories = _build_repositories(database)
    capabilities = CapabilityRegistry(repositories.capabilities, config.capabilities, clock)

    fees = FeeService(
        config.fees,
        clock,
        policies=_fee_policies(config),
        repository=repositories.fees,
    )
    gas = GasEstimator(
        clock,
        price_providers=_gas_providers(
            config, http_client=http_client, resources=resources, clock=clock, networks=networks
        ),
        native_tokens={
            str(network.network_id): networks.wrapped_native_token(network.network_id)
            for network in networks.enabled()
        },
        # Цена газа из котировки предпочитается, если источник включён:
        # значение уже получено вместе с ценой маршрута, и обращаться к
        # узлу сети отдельным запросом незачем.
        prefer_quoted_price=GasSource.QUOTE in config.gas.sources,
    )
    conversion = ConversionService(
        clock,
        providers=_price_providers(
            config,
            adapters=provider_adapters,
            tokens=tokens,
            networks=networks,
            http_client=http_client,
            resources=resources,
            clock=clock,
        ),
    )
    calculator = ProfitCalculator(clock)
    transitions = TransitionRecorder(repositories.transitions, clock)

    formatter = MessageFormatter(
        config.notifications,
        tokens,
        providers=providers,
        networks=networks,
        timezone=config.application.timezone,
    )
    opportunities = OpportunityService(
        publisher=repositories.confirmations,
        notifications=repositories.notifications,
        opportunities=repositories.opportunities,
        sequences=repositories.sequences,
        clock=clock,
        destinations=_destinations(loaded),
        renderer=formatter,
    )
    # Поправка к оценке расхода газа. Общая для поиска и исполнения:
    # исполнение её пополняет замерами, поиск ею пользуется.
    calibration = GasCalibration(
        config=config.gas.calibration,
        clock=clock,
        default_multiplier=config.gas.quote_estimate_multiplier,
        per_provider=dict(config.gas.quote_estimate_multipliers),
        store=SqliteGasCalibrationRepository(database),
    )
    level2_worker, level2 = _build_level2(
        config,
        adapters=provider_adapters,
        calibration=calibration,
        capabilities=capabilities,
        calculator=calculator,
        fees=fees,
        gas=gas,
        conversion=conversion,
        tokens=tokens,
        networks=networks,
        repositories=repositories,
        clock=clock,
        metrics=registry,
        on_confirmation=_confirmation_handler(opportunities),
    )
    level1 = _build_level1(
        config,
        adapters=provider_adapters,
        calibration=calibration,
        capabilities=capabilities,
        calculator=calculator,
        fees=fees,
        gas=gas,
        conversion=conversion,
        tokens=tokens,
        networks=networks,
        providers=providers,
        repositories=repositories,
        dispatcher=level2_worker,
        clock=clock,
        metrics=registry,
    )

    telegram = _build_telegram(loaded, http_client=http_client, resources=resources, clock=clock)
    notifications = NotificationDispatcher(
        config.notifications,
        store=repositories.notifications,
        transports=({DestinationKind.TELEGRAM.value: telegram} if telegram else {}),
        clock=clock,
        metrics=registry,
    )
    backups = BackupService(
        config.database, database=database, clock=clock, state=repositories.metadata
    )
    updater = AptSystemUpdater()
    token_check = TokenAddressCheck(
        metadata=OnchainTokenMetadata(
            http=http_client(),
            resources=resources,
            clock=clock,
            # Узел сети — тот же, что отвечает за цену газа: второго
            # источника для одной сети не заводится.
            rpc_urls={
                str(network.network_id): endpoints
                for network in networks.enabled()
                if (endpoints := networks.rpc_endpoints(network.network_id))
            },
        ),
        tokens=tokens,
        networks=networks,
    )
    wallet = _build_wallet(loaded)
    chain_account = (
        ChainAccount(
            address=wallet.address,
            http=http_client(),
            resources=resources,
            clock=clock,
            rpc_urls={
                str(network.network_id): endpoints
                for network in networks.enabled()
                if (endpoints := networks.rpc_endpoints(network.network_id))
            },
        )
        if wallet is not None
        else None
    )
    trading_switch = TradingSwitch(allowed=config.trading.execution_enabled)
    executor: TradeExecutor | None = None
    watcher: PositionWatcher | None = None
    if wallet is not None and chain_account is not None:
        positions_store = SqlitePositionRepository(database)
        sender = TransactionSender(
            wallet=wallet,
            account=chain_account,
            clock=clock,
            chain_ids={
                str(network.network_id): network.chain_id for network in config.enabled_networks
            },
            priority_fees_wei={
                str(network.network_id): network.priority_fee_wei
                for network in config.enabled_networks
            },
        )
        base_decimals = {
            str(network.network_id): _base_decimals(config, network.network_id)
            for network in config.enabled_networks
        }
        gas_costs = GasCostConverter(tokens=tokens, networks=networks, rates=conversion)
        # Исполнение подписанной заявкой. Нужно провайдеру, который
        # транзакции не отдаёт (``the_main_rules.md``, правило 16); для
        # остальных остаётся неиспользованным.
        order_runner = SignedOrderRunner(
            wallet=wallet,
            clock=clock,
            sleeper=asyncio.sleep,
            poll_seconds=config.trading.order_poll_seconds,
            timeout_seconds=config.trading.order_timeout_seconds,
        )
        executor = TradeExecutor(
            adapters={key.value: value for key, value in provider_adapters.items()},
            positions=positions_store,
            sequences=repositories.sequences,
            account=chain_account,
            sender=sender,
            tokens=tokens,
            calculator=calculator,
            costs=gas_costs,
            calibration=calibration,
            clock=clock,
            is_execution_open=lambda: trading_switch.is_open,
            slippage_bps=int(config.trading.slippage_percent * 100),
            # Не открываем того, что не сможем закрыть: порог входа — тот
            # же, при котором подсистема согласна выйти из уже открытой
            # сделки.
            min_entry_profit_raw=int(
                config.trading.min_exit_profit_waiting * (10 ** max(base_decimals.values()))
            ),
            orders=order_runner,
            max_offer_checks=config.trading.max_offer_checks,
            receipt_timeout_seconds=config.trading.receipt_timeout_seconds,
            receipt_poll_seconds=config.trading.receipt_poll_seconds,
        )
        watcher = PositionWatcher(
            adapters={key.value: value for key, value in provider_adapters.items()},
            positions=positions_store,
            account=chain_account,
            sender=sender,
            executor=executor,
            costs=gas_costs,
            calibration=calibration,
            clock=clock,
            # Порог выхода задан в базовом токене. Сети могут отличаться
            # знаками базового токена, поэтому берётся максимум: занижать
            # порог нельзя, а сети сейчас совпадают.
            min_exit_profit_raw=int(
                config.trading.min_exit_profit * (10 ** max(base_decimals.values()))
            ),
            min_exit_profit_waiting_raw=int(
                config.trading.min_exit_profit_waiting * (10 ** max(base_decimals.values()))
            ),
            slippage_bps=int(config.trading.slippage_percent * 100),
            long_wait_enabled=config.trading.max_wait_notice_enabled,
            long_wait_seconds=config.trading.max_wait_notice_seconds,
            orders=order_runner,
            receipt_timeout_seconds=config.trading.receipt_timeout_seconds,
            receipt_poll_seconds=config.trading.receipt_poll_seconds,
        )
        # Покупка сразу спрашивает цену выхода, не дожидаясь такта
        # расписания: отклонение, ради которого сделка открыта, живёт
        # минуты, и пять секунд ожидания на ровном месте его теряют.
        executor.set_exit_check(watcher.consider_now)
    commands = _build_commands(
        loaded,
        repositories=repositories,
        telegram=telegram,
        http_client=http_client,
        resources=resources,
        clock=clock,
        health=health,
        metrics=registry,
        control=control,
        trading=trading_switch,
        backups=backups,
        updater=updater,
        providers=providers,
    )
    system_notifier = _build_system_notifier(
        loaded, telegram=telegram, repositories=repositories, clock=clock
    )

    return Container(
        configuration=config,
        clock=clock,
        database=database,
        metrics=registry,
        health=health,
        resources=resources,
        http_clients=tuple(clients),
        adapters=provider_adapters,
        networks=networks,
        tokens=tokens,
        providers=providers,
        capabilities=capabilities,
        fees=fees,
        gas=gas,
        conversion=conversion,
        calculator=calculator,
        level1=level1,
        level2=level2,
        level2_worker=level2_worker,
        opportunities=opportunities,
        notifications=notifications,
        transitions=transitions,
        repositories=repositories,
        commands=commands,
        telegram=telegram,
        formatter=formatter,
        system_notifier=system_notifier,
        control=control,
        backups=backups,
        updater=updater,
        token_check=token_check,
        calibration=calibration,
        wallet=wallet,
        chain_account=chain_account,
        trading=trading_switch,
        executor=executor,
        watcher=watcher,
    )


# --- сборка отдельных частей ---------------------------------------------


def _allowed_hosts(loaded: LoadedConfiguration) -> tuple[str, ...]:
    """Хосты, к которым приложению разрешено обращаться.

    Allowlist строится из фактически настроенных endpoints: провайдеров,
    RPC, price API и Telegram (``32_SECURITY.md``). Всё остальное
    блокируется политикой URL ещё до отправки запроса.
    """
    config = loaded.config
    urls: list[str] = list(config.http.extra_allowed_hosts)
    for provider in config.providers:
        if not provider.enabled:
            continue
        default = _DEFAULT_BASE_URLS.get(provider.provider_id)
        base_url = provider.base_url or default
        if base_url:
            urls.append(base_url)
    for network in config.networks:
        if network.enabled:
            # В список разрешённых адресов попадают все узлы сети, а не
            # только основной: иначе переход на запасной упрётся в
            # собственную политику.
            urls.extend(network.rpc_endpoints)
    if config.prices.http_endpoint:
        urls.append(config.prices.http_endpoint)
    if config.notifications.telegram.enabled:
        urls.append(config.notifications.telegram.api_base_url)
    return tuple(sorted({_host_of(url) for url in urls if url}))


def _host_of(value: str) -> str:
    """Имя хоста из URL или уже готового имени."""
    if "://" not in value:
        return value.strip().lower()
    return urlsplit(value).hostname or ""


def _build_repositories(database: Database) -> Repositories:
    """Репозитории поверх одного соединения с базой."""
    return Repositories(
        jobs=SqliteJobRepository(database),
        opportunities=SqliteOpportunityRepository(database),
        notifications=SqliteNotificationRepository(database),
        scans=SqliteScanRepository(database),
        sequences=SqliteIdSequenceRepository(database),
        scheduler=SqliteSchedulerRepository(database),
        metadata=SqliteMetadataRepository(database),
        transitions=SqliteStateTransitionRepository(database),
        capabilities=SqliteCapabilityRepository(database),
        fees=SqliteFeeRepository(database),
        confirmations=SqliteConfirmationRepository(database),
    )


def _build_adapters(
    loaded: LoadedConfiguration,
    *,
    http_client: HttpClientFactory,
    resources: ResourceManager,
    clock: Clock,
) -> dict[ProviderId, AggregatorAdapter]:
    """Адаптеры включённых провайдеров.

    Отключённый провайдер адаптер не получает: запросы к нему не
    выполняются (``02_LEVEL1_SCANNER.md`` §71).
    """
    adapters: dict[ProviderId, AggregatorAdapter] = {}
    for provider in loaded.config.providers:
        if not provider.enabled:
            continue
        factory = _ADAPTERS.get(provider.provider_id)
        if factory is None:  # pragma: no cover - защита от нового провайдера
            continue
        adapters[provider.provider_id] = factory(
            provider,
            http=http_client(),
            resources=resources,
            clock=clock,
            api_key=_provider_secret(loaded, provider),
        )
    return adapters


def _provider_secret(loaded: LoadedConfiguration, provider: ProviderConfig) -> SecretValue | None:
    """Разрешённый API-ключ провайдера, если он задан."""
    if provider.api_key is None:
        return None
    return loaded.secrets.get(provider.api_key)


def _fee_policies(config: Configuration) -> dict[ProviderId, FeePolicy]:
    """Политики комиссий включённых провайдеров.

    Агрегаторы возвращают итоговую сумму маршрута, поэтому комиссия уже
    учтена в котировке (``01_PROJECT_REQUIREMENTS.md`` §29). Провайдер без
    политики получает ``UNKNOWN``, а не ноль.
    """
    return {
        provider.provider_id: QuoteInclusiveFeePolicy(
            provider.provider_id, source=f"policy:{provider.provider_id.value}"
        )
        for provider in config.providers
        if provider.enabled
    }


def _gas_providers(
    config: Configuration,
    *,
    http_client: HttpClientFactory,
    resources: ResourceManager,
    clock: Clock,
    networks: NetworkRegistry,
) -> tuple[GasPriceProvider, ...]:
    """Источники цены газа согласно конфигурации (решение D-4)."""
    providers: list[GasPriceProvider] = []
    if GasSource.STATIC in config.gas.sources and config.gas.static_wei_per_gas:
        providers.append(StaticGasPriceProvider(clock, prices=dict(config.gas.static_wei_per_gas)))
    if GasSource.RPC in config.gas.sources:
        rpc_urls = {
            str(network.network_id): endpoints
            for network in networks.enabled()
            if (endpoints := networks.rpc_endpoints(network.network_id))
        }
        if rpc_urls:
            providers.append(
                RpcGasPriceProvider(
                    http=http_client(),
                    resources=resources,
                    clock=clock,
                    rpc_urls=rpc_urls,
                    freshness_seconds=config.gas.freshness_seconds,
                    timeout_seconds=config.gas.request_timeout_seconds,
                    priority_fees_wei={
                        str(network.network_id): network.priority_fee_wei
                        for network in config.enabled_networks
                    },
                )
            )
    if not providers:
        raise ConfigurationError(
            "no usable gas price source is configured: set network rpc_url for the rpc "
            "source or configure gas.static_wei_per_gas; unknown gas is never zero"
        )
    return tuple(providers)


def _price_providers(
    config: Configuration,
    *,
    adapters: dict[ProviderId, AggregatorAdapter],
    tokens: TokenRegistry,
    networks: NetworkRegistry,
    http_client: HttpClientFactory,
    resources: ResourceManager,
    clock: Clock,
) -> tuple[TokenPriceProvider, ...]:
    """Источники курса native token (решение D-4)."""
    providers: list[TokenPriceProvider] = []
    if PriceSource.AGGREGATOR_QUOTE in config.prices.sources and adapters:
        # Источник заводится на **каждый** включённый агрегатор, а не на
        # первый попавшийся. Агрегаторы работают в разных сетях, и
        # единственный выбранный наугад может не знать ту сеть, в которой
        # курс понадобился: тогда стоимость газа оказывается неизвестной,
        # а сделка — незакрываемой. Служба курсов перебирает источники по
        # порядку и берёт первый ответивший; агрегатор, не работающий в
        # сети, отвечает отказом сразу, без обращения к своему API.
        providers.extend(
            AggregatorQuotePriceProvider(
                adapter,
                clock,
                # Пробная сумма — один native token: курс берётся из
                # исполнимой котировки, а не из абстрактной цены. Знаки
                # берутся у токена в момент запроса, поэтому источник
                # одинаково работает в любой сети.
                probe_tokens=1,
                ttl_seconds=config.prices.freshness_seconds,
            )
            for adapter in adapters.values()
        )
    if PriceSource.HTTP in config.prices.sources and config.prices.http_endpoint:
        providers.append(
            HttpPriceProvider(
                http=http_client(),
                resources=resources,
                clock=clock,
                endpoint=config.prices.http_endpoint,
                ttl_seconds=config.prices.freshness_seconds,
                timeout_seconds=config.prices.request_timeout_seconds,
            )
        )
    if not providers:
        raise ConfigurationError(
            "no usable price source is configured; gas cost could not be converted"
        )
    return tuple(providers)


def _build_level2(
    config: Configuration,
    *,
    adapters: dict[ProviderId, AggregatorAdapter],
    calibration: GasCalibration,
    capabilities: CapabilityRegistry,
    calculator: ProfitCalculator,
    fees: FeeService,
    gas: GasEstimator,
    conversion: ConversionService,
    tokens: TokenRegistry,
    networks: NetworkRegistry,
    repositories: Repositories,
    clock: Clock,
    metrics: MetricsRegistry,
    on_confirmation: ConfirmationHandler,
) -> tuple[Level2Worker, Level2Scanner]:
    """Level 2 вместе с его очередью."""
    verifier = AmountVerifier(
        RouteVerifier(
            adapters,
            capabilities,
            clock,
            quote_max_age=timedelta(seconds=config.scanner.level2.quote_max_age_seconds),
        ),
        Level2Financials(
            calculator,
            fees=fees,
            gas=gas,
            rates=conversion,
            tokens=tokens,
            networks=networks,
            profitability=config.profitability,
            gas_correction=calibration,
        ),
        tokens,
    )
    scanner = Level2Scanner(
        config.scanner.level2,
        verifier=verifier,
        jobs=repositories.jobs,
        opportunities=repositories.opportunities,
        tokens=tokens,
        amounts=config.scanner.amounts,
        clock=clock,
        metrics=metrics,
    )
    return Level2Worker(scanner, config.scanner.level2, on_confirmation=on_confirmation), scanner


def _confirmation_handler(opportunities: OpportunityService) -> ConfirmationHandler:
    """Что происходит после того, как Level 2 закончил проверку.

    Без этой связки подтверждение оставалось в базе и не доходило до
    оператора: уведомление не создавалось, а возможность навсегда
    оставалась в статусе проверки. Level 2 о доставке по-прежнему не
    знает — он лишь сообщает результат тому, кто владеет жизненным циклом
    возможности (``10_LEVEL_1_SCANNER.md`` §61-62).
    """

    async def handler(opportunity: Opportunity, result: ConfirmationResult) -> None:
        await opportunities.record_confirmation(opportunity, result)

    return handler


def _build_level1(
    config: Configuration,
    *,
    adapters: dict[ProviderId, AggregatorAdapter],
    calibration: GasCalibration,
    capabilities: CapabilityRegistry,
    calculator: ProfitCalculator,
    fees: FeeService,
    gas: GasEstimator,
    conversion: ConversionService,
    tokens: TokenRegistry,
    networks: NetworkRegistry,
    providers: ProviderRegistry,
    repositories: Repositories,
    dispatcher: Level2Worker,
    clock: Clock,
    metrics: MetricsRegistry,
) -> Level1Scanner:
    """Level 1 со всеми зависимостями."""
    no_route = NoRouteMemory(config.scanner.level1.no_route_memory, clock)
    return Level1Scanner(
        config,
        adapters=adapters,
        scope_builder=ScopeBuilder(
            config, networks=networks, tokens=tokens, providers=providers, clock=clock
        ),
        combinations=CombinationFilter(capabilities, config.scanner.level1, no_route),
        no_route=no_route,
        evaluator=PreliminaryEvaluator(
            calculator,
            fees=fees,
            gas=gas,
            rates=conversion,
            tokens=tokens,
            networks=networks,
            profitability=config.profitability,
            gas_correction=calibration,
        ),
        opportunities=repositories.opportunities,
        scans=repositories.scans,
        sequences=repositories.sequences,
        dispatcher=dispatcher,
        clock=clock,
        # Найденное режимами ur и fest потребляет Level 2. У торгового
        # режима ann потребителем будет подсистема исполнения; пока её
        # нет, его находки только записываются в журнал.
        dispatch_modes=frozenset({ScanMode.UR, ScanMode.FEST}),
        metrics=metrics,
    )


def _destinations(loaded: LoadedConfiguration) -> tuple[NotificationDestination, ...]:
    """Настроенные назначения доставки (``15_NOTIFICATION_SYSTEM.md`` §53)."""
    telegram = loaded.config.notifications.telegram
    if not (loaded.config.notifications.enabled and telegram.enabled and telegram.chat_id):
        return ()
    return (
        NotificationDestination(
            destination_id=telegram.chat_id.env,
            kind=DestinationKind.TELEGRAM,
            mode=loaded.config.notifications.mode,
        ),
    )


def _allowed_chat_ids(loaded: LoadedConfiguration) -> frozenset[str]:
    """Чаты, из которых принимаются команды.

    Отдельной настройки не заводится: чат, в который Monik пишет, и есть
    чат оператора. Второй список означал бы второй источник истины и
    расходился бы с первым при переносе на другой сервер.
    """
    telegram = loaded.config.notifications.telegram
    if telegram.chat_id is None or not loaded.secrets.has(telegram.chat_id):
        return frozenset()
    return frozenset({loaded.secrets.get(telegram.chat_id).get()})


def _build_telegram(
    loaded: LoadedConfiguration,
    *,
    http_client: HttpClientFactory,
    resources: ResourceManager,
    clock: Clock,
) -> TelegramNotificationAdapter | None:
    """Адаптер доставки, если Telegram настроен."""
    telegram = loaded.config.notifications.telegram
    if not telegram.enabled or telegram.bot_token is None or telegram.chat_id is None:
        return None
    return TelegramNotificationAdapter(
        telegram,
        http=http_client(),
        resources=resources,
        clock=clock,
        bot_token=loaded.secrets.get(telegram.bot_token),
        chat_id=loaded.secrets.get(telegram.chat_id),
    )


def _register_provider_limits(config: Configuration, resources: ResourceManager) -> None:
    """Передать Resource Manager лимиты включённых провайдеров.

    Без этого шага ``requests_per_second`` и ``max_concurrent_requests``
    остаются объявленными, но не действующими: Resource Manager не находит
    лимитов для ресурса и пропускает запросы без ограничения частоты
    (``CLAUDE.md`` §14, ``05_RESOURCE_MANAGER.md`` §58).

    Лимиты регистрируются на **уровне провайдера**, без сети и операции.
    Это создаёт одну очередь на агрегатор, а значит BUY и SELL, Level 1 и
    Level 2, котировки и комиссии делят общий бюджет: API ограничивает ключ
    целиком, и разделять его на независимые корзины нельзя
    (``05_RESOURCE_MANAGER.md`` §51). Очередь появляется из конфигурации,
    поэтому новый агрегатор не требует изменений в самой очереди.

    Пауза между запросами берётся из настройки агрегатора, а при её
    отсутствии — из общей. Требования у агрегаторов разные: один отвечает
    ошибкой частоты там, где другой работает без замечаний, и замедлять
    из-за него остальных незачем.

    ``burst`` отдельным параметром не задаётся: второй источник истины для
    частоты запросов создавать нельзя. Он равен единице, и это не
    придирка, а условие соблюдения лимита. Корзина ёмкостью ``B`` при
    частоте ``R`` способна выдать за секунду ``B + R`` запросов, поэтому
    накопленный запас превращает настроенные 5.9 запроса в секунду почти
    в двенадцать. При ``B = 1`` запросы идут ровно с настроенной частотой,
    и в любое секундное окно попадает не больше ``ceil(R)`` из них.
    Средняя частота при этом не меняется — исчезает только всплеск.
    """
    for provider in config.enabled_providers:
        resources.register_limits(
            ResourceKey(provider_id=provider.provider_id),
            ResourceLimits(
                max_concurrent=provider.max_concurrent_requests,
                requests_per_second=provider.requests_per_second,
                burst=1,
                min_interval_seconds=(
                    provider.min_interval_seconds
                    if provider.min_interval_seconds is not None
                    else config.resources.provider_min_interval_seconds
                ),
            ),
        )


def _build_system_notifier(
    loaded: LoadedConfiguration,
    *,
    telegram: TelegramNotificationAdapter | None,
    repositories: Repositories,
    clock: Clock,
) -> SystemNotifier | None:
    """Канал операционных уведомлений, если Telegram настроен.

    Используется тот же транспорт, что и для уведомлений о возможностях:
    отдельного, неконтролируемого обращения к Telegram API подсистемы не
    создают (``15_NOTIFICATION_SYSTEM.md`` §10, §29).
    """
    config = loaded.config.notifications
    if telegram is None or not config.system.enabled:
        return None
    destinations = _destinations(loaded)
    if not destinations:
        return None
    return SystemNotifier(
        config.system,
        transport=telegram,
        destination=destinations[0],
        clock=clock,
        state=repositories.metadata,
    )


def _build_commands(
    loaded: LoadedConfiguration,
    *,
    repositories: Repositories,
    telegram: TelegramNotificationAdapter | None,
    http_client: HttpClientFactory,
    resources: ResourceManager,
    clock: Clock,
    health: HealthMonitor,
    metrics: MetricsRegistry,
    control: ScannerSwitch,
    trading: TradingSwitch,
    backups: BackupService,
    updater: SystemUpdater,
    providers: ProviderRegistry,
) -> CommandService | None:
    """Входящий канал команд, если он включён конфигурацией."""
    config = loaded.config.notifications.telegram
    if telegram is None or not config.commands_enabled or config.bot_token is None:
        return None
    destinations = _destinations(loaded)
    if not destinations:
        return None
    allowed = _allowed_chat_ids(loaded)
    if not allowed:
        # Открытый канал команд опаснее отсутствующего: без известного
        # чата оператора любой собеседник бота управлял бы сканером.
        _LOGGER.warning("telegram commands are disabled: operator chat is unknown")
        return None
    router = CommandRouter(
        jobs=repositories.jobs,
        notifications=repositories.notifications,
        status=_HealthStatusSource(health),
        stats=_MetricsStatsSource(metrics),
        providers=_ProviderStatusSource(
            health, resources, loaded.config, providers=providers, clock=clock
        ),
        scans=repositories.scans,
        control=control,
        trading=trading,
        backups=_BackupStatusSource(backups),
        updater=updater,
        application=version_label(),
        environment=loaded.config.application.environment.value,
    )
    return CommandService(
        router=router,
        # Команды управляют production и системой, поэтому источник
        # команды ограничен настроенным чатом. Пустой набор означал бы
        # «принимать от кого угодно»: бота можно найти по имени, и тогда
        # остановить сканер или поставить обновления смог бы посторонний.
        allowed_chat_ids=allowed,
        updates=TelegramUpdateSource(
            config,
            http=http_client(),
            resources=resources,
            clock=clock,
            bot_token=loaded.secrets.get(config.bot_token),
        ),
        transport=telegram,
        destination=destinations[0],
        offsets=repositories.metadata,
        clock=clock,
    )


class _HealthStatusSource:
    """Снимок состояния подсистем для команды ``/status``."""

    def __init__(self, health: HealthMonitor) -> None:
        self._health = health

    def components(self) -> tuple[ComponentStatus, ...]:
        """Состояние подсистем из Health Monitor."""
        snapshot = self._health.application_health()
        components = tuple(
            ComponentStatus(name=item.component, state=item.status.value, detail=item.reason)
            for item in snapshot.components
        )
        providers = tuple(
            ComponentStatus(
                name=f"provider:{item.provider_id.value}",
                state=item.status.value,
                # Код причины помогает понять, чем именно деградировал
                # провайдер. Секретов он не содержит: это нормализованный
                # код ошибки, а не тело ответа (``19`` §65).
                detail=item.reason,
            )
            for item in snapshot.providers
        )
        return (
            ComponentStatus(name="application", state=snapshot.status.value),
            *components,
            *providers,
        )


class _ProviderStatusSource:
    """Состояние агрегаторов для команд.

    Собирается из двух уже существующих источников: Health Monitoring
    знает доступность, Resource Manager — очередь и применённые лимиты.
    Третьего хранилища состояния не создаётся.
    """

    def __init__(
        self,
        health: HealthMonitor,
        resources: ResourceManager,
        config: Configuration,
        *,
        providers: ProviderRegistry,
        clock: Clock,
    ) -> None:
        self._health = health
        self._resources = resources
        self._config = config
        self._providers = providers
        self._clock = clock

    def providers(self) -> tuple[ProviderStatus, ...]:
        """Состояние каждого включённого агрегатора."""
        queues = {snapshot.resource: snapshot for snapshot in self._resources.queue_snapshots()}
        now = self._clock.now()
        statuses = []
        for provider in self._config.enabled_providers:
            name = provider.provider_id.value
            health = self._health.provider(provider.provider_id)
            queue = queues.get(name)
            window = self._providers.window(provider.provider_id)
            statuses.append(
                ProviderStatus(
                    provider=name,
                    health=health.status.value,
                    circuit_state=queue.circuit_state.value if queue else "unknown",
                    requests_per_second=provider.requests_per_second,
                    max_concurrent=provider.max_concurrent_requests,
                    active=queue.active if queue else 0,
                    waiting=queue.waiting if queue else 0,
                    reason=health.reason,
                    schedule=window.describe() if window else None,
                    within_schedule=self._providers.is_active(provider.provider_id, now),
                )
            )
        return tuple(statuses)


class _BackupStatusSource:
    """Состояние резервного копирования для команды ``/backup``."""

    def __init__(self, backups: BackupService) -> None:
        self._backups = backups

    async def status(self) -> BackupStatus:
        """Текущее состояние копий."""
        if not self._backups.enabled:
            return BackupStatus(enabled=False)
        last_run, outcome = await self._backups.last_run()
        directory = self._backups.directory
        return BackupStatus(
            enabled=True,
            last_run_at=last_run,
            last_outcome=outcome,
            copies=len(self._backups.copies()),
            # Путь каталога секретом не является и нужен оператору.
            detail=f"Каталог: {directory}" if directory else None,
        )


class _MetricsStatsSource:
    """Статистика для команды ``/stats``.

    Читает **живой** реестр метрик, который наполняют Level 1, Level 2 и
    Notification System во время работы. Собственных счётчиков здесь нет:
    вторая система метрик создавала бы второй источник истины
    (``28_OBSERVABILITY.md`` §29).

    Значения накапливаются с момента запуска процесса: реестр метрик
    хранится в памяти и обнуляется при перезапуске.
    """

    def __init__(self, metrics: MetricsRegistry) -> None:
        self._metrics = metrics

    def snapshot(self) -> StatsSnapshot:
        """Текущая статистика из реестра метрик."""
        return StatsSnapshot(
            confirmations=ConfirmationStatistics(
                confirmed=self._confirmations(AmountConfirmationStatus.CONFIRMED),
                unconfirmed=self._confirmations(AmountConfirmationStatus.UNCONFIRMED),
                partial=self._confirmations(AmountConfirmationStatus.PARTIAL),
            ),
            # Учитываются все завершённые циклы независимо от итогового
            # статуса: пользователь спрашивает, сколько раз сканер отработал.
            scans_completed=self._metrics.total(names.LEVEL1_SCANS),
            opportunities_created=self._metrics.counter(
                names.LEVEL1_OPPORTUNITIES, status="created"
            ),
            notifications_sent=self._metrics.counter(names.NOTIFICATIONS, outcome="delivered"),
        )

    def _confirmations(self, status: AmountConfirmationStatus) -> int:
        return self._metrics.counter(names.LEVEL2_CONFIRMATIONS, status=status.value)


def _build_wallet(loaded: LoadedConfiguration) -> TradingWallet | None:
    """Торговый счёт, если ключ настроен.

    Отсутствие ключа — не ошибка: режим ``ann`` умеет работать в сухом
    прогоне, находя сделки и не исполняя их. Ошибкой это становится
    только при включённой торговле, и проверяется она конфигурацией.
    """
    reference = loaded.config.trading.private_key
    if reference is None:
        return None
    if not loaded.secrets.has(reference):
        raise ConfigurationError(
            f"trading private key is configured as {reference.env} but the variable is not set",
            code="trading_key_missing",
        )
    return TradingWallet(loaded.secrets.get(reference))


def _base_decimals(config: Configuration, network_id: NetworkId) -> int:
    """Знаки базового токена сети."""
    network = config.network(network_id)
    if network is None:
        return 18
    token = config.token(network_id, network.base_token_address)
    return token.decimals if token is not None else 18
