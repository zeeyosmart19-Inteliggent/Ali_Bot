"""Generate the ALI AI logo: a static PNG and a seamlessly looping rotating GIF.

Run:  python tools/make_logo.py
Output: assets/logo.png, assets/logo_spin.gif
Requires: pillow (dev-time only; the bot just ships the generated files).
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "assets"
BG = (9, 12, 24)
CYAN = (56, 224, 255)
VIOLET = (150, 92, 255)
SS = 3  # supersampling factor


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(size):
    """Diagonal cyan -> violet gradient."""
    g = Image.new("RGB", (size, size))
    px = g.load()
    for y in range(size):
        for x in range(size):
            px[x, y] = lerp(CYAN, VIOLET, (x + y) / (2 * size - 2))
    return g


def sparkle_mask(size, radius, pinch=0.16):
    """Four-point concave star (the 'spark' core)."""
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    c = size / 2
    pts = []
    n = 64
    for i in range(n * 4):
        a = 2 * math.pi * i / (n * 4)
        # astroid (|cos|^3, |sin|^3) -> concave four-point star
        x = radius * math.copysign(abs(math.cos(a)) ** 3, math.cos(a))
        y = radius * math.copysign(abs(math.sin(a)) ** 3, math.sin(a))
        # blend astroid with a small circle so the centre stays solid
        x = x * (1 - pinch) + radius * pinch * math.cos(a)
        y = y * (1 - pinch) + radius * pinch * math.sin(a)
        pts.append((c + x, c + y))
    d.polygon(pts, fill=255)
    return m


def frame(phase: float, px: int = 512, with_bg: bool = True) -> Image.Image:
    """phase in [0,1) -> one frame; frame(0) == frame(1) so the loop is seamless."""
    S = px * SS
    c = S / 2
    base = Image.new("RGBA", (S, S), BG + (255,) if with_bg else (0, 0, 0, 0))
    grad = gradient(S).convert("RGBA")

    # --- orbit rings (3-fold symmetric: rotating 60 deg == identical image) ---
    rings = Image.new("L", (S, S), 0)
    rd = ImageDraw.Draw(rings)
    a_ax, b_ax = S * 0.40, S * 0.15
    stroke = max(2, int(S * 0.012))
    electrons = []
    for k in range(3):
        rot = math.radians(60 * k + 60 * phase)
        pts = []
        for i in range(181):
            t = 2 * math.pi * i / 180
            x, y = a_ax * math.cos(t), b_ax * math.sin(t)
            pts.append((c + x * math.cos(rot) - y * math.sin(rot),
                        c + x * math.sin(rot) + y * math.cos(rot)))
        rd.line(pts, fill=150, width=stroke, joint="curve")
        # one electron per ring, travelling a full revolution per loop
        t = 2 * math.pi * (phase + k / 3)
        x, y = a_ax * math.cos(t), b_ax * math.sin(t)
        electrons.append((c + x * math.cos(rot) - y * math.sin(rot),
                          c + x * math.sin(rot) + y * math.cos(rot)))

    # --- electrons ---
    dots = Image.new("L", (S, S), 0)
    dd = ImageDraw.Draw(dots)
    r = S * 0.028
    for (x, y) in electrons:
        dd.ellipse([x - r, y - r, x + r, y + r], fill=255)

    # --- core spark, gently pulsing ---
    pulse = 1 + 0.06 * math.sin(2 * math.pi * phase)
    core = sparkle_mask(S, S * 0.25 * pulse)

    # glow layers
    glow = Image.new("L", (S, S), 0)
    glow.paste(rings, mask=rings)
    glow = Image.composite(Image.new("L", (S, S), 255), glow, core)
    glow = Image.composite(Image.new("L", (S, S), 255), glow, dots)
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.03))

    out = base
    out.paste(grad, mask=glow.point(lambda v: int(v * 0.65)))
    out.paste(grad, mask=rings)
    out.paste(grad, mask=dots)
    out.paste(Image.new("RGBA", (S, S), (255, 255, 255, 255)), mask=dots.point(lambda v: v // 3))
    out.paste(grad, mask=core)
    # bright white centre
    centre = sparkle_mask(S, S * 0.10 * pulse).filter(ImageFilter.GaussianBlur(S * 0.004))
    out.paste(Image.new("RGBA", (S, S), (255, 255, 255, 255)), mask=centre.point(lambda v: int(v * 0.9)))

    if with_bg:
        # rounded-square app-icon mask
        rm = Image.new("L", (S, S), 0)
        ImageDraw.Draw(rm).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
        out.putalpha(rm)

    return out.resize((px, px), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    frame(0.0, 512).save(OUT / "logo.png")

    n = 36
    frames = [frame(i / n, 288) for i in range(n)]
    # Telegram wants an opaque GIF; flatten rounded corners onto the dark BG.
    flat = []
    for f in frames:
        bg = Image.new("RGB", f.size, BG)
        bg.paste(f, mask=f.split()[3])
        flat.append(bg.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE))
    flat[0].save(OUT / "logo_spin.gif", save_all=True, append_images=flat[1:],
                 duration=55, loop=0, optimize=True, disposal=2)
    print("wrote", OUT / "logo.png", OUT / "logo_spin.gif")


if __name__ == "__main__":
    main()
