#!/usr/bin/env python3
"""Render config templates that contain SIP passwords.

Values come from secrets.env (KEY=value lines, kept out of git). Missing PHONE_*_PASSWORD
keys are generated once and appended, so the first run on a new machine just works.

    templates/freeswitch.xml.tmpl            -> build/freeswitch/freeswitch.xml
    templates/SEP<mac>.cnf.xml.tmpl          -> tftpboot/SEP<mac>.cnf.xml

Every output is rendered and parsed before anything is written, then replaced atomically
with mode 0600, so a failed run never leaves FreeSWITCH or TFTP serving a half-written file.
"""
import os
import re
import secrets
import sys
import tempfile
import xml.dom.minidom
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
OUTPUTS = {
    "freeswitch.xml.tmpl": os.path.join("build", "freeswitch", "freeswitch.xml"),
    "SEP001122334455.cnf.xml.tmpl": os.path.join("tftpboot", "SEP001122334455.cnf.xml"),
    "SEP00AABBCCDDEE.cnf.xml.tmpl": os.path.join("tftpboot", "SEP00AABBCCDDEE.cnf.xml"),
}
XML_ATTR_ENTITIES = {'"': "&quot;", "'": "&apos;"}


def load_secrets(path):
    values = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()
    return values


def ensure_passwords(path, values, needed):
    """Generate any missing *_PASSWORD keys and append them to the secrets file."""
    missing = sorted(k for k in needed if k not in values and k.endswith("_PASSWORD"))
    if missing:
        generated = {k: secrets.token_urlsafe(18) for k in missing}
        needs_newline = os.path.exists(path) and os.path.getsize(path) > 0 and not _ends_with_newline(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            if needs_newline:
                f.write("\n")
            for key, value in generated.items():
                f.write(f"{key}={value}\n")
        values = {**values, **generated}
    if os.path.exists(path):
        os.chmod(path, 0o600)
    return values


def _ends_with_newline(path):
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1) == b"\n"


def render(text, values):
    unknown = sorted(set(PLACEHOLDER.findall(text)) - set(values))
    if unknown:
        raise KeyError(f"no value for {', '.join(unknown)}")
    blank = sorted(k for k in set(PLACEHOLDER.findall(text)) if not values[k])
    if blank:
        # FreeSWITCH accepts an empty directory password as "no authentication needed"
        raise ValueError(f"empty value for {', '.join(blank)}")
    return PLACEHOLDER.sub(lambda m: escape(values[m.group(1)], XML_ATTR_ENTITIES), text)


def _write_private(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".render-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def main(base=HERE):
    templates = {name: open(os.path.join(base, "templates", name)).read() for name in OUTPUTS}
    needed = {key for text in templates.values() for key in PLACEHOLDER.findall(text)}
    secrets_path = os.path.join(base, "secrets.env")
    values = ensure_passwords(secrets_path, load_secrets(secrets_path), needed)

    rendered = {out: render(templates[name], values) for name, out in OUTPUTS.items()}
    for out, text in rendered.items():
        xml.dom.minidom.parseString(text)  # refuse to publish a document that does not parse
    for out, text in rendered.items():
        _write_private(os.path.join(base, out), text)
        print(f"wrote {out}")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError) as exc:
        sys.exit(f"render failed: {exc}")
