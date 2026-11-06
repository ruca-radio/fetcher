from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtMultimedia import QMediaPlayer

from fetcher_core.models import Video
from fetcher_gui.player import MediaPlayerWidget


class FakePlayer(QObject):
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    seekableChanged = Signal(bool)
    playbackStateChanged = Signal(object)
    errorOccurred = Signal(object, str)

    def __init__(self) -> None:
        super().__init__()
        self.positions: list[int] = []
        self.sources: list[QUrl] = []
        self.play_calls = 0
        self.pause_calls = 0
        self.stop_calls = 0

    def setAudioOutput(self, _output: object) -> None:
        pass

    def setVideoOutput(self, _output: object) -> None:
        pass

    def setPosition(self, position: int) -> None:
        self.positions.append(position)

    def setSource(self, source: QUrl) -> None:
        self.sources.append(source)

    def play(self) -> None:
        self.play_calls += 1

    def pause(self) -> None:
        self.pause_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def playbackState(self) -> QMediaPlayer.PlaybackState:
        return QMediaPlayer.PlaybackState.StoppedState


class FakeAudioOutput:
    def __init__(self) -> None:
        self.volumes: list[float] = []
        self.mutes: list[bool] = []
        self._muted = False

    def setVolume(self, volume: float) -> None:
        self.volumes.append(volume)

    def setMuted(self, muted: bool) -> None:
        self._muted = muted
        self.mutes.append(muted)

    def isMuted(self) -> bool:
        return self._muted


def make_widget(qtbot):
    player = FakePlayer()
    audio = FakeAudioOutput()
    widget = MediaPlayerWidget(player=player, audio_output=audio)
    qtbot.addWidget(widget)
    return widget, player, audio


def test_position_updates_do_not_move_slider_while_scrubbing(qtbot) -> None:
    widget, player, _ = make_widget(qtbot)
    player.durationChanged.emit(10_000)
    player.positionChanged.emit(2_000)
    assert widget.position_slider.value() == 2_000

    widget.position_slider.sliderPressed.emit()
    widget.position_slider.setValue(7_000)
    player.positionChanged.emit(3_000)

    assert widget.position_slider.value() == 7_000


def test_releasing_scrubber_seeks_exactly_once(qtbot) -> None:
    widget, player, _ = make_widget(qtbot)
    player.durationChanged.emit(10_000)
    widget.position_slider.sliderPressed.emit()
    widget.position_slider.setValue(7_500)

    widget.position_slider.sliderReleased.emit()

    assert player.positions == [7_500]


def test_unseekable_source_disables_scrubber(qtbot) -> None:
    widget, player, _ = make_widget(qtbot)
    player.seekableChanged.emit(True)
    assert widget.position_slider.isEnabled()

    player.seekableChanged.emit(False)

    assert not widget.position_slider.isEnabled()


def test_player_initially_shows_thumbnail_page(qtbot) -> None:
    widget, _player, _audio = make_widget(qtbot)

    assert widget.display_stack.currentWidget() is widget.thumbnail_label
    assert widget.display_stack.currentWidget() is not widget.video_widget


def test_show_thumbnail_hides_native_video_page(qtbot) -> None:
    widget, player, _audio = make_widget(qtbot)
    video = Video("one", "One", 1, 1, "https://one", "Eporner")
    pixmap = QPixmap(160, 90)
    widget.set_source("https://media.example/sample.m3u8")
    player.playbackState = lambda: QMediaPlayer.PlaybackState.PlayingState

    widget.show_thumbnail(video, pixmap)

    assert widget.display_stack.currentWidget() is widget.thumbnail_label
    assert widget.display_stack.currentWidget() is not widget.video_widget
    assert player.stop_calls == 1
    assert not widget.thumbnail_label.pixmap().isNull()


def test_thumbnail_scales_to_label_after_first_show(qtbot) -> None:
    widget, _player, _audio = make_widget(qtbot)
    pixmap = QPixmap(320, 180)
    pixmap.fill(Qt.GlobalColor.red)
    widget.resize(900, 360)
    widget.show_thumbnail(Video("one", "One", 1, 1, "https://one", "Eporner"), pixmap)

    widget.show()
    qtbot.waitExposed(widget)
    qtbot.waitUntil(lambda: widget.thumbnail_label.size().width() >= 400, timeout=2_000)
    shown = widget.thumbnail_label.pixmap()
    label = widget.thumbnail_label.size()

    assert shown.width() >= 400
    assert shown.height() >= 200
    assert shown.width() <= label.width()
    assert shown.height() <= label.height()


def test_thumbnail_label_rescales_when_player_grows(qtbot) -> None:
    widget, _player, _audio = make_widget(qtbot)
    pixmap = QPixmap(320, 180)
    pixmap.fill(Qt.GlobalColor.red)
    widget.resize(200, 120)
    widget.show_thumbnail(Video("one", "One", 1, 1, "https://one", "Eporner"), pixmap)
    widget.show()
    qtbot.waitExposed(widget)

    widget.resize(900, 360)
    qtbot.waitUntil(lambda: widget.thumbnail_label.size().width() >= 400, timeout=2_000)
    shown = widget.thumbnail_label.pixmap()
    label = widget.thumbnail_label.size()

    assert shown.width() >= 400
    assert shown.height() >= 200
    assert shown.width() <= label.width()
    assert shown.height() <= label.height()
    assert abs(shown.width() / shown.height() - 320 / 180) < 0.15


def test_set_source_switches_to_video_page(qtbot) -> None:
    widget, _player, _audio = make_widget(qtbot)
    widget.show_thumbnail(Video("one", "One", 1, 1, "https://one", "Eporner"), QPixmap(160, 90))

    widget.set_source("https://media.example/sample.m3u8")

    assert widget.display_stack.currentWidget() is widget.video_widget


def test_set_source_uses_local_file_url_and_remote_url(qtbot, tmp_path: Path) -> None:
    widget, player, _ = make_widget(qtbot)
    local = tmp_path / "sample.mp4"
    local.touch()

    widget.set_source(local)
    widget.set_source("https://media.example/sample.m3u8")

    assert player.sources[0].isLocalFile()
    assert player.sources[0].toLocalFile() == str(local)
    assert player.sources[1].toString() == "https://media.example/sample.m3u8"
