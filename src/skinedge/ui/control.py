"""Сканер внутри приложения: запуск и остановка кнопкой из интерфейса.

Сканер работает в отдельном потоке того же процесса, что и интерфейс, со своим
соединением с базой. Остановка не обрывает проход: ожидание следующего прохода
прерывается сразу, а начатый проход сканер доводит до конца и выходит.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from skinedge.runner import Runner, release_lock
from skinedge.storage import Storage

log = logging.getLogger(__name__)

RunnerFactory = Callable[[threading.Event, bool], Runner]


class ScannerControl:
    """Запуск, остановка и состояние сканера.

    Args:
        factory: Собирает сканер; получает событие остановки и признак ``force``
            (запуститься, даже если отметка другого запуска свежая).
        clock: Текущее время UTC.
    """

    def __init__(
        self, factory: RunnerFactory, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._lock = threading.Lock()
        self._runner: Runner | None = None
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self.started_at: datetime | None = None
        self.error: str | None = None  # почему сканер остановился сам

    @property
    def alive(self) -> bool:
        """Поток сканера работает (в том числе доводит проход после остановки)."""
        return self._thread is not None and self._thread.is_alive()

    @staticmethod
    def idle() -> dict[str, Any]:
        """Состояние остановленного сканера."""
        return {
            "state": "stopped",
            "pass": None,
            "next_at": None,
            "started_at": None,
            "error": None,
        }

    def status(self) -> dict[str, Any]:
        """Состояние: ``running``, ``stopping`` или ``stopped``; идущий и следующий проход."""
        runner, stop = self._runner, self._stop
        if not self.alive or runner is None:
            return {**self.idle(), "error": self.error}
        stopping = stop is not None and stop.is_set()
        next_at = runner.next_at
        return {
            "state": "stopping" if stopping else "running",
            "pass": runner.current,
            "next_at": None if stopping or next_at is None else next_at.isoformat(),
            "started_at": None if self.started_at is None else self.started_at.isoformat(),
            "error": None,
        }

    def start(self, force: bool = False) -> None:
        """Запускает сканер; если он уже работает, ничего не делает.

        Raises:
            AlreadyRunning: Если с этой базой работает другой запуск.
            SetupError: Если не хватает ключей в ``.env``.
        """
        with self._lock:
            if self.alive:
                return
            stop = threading.Event()
            runner = self._factory(stop, force)
            try:
                runner.acquire()
            except BaseException:
                runner.storage.close()
                raise
            thread = threading.Thread(
                target=self._work, args=(runner,), name="skinedge-scanner", daemon=True
            )
            self._runner, self._stop, self._thread = runner, stop, thread
            self.started_at, self.error = self._clock(), None
            thread.start()
        log.info("Сканер запущен из интерфейса. Только анализ, сделки не совершаются")

    def _work(self, runner: Runner) -> None:
        try:
            runner.loop()
        except Exception as exc:  # без этого поток умер бы молча
            self.error = f"Сканер остановился из-за ошибки: {type(exc).__name__}: {exc}"
            log.exception("Сканер остановился из-за ошибки")
        finally:
            try:
                runner.release()
            finally:
                runner.storage.close()
            log.info("Сканер остановлен")

    def stop(self) -> None:
        """Просит сканер остановиться: сразу, если он ждёт, или после текущего прохода."""
        stop = self._stop
        if stop is None or stop.is_set() or not self.alive:
            return
        stop.set()
        if self._runner is not None and self._runner.current is not None:
            log.info("Остановка сканера: дождусь конца текущего прохода")

    def shutdown(self, storage: Storage, timeout: float) -> None:
        """Останавливает сканер при выходе из приложения.

        Ждёт не дольше ``timeout`` секунд. Если проход ещё идёт, поток прервётся
        вместе с процессом (база откатит незаконченную запись), а отметку
        работающего сканера снимаем сами, чтобы следующий запуск не ждал 20 минут.

        Args:
            storage: Соединение с базой этого потока (не соединение сканера).
            timeout: Сколько ждать конца прохода, секунд.
        """
        self.stop()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                log.warning("Проход не закончился за %.0f с, выхожу без него", timeout)
        release_lock(storage)
