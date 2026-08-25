from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
SCALE = 4
CANVAS = SIZE * SCALE
PACKAGING_DIR = Path(__file__).resolve().parent


def point(value: int) -> int:
    return value * SCALE


def main() -> None:
    image = Image.new("RGBA", (CANVAS, CANVAS), (247, 243, 232, 255))
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle(
        (point(70), point(70), point(954), point(954)),
        radius=point(210),
        fill=(238, 225, 194, 255),
        outline=(73, 96, 79, 255),
        width=point(24),
    )

    green = (48, 71, 57, 255)
    beige = (190, 139, 79, 255)
    line = point(38)
    draw.line(
        [(point(230), point(487)), (point(505), point(225)), (point(770), point(468))],
        fill=green,
        width=line,
        joint="curve",
    )
    draw.line(
        [(point(274), point(445)), (point(274), point(720))],
        fill=green,
        width=line,
    )
    draw.arc(
        (point(274), point(578), point(825), point(828)),
        start=104,
        end=260,
        fill=green,
        width=line,
    )

    for x, y in ((440, 420), (550, 420), (440, 530), (550, 530)):
        draw.rounded_rectangle(
            (point(x), point(y), point(x + 74), point(y + 74)),
            radius=point(15),
            fill=beige,
        )

    pin_center = (point(760), point(675))
    pin_radius = point(92)
    draw.ellipse(
        (
            pin_center[0] - pin_radius,
            pin_center[1] - pin_radius,
            pin_center[0] + pin_radius,
            pin_center[1] + pin_radius,
        ),
        fill=beige,
    )
    draw.polygon(
        [
            (point(696), point(730)),
            (point(824), point(730)),
            (point(760), point(848)),
        ],
        fill=beige,
    )
    draw.ellipse(
        (point(724), point(639), point(796), point(711)),
        fill=(247, 243, 232, 255),
    )

    image = image.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    image.save(PACKAGING_DIR / "LandPlotFinder.png")
    image.save(
        PACKAGING_DIR / "LandPlotFinder.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    image.save(PACKAGING_DIR / "LandPlotFinder.icns")


if __name__ == "__main__":
    main()
