"""Shared design tokens (#243): tokens.css is a pure token file linked first on every page."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "portal"
TOKENS = ROOT / "static" / "css" / "tokens.css"
LOCAL_STYLESHEET = re.compile(r"""<link rel="stylesheet" href="{{ url_for\('static', path='(css/[^']+)'\)""")
# embed.html is themed by the embedding site (server-sanitised params and custom_css_url).
NOT_PAGES = {"embed.html"}


def _token_values():
    css = re.sub(r"/\*.*?\*/", "", TOKENS.read_text(encoding="utf-8"), flags=re.S)
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", css))


def _page_templates():
    for template in sorted((ROOT / "templates").rglob("*.html")):
        name = template.relative_to(ROOT / "templates").as_posix()
        source = template.read_text(encoding="utf-8")
        if name.startswith("email/") or name in NOT_PAGES or not source.lower().startswith("<!doctype html>"):
            continue
        if LOCAL_STYLESHEET.search(source):
            yield pytest.param(source, id=name)


def test_tokens_file_only_defines_vb_custom_properties():
    css = re.sub(r"/\*.*?\*/", "", TOKENS.read_text(encoding="utf-8"), flags=re.S)
    # Nothing outside the single :root block: no other selectors, no @import or other at-rules.
    root = re.fullmatch(r"\s*:root\s*{([^{}]*)}\s*", css)
    assert root, "tokens.css must contain only comments and one :root block"
    declarations = [d.strip() for d in root.group(1).split(";") if d.strip()]
    assert declarations
    for declaration in declarations:
        assert declaration.startswith("--vb-"), f"tokens.css may only declare --vb-* properties: {declaration}"


@pytest.mark.parametrize("source", list(_page_templates()))
def test_page_links_tokens_before_other_stylesheets(source):
    assert LOCAL_STYLESHEET.findall(source)[0] == "css/tokens.css"


# Stylesheets migrated to the shared tokens (#243). Their own custom properties must take
# their colours from tokens.css rather than restating them, so the palette has one source.
ADOPTED_STYLESHEETS = ["admin.css", "interpreter.css", "auth.css", "account-area.css"]
COLOUR_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\s*\(", re.I)


@pytest.mark.parametrize("stylesheet", ADOPTED_STYLESHEETS)
def test_adopted_stylesheet_variables_take_their_colours_from_tokens(stylesheet):
    css = re.sub(r"/\*.*?\*/", "", (TOKENS.parent / stylesheet).read_text(encoding="utf-8"), flags=re.S)
    # Declaration position only: the lookbehind keeps class names like .btn--danger:hover out.
    declarations = {name: value.strip() for name, value in re.findall(r"(?<![\w-])(--[\w-]+)\s*:\s*([^;{}]+)", css)}
    assert declarations, f"{stylesheet} defines no custom properties"
    tokens = _token_values()
    refs = {}
    for name, value in declarations.items():
        if not COLOUR_LITERAL.search(value):
            # Spacing, radii and the like may stay literal; only colours must come from tokens.
            refs[name] = re.findall(r"var\(\s*(--[\w-]+)", value)
            continue
        assert False, f"{stylesheet}: {name} hard-codes a colour ({value}) instead of using a --vb-* token"
    # Follow each reference: every name must resolve to a shared token or a sibling, without a cycle.
    for name, targets in refs.items():
        for target in targets:
            seen, cursor = {name}, target
            while cursor in refs:
                assert cursor not in seen, f"{stylesheet}: {name} is part of a variable cycle"
                seen.add(cursor)
                nxt = refs[cursor]
                if not nxt:
                    break
                cursor = nxt[0]
            assert cursor in tokens or cursor in refs, f"{stylesheet}: {name} references undefined {cursor}"


def _stylesheets():
    for stylesheet in sorted(TOKENS.parent.glob("*.css")):
        if stylesheet != TOKENS:
            yield pytest.param(stylesheet, id=stylesheet.name)


@pytest.mark.parametrize("stylesheet", list(_stylesheets()))
def test_every_shared_token_reference_resolves(stylesheet):
    """A var(--vb-*) that no longer exists silently drops the whole declaration."""
    css = re.sub(r"/\*.*?\*/", "", stylesheet.read_text(encoding="utf-8"), flags=re.S)
    tokens = _token_values()
    for name in sorted(set(re.findall(r"var\(\s*(--vb-[\w-]+)", css))):
        assert name in tokens, f"{stylesheet.name} uses {name}, which tokens.css does not define"


def _contrast_on_white(hex_color: str) -> float:
    # Opaque #rrggbb only: an alpha channel would make the real contrast lower than computed.
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", hex_color), f"text tokens must be opaque #rrggbb, got {hex_color}"
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 1.05 / (0.2126 * r + 0.7152 * g + 0.0722 * b + 0.05)


@pytest.mark.parametrize(
    "token",
    [
        "--vb-color-text",
        "--vb-color-text-secondary",
        "--vb-color-muted",
        "--vb-color-primary",
        "--vb-color-danger",
        "--vb-color-danger-text",
        "--vb-color-success-text",
        "--vb-color-warning-text",
    ],
)
def test_text_token_meets_wcag_aa_on_white(token):
    assert _contrast_on_white(_token_values()[token].strip()) >= 4.5


def _tint_over_white(hex_color: str, percent: int) -> str:
    """color-mix(in srgb, <hex_color> <percent>%, white), the way the badge backgrounds are built."""
    mix = percent / 100
    return "#" + "".join(f"{round(mix * int(hex_color[i : i + 2], 16) + (1 - mix) * 255):02x}" for i in (1, 3, 5))


def _contrast_on_tint(text: str, tint: str) -> float:
    # Both ratios are measured against white, so text-on-tint = text-on-white / tint-on-white.
    return _contrast_on_white(text) / _contrast_on_white(tint)


@pytest.mark.parametrize("status", ["danger", "success", "warning"])
def test_status_text_meets_wcag_aa_on_its_badge_tint(status):
    tokens = _token_values()
    fill = tokens[f"--vb-color-{status}"].strip()
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", fill), f"--vb-color-{status} must be opaque #rrggbb, got {fill}"
    # Badges tint their background with 10% of the fill colour over white (color-mix in srgb).
    assert _contrast_on_tint(tokens[f"--vb-color-{status}-text"].strip(), _tint_over_white(fill, 10)) >= 4.5


@pytest.mark.parametrize("stylesheet", ADOPTED_STYLESHEETS)
def test_status_tinted_backgrounds_stay_readable(stylesheet):
    """Status text sits on these, so a stronger tint has to keep its own text above AA."""
    css = re.sub(r"/\*.*?\*/", "", (TOKENS.parent / stylesheet).read_text(encoding="utf-8"), flags=re.S)
    # Backgrounds only: a border tint sits beside the text, not behind it, so it may be stronger.
    declaration = re.compile(r"(?:background(?:-color)?|--[\w-]*-bg)\s*:\s*([^;{}]+)")
    # Every surface these mix over is white or near-white, so measure against white.
    mixed = re.compile(r"color-mix\(\s*in srgb,\s*var\(\s*--vb-color-(danger|success|warning)\s*\)\s*(\d+)%")
    tokens = _token_values()
    for value in declaration.findall(css):
        for status, percent in mixed.findall(value):
            tint = _tint_over_white(tokens[f"--vb-color-{status}"].strip(), int(percent))
            ratio = _contrast_on_tint(tokens[f"--vb-color-{status}-text"].strip(), tint)
            assert ratio >= 4.5, (
                f"{stylesheet}: --vb-color-{status}-text on a {percent}% {status} tint ({tint}) is only {ratio:.2f}:1"
            )
