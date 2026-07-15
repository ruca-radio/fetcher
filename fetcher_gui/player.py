from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)


def _format_milliseconds(milliseconds: int) -> str:
    seconds = max(0, milliseconds) // 1000
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class MediaPlayerWidget(QWidget):
    """Qt Multimedia video player with controlled seek/scrub behavior."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        player: Any | None = None,
        audio_output: Any | None = None,
        video_widget: QVideoWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.player = player if player is not None else QMediaPlayer(self)
        self.audio_output = audio_output if audio_output is not None else QAudioOutput(self)
        self.video_widget = video_widget if video_widget is not None else QVideoWidget(self)
        self._scrubbing = False

        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)

        self.play_button = QPushButton("Play", self)
        self.mute_button = QPushButton("Mute", self)
        self.full_screen_button = QPushButton("Full screen", self)
        self.position_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.position_slider.setRange(0, 0)
        self.position_slider.setEnabled(False)
        self.elapsed_label = QLabel("0:00", self)
        self.duration_label = QLabel("0:00", self)
        self.error_label = QLabel("", self)
        self.error_label.setWordWrap(True)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)

        seek_row = QHBoxLayout()
        seek_row.addWidget(self.elapsed_label)
        seek_row.addWidget(self.position_slider, 1)
        seek_row.addWidget(self.duration_label)

        controls = QHBoxLayout()
        controls.addWidget(self.play_button)
        controls.addWidget(self.mute_button)
        controls.addWidget(QLabel("Volume", self))
        controls.addWidget(self.volume_slider, 1)
        controls.addWidget(self.full_screen_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.video_widget, 1)
        layout.addLayout(seek_row)
        layout.addLayout(controls)
        layout.addWidget(self.error_label)

        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.seekableChanged.connect(self.position_slider.setEnabled)
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.errorOccurred.connect(self._show_error)
        self.position_slider.sliderPressed.connect(self._start_scrubbing)
        self.position_slider.sliderMoved.connect(self._scrub_position_changed)
        self.position_slider.sliderReleased.connect(self._finish_scrubbing)
        self.play_button.clicked.connect(self.toggle_playback)
        self.mute_button.clicked.connect(self.toggle_muted)
        self.volume_slider.valueChanged.connect(self._volume_changed)
        self.full_screen_button.clicked.connect(self.toggle_full_screen)
        self._volume_changed(self.volume_slider.value())

    def set_source(self, source: str | Path | QUrl) -> None:
        self.error_label.clear()
        if isinstance(source, QUrl):
            url = source
        elif isinstance(source, Path):
            url = QUrl.fromLocalFile(str(source.expanduser().resolve()))
        elif source.startswith(("http://", "https://")):
            url = QUrl(source)
        else:
            url = QUrl.fromLocalFile(str(Path(source).expanduser().resolve()))
        self.player.setSource(url)

    @Slot()
    def toggle_playback(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    @Slot()
    def toggle_muted(self) -> None:
        muted = not self.audio_output.isMuted()
        self.audio_output.setMuted(muted)
        self.mute_button.setText("Unmute" if muted else "Mute")

    @Slot()
    def toggle_full_screen(self) -> None:
        self.video_widget.setFullScreen(not self.video_widget.isFullScreen())

    @Slot(int)
    def _position_changed(self, position: int) -> None:
        if not self._scrubbing:
            self.position_slider.setValue(position)
            self.elapsed_label.setText(_format_milliseconds(position))

    @Slot(int)
    def _duration_changed(self, duration: int) -> None:
        self.position_slider.setRange(0, max(0, duration))
        self.duration_label.setText(_format_milliseconds(duration))

    @Slot()
    def _start_scrubbing(self) -> None:
        self._scrubbing = True

    @Slot(int)
    def _scrub_position_changed(self, position: int) -> None:
        self.elapsed_label.setText(_format_milliseconds(position))

    @Slot()
    def _finish_scrubbing(self) -> None:
        if not self._scrubbing:
            return
        self._scrubbing = False
        position = self.position_slider.value()
        self.player.setPosition(position)
        self.elapsed_label.setText(_format_milliseconds(position))

    @Slot(int)
    def _volume_changed(self, value: int) -> None:
        self.audio_output.setVolume(value / 100.0)

    @Slot(object)
    def _playback_state_changed(self, state: object) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setText("Pause" if playing else "Play")

    @Slot(object, str)
    def _show_error(self, _error: object, message: str) -> None:
        self.error_label.setText(message or "Unable to play this media.")
