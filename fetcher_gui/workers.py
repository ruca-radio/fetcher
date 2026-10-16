from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class WorkerSignals(QObject):
    """Signals emitted by a background :class:`Task`."""

    result = Signal(object)
    error = Signal(object)
    cancelled = Signal()
    finished = Signal()


class Task(QRunnable):
    """Run one callable in a ``QThreadPool`` and report its outcome."""

    def __init__(self, function: Callable[..., Any], *args: object, **kwargs: object) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """Prevent execution when the task has not started yet."""

        self._cancelled.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    @Slot()
    def run(self) -> None:
        try:
            if self.is_cancelled:
                self.signals.cancelled.emit()
                return
            result = self.function(*self.args, **self.kwargs)
            self.signals.result.emit(result)
        except Exception as exception:
            self.signals.error.emit(
                (type(exception), exception, traceback.format_exc())
            )
        finally:
            self.signals.finished.emit()
