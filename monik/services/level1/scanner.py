"""Level 1 Scanner — оркестрация одного цикла поиска.

Level 1 находит кандидата и фиксирует маршрут; подтверждает его Level 2
(``10_LEVEL_1_SCANNER.md`` §95). Scanner не выполняет swap, не отправляет
Telegram-уведомления, не обходит Resource Manager и не реализует
собственную финансовую формулу (``02_LEVEL1_SCANNER.md`` §96).

Собственного бесконечного таймера у Scanner нет: цикл запускает Scheduler
(``10_LEVEL_1_SCANNER.md`` §65).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

from monik.config.root import Configuration
from monik.domain.enums.lifecycle import ScanStatus
from monik.domain.enums.modes import ScanMode
from monik.domain.enums.providers import ProviderId
from monik.domain.models.opportunity import Candidate, Opportunity
from monik.domain.models.scan import BestCombination, Scan, ScanScope, ScanStatistics
from monik.domain.models.token import TokenKey
from monik.domain.value_objects.identifiers import ScanId
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.providers.contract import AggregatorAdapter
from monik.services.level1.cycle import TokenCycle
from monik.services.level1.dedup import DeduplicationGuard
from monik.services.level1.filters import CombinationFilter
from monik.services.level1.grouping import CandidateGroup, group_candidates
from monik.services.level1.handoff import OpportunityHandoff
from monik.services.level1.no_route import NoRouteMemory
from monik.services.level1.ports import (
    IdSequenceSource,
    Level2Dispatcher,
    OpportunityStore,
    ScanStore,
)
from monik.services.level1.preliminary import PreliminaryEvaluator
from monik.services.level1.quotes import (
    QuoteCollector,
    QuoteStatistics,
)
from monik.services.level1.ranking import rank_candidates, rank_groups
from monik.services.level1.results import ScanResult
from monik.services.level1.scope import ScopeBuilder
from monik.services.observability import names
from monik.services.observability.clock import Clock
from monik.services.observability.context import log_context
from monik.services.observability.logging import get_logger, log_fields
from monik.services.observability.metrics import MetricsRegistry

__all__ = ["Level1Scanner"]

_LOGGER = get_logger("services.level1.scanner")


class Level1Scanner:
    """Выполняет один цикл Level 1 и передаёт найденные возможности Level 2."""

    def __init__(
        self,
        configuration: Configuration,
        *,
        adapters: dict[ProviderId, AggregatorAdapter],
        scope_builder: ScopeBuilder,
        combinations: CombinationFilter,
        no_route: NoRouteMemory,
        evaluator: PreliminaryEvaluator,
        opportunities: OpportunityStore,
        scans: ScanStore,
        sequences: IdSequenceSource,
        dispatcher: Level2Dispatcher,
        clock: Clock,
        dispatch_modes: frozenset[ScanMode] = frozenset(ScanMode),
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._configuration = configuration
        self._adapters = adapters
        self._scope_builder = scope_builder
        self._combinations = combinations
        self._no_route = no_route
        self._evaluator = evaluator
        self._opportunities = opportunities
        self._scans = scans
        self._sequences = sequences
        self._dispatcher = dispatcher
        #: Режимы, у найденного которых есть потребитель. Возможность без
        #: потребителя не создаётся: она осталась бы в состоянии CREATED
        #: навсегда и засоряла бы историю. Проход при этом выполняется
        #: полностью — статистика и лучший результат записываются, — так
        #: что режим можно наблюдать до того, как появится тот, кто его
        #: находки обрабатывает.
        self._dispatch_modes = dispatch_modes
        self._clock = clock
        self._metrics = metrics

    def scan_networks(self) -> tuple[NetworkId, ...]:
        """Сети, подлежащие сканированию в этом такте."""
        return self._scope_builder.scan_networks()

    def scopes(self, mode: ScanMode) -> tuple[ScanScope, ...]:
        """Границы прохода режима — по одному scope на сеть.

        Сеть, которой в этом режиме нечего проверять, в набор не
        попадает: нет работающего провайдера режима или нет подходящих
        токенов. Пустая запись сканирования по ней не создаётся.
        """
        scopes = (
            self._scope_builder.build(network_id, mode)
            for network_id in self._scope_builder.scan_networks(mode)
        )
        return tuple(scope for scope in scopes if scope is not None)

    def has_active_providers(self) -> bool:
        """Есть ли сейчас хотя бы одна сеть с работающим провайдером.

        Спрашивается до начала такта: когда отдыхают все, цикл не нужен
        вовсе, и создавать пустую запись сканирования незачем.
        """
        return any(
            self._scope_builder.active_providers(network_id)
            for network_id in self._scope_builder.scan_networks()
        )

    async def scan_all(self, mode: ScanMode) -> tuple[ScanResult, ...]:
        """Выполнить проход режима в каждой сканируемой сети.

        Возвращаются только состоявшиеся циклы: сети независимы, и отказ
        узла или провайдера в одной из них не должен отменять поиск в
        остальных.
        """
        return await self._sweep(self.scopes(mode))

    async def _sweep(self, scopes: tuple[ScanScope, ...]) -> tuple[ScanResult, ...]:
        """Пройти набор scope'ов параллельно.

        Параллельно, а не подряд: пока провайдер выдерживает свою паузу
        между запросами в одной сети, в другой работает второй, и такт не
        растягивается на сумму сетей. Комбинация из разных сетей при этом
        возникнуть не может — каждый цикл замкнут в своём scope
        (``10_LEVEL_1_SCANNER.md`` §38).
        """
        if not scopes:
            return ()
        outcomes = await asyncio.gather(
            *(self.scan(scope) for scope in scopes), return_exceptions=True
        )
        results: list[ScanResult] = []
        for scope, outcome in zip(scopes, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                _LOGGER.warning(
                    "level 1 scan failed",
                    extra=log_fields(network=str(scope.networks[0]), error=type(outcome).__name__),
                )
                continue
            results.append(outcome)
        return tuple(results)

    async def scan(self, scope: ScanScope) -> ScanResult:
        """Выполнить цикл одной сети.

        ``scope`` фиксируется на старте: изменение конфигурации применяется
        со следующего цикла (``02_LEVEL1_SCANNER.md`` §69). Scope всегда
        передаётся явно и всегда принадлежит одной сети — сканер не
        выбирает сеть сам, иначе выбор существовал бы в двух местах.
        """
        config = self._configuration.scanner.level1
        scan_scope = scope
        scan_id = ScanId.generate()
        started_at = self._clock.now()
        scan = Scan(
            scan_id=scan_id,
            status=ScanStatus.RUNNING,
            scope=scan_scope,
            started_at=started_at,
        )
        collector = QuoteCollector(
            self._adapters,
            self._clock,
            no_route=self._no_route,
            scan_id=scan_id,
            max_age=timedelta(seconds=config.quote_max_age_seconds),
            max_concurrent=config.max_concurrent_requests,
            # Правило приоритета принадлежит режиму (``the_main_rules.md``,
            # правило 13), поэтому сборщик знает, в каком режиме работает.
            mode=scan_scope.mode,
        )
        with log_context(scan_id=str(scan_id)):
            try:
                await self._scans.create(scan)
                return await self._run(scan, scan_scope, collector)
            except asyncio.CancelledError:
                # Частичные результаты отменённого цикла не считаются
                # успешными (``10_LEVEL_1_SCANNER.md`` §67). Обновление
                # безопасно и тогда, когда цикл был отменён до записи строки.
                await self._finish(
                    scan, collector, ScanStatus.CANCELLED, opportunities=(), duplicates=0
                )
                raise

    async def _run(self, scan: Scan, scope: ScanScope, collector: QuoteCollector) -> ScanResult:
        timed_out = False
        candidates: tuple[Candidate, ...] = ()
        try:
            # Срок берётся у режима: у частого прохода он свой, иначе
            # общий срок пришлось бы равнять по самому быстрому режиму.
            async with asyncio.timeout(self._configuration.scanner.scan_timeout_for(scope.mode)):
                candidates = await self._collect_candidates(scan, scope, collector)
        except TimeoutError:
            # Общий таймаут цикла (``10_LEVEL_1_SCANNER.md`` §68): уже
            # полученные результаты не выбрасываются, но цикл не полон.
            timed_out = True
            _LOGGER.warning("level 1 scan timed out")

        opportunities, duplicates, qualified = await self._create_opportunities(scan, candidates)
        status = self._final_status(collector, timed_out=timed_out)
        finished = await self._finish(
            scan,
            collector,
            status,
            opportunities=opportunities,
            duplicates=duplicates,
            candidates=candidates,
        )
        return ScanResult(
            scan=finished,
            opportunities=opportunities,
            qualified=qualified,
            failures=tuple(
                attempt for attempt in collector.statistics.attempts if not attempt.is_usable
            ),
        )

    async def _collect_candidates(
        self, scan: Scan, scope: ScanScope, collector: QuoteCollector
    ) -> tuple[Candidate, ...]:
        """Запустить независимые циклы токенов параллельно.

        Цикл токена самодостаточен: SELL одного токена не ждёт BUY другого
        (``CLAUDE.md`` §16).
        """
        network_id = scope.networks[0]
        pairs = self._combinations.provider_pairs(
            scope.providers, self._allowed_pairs(scope.providers)
        )
        if not pairs:
            return ()
        cycle = TokenCycle(
            collector=collector,
            combinations=self._combinations,
            evaluator=self._evaluator,
            adapters=self._adapters,
            clock=self._clock,
            scan_id=scan.scan_id,
            network_id=network_id,
            mode=scope.mode,
            base_token=self._scope_builder.base_token(network_id),
            providers=scope.providers,
            pairs=pairs,
            raw_amounts=scope.raw_amounts,
        )
        tokens = [
            token
            for token in self._scope_builder.scan_tokens(network_id)
            if token.key in set(scope.tokens)
        ]
        results = await asyncio.gather(
            *(cycle.run(token) for token in tokens), return_exceptions=True
        )
        candidates: list[Candidate] = []
        for token, outcome in zip(tokens, results, strict=True):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                # Ошибка одного токена не останавливает остальные (§75).
                _LOGGER.warning(
                    "token cycle failed",
                    extra=log_fields(token=str(token.key), error=type(outcome).__name__),
                )
                continue
            candidates.extend(outcome)
        return tuple(candidates)

    def _allowed_pairs(
        self, providers: tuple[ProviderId, ...]
    ) -> tuple[tuple[ProviderId, ProviderId], ...]:
        """Пары провайдеров, разрешённые route policy."""
        policy = self._configuration.routes
        return tuple(
            (buy, sell) for buy in providers for sell in providers if policy.is_allowed(buy, sell)
        )

    async def _create_opportunities(
        self, scan: Scan, candidates: tuple[Candidate, ...]
    ) -> tuple[tuple[Opportunity, ...], int, tuple[Candidate, ...]]:
        """Создать Opportunity из кандидатов, прошедших порог.

        Третьим значением возвращаются сами прошедшие кандидаты в порядке
        привлекательности: у режима, чьи находки потребляет не Level 2,
        решение принимается по ним.
        """
        config = self._configuration.scanner.level1
        qualified = tuple(
            candidate for candidate in candidates if _passes_preliminary_threshold(candidate)
        )
        # Торговый режим выбирает по заработку в базовом токене, остальные —
        # по доходности в процентах. Порядок нужен и между группами, и
        # внутри них: суммы одной группы различаются именно заработком.
        by_profit = scan.scope.mode is ScanMode.ANN
        groups = rank_groups(group_candidates(qualified), by_profit=by_profit)
        ranked = rank_candidates(groups, by_profit=by_profit)
        if scan.scope.mode not in self._dispatch_modes:
            _log_observed(scan.scope.mode, groups)
            return (), 0, ranked
        guard = DeduplicationGuard(
            self._opportunities,
            window=timedelta(seconds=config.deduplication_window_seconds),
        )
        handoff = OpportunityHandoff(
            store=self._opportunities,
            sequences=self._sequences,
            dispatcher=self._dispatcher,
            clock=self._clock,
            opportunity_ttl=timedelta(seconds=config.opportunity_ttl_seconds),
            job_ttl=timedelta(seconds=self._configuration.scanner.level2.job_ttl_seconds),
        )
        capacity = min(config.max_opportunities_per_scan, self._dispatcher.available_capacity())
        created: list[Opportunity] = []
        for group in groups:
            if len(created) >= capacity:
                # Backpressure: бесконечная очередь Job не создаётся (§47).
                break
            opportunity = await self._create_one(group, scan, guard, handoff)
            if opportunity is not None:
                created.append(opportunity)
        return tuple(created), guard.duplicates, ranked

    async def _create_one(
        self,
        group: CandidateGroup,
        scan: Scan,
        guard: DeduplicationGuard,
        handoff: OpportunityHandoff,
    ) -> Opportunity | None:
        now = self._clock.now()
        if await guard.is_duplicate(group.fingerprint, now=now):
            return None
        try:
            opportunity = await handoff.create(group, scan_id=scan.scan_id, mode=scan.scope.mode)
        except Exception as error:  # noqa: BLE001 - ошибка фиксируется и цикл продолжается
            # Неполная Opportunity не должна продолжать workflow (§92).
            _LOGGER.error(
                "opportunity creation failed",
                extra=log_fields(error=type(error).__name__, detail=str(error)),
            )
            return None
        guard.remember(opportunity.fingerprint)
        return opportunity

    def _final_status(self, collector: QuoteCollector, *, timed_out: bool) -> ScanStatus:
        """Итог цикла.

        Неполным цикл делает и отказ провайдера, и неотправленный запрос:
        в обоих случаях часть комбинаций осталась непосчитанной. Разводятся
        они только в счётчиках — там от этого зависит, чью работу они
        описывают: агрегаторов или самого Monik.
        """
        statistics = collector.statistics
        if timed_out:
            return ScanStatus.PARTIAL if statistics.successful else ScanStatus.FAILED
        if statistics.requests == 0 and statistics.refused == 0:
            return ScanStatus.COMPLETE
        incomplete = statistics.failed + statistics.refused
        if incomplete and statistics.successful:
            return ScanStatus.PARTIAL
        if incomplete:
            return ScanStatus.FAILED
        return ScanStatus.COMPLETE

    async def _finish(
        self,
        scan: Scan,
        collector: QuoteCollector,
        status: ScanStatus,
        *,
        opportunities: tuple[Opportunity, ...],
        duplicates: int,
        candidates: tuple[Candidate, ...] = (),
    ) -> Scan:
        statistics = collector.statistics
        evaluated = _evaluated_candidates(candidates)
        best = _best_combination(evaluated)
        best_volatile = _best_combination(_volatile_candidates(evaluated, self._stable_tokens()))
        blocked = _blocked_by_unknown_cost(candidates)
        unknown_costs = _unknown_cost_components(candidates)
        finished = scan.replace(
            status=status,
            finished_at=self._clock.now(),
            statistics=ScanStatistics(
                quote_requests=statistics.requests,
                successful_quotes=statistics.successful,
                failed_quotes=statistics.failed,
                refused_requests=statistics.refused,
                skipped_combinations=statistics.skipped,
                opportunities_created=len(opportunities),
                duplicate_opportunities=duplicates,
                evaluated_combinations=len(evaluated),
                blocked_by_unknown_cost=len(blocked),
                unknown_cost_components=unknown_costs,
                best_combination=best,
                best_volatile_combination=best_volatile,
            ),
        )
        await self._scans.update(finished)
        self._record_metrics(finished, statistics)
        _LOGGER.info(
            "level 1 scan finished",
            extra=log_fields(
                # Сеть цикла. Сетей в работе может быть несколько, и без
                # этого поля записи разных сетей в журнале неотличимы:
                # сеть приходилось бы выводить из адреса лучшего токена.
                mode=scan.scope.mode.value,
                network=str(scan.scope.networks[0]),
                status=status.value,
                requests=statistics.requests,
                successful=statistics.successful,
                failed=statistics.failed,
                refused=statistics.refused,
                opportunities=len(opportunities),
                # Лучший результат цикла независимо от порога: без него по
                # записи «ноль возможностей» нельзя понять, насколько
                # близко было к прибыли.
                evaluated=len(evaluated),
                best_net_roi=None if best is None else str(best.net_roi.value),
                best_route=None
                if best is None
                else f"{best.buy_provider.value}->{best.sell_provider.value}",
                best_token=None if best is None else str(best.token),
                # Отдельно — лучшее среди волатильных токенов: стабильная
                # пара почти всегда впереди просто потому, что теряет
                # меньше, и по общему лучшему результату не видно, как
                # близко было у остальных.
                # Разбивка газа: по итоговой стоимости не видно, в чём
                # ошибка — в расходе или в цене. Единицы обещанные, без
                # поправки: применённую поправку пишет сама калибровка.
                best_gas_cost=None if best is None else str(best.gas_cost),
                best_quoted_gas_units=None if best is None else best.quoted_gas_units,
                best_quoted_gas_price_wei=(None if best is None else best.quoted_gas_price_wei),
                best_volatile_net_roi=(
                    None if best_volatile is None else str(best_volatile.net_roi.value)
                ),
                best_volatile_route=(
                    None
                    if best_volatile is None
                    else f"{best_volatile.buy_provider.value}->{best_volatile.sell_provider.value}"
                ),
                best_volatile_token=None if best_volatile is None else str(best_volatile.token),
                # Почему лучшая комбинация не стала возможностью. Без этих
                # полей «ноль возможностей» выглядит одинаково и когда
                # доходность не дотянула до порога, и когда порог не
                # оценивался из-за неизвестного расхода.
                blocked_by_unknown_cost=len(blocked),
                unknown_costs=",".join(unknown_costs) or None,
            ),
        )
        return finished

    def _stable_tokens(self) -> frozenset[TokenKey]:
        """Токены, помеченные в конфигурации как стабильные.

        Признак принадлежит токену, а не провайдеру и не сети, поэтому
        читается из конфигурации напрямую: отдельного источника для него
        заводить не нужно.
        """
        return frozenset(
            TokenKey(network_id=token.network_id, address=token.address)
            for token in self._configuration.tokens
            if token.usd_stable
        )

    def _record_metrics(self, scan: Scan, statistics: QuoteStatistics) -> None:
        """Записать метрики цикла (``28_OBSERVABILITY.md`` §30).

        В labels попадают только low-cardinality значения: идентификаторы
        цикла и возможностей туда не входят (``28`` §42).
        """
        if self._metrics is None:
            return
        self._metrics.increment(names.LEVEL1_SCANS, status=scan.status.value)
        self._metrics.increment(
            names.LEVEL1_QUOTE_REQUESTS, amount=statistics.requests, status="total"
        )
        self._metrics.increment(
            names.LEVEL1_QUOTE_FAILURES, amount=statistics.failed, status="failed"
        )
        self._metrics.increment(
            names.LEVEL1_OPPORTUNITIES,
            amount=scan.statistics.opportunities_created,
            status="created",
        )
        if scan.finished_at is not None:
            self._metrics.observe(
                names.LEVEL1_SCAN_SECONDS,
                (scan.finished_at - scan.started_at).total_seconds(),
                status=scan.status.value,
            )


def _evaluated_candidates(candidates: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
    """Комбинации, для которых расчёт удалось довести до конца.

    Отличается от числа успешных котировок: комбинация требует обеих ног
    и всех известных издержек. Незавершённый расчёт в сравнении не
    участвует — неизвестное не выдаётся за худшее или лучшее.
    """
    return tuple(
        candidate for candidate in candidates if candidate.preliminary_result.net_roi is not None
    )


#: Сколько разных меток неизвестных расходов сохраняется. Диагностике
#: важен состав, а не полный перечень: метки повторяются от комбинации к
#: комбинации.
_UNKNOWN_COST_LIMIT = 10


def _blocked_by_unknown_cost(candidates: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
    """Комбинации, у которых порог не оценивался из-за неизвестного расхода.

    Отличаются от не прошедших порог: доходность у них может быть какой
    угодно, включая достаточную. Решение не засчитывать такой порог
    принято архитектурой (``09_PROFIT_CALCULATOR.md`` §27) и здесь только
    подсчитывается.
    """
    return tuple(
        candidate
        for candidate in candidates
        if (outcome := candidate.preliminary_result.threshold_outcome) is not None
        and outcome.blocked_by_unknown_cost
    )


def _unknown_cost_components(candidates: tuple[Candidate, ...]) -> tuple[str, ...]:
    """Какие расходы оказались неизвестны — различные метки, по порядку."""
    labels: set[str] = set()
    for candidate in candidates:
        costs = candidate.preliminary_result.costs
        if costs is not None:
            labels.update(costs.unknown_components)
    return tuple(sorted(labels))[:_UNKNOWN_COST_LIMIT]


def _volatile_candidates(
    candidates: tuple[Candidate, ...], stable: frozenset[TokenKey]
) -> tuple[Candidate, ...]:
    """Комбинации по токенам, не помеченным как стабильные.

    Если стабильных токенов в конфигурации нет, набор не меняется:
    отбор не должен зависеть от того, пользуется ли пометкой конкретная
    установка.
    """
    if not stable:
        return candidates
    return tuple(
        candidate for candidate in candidates if candidate.buy_quote.output_token not in stable
    )


def _best_combination(candidates: tuple[Candidate, ...]) -> BestCombination | None:
    """Лучшая комбинация цикла независимо от порога.

    При равной доходности выбор детерминирован: порядок задают провайдеры
    и токен, чтобы повторный цикл на тех же данных дал тот же результат.
    """
    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda candidate: (
            candidate.preliminary_result.net_roi.value,  # type: ignore[union-attr]
            candidate.buy_quote.provider_id.value,
            candidate.sell_quote.provider_id.value,
            str(candidate.buy_quote.output_token),
        ),
    )
    result = best.preliminary_result
    return BestCombination(
        net_roi=result.net_roi,  # type: ignore[arg-type]
        gross_roi=result.gross_roi,
        token=best.buy_quote.output_token,
        buy_provider=best.buy_quote.provider_id,
        sell_provider=best.sell_quote.provider_id,
        gas_cost=None if result.costs is None else result.costs.gas_cost,
        quoted_gas_units=_quoted_units(best),
        quoted_gas_price_wei=_quoted_price(best),
    )


def _quoted_units(candidate: Candidate) -> int | None:
    """Сколько газа обещали котировки обеих ног, без поправки."""
    buy = candidate.buy_quote.estimated_gas_units
    sell = candidate.sell_quote.estimated_gas_units
    return None if buy is None or sell is None else buy + sell


def _quoted_price(candidate: Candidate) -> int | None:
    """Цена газа из котировки. Достаточно, чтобы её назвала одна нога."""
    for quote in (candidate.buy_quote, candidate.sell_quote):
        if quote.estimated_gas_price_wei is not None:
            return quote.estimated_gas_price_wei
    return None


def _passes_preliminary_threshold(candidate: Candidate) -> bool:
    """Прошёл ли кандидат предварительный порог.

    Порог применяет Profit Calculator; Scanner только читает результат
    (``10_LEVEL_1_SCANNER.md`` §46, §48). Неизвестный обязательный расход
    порог не проходит (``02_LEVEL1_SCANNER.md`` §32).
    """
    outcome = candidate.preliminary_result.threshold_outcome
    return outcome is not None and outcome.passed


def _log_observed(mode: ScanMode, groups: tuple[CandidateGroup, ...]) -> None:
    """Записать, что проход **нашёл бы**, не создавая возможности.

    Нужно для наблюдения за режимом, у которого потребителя ещё нет:
    журнал показывает, какая сделка состоялась бы и с каким заработком,
    а состояние системы при этом не меняется.
    """
    if not groups:
        return
    best = groups[0]
    amounts = [
        candidate
        for candidate in best.candidates
        if candidate.preliminary_result.net_profit is not None
    ]
    if not amounts:
        return
    winner = max(amounts, key=lambda item: item.preliminary_result.net_profit or Decimal(0))
    _LOGGER.info(
        "opportunity observed without a consumer",
        extra=log_fields(
            mode=mode.value,
            token=str(winner.buy_quote.output_token),
            amount=str(winner.buy_quote.input_amount.as_decimal),
            route=f"{best.buy_provider_id.value}->{best.sell_provider_id.value}",
            net_profit=str(winner.preliminary_result.net_profit),
            net_roi=(
                None
                if winner.preliminary_result.net_roi is None
                else str(winner.preliminary_result.net_roi.value)
            ),
            candidates=len(groups),
        ),
    )
