"""Фоновый запуск: проход сканера по расписанию.

Сбой одного прохода (площадка не ответила, пропала сеть) не останавливает
работу: ошибка пишется в лог, следующий проход идёт по расписанию. После
нескольких сбоев подряд в Telegram уходит тревога, после восстановления
сообщение, что всё снова работает. Раз в день в Telegram уходит отчёт по
виртуальным сделкам, а из базы убираются лишние старые записи.

Второй экземпляр с той же базой не запускается: работающий процесс раз в
минуту оставляет в базе отметку, и новый запуск не стартует, пока она свежая.

Когда игр три и больше, первая из списка сканируется каждый проход, а остальные
по очереди, по одной за проход (см. :func:`games_for_pass`).

Настройки, сохранённые в интерфейсе (:mod:`skinedge.tuning`), перечитываются в
начале каждого прохода и действуют вместо флагов запуска. Итог каждого прохода и
найденные сделки записываются в базу для карточек интерфейса (:mod:`skinedge.feed`).

В приложении (``skinedge app``) сканер работает в потоке и останавливается
событием ``stop``: ожидание следующего прохода прерывается сразу, а начатый
проход доводится до конца.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from html import escape

from skinedge import feed
from skinedge.fx import FxRates
from skinedge.games import title
from skinedge.notify.message import format_paper_report
from skinedge.notify.telegram import TelegramError
from skinedge.paper import build_report
from skinedge.pipeline import Pipeline, ScanOptions, ScanResult
from skinedge.scanner import ScanConfig
from skinedge.sources.base import SourceError
from skinedge.storage import Storage
from skinedge.tuning import ScanSettings
from skinedge.tuning import load as load_tuning

log = logging.getLogger(__name__)

MIN_EVERY = timedelta(minutes=5)  # Skinport отдаёт данные из кэша на 5 минут
FX_REFRESH = timedelta(hours=6)
FX_MAX_AGE = timedelta(hours=72)  # ЕЦБ не публикует курс в выходные и праздники
LOCK_STALE = timedelta(minutes=20)
BEAT = 60.0  # секунд между отметками во время ожидания
PRUNE_EVERY = timedelta(hours=24)
REPORT_DAYS = 30

HEARTBEAT = "runner.heartbeat"
PID = "runner.pid"
REPORT_DATE = "runner.report_date"
PRUNED_AT = "runner.pruned_at"


def games_for_pass(games: tuple[int, ...], n: int) -> tuple[int, ...]:
    """Игры прохода ``n`` (с единицы).

    Skinport отдаёт цены одним запросом на игру и блокирует соединение на час, если
    запросов в час слишком много (см. :mod:`skinedge.sources.skinport`). Поэтому при
    трёх играх и больше первая игра (по умолчанию CS2) идёт каждый проход, а
    остальные по очереди: с CS2, Dota 2 и Rust это 2 игры за проход вместо 3.
    """
    if len(games) < 3:
        return games
    rest = games[1:]
    return (games[0], rest[(n - 1) % len(rest)])


class AlreadyRunning(RuntimeError):
    """С этой базой уже работает другой фоновый запуск."""


def release_lock(storage: Storage) -> None:
    """Снимает отметку работающего сканера, если её оставил этот процесс."""
    if storage.get_meta(PID) == str(os.getpid()):
        storage.delete_meta(HEARTBEAT)


@dataclass(frozen=True)
class RunOptions:
    """Расписание.

    Attributes:
        every: Интервал между началами проходов.
        iterations: Сколько проходов сделать; 0 — без ограничения.
        report_hour: С какого часа (местное время) слать ежедневный отчёт;
            ``None`` — не слать.
        alert_after: После скольких сбоев подряд слать тревогу.
        force: Запуститься, даже если отметка другого запуска свежая.
    """

    every: timedelta = timedelta(minutes=15)
    iterations: int = 0
    report_hour: int | None = 10
    alert_after: int = 3
    force: bool = False


class Runner:
    """Цикл проходов.

    Args:
        pipeline: Проход сканера; хранит базу, комиссии и Telegram.
        scan: Параметры прохода.
        opts: Расписание.
        fx_source: Откуда брать курс (ЕЦБ или заданный вручную).
        spread: Спред конвертации для отчёта по виртуальным сделкам.
        clock: Текущее время UTC (для тестов).
        sleep: Функция ожидания; в приложении ``stop.wait``, чтобы остановка
            прерывала ожидание.
        tz: Часовой пояс отчёта; ``None`` — местный.
        stop: Событие остановки; ``None`` — работать до ``iterations`` или Ctrl+C.
    """

    def __init__(
        self,
        pipeline: Pipeline,
        scan: ScanOptions,
        opts: RunOptions,
        fx_source: Callable[[], FxRates],
        spread: Decimal,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], object] = time.sleep,
        tz: tzinfo | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        if opts.every < MIN_EVERY:
            raise ValueError(f"Интервал не меньше {MIN_EVERY.seconds // 60} минут")
        self.pipeline = pipeline
        self.storage = pipeline.storage
        self.scan = scan
        self.opts = opts
        self.fx_source = fx_source
        self.spread = spread
        self.clock = clock
        self.sleep = sleep
        self.tz = tz
        self.stop = stop
        self.current: int | None = None  # номер идущего прохода
        self.next_at: datetime | None = None  # начало следующего прохода
        self.failures = 0  # сбоев подряд
        self.alerted = False
        self.passes = 0  # с прошлого отчёта
        self.failed = 0  # с прошлого отчёта
        self._fx: FxRates | None = None
        self._fx_at: datetime | None = None
        self._tuning: ScanSettings | None = None

    # ---------- цикл ----------

    def run(self) -> int:
        """Выполняет проходы по расписанию до ``iterations``, остановки или Ctrl+C.

        Returns:
            Сколько проходов выполнено.

        Raises:
            AlreadyRunning: Если работает другой запуск.
        """
        self.acquire()
        try:
            return self.loop()
        finally:
            self.release()

    def loop(self) -> int:
        """Проходы по расписанию; отметку ставит :meth:`acquire`, снимает :meth:`release`.

        Returns:
            Сколько проходов выполнено.
        """
        done = 0
        try:
            while True:
                started = self.clock()
                self.tick(done + 1)
                done += 1
                if (self.opts.iterations and done >= self.opts.iterations) or self._stopping():
                    return done
                self.next_at = started + self.opts.every
                log.info("Следующий проход в %s", self._local(self.next_at).strftime("%H:%M"))
                self._wait_until(self.next_at)
                if self._stopping():
                    return done
        finally:
            self.next_at = None

    def tick(self, n: int) -> ScanResult | None:
        """Один проход с обработкой сбоя и ежедневными делами.

        Returns:
            Итог прохода или ``None`` при сбое.
        """
        started = self.clock()
        self.current = n
        self._beat(started)
        self.passes += 1
        games = games_for_pass(self.scan.games, n)
        result: ScanResult | None = None
        error: str | None = None
        try:
            self.pipeline.fx = self._rates(started)
            result = self.pipeline.run(self._tuned(replace(self.scan, games=games)))
        except Exception as exc:  # любой сбой прохода не должен останавливать работу
            error = _describe(exc)
            self._failed(n, exc)
        else:
            self._succeeded(n, result, self.clock() - started)
        self._publish(n, started, games, result, error)
        self._chores(self.clock())
        self._beat(self.clock())
        self.current = None
        return result

    def _tuned(self, opts: ScanOptions) -> ScanOptions:
        """Параметры прохода с настройками из интерфейса, если они сохранены."""
        tuning = load_tuning(self.storage)
        if tuning != self._tuning:
            if tuning is None:
                log.info("Настройки из интерфейса сброшены, действуют флаги запуска")
            else:
                log.info("Настройки из интерфейса: %s", tuning.describe())
            self._tuning = tuning
        return opts if tuning is None else tuning.apply(opts)

    def _succeeded(self, n: int, result: ScanResult, took: timedelta) -> None:
        if self.alerted:
            self._send(f"✅ SkinEdge снова работает после {self.failures} сбоев подряд.")
            self.alerted = False
        self.failures = 0
        complete = sum(o.result.complete for o in result.found)
        log.info(
            "Проход %d: сравнено %d, возможностей %d (полных %d), записано %d, "
            "отправлено %d; %.0f с",
            n,
            result.pairs,
            len(result.found),
            complete,
            result.recorded,
            result.notified,
            took.total_seconds(),
        )
        for opp in result.found[:3]:
            r = opp.result
            log.info(
                "  %s → %s  %s (%s): прибыль %s, ROI %.1f%%",
                r.buy_market,
                r.sell_market,
                opp.name,
                title(opp.buy_quote.app_id),
                r.profit,
                r.roi * 100,
            )

    def _publish(
        self,
        n: int,
        started: datetime,
        games: tuple[int, ...],
        result: ScanResult | None,
        error: str | None,
    ) -> None:
        """Записывает итог прохода и найденные сделки для интерфейса."""
        now = self.clock()
        summary: dict[str, object] = {
            "n": n,
            "at": started.isoformat(),
            "took": round((now - started).total_seconds()),
            "games": list(games),
            "ok": result is not None,
            "error": error,
        }
        if result is not None:
            summary.update(
                pairs=result.pairs,
                found=len(result.found),
                complete=sum(o.result.complete for o in result.found),
                recorded=result.recorded,
                notified=result.notified,
            )
        try:
            feed.record_pass(self.storage, summary)
            if result is not None:
                dmarket = self.pipeline.dmarket
                fetch = dmarket.fetch_image if dmarket is not None else None
                feed.update_deals(self.storage, result.at, games, result.found, fetch, now)
        except Exception:
            log.exception("Сделки для интерфейса не записаны")

    def _failed(self, n: int, exc: Exception) -> None:
        self.failures += 1
        self.failed += 1
        reason = _describe(exc)
        if isinstance(exc, SourceError):
            log.error("Проход %d не удался: %s", n, reason)
        else:
            log.exception("Проход %d не удался: %s", n, reason)
        if self.failures == self.opts.alert_after:
            self.alerted = self._send(
                f"⚠️ SkinEdge: {self.failures} прохода подряд с ошибкой.\n"
                f"Последняя: <code>{escape(reason)}</code>\n"
                "Работа продолжается по расписанию, подробности в логе."
            )

    # ---------- курс ----------

    def _rates(self, now: datetime) -> FxRates:
        if self._fx is not None and self._fx_at is not None and now - self._fx_at < FX_REFRESH:
            return self._fx
        try:
            fx = self.fx_source()
        except SourceError as exc:
            if self._fx is None or self._fx_at is None or now - self._fx_at > FX_MAX_AGE:
                raise
            log.warning(
                "Курс не обновлён (%s), использую полученный %s UTC",
                exc,
                self._fx_at.strftime("%d.%m %H:%M"),
            )
            return self._fx
        self._fx, self._fx_at = fx, now
        log.info("Курс: %s (%s)", fx.describe(), fx.source)
        return fx

    # ---------- ежедневные дела ----------

    def _chores(self, now: datetime) -> None:
        try:
            self._maybe_prune(now)
        except Exception:
            log.exception("Очистка базы не удалась")
        try:
            self._maybe_report(now)
        except Exception:
            log.exception("Отчёт по виртуальным сделкам не отправлен")

    def _maybe_prune(self, now: datetime) -> None:
        last = self.storage.get_meta(PRUNED_AT)
        if last is not None and now - datetime.fromisoformat(last) < PRUNE_EVERY:
            return
        removed = self.storage.prune(now)
        self.storage.set_meta(PRUNED_AT, now.isoformat())
        log.info(
            "Очистка базы: удалено цен %d, записей продаж %d",
            removed["quotes"],
            removed["sales_stats"],
        )

    def _maybe_report(self, now: datetime) -> None:
        if self.opts.report_hour is None or self.pipeline.telegram is None:
            return
        local = self._local(now)
        today = local.date().isoformat()
        if local.hour < self.opts.report_hour or self.storage.get_meta(REPORT_DATE) == today:
            return
        report = build_report(
            self.storage,
            self.pipeline.schedule,
            self.spread,
            ScanConfig(),
            now,
            now - timedelta(days=REPORT_DAYS),
        )
        text = format_paper_report(report, REPORT_DAYS)
        text += f"\n\nПроходов с прошлого отчёта: {self.passes}, с ошибкой: {self.failed}."
        if self._send(text):
            self.storage.set_meta(REPORT_DATE, today)
            self.passes = self.failed = 0
            log.info("Отчёт по виртуальным сделкам отправлен в Telegram")

    # ---------- служебное ----------

    def _send(self, text: str) -> bool:
        if self.pipeline.telegram is None:
            return False
        bot, chat_id = self.pipeline.telegram
        try:
            bot.send(chat_id, text)
        except TelegramError as exc:
            log.warning("Telegram: сообщение не отправлено (%s)", exc)
            return False
        return True

    def _local(self, moment: datetime) -> datetime:
        return moment.astimezone(self.tz)

    def _stopping(self) -> bool:
        return self.stop is not None and self.stop.is_set()

    def acquire(self) -> None:
        """Ставит отметку работающего сканера.

        Raises:
            AlreadyRunning: Если отметка другого запуска свежая (и не ``force``).
        """
        beat = self.storage.get_meta(HEARTBEAT)
        if beat is not None and not self.opts.force:
            last = datetime.fromisoformat(beat)
            if self.clock() - last < LOCK_STALE:
                raise AlreadyRunning(
                    f"с этой базой уже работает skinedge run (процесс {self.storage.get_meta(PID)},"
                    f" последняя отметка {self._local(last):%H:%M:%S})"
                )
        self.storage.set_meta(PID, str(os.getpid()))
        self._beat(self.clock())

    def release(self) -> None:
        """Снимает отметку, если её оставил этот процесс."""
        release_lock(self.storage)

    def _beat(self, now: datetime) -> None:
        # После остановки отметку не обновляем: её снимают при выходе.
        if not self._stopping():
            self.storage.set_meta(HEARTBEAT, now.isoformat())

    def _wait_until(self, moment: datetime) -> None:
        while True:
            left = (moment - self.clock()).total_seconds()
            if left <= 0 or self._stopping():
                return
            self.sleep(min(left, BEAT))
            self._beat(self.clock())


def _describe(exc: Exception) -> str:
    text = str(exc) if isinstance(exc, SourceError) else f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 300 else text[:297] + "..."
