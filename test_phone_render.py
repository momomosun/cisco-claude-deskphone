"""phone_render: the pictures and icon the 7970 shows."""
import io

from PIL import Image

import phone_render


def orange(pixel):
    r, g, b = pixel[:3]
    return r > 180 and 80 < g < 150 and b < 110


def test_the_claude_icon_is_the_orange_spark_at_title_size():
    img = Image.open(io.BytesIO(phone_render.claude_icon()))
    assert img.size == (18, 18) and img.mode == "RGBA"
    assert img.getpixel((0, 0))[3] == 0  # between the rays the background shows through
    assert orange(img.getpixel((9, 9))) and img.getpixel((9, 9))[3] > 240


def test_the_list_window_keeps_the_cursor_in_view():
    for count in (1, 5, 30):
        for cursor in range(count):
            first, capacity = phone_render.list_window(count, cursor, "footer")
            assert first <= cursor < first + capacity and 0 <= first and first + min(capacity, count) <= count


def test_picture_pages_lead_their_title_with_the_claude_icon():
    for png in (phone_render.render_screen("Claude Code | 在座位", "还没有会话活动。", "待批准 0"),
                phone_render.render_list("桌面会话", ["电话", "demo app"], 0, "footer")):
        img = Image.open(io.BytesIO(png)).convert("RGB")
        assert orange(img.getpixel((phone_render.MARGIN + 9, phone_render.MARGIN + 1 + 9)))


def test_a_conversation_becomes_lines_that_fit_and_marks_the_last_question():
    turns = [{"role": "user", "text": "第一问"}, {"role": "assistant", "text": "答" * 200},
             {"role": "user", "text": "第二问"}, {"role": "assistant", "text": "好。"}]
    lines, last_question = phone_render.conversation_lines(turns)
    assert lines[0] == "你：第一问" and lines[last_question] == "你：第二问" and lines[-1] == "Claude：好。"
    font = phone_render._font(phone_render.BODY_SIZE)
    assert all(font.getlength(line) <= phone_render.WIDTH - 2 * phone_render.MARGIN for line in lines)
    assert phone_render.conversation_lines([]) == ([], 0)
    png = phone_render.render_lines("电话", lines, 3, "1/4 页")
    assert Image.open(io.BytesIO(png)).size == (phone_render.WIDTH, phone_render.HEIGHT)



def test_a_question_shows_two_lines_at_most_then_numbered_options():
    long = "这是一个很长的问题，" * 10
    lines, first = phone_render.choice_lines(long, ["方案甲", "方案乙", "方案丙"])
    assert first == 2 and lines[1].endswith("…") and lines[2:] == ["1. 方案甲", "2. 方案乙", "3. 方案丙"]
    lines, first = phone_render.choice_lines("选哪些？", ["甲", "乙"], chosen={1}, several=True)
    assert lines == ["选哪些？", "1. □ 甲", "2. ■ 乙"] and first == 1
    assert first + 4 <= phone_render.body_capacity("footer")  # four options always fit under the question


def assert_line_keys(png):
    """A wide picture, drawing the line keys' icons itself where it covers the phone's."""
    wide = Image.open(io.BytesIO(png)).convert("RGB")
    assert wide.size == (phone_render.WIDE_WIDTH, phone_render.HEIGHT)
    for key in (2, 3):  # where the phone draws a globe beside line keys 2 and 3
        assert min(wide.getpixel((phone_render.WIDE_WIDTH - 11, phone_render.KEY_Y[key]))) > 180  # our arrow
    y, x = phone_render.KEY_Y[1], phone_render.WIDE_WIDTH - 11  # and the line's telephone beside key 1
    assert any(b > 200 and r < 180 for r, g, b in (wide.getpixel((x + dx, y + dy))
                                                for dx in range(-7, 8) for dy in range(-5, 6)))
    assert max(wide.getpixel((phone_render.WIDE_WIDTH - 11, 100))) < 40  # and nothing below them


def test_the_wide_list_draws_arrows_beside_line_keys_2_and_3():
    plain = Image.open(io.BytesIO(phone_render.render_list("桌面会话", ["电话"], 0, "footer")))
    assert plain.size == (phone_render.WIDTH, phone_render.HEIGHT)
    assert_line_keys(phone_render.render_list("桌面会话", ["电话"], 0, "footer", arrows=True))


def test_the_other_pictures_cover_the_line_keys_the_same_way():
    assert_line_keys(phone_render.render_screen("Claude Code | 离开", "还没有会话活动。", "待批准 0", arrows=True))
    assert_line_keys(phone_render.render_lines("电话", ["你：在吗"] * 20, 3, "4-9/20 行", arrows=True))


def test_a_camera_picture_fills_the_screen_under_its_caption():
    buf = io.BytesIO()
    Image.new("RGB", (640, 360), (0, 0, 220)).save(buf, format="JPEG")
    png = phone_render.render_photo(buf.getvalue(), "门铃 · 23:41", arrows=True)
    assert_line_keys(png)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    r, g, b = img.getpixel((150, 120))
    assert b > 150 and r < 80  # the picture
    band = [img.getpixel((x, 12)) for x in range(phone_render.MARGIN, 120)]
    assert any(min(p) > 200 for p in band)  # white caption text on the band
    noise = io.BytesIO()
    Image.effect_noise((640, 360), 80).convert("RGB").save(noise, format="JPEG")
    assert len(phone_render.render_photo(noise.getvalue(), "门口", arrows=True)) < 64 * 1024  # small for the phone
