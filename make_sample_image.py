"""Generates a simple fake 'outfit' image (blue/white shirt) so you can test image+text without uploading anything."""
from pathlib import Path
from PIL import Image, ImageDraw

SAMPLE_PATH = Path(__file__).parent / "samples" / "sample_outfit.png"


def make_sample_outfit(path: Path = SAMPLE_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (600, 700), "#f4f1ea")
    d = ImageDraw.Draw(img)
    d.polygon([(150, 120), (250, 80), (350, 80), (450, 120), (540, 260), (470, 300), (430, 240),
               (430, 640), (170, 640), (170, 240), (130, 300), (60, 260)], fill="#2457a6")
    for x in range(170, 430, 40):  # white pinstripes
        d.line([(x, 130), (x, 640)], fill="white", width=6)
    d.polygon([(250, 80), (300, 150), (350, 80)], fill="white")  # collar
    img.save(path)
    return path


if __name__ == "__main__":
    print("Saved", make_sample_outfit())
