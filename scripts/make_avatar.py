"""Рисует аватар бота: data/brand/avatar.png (640×640, безопасная зона под круглую маску Telegram).

Запуск: python scripts/make_avatar.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "brand" / "avatar.png"
S = 4  # рисуем в 4 раза крупнее и уменьшаем, чтобы сгладить края
W = 640 * S

RED_TOP = (214, 32, 39)
RED_BOTTOM = (120, 8, 18)
GOLD = (255, 205, 64)
WHITE = (255, 255, 255)
DARK = (40, 6, 10)


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for f in (name, "seguibl.ttf", "segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def gradient() -> Image.Image:
    img = Image.new("RGB", (W, W))
    d = ImageDraw.Draw(img)
    for y in range(W):
        t = y / (W - 1)
        c = tuple(int(RED_TOP[i] + (RED_BOTTOM[i] - RED_TOP[i]) * t) for i in range(3))
        d.line([(0, y), (W, y)], fill=c)
    return img


def star(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    import math

    pts = []
    for k in range(10):
        ang = -math.pi / 2 + k * math.pi / 5
        rr = r if k % 2 == 0 else r * 0.42
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    return pts


def car(d: ImageDraw.ImageDraw, x: float, y: float, w: float, color, wheel_hole) -> None:
    """Силуэт кроссовера, x/y — левый нижний угол кузова, w — длина."""
    h = w * 0.30
    body = [
        (x, y), (x, y - h * 0.62), (x + w * 0.05, y - h * 0.78),
        (x + w * 0.24, y - h * 0.86), (x + w * 0.36, y - h * 1.42),
        (x + w * 0.70, y - h * 1.45), (x + w * 0.84, y - h * 0.92),
        (x + w * 0.97, y - h * 0.80), (x + w, y - h * 0.55), (x + w, y),
    ]
    d.polygon(body, fill=color)
    # окна
    win = [
        (x + w * 0.31, y - h * 0.90), (x + w * 0.39, y - h * 1.30),
        (x + w * 0.52, y - h * 1.32), (x + w * 0.52, y - h * 0.90),
    ]
    win2 = [
        (x + w * 0.56, y - h * 0.90), (x + w * 0.56, y - h * 1.32),
        (x + w * 0.68, y - h * 1.30), (x + w * 0.78, y - h * 0.90),
    ]
    d.polygon(win, fill=wheel_hole)
    d.polygon(win2, fill=wheel_hole)
    # колёса
    r = w * 0.105
    for cx in (x + w * 0.22, x + w * 0.79):
        d.ellipse([cx - r * 1.18, y - r * 1.18, cx + r * 1.18, y + r * 1.18], fill=wheel_hole)
        d.ellipse([cx - r, y - r, cx + r, y + r], fill=color)
        d.ellipse([cx - r * 0.45, y - r * 0.45, cx + r * 0.45, y + r * 0.45], fill=wheel_hole)


def main() -> None:
    img = gradient()
    d = ImageDraw.Draw(img)

    # мягкое свечение за машиной
    glow = Image.new("L", (W, W), 0)
    ImageDraw.Draw(glow).ellipse([W * 0.12, W * 0.32, W * 0.88, W * 0.82], fill=120)
    glow = glow.filter(ImageFilter.GaussianBlur(W * 0.08))
    img.paste(Image.new("RGB", (W, W), (255, 90, 80)), (0, 0), glow)
    d = ImageDraw.Draw(img)

    # звёзды как на флаге, вверху слева
    d.polygon(star(W * 0.28, W * 0.205, W * 0.07), fill=GOLD)
    for cx, cy in ((W * 0.375, W * 0.125), (W * 0.41, W * 0.18), (W * 0.41, W * 0.245), (W * 0.375, W * 0.30)):
        d.polygon(star(cx, cy, W * 0.024), fill=GOLD)

    # стрелка ¥ → ₽ вверху справа
    f = font("seguibl.ttf", int(W * 0.105))
    d.text((W * 0.53, W * 0.13), "¥", font=f, fill=WHITE)
    d.text((W * 0.615, W * 0.118), "→", font=font("segoeuib.ttf", int(W * 0.115)), fill=GOLD)
    d.text((W * 0.755, W * 0.13), "₽", font=f, fill=WHITE)

    # машина
    car(d, W * 0.14, W * 0.68, W * 0.72, WHITE, RED_BOTTOM)

    # подпись снизу
    label = "ПОД КЛЮЧ"
    fl = font("seguibl.ttf", int(W * 0.075))
    bbox = d.textbbox((0, 0), label, font=fl)
    tw = bbox[2] - bbox[0]
    d.rounded_rectangle([W / 2 - tw / 2 - W * 0.04, W * 0.785, W / 2 + tw / 2 + W * 0.04, W * 0.89], radius=W * 0.05, fill=GOLD)
    d.text((W / 2 - tw / 2, W * 0.792), label, font=fl, fill=DARK)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.resize((640, 640), Image.LANCZOS).save(OUT, optimize=True)
    print(OUT)


if __name__ == "__main__":
    main()
