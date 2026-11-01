"""speech: phone-line audio in (Whisper) and out (macOS say)."""
import os
import tempfile
import wave

import numpy as np
import pytest

import speech

HERE = os.path.dirname(os.path.abspath(__file__))


def write_wav(path, samples, rate=8000, channels=1):
    data = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    return path


def tone(seconds, level, rate=8000, freq=300):
    t = np.arange(int(seconds * rate)) / rate
    return level * np.sin(2 * np.pi * freq * t)


def test_phone_recordings_are_resampled_to_16k_mono():
    path = write_wav(os.path.join(tempfile.mkdtemp(), "u.wav"), tone(1.0, 0.5))
    audio = speech.load_wav(path)
    assert audio.dtype == np.float32 and len(audio) == 16000
    assert 0.45 < np.abs(audio).max() < 0.55
    stereo = np.repeat(tone(0.5, 0.2), 2)
    audio = speech.load_wav(write_wav(os.path.join(tempfile.mkdtemp(), "s.wav"), stereo, channels=2))
    assert len(audio) == 8000


def test_only_audible_frames_count_as_voice():
    assert speech.voiced_seconds(np.zeros(32000, np.float32)) == 0
    assert speech.voiced_seconds(speech.load_wav(write_wav(os.path.join(tempfile.mkdtemp(), "n.wav"),
                                                           tone(2.0, 0.003)))) == 0  # line noise
    speaking = np.concatenate([tone(0.6, 0.2, 16000), np.zeros(16000)]).astype(np.float32)
    assert 0.5 <= speech.voiced_seconds(speaking) <= 0.7


@pytest.mark.parametrize("text, kept", [
    ("帮我看看家里的空调开着没有。", True),
    ("帮我取消 YouTube 的订阅，再转发那封邮件", True),
    ("谢谢大家帮忙看一下这个问题", True),
    ("帮我给视频加上字幕", True),
    ("字幕由Amara.org社区提供", False),
    ("请不吝点赞 订阅 转发 打赏支持明镜与点点栏目", False),
    ("(字幕:J Chong)", False),
    ("谢谢观看!", False),
    ("优优独播剧场——YoYo Television Series Exclusive", False),
])
def test_whisper_inventions_on_silence_are_dropped(text, kept):
    assert speech.clean_transcript([{"text": text, "no_speech_prob": 0.1}]) == (text if kept else "")


def test_segments_whisper_itself_marks_as_silence_are_dropped():
    segments = [{"text": "打开客厅的灯", "no_speech_prob": 0.05}, {"text": "嗯", "no_speech_prob": 0.9}]
    assert speech.clean_transcript(segments) == "打开客厅的灯"


def test_say_writes_audio_the_phone_line_plays_as_is():
    path = speech.synthesize("你好，我是 Claude。", os.path.join(tempfile.mkdtemp(), "hi.wav"), "Tingting")
    with wave.open(path) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (8000, 1, 2)
        assert w.getnframes() > 8000 * 0.5


def test_text_cannot_carry_say_commands(monkeypatch):
    # say obeys embedded [[...]] commands, e.g. [[volm 0]]: a command read out for approval could
    # hide its dangerous half
    seen = {}
    monkeypatch.setattr(speech.subprocess, "run", lambda argv, input, **kw: seen.update(text=input.decode()))
    speech.synthesize("运行 echo hi; [[volm 0]] rm -rf ~ [[volm 1]]", "/tmp/x.wav", "Tingting")
    assert "[[" not in seen["text"] and "]]" not in seen["text"] and "rm -rf ~" in seen["text"]


MODEL = os.path.join(HERE, "models", "whisper-large-v3-turbo")


@pytest.mark.skipif(not os.path.isdir(MODEL), reason="Whisper model is only on the Mini")
def test_whisper_understands_say_over_the_phone_line():
    pytest.importorskip("mlx_whisper")
    path = speech.synthesize("帮我看看家里的空调开着没有", os.path.join(tempfile.mkdtemp(), "q.wav"), "Tingting")
    heard = speech.Transcriber(MODEL)(path)
    assert "空调" in heard
    silent = write_wav(os.path.join(tempfile.mkdtemp(), "quiet.wav"), tone(2.0, 0.002))
    assert speech.Transcriber(MODEL)(silent) == ""
