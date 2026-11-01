"""Render text screens for the Cisco 7970 as PNG.

The phone's built-in font has no CJK glyphs, so anything Chinese is drawn here and shown
with CiscoIPPhoneImageFile. 298x168 is the 7970/7971 image area that leaves the title
bar and softkeys visible.
"""
import io
import os

from PIL import Image, ImageDraw, ImageFont, ImageOps

WIDTH, HEIGHT = 298, 168
WIDE_WIDTH = 320  # a WindowMode="Wide" page also covers the column of line keys on the right
KEY_Y = {1: 13, 2: 30, 3: 51}  # picture rows beside line keys 1-3 (the line, PgUp, PgDn), measured on the 7970
SYMBOL_FONT = "/System/Library/Fonts/Apple Symbols.ttf"  # has the telephone sign the CJK font lacks
ICON_SIZE = 18
# Claude's spark: the desktop app's tray icon (Claude.app/Contents/Resources/TrayIconTemplate@3x.png)
# in Claude orange #D97757, cropped to the spark
ICON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_icon.png")
MARGIN = 6
# Picked on the real phone (2026-10-27, option "B"): medium-weight STHeiti, anti-aliased,
# white on pure black. Thin weights and grey backgrounds smear on the 7970's 12-bit panel.
BG = (0, 0, 0)
FG = (255, 255, 255)
DIM = (200, 200, 200)
ACCENT = (255, 210, 80)
FONT_PATHS = (
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)


def _font(size):
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def wrap(text, font, width):
    """Wrap text to pixel width, character by character (works for CJK and ASCII alike)."""
    lines = []
    for paragraph in str(text).replace("\r", "").split("\n"):
        line = ""
        for ch in paragraph:
            if font.getlength(line + ch) > width and line:
                lines.append(line)
                line = ch.lstrip() if ch == " " else ch
            else:
                line += ch
        lines.append(line)
    return lines


TITLE_SIZE, BODY_SIZE, FOOT_SIZE = 16, 15, 13
LINE_H = 19
BODY_TOP = MARGIN + 21 + 4


def body_capacity(footer=""):
    """How many body lines fit on screen."""
    bottom = HEIGHT - MARGIN - (17 if footer else 0)
    return max(1, (bottom - BODY_TOP) // LINE_H)


def fits(body, footer=""):
    """True if the whole body shows without being cut (used before asking for approval)."""
    return len(wrap(body, _font(BODY_SIZE), WIDTH - 2 * MARGIN)) <= body_capacity(footer)


def draw_title(img, draw, title, font, fill):
    """Claude's icon, then the title cut to the width that is left. (The phone keeps its own
    globe in the status bar above: no service can change that.)"""
    icon = Image.open(io.BytesIO(claude_icon(ICON_SIZE)))
    img.paste(icon, (MARGIN, MARGIN + 1), icon)
    x = MARGIN + ICON_SIZE + 4
    draw.text((x, MARGIN), truncate(title, font, WIDTH - MARGIN - x), font=font, fill=fill)


def render_screen(title, body, footer="", accent=False, arrows=False):
    """Title line, wrapped body (cut to fit with an ellipsis) and an optional footer line;
    arrows as for render_list."""
    img = Image.new("RGB", (WIDE_WIDTH if arrows else WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    title_font, body_font, foot_font = _font(TITLE_SIZE), _font(BODY_SIZE), _font(FOOT_SIZE)
    inner = WIDTH - 2 * MARGIN

    y = MARGIN
    draw_title(img, draw, title, title_font, ACCENT if accent else FG)
    draw.line((MARGIN, y + 21, WIDTH - MARGIN, y + 21), fill=DIM)

    y = BODY_TOP
    max_lines = body_capacity(footer)
    lines = wrap(body, body_font, inner)
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [lines[max_lines - 1][:-1] + "…"]
    for line in lines:
        draw.text((MARGIN, y), line, font=body_font, fill=FG)
        y += LINE_H

    if footer:
        draw.text((MARGIN, HEIGHT - MARGIN - 15), wrap(footer, foot_font, inner)[0], font=foot_font, fill=DIM)
    if arrows:
        draw_line_keys(draw)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def conversation_lines(turns):
    """The turns as screen lines, "你：" or "Claude：" before each, and the index of the first
    line of the last thing the user said (0 when nothing was)."""
    font, inner = _font(BODY_SIZE), WIDTH - 2 * MARGIN
    lines, last_question = [], 0
    for turn in turns:
        if turn.get("role") == "user":
            last_question = len(lines)
        lines.extend(wrap(("你：" if turn.get("role") == "user" else "Claude：") + str(turn.get("text") or ""),
                          font, inner))
    return lines, last_question


def choice_lines(question, options, chosen=(), several=False):
    """A question, two lines at most (cut with "…"), then a numbered line per option, "□" or "■"
    in front when several may be chosen. Returns (lines, index of the first option's line)."""
    font, inner = _font(BODY_SIZE), WIDTH - 2 * MARGIN
    asked = wrap(question, font, inner)
    if len(asked) > 2:
        asked = [asked[0], truncate("".join(asked[1:]), font, inner)]
    marks = [("■ " if i in chosen else "□ ") if several else "" for i in range(len(options))]
    return asked + [truncate(f"{i + 1}. {marks[i]}{label}", font, inner) for i, label in enumerate(options)], len(asked)


def render_lines(title, lines, start, footer="", arrows=False):
    """Title, then as many lines from start on as fit, and the footer; arrows as for render_list."""
    img = Image.new("RGB", (WIDE_WIDTH if arrows else WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    title_font, body_font, foot_font = _font(TITLE_SIZE), _font(BODY_SIZE), _font(FOOT_SIZE)
    draw_title(img, draw, title, title_font, FG)
    draw.line((MARGIN, MARGIN + 21, WIDTH - MARGIN, MARGIN + 21), fill=DIM)
    for k, line in enumerate(lines[start:start + body_capacity(footer)]):
        draw.text((MARGIN, BODY_TOP + k * LINE_H), line, font=body_font, fill=FG)
    if footer:
        draw.text((MARGIN, HEIGHT - MARGIN - 15), truncate(footer, foot_font, WIDTH - 2 * MARGIN),
                  font=foot_font, fill=DIM)
    if arrows:
        draw_line_keys(draw)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


HILITE = (40, 80, 160)
LINE_BLUE = (120, 190, 255)


def truncate(text, font, width):
    """One line of at most width pixels, with an ellipsis when cut."""
    text = str(text).replace("\n", " ")
    if font.getlength(text) <= width:
        return text
    while text and font.getlength(text + "…") > width:
        text = text[:-1]
    return text + "…"


def list_window(count, cursor, footer=""):
    """(first row shown, rows that fit) for a list of count rows that keeps the cursor in view.
    Row k of the window is drawn from y = BODY_TOP + k * LINE_H - 1, LINE_H high."""
    capacity = body_capacity(footer)
    return min(max(0, cursor - capacity // 2), max(0, count - capacity)), capacity


def render_list(title, rows, cursor, footer="", arrows=False):
    """Title line and one line per row, the cursor row highlighted; scrolls to keep it in view.
    With arrows the picture is wide, covering the column where the phone draws the line keys' icons,
    and draws its own there (draw_line_keys)."""
    img = Image.new("RGB", (WIDE_WIDTH if arrows else WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    title_font, body_font, foot_font = _font(TITLE_SIZE), _font(BODY_SIZE), _font(FOOT_SIZE)
    inner = WIDTH - 2 * MARGIN
    draw_title(img, draw, title, title_font, FG)
    draw.line((MARGIN, MARGIN + 21, WIDTH - MARGIN, MARGIN + 21), fill=DIM)
    first, capacity = list_window(len(rows), cursor, footer)
    y = BODY_TOP
    for i in range(first, min(len(rows), first + capacity)):
        if i == cursor:
            draw.rectangle((MARGIN - 3, y - 1, WIDTH - MARGIN + 3, y + LINE_H - 2), fill=HILITE)
        draw.text((MARGIN, y), truncate(rows[i], body_font, inner), font=body_font, fill=FG)
        y += LINE_H
    if footer:
        draw.text((MARGIN, HEIGHT - MARGIN - 15), truncate(footer, foot_font, inner), font=foot_font, fill=DIM)
    if arrows:
        draw_line_keys(draw)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_photo(jpeg, caption, arrows=False):
    """A camera picture filling the image area (cropped to its shape) under a caption on a dark band,
    in 256 colours: the 7970 shows 12-bit colour, and a full-colour photo PNG is needlessly big for
    it. arrows as for render_list."""
    photo = ImageOps.fit(Image.open(io.BytesIO(jpeg)).convert("RGB"), (WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    img = Image.new("RGB", (WIDE_WIDTH if arrows else WIDTH, HEIGHT), BG)
    img.paste(photo, (0, 0))
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rectangle((0, 0, WIDTH - 1, 24), fill=(0, 0, 0, 150))
    font = _font(TITLE_SIZE)
    draw.text((MARGIN, 3), truncate(caption, font, WIDTH - 2 * MARGIN), font=font, fill=FG)
    if arrows:
        draw_line_keys(draw)
    buf = io.BytesIO()
    img.quantize(colors=256).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def draw_line_keys(draw):
    """On a wide picture, the icons it covers: a telephone beside key 1 (the line) and up/down
    arrows beside keys 2 and 3 (the PgUp/PgDn keys), where the phone would show a globe each."""
    x = WIDE_WIDTH - 11
    for key in (2, 3):
        y = KEY_Y[key]
        tip, base = (y - 5, y + 4) if key == 2 else (y + 5, y - 4)
        draw.polygon([(x - 6, base), (x + 6, base), (x, tip)], fill=FG)
    try:
        draw.text((x, KEY_Y[1]), "☎", font=ImageFont.truetype(SYMBOL_FONT, 18), fill=LINE_BLUE, anchor="mm")
    except OSError:
        pass  # no symbol font: the arrows still say what keys 2 and 3 do


def claude_icon(size=18):
    """The orange asterisk, for title bars."""
    buf = io.BytesIO()
    Image.open(ICON_FILE).convert("RGBA").resize((size, size), Image.LANCZOS).save(buf, format="PNG")
    return buf.getvalue()
