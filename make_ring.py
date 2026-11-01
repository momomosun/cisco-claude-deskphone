#!/usr/bin/env python3
"""Writes the sounds the desk phone plays (Play:<file>, fetched over TFTP from tftpboot/):
ClaudeRing.raw while an approval waits on it, a falling two-note chime, twice, just under two
seconds; DoorChime.raw when someone is at the front door, a lower, slower ding-dong, once.
Cisco's rules for ring files: raw, 8000 samples a second, 8-bit mu-law, at most 16080 samples
and a multiple of 240, starting and ending at the zero crossing.
"""
import math
import os

NAME = "ClaudeRing.raw"
RATE = 8000
SAMPLES = 15840  # 1.98 s
NOTES = ((0.00, 1318.5), (0.24, 1046.5), (0.96, 1318.5), (1.20, 1046.5))  # (start s, Hz): E6 C6, twice
DECAY = 0.22  # seconds for a note to fall to 1/e
LEVEL = 0.55
DOOR_NAME = "DoorChime.raw"
DOOR_NOTES = ((0.00, 659.3), (0.50, 523.3))  # E5 C5: ding, dong
DOOR_DECAY = 0.45


def mu_law(sample):
    """G.711 mu-law byte for a 16-bit linear sample."""
    sign = 0x80 if sample < 0 else 0
    magnitude = min(abs(sample), 32635) + 0x84
    exponent = max(0, (magnitude >> 7).bit_length() - 1)
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return ~(sign | exponent << 4 | mantissa) & 0xFF


def tones(notes, decay):
    out = bytearray()
    for n in range(SAMPLES):
        t, value = n / RATE, 0.0
        for start, freq in notes:
            if t >= start:
                age = t - start
                value += min(1.0, age / 0.005) * math.exp(-age / decay) * (
                    math.sin(2 * math.pi * freq * age) + 0.3 * math.sin(4 * math.pi * freq * age))
        fade = min(1.0, n / 40, (SAMPLES - 1 - n) / 400)  # zero at both ends
        out.append(mu_law(int(max(-1.0, min(1.0, value * LEVEL * fade)) * 32767)))
    return bytes(out)


def ring():
    return tones(NOTES, DECAY)


def chime():
    return tones(DOOR_NOTES, DOOR_DECAY)


def main():
    for name, sound in ((NAME, ring), (DOOR_NAME, chime)):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tftpboot", name)
        with open(path, "wb") as f:
            f.write(sound())
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
