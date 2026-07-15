from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QThreadPool, Signal, Slot
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from fetcher_core.models import AppSettings

from .workers import Task


def apply_theme(theme: str) -> None:
    application = QApplication.instance()
    if application is None:
        return
    selected = theme if theme in {"System", "Light", "Dark"} else "System"
    application.setProperty("fetcher-theme", selected)
    if selected == "System":
        application.setPalette(application.style().standardPalette())
        application.setStyleSheet("")
        return
    if selected == "Light":
        application.setPalette(application.style().standardPalette())
        application.setStyleSheet("QToolTip { color: #202124; background: #ffffff; border: 1px solid #9aa0a6; }")
        return
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#202124",
        QPalette.ColorRole.WindowText: "#f1f3f4",
        QPalette.ColorRole.Base: "#171717",
        QPalette.ColorRole.AlternateBase: "#292a2d",
        QPalette.ColorRole.Text: "#f1f3f4",
        QPalette.ColorRole.Button: "#303134",
        QPalette.ColorRole.ButtonText: "#f1f3f4",
        QPalette.ColorRole.Highlight: "#8ab4f8",
        QPalette.ColorRole.HighlightedText: "#171717",
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    application.setPalette(palette)
    application.setStyleSheet("QToolTip { color: #f1f3f4; background: #303134; border: 1px solid #8ab4f8; }")


class SettingsPage(QWidget):
    settings_saved = Signal()
    restart_required = Signal(str)

    def __init__(self, settings_service=None, *, diagnostics=None, thread_pool: QThreadPool | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.settings_service = settings_service
        self.diagnostics = diagnostics
        self.thread_pool = thread_pool or QThreadPool(self)
        self._tasks: set[Task] = set()
        self._settings = getattr(settings_service, "settings", AppSettings())
        load_preferences = getattr(settings_service, "load_gui_preferences", None)
        self._gui_preferences = load_preferences() if load_preferences is not None else {}
        self.storage_edit = QLineEdit(self._settings.plex_dir, self)
        self.temp_edit = QLineEdit(self._settings.tmp_dir, self)
        self.staging_edit = QLineEdit(str(self._gui_preferences.get("staging_dir", "")), self)
        self.parallel_spin = QSpinBox(self)
        self.parallel_spin.setRange(1, 16)
        self.parallel_spin.setValue(self._settings.parallel_downloads)
        self.fragments_spin = QSpinBox(self)
        self.fragments_spin.setRange(1, 32)
        self.fragments_spin.setValue(self._settings.concurrent_fragments)
        self.cache_size_spin = QSpinBox(self)
        self.cache_size_spin.setRange(1, 102_400)
        self.cache_size_spin.setValue(int(self._gui_preferences.get("cache_size_mib", 512)))
        self.cache_age_spin = QSpinBox(self)
        self.cache_age_spin.setRange(1, 365)
        self.cache_age_spin.setValue(int(self._gui_preferences.get("cache_age_days", 14)))
        self.theme_combo = QComboBox(self)
        self.theme_combo.addItems(["System", "Light", "Dark"])
        self.theme_combo.setCurrentText(str(self._gui_preferences.get("theme", "System")))
        self.naming_edit = QLineEdit(self._settings.naming_template, self)
        self.searx_checkbox = QCheckBox("Enable SearXNG", self)
        self.searx_checkbox.setChecked(self._settings.searxng_enabled)
        self.save_button = QPushButton("Save settings", self)
        self.diagnostics_button = QPushButton("Run diagnostics", self)
        self.diagnostics_label = QLabel("", self)
        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Plex storage (path or SFTP)", self.storage_edit)
        form.addRow("Temporary directory", self.temp_edit)
        form.addRow("Local staging directory", self.staging_edit)
        form.addRow("Parallel downloads", self.parallel_spin)
        form.addRow("Concurrent fragments", self.fragments_spin)
        form.addRow("Preview cache (MiB)", self.cache_size_spin)
        form.addRow("Preview cache age (days)", self.cache_age_spin)
        form.addRow("Theme", self.theme_combo)
        form.addRow("Filename template", self.naming_edit)
        form.addRow("Provider preferences", self.searx_checkbox)
        actions = QHBoxLayout()
        actions.addWidget(self.save_button)
        actions.addWidget(self.diagnostics_button)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(actions)
        layout.addWidget(self.status_label)
        layout.addWidget(self.diagnostics_label)
        layout.addStretch(1)
        self.save_button.clicked.connect(self.save_settings)
        self.diagnostics_button.clicked.connect(self.run_diagnostics)

    @Slot()
    def save_settings(self) -> None:
        settings = replace(
            self._settings,
            plex_dir=self.storage_edit.text().strip(),
            tmp_dir=self.temp_edit.text().strip(),
            parallel_downloads=self.parallel_spin.value(),
            concurrent_fragments=self.fragments_spin.value(),
            naming_template=self.naming_edit.text(),
            searxng_enabled=self.searx_checkbox.isChecked(),
        )
        preferences = {
            "staging_dir": self.staging_edit.text().strip(),
            "cache_size_mib": self.cache_size_spin.value(),
            "cache_age_days": self.cache_age_spin.value(),
            "theme": self.theme_combo.currentText(),
        }
        if self.settings_service is None:
            self.status_label.setText("Settings save failed: no settings service is available.")
            return
        try:
            validate = getattr(self.settings_service, "validate", None)
            if validate is not None:
                settings, preferences = validate(settings, preferences)
            else:
                self._validate_paths(settings, preferences)
            self.settings_service.save(settings)
            save_preferences = getattr(self.settings_service, "save_gui_preferences", None)
            if save_preferences is not None:
                preferences = save_preferences(preferences) or preferences
                self._gui_preferences = preferences
            else:
                raise RuntimeError("GUI preference persistence is unavailable")
        except Exception as exc:
            self.status_label.setText(f"Settings save failed: {exc}")
            return
        self._settings = settings
        apply_theme(str(preferences["theme"]))
        message = (
            "Settings saved. Theme applied now. Restart required for storage, download, "
            "search, or cache changes."
        )
        self.status_label.setText(message)
        self.settings_saved.emit()
        self.restart_required.emit(message)

    @staticmethod
    def _validate_paths(settings: AppSettings, preferences: dict[str, object]) -> None:
        values = {
            "temporary directory": settings.tmp_dir,
            "Plex storage": settings.plex_dir,
            "staging directory": str(preferences.get("staging_dir") or ""),
        }
        for label, value in values.items():
            if not str(value).strip():
                raise ValueError(f"{label} is required")
            if any(ord(character) < 32 or ord(character) == 127 for character in str(value)):
                raise ValueError(f"{label} contains control characters")
        local_labels = ["temporary directory", "staging directory"]
        if not settings.plex_dir.startswith("sftp:"):
            local_labels.append("Plex storage")
        for label in local_labels:
            path = Path(values[label]).expanduser()
            if path.exists() and not path.is_dir():
                raise ValueError(f"{label} is not a directory: {path}")

    def cache_settings(self) -> tuple[int, int]:
        return self.cache_size_spin.value(), self.cache_age_spin.value()

    @Slot()
    def run_diagnostics(self) -> None:
        if self.diagnostics is None:
            return
        task = Task(self.diagnostics)
        task.signals.result.connect(self._show_diagnostics)
        self._tasks.add(task)
        task.signals.finished.connect(lambda task=task: self._tasks.discard(task))
        try:
            self.thread_pool.start(task)
        except Exception as exc:
            task.cancel()
            self._tasks.discard(task)
            self.diagnostics_label.setText(f"Diagnostics could not start: {exc}")

    @Slot(object)
    def _show_diagnostics(self, result) -> None:
        if isinstance(result, dict):
            self.diagnostics_label.setText("\n".join(f"{name}: {value}" for name, value in result.items()))
        else:
            self.diagnostics_label.setText(str(result))

    def invalidate(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()

    def set_submissions_enabled(self, enabled: bool) -> None:
        self.save_button.setEnabled(enabled)
        self.diagnostics_button.setEnabled(enabled)
