"""Speech for voice_agent: phone recordings to text with Whisper (MLX, model stored locally),
and text to phone audio with macOS `say`."""
import math
import re
import subprocess
import threading
import wave

import numpy as np

WHISPER_RATE = 16000
FRAME = WHISPER_RATE // 50  # 20 ms
VOICE_LEVEL = 0.01          # mean |sample| of a 20 ms frame that counts as someone speaking
MIN_VOICE_SECONDS = 0.3     # less than this is a cough or a click, not worth transcribing
NO_SPEECH = 0.6             # Whisper's own no_speech_prob above which a segment is dropped
PROMPT = "以下是普通话的句子。"  # nudges Whisper to simplified Chinese with punctuation
# Lines Whisper invents on silence or noise (it learned them from subtitled video).
INVENTED = [re.compile(p, re.I) for p in (
    r"[(（]?字幕.*", r".*Amara\.org.*", r".*点赞.*订阅.*", r".*明镜.*栏目.*",
    r".*YoYo Television.*", r"(谢谢|感谢)(观看|收看|收听)[。.!！]?")]


def load_wav(path):
    """A 16-bit WAV as mono float32 samples at Whisper's 16 kHz."""
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit samples")
        rate, channels = w.getframerate(), w.getnchannels()
        frames = w.readframes(w.getnframes())
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != WHISPER_RATE:
        from scipy.signal import resample_poly
        g = math.gcd(rate, WHISPER_RATE)
        audio = resample_poly(audio, WHISPER_RATE // g, rate // g).astype(np.float32)
    return audio


def voiced_seconds(audio):
    frames = len(audio) // FRAME
    if not frames:
        return 0.0
    level = np.abs(audio[: frames * FRAME]).reshape(frames, FRAME).mean(axis=1)
    return float((level >= VOICE_LEVEL).sum()) * FRAME / WHISPER_RATE


def clean_transcript(segments):
    text = "".join(s["text"] for s in segments if s.get("no_speech_prob", 0) < NO_SPEECH).strip()
    return "" if any(p.fullmatch(text) for p in INVENTED) else text


class Transcriber:
    """Callable: WAV path -> what was said ("" for silence). Keeps the model loaded."""

    def __init__(self, model_dir):
        import mlx_whisper  # only the voice agent needs MLX; tests and the hub do not
        self.whisper = mlx_whisper
        self.model_dir = model_dir
        self.lock = threading.Lock()

    def _run(self, audio):
        with self.lock:  # one transcription at a time on the GPU
            return self.whisper.transcribe(audio, path_or_hf_repo=self.model_dir, language="zh",
                                           initial_prompt=PROMPT, condition_on_previous_text=False,
                                           verbose=None)

    def warm_up(self):
        """Load the model now, so the first caller does not wait for it."""
        self._run(np.zeros(WHISPER_RATE, np.float32))

    def __call__(self, path):
        audio = load_wav(path)
        if voiced_seconds(audio) < MIN_VOICE_SECONDS:
            return ""
        return clean_transcript(self._run(audio).get("segments", []))


def synthesize(text, path, voice):
    """Speak text into a WAV the phone line plays as is: 8 kHz, 16-bit, mono. say obeys embedded
    [[...]] commands ([[slnc 3000]], [[volm 0]]), which could silence part of a command read out
    for approval, so the brackets never reach it."""
    text = text.replace("[[", " ").replace("]]", " ")
    subprocess.run(["/usr/bin/say", "-v", voice, "--file-format=WAVE", "--data-format=LEI16@8000",
                    "-o", path, "-f", "-"], input=text.encode("utf-8"), check=True, timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return path
