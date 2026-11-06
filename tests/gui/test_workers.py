from __future__ import annotations

import gc
import threading
import tomllib
from pathlib import Path

from PySide6.QtCore import QThreadPool

from fetcher_gui.workers import Task


def test_task_emits_result_and_finished() -> None:
    events: list[tuple[str, object]] = []
    task = Task(lambda left, right: left + right, 20, 22)
    task.signals.result.connect(lambda value: events.append(("result", value)))
    task.signals.finished.connect(lambda: events.append(("finished", None)))

    task.run()

    assert events == [("result", 42), ("finished", None)]


def test_task_emits_exception_and_traceback() -> None:
    errors: list[tuple[type[BaseException], BaseException, str]] = []
    finished: list[bool] = []

    def fail() -> None:
        raise ValueError("bad task")

    task = Task(fail)
    task.signals.error.connect(lambda error: errors.append(error))
    task.signals.finished.connect(lambda: finished.append(True))

    task.run()

    assert len(errors) == 1
    error_type, exception, traceback_text = errors[0]
    assert error_type is ValueError
    assert str(exception) == "bad task"
    assert "ValueError: bad task" in traceback_text
    assert finished == [True]


def test_cancelled_task_does_not_call_function() -> None:
    called: list[bool] = []
    cancelled: list[bool] = []
    finished: list[bool] = []
    task = Task(lambda: called.append(True))
    task.signals.cancelled.connect(lambda: cancelled.append(True))
    task.signals.finished.connect(lambda: finished.append(True))

    task.cancel()
    task.run()

    assert called == []
    assert cancelled == [True]
    assert finished == [True]


def test_task_delivers_signals_from_real_thread_pool(qtbot) -> None:
    main_thread = threading.get_ident()
    worker_threads: list[int] = []
    results: list[int] = []
    finished: list[bool] = []
    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    task = Task(lambda: (worker_threads.append(threading.get_ident()), 42)[1])
    signals = task.signals
    signals.result.connect(results.append)
    signals.finished.connect(lambda: finished.append(True))

    pool.start(task)
    del task
    gc.collect()
    qtbot.waitUntil(lambda: finished == [True], timeout=2_000)
    assert pool.waitForDone(2_000)

    assert results == [42]
    assert worker_threads and worker_threads[0] != main_thread
    assert signals is not None


def test_wheel_configuration_includes_gui_core_and_top_level_app_files() -> None:
    repo = Path(__file__).resolve().parents[2]
    with (repo / "pyproject.toml").open("rb") as project_file:
        config = tomllib.load(project_file)

    includes = config["tool"]["hatch"]["build"]["targets"]["wheel"]["include"]

    assert "/fetcher_gui" in includes
    assert "/fetcher_core" in includes
    assert "/fetchtui.py" in includes
    assert "/extract_twitter_cookies.py" in includes
