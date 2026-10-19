from __future__ import annotations

import threading
from dataclasses import dataclass, replace
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

_DEFAULT_SIGN_IN_SITES: tuple[str, ...] = ("PornHub", "RedTube", "xHamster")
_ACTIVE_SIGN_IN_STATUS = "Sign-in window open — log in, then close Chrome."
_LOGIN_HELP_TEXT = (
    "Sign-in opens a Chrome window on the provider's login page. Log in there, then close "
    "the window; Fetcher saves the session cookies (owner-only, under .fetcher/cookies). "
    "Cookie values are never shown."
)


@dataclass
class ProviderSignInRow:
    """Widgets for one provider's interactive sign-in controls."""

    site: str
    name_label: QLabel
    status_label: QLabel
    sign_in_button: QPushButton
    sign_out_button: QPushButton
    container: QWidget


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
                 login_service=None, parent=None) -> None:
        super().__init__(parent)
        self.settings_service = settings_service
        self.diagnostics = diagnostics
        self.thread_pool = thread_pool or QThreadPool(self)
        self._tasks: set[Task] = set()
        self._submissions_enabled = True
        if login_service is None:
            from fetcher_core import provider_login

            login_service = provider_login
        self.login_service = login_service
        self._active_signin_site: str | None = None
        self._active_signin_cancel: threading.Event | None = None
        self._active_signin_task: Task | None = None
        self._settings = getattr(settings_service, "settings", AppSettings())
        load_preferences = getattr(settings_service, "load_gui_preferences", None)
        self._gui_preferences = load_preferences() if load_preferences is not None else {}
        self.storage_edit = QLineEdit(self._settings.plex_dir, self)
        self.temp_edit = QLineEdit(self._settings.tmp_dir, self)
        self.staging_edit = QLineEdit(str(self._gui_preferences.get("staging_dir", "")), self)
        self.parallel_spin = QSpinBox(self)
        self.parallel_spin.setRange(1, 16)
        self.parallel_spin.setValue(self._settings.parallel_downloads)
        self.min_height_spin = QSpinBox(self)
        self.min_height_spin.setRange(0, 4320)
        self.min_height_spin.setValue(self._settings.min_download_height)
        self.min_height_spin.setToolTip("Videos below this height are rejected; 0 disables the minimum-resolution check.")
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
        self.login_section_label = QLabel("Provider sign-ins", self)
        section_font = self.login_section_label.font()
        section_font.setBold(True)
        self.login_section_label.setFont(section_font)
        self.login_section_label.setAccessibleName("Provider sign-ins")
        self.login_help_label = QLabel(_LOGIN_HELP_TEXT, self)
        self.login_help_label.setWordWrap(True)
        self.login_help_label.setAccessibleName("Provider sign-in help")
        self.login_status_label = QLabel("", self)
        self.login_status_label.setWordWrap(True)
        self.login_status_label.setAccessibleName("Provider sign-in status")
        self.login_rows: dict[str, ProviderSignInRow] = {}
        login_form = QFormLayout()
        for site in self._sign_in_sites():
            row = self._create_login_row(site)
            self.login_rows[site] = row
            login_form.addRow(row.name_label, row.container)

        form = QFormLayout()
        form.addRow("Plex storage (path or SFTP)", self.storage_edit)
        form.addRow("Temporary directory", self.temp_edit)
        form.addRow("Local staging directory", self.staging_edit)
        form.addRow("Parallel downloads", self.parallel_spin)
        form.addRow("Minimum download height (pixels, 0 = no minimum)", self.min_height_spin)
        self.fragments_spin.setToolTip("HLS fragments or parallel HTTP connections with aria2 (maximum 16 HTTP connections).")
        form.addRow("Connections / fragments", self.fragments_spin)
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
        layout.addWidget(self.login_section_label)
        layout.addWidget(self.login_help_label)
        layout.addLayout(login_form)
        layout.addWidget(self.login_status_label)
        layout.addStretch(1)
        self.save_button.clicked.connect(self.save_settings)
        self.diagnostics_button.clicked.connect(self.run_diagnostics)
        self.refresh_login_statuses()
        self._update_sign_in_controls()

    @Slot()
    def save_settings(self) -> None:
        settings = replace(
            self._settings,
            plex_dir=self.storage_edit.text().strip(),
            tmp_dir=self.temp_edit.text().strip(),
            parallel_downloads=self.parallel_spin.value(),
            min_download_height=self.min_height_spin.value(),
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
            save_all = getattr(self.settings_service, "save_all", None)
            if save_all is not None:
                preferences = save_all(settings, preferences) or preferences
            else:
                self.settings_service.save(settings)
                save_preferences = getattr(self.settings_service, "save_gui_preferences", None)
                if save_preferences is None:
                    raise RuntimeError("GUI preference persistence is unavailable")
                preferences = save_preferences(preferences) or preferences
            self._gui_preferences = preferences
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

    # ── Provider sign-ins ────────────────────────────────────────────────

    def _sign_in_sites(self) -> tuple[str, ...]:
        sites = getattr(self.login_service, "SIGN_IN_SITES", None)
        try:
            normalized = tuple(site for site in sites if isinstance(site, str)) if sites else ()
        except TypeError:
            normalized = ()
        return normalized or _DEFAULT_SIGN_IN_SITES

    def _create_login_row(self, site: str) -> ProviderSignInRow:
        name_label = QLabel(site, self)
        name_label.setAccessibleName(f"{site} provider")
        status_label = QLabel("", self)
        status_label.setWordWrap(True)
        status_label.setAccessibleName(f"{site} sign-in status")
        sign_in_button = QPushButton("Sign in", self)
        sign_in_button.setAccessibleName(f"Sign in to {site}")
        sign_in_button.setToolTip(f"Open a Chrome window to sign in to {site}.")
        sign_out_button = QPushButton("Sign out", self)
        sign_out_button.setAccessibleName(f"Sign out of {site}")
        sign_out_button.setToolTip(f"Forget the saved {site} session cookies.")
        controls = QHBoxLayout()
        controls.addWidget(status_label, 1)
        controls.addWidget(sign_in_button)
        controls.addWidget(sign_out_button)
        container = QWidget(self)
        container.setLayout(controls)
        sign_in_button.clicked.connect(lambda _checked=False, site=site: self.sign_in(site))
        sign_out_button.clicked.connect(lambda _checked=False, site=site: self.sign_out(site))
        return ProviderSignInRow(
            site=site,
            name_label=name_label,
            status_label=status_label,
            sign_in_button=sign_in_button,
            sign_out_button=sign_out_button,
            container=container,
        )

    def _login_status_text(self, site: str) -> str:
        service = self.login_service
        try:
            is_signed_in = getattr(service, "is_signed_in", None)
            count_cookies = getattr(service, "cookie_count", None)
            if is_signed_in is not None:
                if not is_signed_in(site):
                    return "Not signed in"
                count = count_cookies(site) if count_cookies is not None else 0
                return f"Signed in ({count} cookies)" if count else "Signed in"
            if count_cookies is not None:
                count = count_cookies(site)
                return f"Signed in ({count} cookies)" if count else "Not signed in"
        except Exception:
            return "Status unavailable"
        return "Status unavailable"

    def refresh_login_statuses(self, *, skip: tuple[str, ...] = ()) -> None:
        """Refresh every provider's signed-in label from the login service."""
        for site, row in self.login_rows.items():
            if site in skip:
                continue
            row.status_label.setText(self._login_status_text(site))

    def sign_in(self, site: str) -> None:
        """Open the provider sign-in browser window off the UI thread."""
        row = self.login_rows.get(site)
        if row is None:
            return
        if self._active_signin_site == site:
            self._cancel_active_sign_in()
            return
        if self._active_signin_site is not None or not self._submissions_enabled:
            return
        cancel = threading.Event()
        self._active_signin_site = site
        self._active_signin_cancel = cancel
        row.status_label.setText(_ACTIVE_SIGN_IN_STATUS)
        self.login_status_label.setText(f"{site}: opening the sign-in window…")
        self._update_sign_in_controls()
        task = Task(self._run_sign_in, site, cancel)
        task.signals.result.connect(lambda result, site=site: self._sign_in_completed(site, result))
        task.signals.error.connect(lambda error, site=site: self._sign_in_failed(site, error))
        task.signals.finished.connect(lambda site=site, task=task: self._sign_in_finished(site, task))
        self._tasks.add(task)
        self._active_signin_task = task
        try:
            self.thread_pool.start(task)
        except Exception as exc:
            task.cancel()
            self._tasks.discard(task)
            self._active_signin_site = None
            self._active_signin_cancel = None
            self._active_signin_task = None
            row.status_label.setText(f"{site}: sign-in could not start: {exc}")
            self.login_status_label.setText(f"{site}: sign-in could not start: {exc}")
            self._update_sign_in_controls()

    def _run_sign_in(self, site: str, cancel: threading.Event):
        sign_in = getattr(self.login_service, "sign_in", None)
        if sign_in is None:
            raise RuntimeError("provider sign-in is unavailable")
        return sign_in(site, cancel=cancel)

    def _cancel_active_sign_in(self) -> None:
        cancel = self._active_signin_cancel
        if cancel is not None:
            cancel.set()
        site = self._active_signin_site
        if site is None:
            return
        row = self.login_rows.get(site)
        if row is not None and row.status_label.text() == _ACTIVE_SIGN_IN_STATUS:
            row.status_label.setText(f"{site}: cancelling sign-in…")
        self.login_status_label.setText(f"{site}: cancelling sign-in…")

    def _sign_in_completed(self, site: str, result) -> None:
        message = str(getattr(result, "message", "") or f"{site}: sign-in finished.")
        row = self.login_rows.get(site)
        if row is not None:
            row.status_label.setText(message)
        self.login_status_label.setText(message)
        self.refresh_login_statuses(skip=(site,))

    def _sign_in_failed(self, site: str, error) -> None:
        exception = error[1] if isinstance(error, tuple) and len(error) > 1 else error
        message = f"{site}: sign-in failed — {exception}"
        row = self.login_rows.get(site)
        if row is not None:
            row.status_label.setText(message)
        self.login_status_label.setText(message)
        self.refresh_login_statuses(skip=(site,))

    def _sign_in_finished(self, site: str, task: Task) -> None:
        self._tasks.discard(task)
        if self._active_signin_task is task:
            self._active_signin_site = None
            self._active_signin_cancel = None
            self._active_signin_task = None
        row = self.login_rows.get(site)
        if row is not None and row.status_label.text() == _ACTIVE_SIGN_IN_STATUS:
            row.status_label.setText(self._login_status_text(site))
        self._update_sign_in_controls()

    def sign_out(self, site: str) -> None:
        """Forget a provider session (a fast local disk operation)."""
        clear = getattr(self.login_service, "clear_cookies", None)
        if clear is None:
            message = f"{site}: sign-out is unavailable."
        else:
            try:
                cleared = clear(site)
                message = (
                    f"Signed out of {site}." if cleared
                    else f"{site}: no stored session."
                )
            except Exception as exc:
                message = f"{site}: sign-out failed ({exc})."
        self.login_status_label.setText(message)
        active_site = self._active_signin_site
        self.refresh_login_statuses(skip=(active_site,) if active_site is not None else ())

    def _update_sign_in_controls(self) -> None:
        active = self._active_signin_site
        for site, row in self.login_rows.items():
            if active == site:
                row.sign_in_button.setText("Cancel sign-in")
                row.sign_in_button.setAccessibleName(f"Cancel sign-in for {site}")
                row.sign_in_button.setEnabled(self._submissions_enabled)
            else:
                row.sign_in_button.setText("Sign in")
                row.sign_in_button.setAccessibleName(f"Sign in to {site}")
                row.sign_in_button.setEnabled(self._submissions_enabled and active is None)
            row.sign_out_button.setEnabled(self._submissions_enabled)

    def invalidate(self) -> None:
        cancel = self._active_signin_cancel
        if cancel is not None:
            cancel.set()
        for task in tuple(self._tasks):
            task.cancel()

    def set_submissions_enabled(self, enabled: bool) -> None:
        self._submissions_enabled = enabled
        self.save_button.setEnabled(enabled)
        self.diagnostics_button.setEnabled(enabled)
        self._update_sign_in_controls()
