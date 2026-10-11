"""tbgw.gui.theme
=================

One place for the sizing/typography decisions that make the GUI legible
on THIS panel, so tiles don't each hard-code pixel values that only
happen to work at one resolution.

The panel is 1024x600 on a 5" diagonal (~240 DPI-ish in practice), and
it is operated by finger, not stylus. Two consequences drive everything
here:

  * LVGL's default 14px font is far too small to read at arm's length on
    this panel. Larger Montserrat faces are compiled into the firmware
    via LV_CFLAGS (see build/build_firmware.sh); `font()` falls back
    gracefully to whatever is actually present, so this module never
    hard-fails if a build ships without them.
  * Touch targets need to be finger-sized. TOUCH_MIN is the floor for
    anything tappable -- an earlier layout used 32px-high buttons and
    90px-wide pairs that both overflowed their cards and were fiddly to
    hit.

Sizes are expressed as module constants rather than magic numbers at the
call sites so that a future panel change is a single edit here.
"""

# Panel geometry (matches lvgl_setup.DISPLAY_WIDTH/HEIGHT).
SCREEN_W = 1024
SCREEN_H = 600

STATUS_BAR_H = 34
HEADER_H = 46          # back button + screen title row

# Content area below the status bar and header.
CONTENT_TOP = STATUS_BAR_H + HEADER_H
CONTENT_H = SCREEN_H - CONTENT_TOP - 8

GAP = 10               # gap between tiles and between rows inside a tile
PAD = 10               # inner padding of a card

# Three tiles fit across 1024 with gaps; this is a width, not a share, so
# a home with more devices wraps onto a second row instead of shrinking
# everything into illegibility.
TILE_W = 328
TILE_H = CONTENT_H - 2 * GAP
# Width used by the dedicated page for one device. The content container
# is SCREEN_W - 8 wide and has GAP padding on each side, so leaving two
# extra gaps prevents the card from being clipped by the container chrome.
DETAIL_TILE_W = SCREEN_W - 4 * GAP

TOUCH_MIN = 44         # minimum height/width for anything tappable
ROW_H = 52             # one label+control row inside a tile


def font(lv, size):
    """Returns the closest available Montserrat face at or below `size`.

    Falls back down the list because which faces are compiled in is a
    firmware build option (LV_FONT_MONTSERRAT_* via LV_CFLAGS): a build
    without the larger faces still renders, just smaller, instead of
    raising AttributeError deep inside a widget constructor.
    """
    for candidate in (28, 24, 20, 16, 14, 12):
        if candidate > size:
            continue
        face = getattr(lv, "font_montserrat_%d" % candidate, None)
        if face is not None:
            return face
    return None


def apply_screen_style(lv, screen):
    """Base typography for a screen: everything inherits this unless a
    widget overrides it.
    """
    face = font(lv, 20)
    if face is not None:
        screen.set_style_text_font(face, 0)


def style_card(lv, card):
    """Common card geometry: fixed tile size, comfortable padding, and
    column flow so children stack without manual y-offsets (the old
    hard-coded offsets are exactly what clipped the thermostat's button
    rows and overlapped the blind's buttons).
    """
    card.set_size(TILE_W, TILE_H)
    card.set_style_pad_all(PAD, 0)
    card.set_style_pad_row(GAP, 0)
    card.set_flex_flow(lv.FLEX_FLOW.COLUMN)


def style_row(lv, row, height=ROW_H):
    """A transparent, borderless, zero-padding horizontal strip used to
    lay out a label plus a control. Without clearing the default padding
    and border, LVGL's own container chrome eats enough width to push
    children out of the card.
    """
    row.set_size(lv.pct(100), height)
    row.set_style_pad_all(0, 0)
    row.set_style_border_width(0, 0)
    row.set_style_bg_opa(0, 0)
    row.set_flex_flow(lv.FLEX_FLOW.ROW)
    row.set_flex_align(lv.FLEX_ALIGN.SPACE_BETWEEN, lv.FLEX_ALIGN.CENTER, lv.FLEX_ALIGN.CENTER)


def style_button_row(lv, row, height=TOUCH_MIN + 6):
    """Like style_row(), but children share the width evenly -- used for
    the thermostat's mode/fan/hand-auto selectors, which previously
    positioned buttons at manual x-offsets and ran off the card.
    """
    row.set_size(lv.pct(100), height)
    row.set_style_pad_all(0, 0)
    row.set_style_pad_column(6, 0)
    row.set_style_border_width(0, 0)
    row.set_style_bg_opa(0, 0)
    row.set_flex_flow(lv.FLEX_FLOW.ROW)
    row.set_flex_align(lv.FLEX_ALIGN.SPACE_BETWEEN, lv.FLEX_ALIGN.CENTER, lv.FLEX_ALIGN.CENTER)


def title_label(lv, parent, text):
    label = lv.label(parent)
    label.set_text(text)
    face = font(lv, 24)
    if face is not None:
        label.set_style_text_font(face, 0)
    return label
