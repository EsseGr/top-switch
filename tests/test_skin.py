"""Static checks on the default skin: devices inside a group are indented, other elements are not."""
import re
from pathlib import Path

SKIN = Path(__file__).resolve().parent.parent / "skins" / "default"
JS = (SKIN / "app.js").read_text(encoding="utf-8")
CSS = (SKIN / "style.css").read_text(encoding="utf-8")


def func_body(name):
    start = JS.index(f"function {name}(")
    m = re.search(r"\n}\n", JS[start:])
    return JS[start:start + m.end()]


def rules(selector):
    """Declaration blocks of top-level rules whose selector list contains `selector` exactly."""
    out = []
    for sel, body in re.findall(r"(?m)^([^@{}\n][^{}\n]*)\{([^}]*)\}", CSS):
        if selector in [s.strip() for s in sel.split(",")]:
            out.append(body)
    return out


def test_group_device_rows_have_in_group_class():
    body = func_body("groupCard")
    assert 'el("ul", "devices in-group")' in body
    assert 'el("li", "device in-group")' in body
    assert 'el("li", "device notes in-group")' in body


def test_standalone_button_card_has_no_in_group_class():
    body = func_body("buttonCard")
    assert "in-group" not in body
    assert '"device' not in body


def test_in_group_indent_is_24_to_32px():
    decl = " ".join(rules(".devices.in-group"))
    m = re.search(r"margin-left:\s*([\d.]+)rem", decl)
    assert m, "missing margin-left indent on .devices.in-group"
    assert 1.5 <= float(m.group(1)) <= 2.0


def test_narrow_screen_keeps_indent_and_wraps_text():
    media = CSS[CSS.index("@media (max-width: 380px)"):]
    m = re.search(r"\.devices\.in-group\s*\{[^}]*margin-left:\s*([\d.]+)rem", media)
    assert m and float(m.group(1)) >= 1.5
    assert "overflow-wrap: anywhere" in " ".join(rules(".device.in-group .ip"))


def test_non_group_elements_are_not_indented():
    for sel in (".card", ".head", ".card .head", ".device", ".devices"):
        for decl in rules(sel):
            assert "margin-left: 1" not in decl and "padding-left" not in decl, sel
