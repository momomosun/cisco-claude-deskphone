"""The approval ring and the door chime meet Cisco's rules for Play:/ring files (Development Notes, "Play")."""
import os

import pytest

import make_ring

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.parametrize("name, sound", [(make_ring.NAME, make_ring.ring), (make_ring.DOOR_NAME, make_ring.chime)])
def test_the_ring_files_are_what_the_phone_can_play(name, sound):
    with open(os.path.join(HERE, "tftpboot", name), "rb") as f:
        data = f.read()
    assert data == sound()  # the committed file is what make_ring.py writes
    assert 240 <= len(data) <= 16080 and len(data) % 240 == 0  # 8 kHz, one byte a sample
    assert data[0] in (0xFF, 0x7F) and data[-1] in (0xFF, 0x7F)  # starts and ends at the zero crossing
    assert len(set(data)) > 50  # a sound, not silence


def test_mu_law_matches_g711():
    assert make_ring.mu_law(0) == 0xFF and make_ring.mu_law(-1) == 0x7F
    assert make_ring.mu_law(32767) == 0x80 and make_ring.mu_law(-32768) == 0x00
    assert make_ring.mu_law(1000) == 0xCE and make_ring.mu_law(-1000) == 0x4E
