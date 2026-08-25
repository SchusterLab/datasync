r"""Regenerate ``src/datasync/backup.ico`` -- the widget's window/taskbar icon.

Committed binaries want a source. This draws the icon (a dark rounded tile, a
blue cloud, a green up-arrow == "backing up") in the widget's own palette and
writes a multi-resolution ``.ico`` (16/32/48/64/128/256 px).

Dev-only: needs Pillow, which is NOT a runtime dependency of datasync.

    python tools/make_backup_icon.py
"""
import os

from PIL import Image, ImageDraw

# Widget palette (see data_sync_ui.py: BG / card border / ACCENT / OK).
TILE = (27, 33, 43, 255)
BORDER = (43, 50, 64, 255)
CLOUD = (91, 157, 217, 255)      # ACCENT blue
ARROW = (76, 175, 80, 255)       # OK green

N = 256
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src", "datasync", "backup.ico")


def draw(n=N):
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = n / 256.0

    def r(*xy):
        return [v * s for v in xy]

    d.rounded_rectangle(r(6, 6, 250, 250), radius=46 * s,
                        fill=TILE, outline=BORDER, width=max(1, int(4 * s)))
    # cloud silhouette, lower ~55%
    d.rounded_rectangle(r(58, 152, 198, 198), radius=24 * s, fill=CLOUD)
    d.ellipse(r(50, 126, 122, 198), fill=CLOUD)
    d.ellipse(r(94, 98, 182, 186), fill=CLOUD)
    d.ellipse(r(148, 132, 208, 194), fill=CLOUD)
    # up arrow rising out of the cloud
    d.rectangle(r(118, 96, 138, 170), fill=ARROW)
    d.polygon([tuple(r(96, 112)), tuple(r(160, 112)), tuple(r(128, 62))],
              fill=ARROW)
    return img


def main():
    base = draw(N)
    base.save(OUT, sizes=[(256, 256), (128, 128), (64, 64),
                          (48, 48), (32, 32), (16, 16)])
    print("wrote", OUT, "(%d bytes)" % os.path.getsize(OUT))


if __name__ == "__main__":
    main()
