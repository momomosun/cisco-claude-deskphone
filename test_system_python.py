"""Several parts run under macOS's own /usr/bin/python3 (3.9): phone-hub and the TFTP server on the
Mini, the desk relay and the Claude Code hook on the Studio. The tests run on a newer Python, so
check that 3.9 can at least compile them: an f-string with a backslash once kept phone-hub from
starting on the Mini while every test here passed."""
import os
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SYSTEM_PYTHON = "/usr/bin/python3"
RUN_BY_SYSTEM_PYTHON = ["phone_hub.py", "phone_render.py", "desk_gateway.py", "ha_door.py", "hubauth.py", "render.py",
                        "tftpd.py", "desk_relay.py", "phone_screen.py", "make_ring.py", "cell_bridge.py",
                        os.path.join("claude_hooks", "phone_hook.py")]


@pytest.mark.skipif(not os.path.exists(SYSTEM_PYTHON), reason="no system python here")
def test_what_the_system_python_runs_compiles_there():
    check = "import sys\nfor f in sys.argv[1:]:\n    compile(open(f, encoding='utf-8').read(), f, 'exec')"
    out = subprocess.run([SYSTEM_PYTHON, "-c", check, *RUN_BY_SYSTEM_PYTHON], cwd=HERE, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
