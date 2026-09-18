"""Long Business Scope extends the license box downward on the same page."""
from license_trans.render import (
    DEFAULT_AUTH_Y,
    PAGE_H,
    compute_english_layout,
)


def test_short_scope_keeps_printed_positions():
    layout = compute_english_layout(
        n_scope_lines=3, n_addr_lines=2, has_term=True,
    )
    assert layout["auth_y"] == DEFAULT_AUTH_Y
    assert layout["page_h"] == PAGE_H
    assert layout["border_bottom"] == 433.0


def test_long_scope_pushes_authority_and_grows_page():
    layout = compute_english_layout(
        n_scope_lines=16, n_addr_lines=2, has_term=False,
    )
    assert layout["scope_end"] > DEFAULT_AUTH_Y
    assert layout["auth_y"] > layout["scope_end"]
    assert layout["footer_y"] > layout["auth_y"]
    assert layout["cert_y"] > layout["footer_y"]
    assert layout["page_h"] > PAGE_H
    assert layout["border_bottom"] > layout["scope_end"]
    assert layout["border_bottom"] < layout["footer_y"]
