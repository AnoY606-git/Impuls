"""Deterministic Cairo visuals driven by the player's real spectrum bands."""
from __future__ import annotations

import math

import cairo


VISUALIZATION_MODES = (('ribbons', 'Ленты'), ('bars', 'Спектр'),
                       ('mirror', 'Зеркало'), ('orbit', 'Орбита'))


def _sample(levels, position):
    """Cubic interpolation removes angular corners between spectrum bins."""
    point = max(0.0, min(1.0, position)) * (len(levels) - 1)
    index = int(point)
    t = point - index
    a, b, c, d = (levels[max(0, min(len(levels) - 1, index + offset))]
                  for offset in (-1, 0, 1, 2))
    return max(0.0, min(1.0, .5 * ((2 * b) + (-a + c) * t
                   + (2 * a - 5 * b + 4 * c - d) * t * t
                   + (-a + 3 * b - 3 * c + d) * t * t * t)))


def _palette(width, height, alpha=1.0):
    gradient = cairo.LinearGradient(0, height, width, 0)
    gradient.add_color_stop_rgba(0, .16, .48, 1.0, alpha)
    gradient.add_color_stop_rgba(.35, .08, .92, .93, alpha)
    gradient.add_color_stop_rgba(.65, .40, .98, .68, alpha)
    gradient.add_color_stop_rgba(1, .66, .47, 1.0, alpha)
    return gradient


def _glowing_stroke(context, width, height, line_width=1.3, alpha=.85):
    path = context.copy_path()
    context.set_line_width(line_width + 5)
    context.set_source(_palette(width, height, .055))
    context.stroke()
    context.append_path(path)
    context.set_line_width(line_width)
    context.set_source(_palette(width, height, alpha))
    context.stroke()


def _ribbons(context, width, height, levels, phase):
    energy = sum(levels) / len(levels)
    samples = [(point / 192, _sample(levels, point / 192),
                math.sin(math.pi * point / 192) ** .7) for point in range(193)]
    for family in range(2):
        direction = -1 if family else 1
        for layer in range(8):
            for point, (x, magnitude, envelope) in enumerate(samples):
                wave = math.sin(x * math.tau * 1.12 + layer * .27
                                + family * .9 + phase * .55)
                cross = direction * (x - .5) * .6
                spread = (layer - 3.5) * .034
                y = .5 + envelope * (cross * energy + wave * (.08 * energy
                                     + .30 * magnitude) + spread * energy)
                if point:
                    context.line_to(width * x, height * y)
                else:
                    context.move_to(0, height * y)
            _glowing_stroke(context, width, height, 1.15, .48 + .055 * layer)


def _rounded_bar(context, x, y, width, height, radius=3):
    radius = max(0.0, min(radius, width / 2, height / 2))
    context.new_sub_path()
    context.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    context.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    context.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    context.arc(x + radius, y + radius, radius, math.pi, math.pi * 1.5)
    context.close_path()


def _bars(context, width, height, levels, _phase):
    padding = width * .055
    step = (width - padding * 2) / len(levels)
    bar_width = max(.3, step * .62)
    baseline = height * .78
    for index, magnitude in enumerate(levels):
        if magnitude <= .002:
            continue
        x = padding + index * step + (step - bar_width) / 2
        bar_height = max(.5, magnitude * height * .61)
        _rounded_bar(context, x, baseline - bar_height, bar_width, bar_height)
        context.set_source(_palette(width, height, .9))
        context.fill()
        context.rectangle(x, baseline + 5, bar_width, bar_height * .17)
        context.set_source(_palette(width, height, .10))
        context.fill()
        context.rectangle(x, baseline - bar_height - 4, bar_width, 1.5)
        context.set_source_rgba(.75, .94, 1, .8)
        context.fill()


def _mirror(context, width, height, levels, phase):
    samples = [(point / 160, _sample(levels, point / 160),
                math.sin(math.pi * point / 160) ** .5) for point in range(161)]
    for layer in range(4):
        for direction in (-1, 1):
            context.move_to(0, height / 2)
            for x, magnitude, envelope in samples:
                y = height / 2 + direction * height * magnitude * envelope * (.37 - layer * .058)
                context.line_to(x * width, y)
            context.line_to(width, height / 2)
            context.close_path()
            path = context.copy_path()
            context.set_source(_palette(width, height, .025 + layer * .016))
            context.fill()
            context.append_path(path)
            _glowing_stroke(context, width, height, 1, .7 - layer * .12)


def _orbit(context, width, height, levels, phase):
    cx, cy = width / 2, height / 2
    radius = min(width, height) * .22
    energy = sum(levels) / len(levels)
    context.arc(cx, cy, radius * .91, 0, math.tau)
    context.set_source(_palette(width, height, .16 + energy * .2))
    context.set_line_width(1)
    context.stroke()
    for layer in range(3):
        for point in range(193):
            fraction = point / 192
            # Mirror the spectrum around the ring to avoid a seam at 0/2π.
            band = 1 - abs(fraction * 2 - 1)
            magnitude = _sample(levels, band)
            angle = fraction * math.tau + phase * (.18 + layer * .07) - math.pi / 2
            distance = radius + magnitude * radius * (.75 - layer * .17)
            x, y = cx + math.cos(angle) * distance, cy + math.sin(angle) * distance
            if point:
                context.line_to(x, y)
            else:
                context.move_to(x, y)
        context.close_path()
        _glowing_stroke(context, width, height, 1.5, .85 - layer * .2)


def draw_visualization(context, width, height, levels, mode='ribbons', phase=0.0):
    """Draw within the canvas; silence has no synthetic signal or animation."""
    if width <= 0 or height <= 0:
        return
    context.save()
    try:
        context.rectangle(0, 0, width, height)
        context.clip()
        background = cairo.LinearGradient(0, 0, width, height)
        background.add_color_stop_rgb(0, .018, .042, .092)
        background.add_color_stop_rgb(.55, .026, .065, .14)
        background.add_color_stop_rgb(1, .045, .035, .11)
        context.set_source(background)
        context.paint()
        # An understated display grid, static even when no sound is present.
        context.set_line_width(.5)
        context.set_source_rgba(.40, .65, .85, .045)
        for line in range(1, 8):
            context.move_to(0, height * line / 8)
            context.line_to(width, height * line / 8)
        context.stroke()
        if not levels or not any(levels):
            return
        context.set_line_join(cairo.LINE_JOIN_ROUND)
        context.set_line_cap(cairo.LINE_CAP_ROUND)
        renderer = {'bars': _bars, 'mirror': _mirror, 'orbit': _orbit}.get(mode, _ribbons)
        renderer(context, width, height, levels, phase)
    finally:
        context.restore()
