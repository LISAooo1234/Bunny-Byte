"""User-facing brand constants and mascot rendering helpers."""

from __future__ import annotations

import math

DISPLAY_NAME = "BunnyByte"
DISPLAY_HANDLE = "bunnybyte"
SUBTITLE = "blue-bunny coding agent"
WELCOME_STATUS = "sugar, spice, and shipped code"
HELP_HINT = "type /help for commands, Ctrl+L to clear, Ctrl+Q to quit"
PROMPT_PLACEHOLDER = "Ask BunnyByte or type /help"
BUSY_PLACEHOLDER = "BunnyByte is working..."

TUI_PANEL_BG = "#10131b"
TUI_PANEL_BORDER = "#66b6ff"
TUI_TEXT = "#eef4ff"
TUI_ACCENT = "#7fc8ff"
TUI_MUTED = "#9ca8bb"
TUI_INPUT_BORDER = "#66b6ff"

ANSI_RESET = "\x1b[0m"
UPPER_HALF_BLOCK = "▀"
LOWER_HALF_BLOCK = "▄"

BASE_MASCOT_WIDTH = 40
BASE_MASCOT_HEIGHT = 36
MASCOT_WIDTH = 28
MASCOT_HEIGHT = 18

OUTLINE = "#72adff"
FACE_FILL = "#f7fbff"
EAR_HIGHLIGHT = "#d9ebff"
CHEEK_RING = "#7cb8ff"
CHEEK_FILL = "#b7dcff"
FEATURE = "#17304e"
NOSE = "#21405f"


def mascot_pixels() -> tuple[tuple[str | None, ...], ...]:
    canvas: list[list[str | None]] = [
        [None for _ in range(MASCOT_WIDTH)] for _ in range(MASCOT_HEIGHT)
    ]

    # Ears first so the head can overlap their lower edge cleanly.
    _fill_rotated_ellipse(canvas, _sx(11.5), _sy(9.0), _sx(5.4), _sy(12.5), -0.55, OUTLINE)
    _fill_rotated_ellipse(canvas, _sx(28.5), _sy(9.0), _sx(5.4), _sy(12.5), 0.55, OUTLINE)
    _fill_rotated_ellipse(canvas, _sx(11.5), _sy(9.0), _sx(4.2), _sy(11.3), -0.55, FACE_FILL)
    _fill_rotated_ellipse(canvas, _sx(28.5), _sy(9.0), _sx(4.2), _sy(11.3), 0.55, FACE_FILL)
    _fill_rotated_ellipse(canvas, _sx(12.2), _sy(11.5), _sx(2.3), _sy(7.3), -0.48, EAR_HIGHLIGHT)
    _fill_rotated_ellipse(canvas, _sx(27.8), _sy(11.5), _sx(2.3), _sy(7.3), 0.48, EAR_HIGHLIGHT)

    # Head: combine two ellipses for a rounded face with a slightly wider base.
    _fill_ellipse(canvas, _sx(20.0), _sy(22.0), _sx(16.8), _sy(11.8), OUTLINE)
    _fill_ellipse(canvas, _sx(20.0), _sy(24.5), _sx(18.2), _sy(8.8), OUTLINE)
    _fill_ellipse(canvas, _sx(20.0), _sy(22.0), _sx(15.5), _sy(10.7), FACE_FILL)
    _fill_ellipse(canvas, _sx(20.0), _sy(24.5), _sx(16.8), _sy(7.8), FACE_FILL)

    # Cheeks.
    _fill_circle(canvas, _sx(8.7), _sy(24.0), _sr(4.3), CHEEK_RING)
    _fill_circle(canvas, _sx(31.3), _sy(24.0), _sr(4.3), CHEEK_RING)
    _fill_circle(canvas, _sx(8.7), _sy(24.0), _sr(3.2), CHEEK_FILL)
    _fill_circle(canvas, _sx(31.3), _sy(24.0), _sr(3.2), CHEEK_FILL)

    # One open eye, one wink, plus nose and smile.
    _fill_ellipse(canvas, _sx(13.2), _sy(19.8), _sx(1.8), _sy(3.0), FEATURE)
    _fill_ellipse(canvas, _sx(20.0), _sy(21.5), _sx(1.9), _sy(1.2), NOSE)
    _draw_thick_line(
        canvas,
        _sx(23.2),
        _sy(19.5),
        _sx(27.1),
        _sy(21.0),
        _sr(1.4),
        FEATURE,
    )
    _draw_thick_line(
        canvas,
        _sx(23.2),
        _sy(19.5),
        _sx(27.0),
        _sy(17.6),
        _sr(1.4),
        FEATURE,
    )
    _draw_arc(
        canvas,
        _sx(20.0),
        _sy(25.1),
        _sx(3.2),
        _sy(2.6),
        0.25,
        2.9,
        _sr(1.1),
        FEATURE,
    )

    return tuple(tuple(row) for row in canvas)


def mascot_visible_width() -> int:
    return MASCOT_WIDTH


def render_mascot_plain_rows(fill: str = "[]", blank: str = "  ") -> tuple[str, ...]:
    rows = []
    for row in mascot_pixels():
        chunks = []
        for color in row:
            chunks.append(blank if color is None else fill)
        rows.append("".join(chunks))
    return tuple(rows)


def mascot_stacked_rows() -> tuple[tuple[tuple[str | None, str | None], ...], ...]:
    pixels = mascot_pixels()
    rows = []
    for index in range(0, len(pixels), 2):
        top = pixels[index]
        bottom = pixels[index + 1] if index + 1 < len(pixels) else tuple(
            None for _ in top
        )
        rows.append(tuple(zip(top, bottom)))
    return tuple(rows)


def render_mascot_ansi_rows() -> tuple[str, ...]:
    rows = []
    for row in mascot_stacked_rows():
        chunks = []
        for top_color, bottom_color in row:
            chunks.append(_ansi_half_block(top_color, bottom_color))
        rows.append("".join(chunks))
    return tuple(rows)


def _ansi_half_block(top_color: str | None, bottom_color: str | None) -> str:
    if top_color is None and bottom_color is None:
        return " "
    if top_color is None:
        r, g, b = _hex_to_rgb(bottom_color)
        return f"\x1b[38;2;{r};{g};{b}m{LOWER_HALF_BLOCK}{ANSI_RESET}"
    if bottom_color is None:
        r, g, b = _hex_to_rgb(top_color)
        return f"\x1b[38;2;{r};{g};{b}m{UPPER_HALF_BLOCK}{ANSI_RESET}"
    fr, fg, fb = _hex_to_rgb(top_color)
    br, bg, bb = _hex_to_rgb(bottom_color)
    return (
        f"\x1b[38;2;{fr};{fg};{fb}m"
        f"\x1b[48;2;{br};{bg};{bb}m"
        f"{UPPER_HALF_BLOCK}{ANSI_RESET}"
    )


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _sx(value: float) -> float:
    return value * MASCOT_WIDTH / BASE_MASCOT_WIDTH


def _sy(value: float) -> float:
    return value * MASCOT_HEIGHT / BASE_MASCOT_HEIGHT


def _sr(value: float) -> float:
    return value * min(MASCOT_WIDTH / BASE_MASCOT_WIDTH, MASCOT_HEIGHT / BASE_MASCOT_HEIGHT)


def _fill_circle(
    canvas: list[list[str | None]], cx: float, cy: float, radius: float, color: str
) -> None:
    _fill_ellipse(canvas, cx, cy, radius, radius, color)


def _fill_ellipse(
    canvas: list[list[str | None]],
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    color: str,
) -> None:
    _fill_rotated_ellipse(canvas, cx, cy, rx, ry, 0.0, color)


def _fill_rotated_ellipse(
    canvas: list[list[str | None]],
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    angle: float,
    color: str,
) -> None:
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    min_x = max(0, int(math.floor(cx - rx - 2)))
    max_x = min(MASCOT_WIDTH - 1, int(math.ceil(cx + rx + 2)))
    min_y = max(0, int(math.floor(cy - ry - 2)))
    max_y = min(MASCOT_HEIGHT - 1, int(math.ceil(cy + ry + 2)))
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            dx = (x + 0.5) - cx
            dy = (y + 0.5) - cy
            xr = dx * cos_a + dy * sin_a
            yr = -dx * sin_a + dy * cos_a
            if (xr / rx) ** 2 + (yr / ry) ** 2 <= 1.0:
                canvas[y][x] = color


def _draw_thick_line(
    canvas: list[list[str | None]],
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    thickness: float,
    color: str,
) -> None:
    steps = max(2, int(max(abs(x2 - x1), abs(y2 - y1)) * 3))
    radius = thickness / 2
    for step in range(steps + 1):
        t = step / steps
        x = x1 + (x2 - x1) * t
        y = y1 + (y2 - y1) * t
        _fill_circle(canvas, x, y, radius, color)


def _draw_arc(
    canvas: list[list[str | None]],
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    start_angle: float,
    end_angle: float,
    thickness: float,
    color: str,
) -> None:
    steps = max(12, int((end_angle - start_angle) * 18))
    radius = thickness / 2
    for step in range(steps + 1):
        t = start_angle + (end_angle - start_angle) * (step / steps)
        x = cx + math.cos(t) * rx
        y = cy + math.sin(t) * ry
        _fill_circle(canvas, x, y, radius, color)
