import os
import shutil
import stat
import tempfile
import xml.dom.minidom

import pytest

import render

HERE = os.path.dirname(os.path.abspath(__file__))
PBX = os.path.join("build", "freeswitch", "freeswitch.xml")
PHONE = os.path.join("tftpboot", "SEP001122334455.cnf.xml")


@pytest.fixture
def base():
    root = tempfile.mkdtemp()
    shutil.copytree(os.path.join(HERE, "templates"), os.path.join(root, "templates"))
    yield root
    shutil.rmtree(root)


def read(base, rel):
    with open(os.path.join(base, rel)) as f:
        return f.read()


def write_secrets(base, text):
    with open(os.path.join(base, "secrets.env"), "w") as f:
        f.write(text)


def test_generates_passwords_once_and_fills_every_placeholder(base):
    render.main(base)
    first = read(base, "secrets.env")
    assert "PHONE_101_PASSWORD=" in first and "PHONE_102_PASSWORD=" in first

    render.main(base)
    assert read(base, "secrets.env") == first  # no regeneration on second run

    pw = render.load_secrets(os.path.join(base, "secrets.env"))["PHONE_101_PASSWORD"]
    pbx, phone = read(base, PBX), read(base, PHONE)
    assert f"<param name=\"password\" value=\"{pw}\"/>" in pbx
    assert f"<authPassword>{pw}</authPassword>" in phone
    assert "{{" not in pbx and "{{" not in phone


def test_existing_secret_is_used_not_replaced(base):
    write_secrets(base, "# test\nPHONE_101_PASSWORD=keepme\n")
    render.main(base)
    assert "<authPassword>keepme</authPassword>" in read(base, PHONE)


def test_blank_password_is_rejected_and_outputs_untouched(base):
    # sofia's allow-empty-password defaults to true: an empty password would skip authentication
    render.main(base)
    before = read(base, PBX)
    write_secrets(base, read(base, "secrets.env").replace(
        render.load_secrets(os.path.join(base, "secrets.env"))["PHONE_101_PASSWORD"], "", 1))
    with pytest.raises(ValueError):
        render.main(base)
    assert read(base, PBX) == before


def test_appending_to_secrets_without_trailing_newline_keeps_lines_apart(base):
    write_secrets(base, "PHONE_101_PASSWORD=abc")  # no final newline
    render.main(base)
    values = render.load_secrets(os.path.join(base, "secrets.env"))
    assert values["PHONE_101_PASSWORD"] == "abc"
    assert "PHONE_102_PASSWORD" in values and "PHONE_200_PASSWORD" in values


def test_xml_special_characters_in_a_password_are_escaped(base):
    pw = 'a&b<c>"d\''
    write_secrets(base, f"PHONE_101_PASSWORD={pw}\n")
    render.main(base)
    phone = xml.dom.minidom.parseString(read(base, PHONE))
    assert phone.getElementsByTagName("authPassword")[0].firstChild.data == pw
    pbx = xml.dom.minidom.parseString(read(base, PBX))
    users = {u.getAttribute("id"): u for u in pbx.getElementsByTagName("user")}
    params = {p.getAttribute("name"): p.getAttribute("value")
              for p in users["101"].getElementsByTagName("param")}
    assert params["password"] == pw


def test_failed_render_leaves_previous_outputs_in_place(base):
    render.main(base)
    before = read(base, PBX), read(base, PHONE)
    with open(os.path.join(base, "templates", "SEP001122334455.cnf.xml.tmpl"), "a") as f:
        f.write("<!-- {{NOT_A_KNOWN_VALUE}} -->\n")
    with pytest.raises(KeyError):
        render.main(base)
    assert (read(base, PBX), read(base, PHONE)) == before


def test_secrets_and_outputs_are_private(base):
    render.main(base)
    for rel in ("secrets.env", PBX, PHONE):
        assert stat.S_IMODE(os.stat(os.path.join(base, rel)).st_mode) == 0o600, rel
    os.chmod(os.path.join(base, "secrets.env"), 0o644)
    render.main(base)
    assert stat.S_IMODE(os.stat(os.path.join(base, "secrets.env")).st_mode) == 0o600


def test_no_profile_lets_lan_devices_skip_authentication(base):
    # sofia's apply-register-acl / apply-inbound-acl let matching IPs register or call WITHOUT a
    # password (REG_AUTO_REGISTER); they were once added here by mistake as a "LAN only" filter.
    render.main(base)
    pbx = read(base, PBX)
    assert 'name="apply-register-acl"' not in pbx
    assert 'name="apply-inbound-acl"' not in pbx
    assert 'name="auth-calls" value="true"' in pbx


def test_every_user_refuses_empty_passwords(base):
    render.main(base)
    pbx = xml.dom.minidom.parseString(read(base, PBX))
    for user in pbx.getElementsByTagName("user"):
        params = {p.getAttribute("name"): p.getAttribute("value")
                  for p in user.getElementsByTagName("param")}
        assert params.get("allow-empty-password") == "false", user.getAttribute("id")


def test_300_hands_the_call_to_the_voice_agent_on_this_machine(base):
    render.main(base)
    pbx = xml.dom.minidom.parseString(read(base, PBX))
    claude = [e for e in pbx.getElementsByTagName("extension") if e.getAttribute("name") == "claude"]
    conds = [(c.getAttribute("field"), c.getAttribute("expression"))
             for c in claude[0].getElementsByTagName("condition")]
    assert conds == [("destination_number", "^300$"), ("${claude_caller}", "^true$")]
    actions = [(a.getAttribute("application"), a.getAttribute("data"))
               for a in claude[0].getElementsByTagName("action")]
    assert actions == [("socket", "127.0.0.1:8085 async full")]


def test_only_the_desk_phone_may_call_claude(base):
    # claude_caller comes from the directory entry the caller authenticated as, so 200 (Home
    # Assistant) or 102 cannot reach 300, however they set their caller ID
    render.main(base)
    pbx = xml.dom.minidom.parseString(read(base, PBX))
    marks = [v for v in pbx.getElementsByTagName("variable") if v.getAttribute("name") == "claude_caller"]
    owners = [v.parentNode.parentNode for v in marks]
    assert [(o.tagName, o.getAttribute("id"), v.getAttribute("value")) for o, v in zip(owners, marks)] == \
        [("user", "101", "true")]


def test_phone_dials_3xx_without_waiting():
    doc = xml.dom.minidom.parse(os.path.join(HERE, "tftpboot", "dialplan.xml"))
    templates = {t.getAttribute("MATCH"): t.getAttribute("Timeout") for t in doc.getElementsByTagName("TEMPLATE")}
    assert templates.get("3..") == "0"


def test_bridges_do_not_follow_redirects(base):
    render.main(base)
    pbx = xml.dom.minidom.parseString(read(base, PBX))
    for ext in pbx.getElementsByTagName("extension"):
        actions = [(a.getAttribute("application"), a.getAttribute("data"))
                   for a in ext.getElementsByTagName("action")]
        apps = [a for a, _ in actions]
        for app, data in actions:
            if app == "bridge":
                # must be a B-leg variable: a `set` before the bridge only reaches the A-leg
                assert data.startswith("{outbound_redirect_fatal=true}"), ext.getAttribute("name")


def test_unknown_non_password_placeholder_fails():
    with pytest.raises(KeyError):
        render.render("x={{SOMETHING_ELSE}}", {})


def test_two_line_keys_page_the_desk_session_list(base):
    render.main(base)
    phone = xml.dom.minidom.parseString(read(base, PHONE))
    keys = {line.getAttribute("button"): line for line in phone.getElementsByTagName("line")}
    text = lambda line, tag: line.getElementsByTagName(tag)[0].firstChild.data  # noqa: E731
    assert text(keys["1"], "featureID") == "9"  # the line itself stays on the top key
    for button, label, path in (("2", "PgUp", "pageup"), ("3", "PgDn", "pagedown")):
        assert text(keys[button], "featureID") == "20"  # Service URI
        assert text(keys[button], "featureLabel") == label
        assert text(keys[button], "serviceURI") == f"http://192.168.1.10:8090/phone/desk/{path}"


def test_sams_phone_is_102_with_its_own_password_and_no_claude(base):
    render.main(base)
    secrets = render.load_secrets(os.path.join(base, "secrets.env"))
    text = read(base, os.path.join("tftpboot", "SEP00AABBCCDDEE.cnf.xml"))
    phone = xml.dom.minidom.parseString(text)
    assert phone.getElementsByTagName("authName")[0].firstChild.data == "102"
    assert phone.getElementsByTagName("authPassword")[0].firstChild.data == secrets["PHONE_102_PASSWORD"]
    assert secrets["PHONE_102_PASSWORD"] != secrets["PHONE_101_PASSWORD"]
    assert phone.getElementsByTagName("loadInformation")[0].firstChild.data == "SIP70.9-2-1S"
    assert ":8090/" not in text  # the Claude console (services, idle panel, auth) is 101's only
    pbx = xml.dom.minidom.parseString(read(base, PBX))
    users = {u.getAttribute("id"): u for u in pbx.getElementsByTagName("user")}
    variables = {v.getAttribute("name"): v.getAttribute("value") for v in users["102"].getElementsByTagName("variable")}
    assert "claude_caller" not in variables  # may not dial 300


def test_sip_profiles_do_not_restart_on_an_address_change(base):
    render.main(base)
    pbx = xml.dom.minidom.parseString(read(base, PBX))
    for profile in pbx.getElementsByTagName("profile"):
        if profile.getAttribute("name") in ("internal", "ha"):
            params = {p.getAttribute("name"): p.getAttribute("value") for p in profile.getElementsByTagName("param")}
            assert params.get("auto-restart") == "false", profile.getAttribute("name")


@pytest.mark.parametrize("phone,label,number", [
    ("SEP001122334455", "Sam", "102"), ("SEP00AABBCCDDEE", "Mo", "101")])
def test_each_phone_speed_dials_the_other_from_its_bottom_right_key(base, phone, label, number):
    render.main(base)
    doc = xml.dom.minidom.parseString(read(base, os.path.join("tftpboot", f"{phone}.cnf.xml")))
    keys = {line.getAttribute("button"): line for line in doc.getElementsByTagName("line")}
    key = keys["8"]
    text = {t: key.getElementsByTagName(t)[0].firstChild.data for t in ("featureID", "featureLabel", "speedDialNumber")}
    assert text == {"featureID": "21", "featureLabel": label, "speedDialNumber": number}


@pytest.mark.parametrize("phone", ["SEP001122334455", "SEP00AABBCCDDEE"])
def test_each_phone_screen_stays_on_from_0930_to_2230(base, phone):
    render.main(base)
    doc = xml.dom.minidom.parseString(read(base, os.path.join("tftpboot", f"{phone}.cnf.xml")))
    vendor = doc.getElementsByTagName("vendorConfig")[0]
    text = {t: vendor.getElementsByTagName(t)[0].firstChild.data
            for t in ("displayOnTime", "displayOnDuration", "displayIdleTimeout")}
    assert text == {"displayOnTime": "09:30", "displayOnDuration": "13:00", "displayIdleTimeout": "01:00"}
