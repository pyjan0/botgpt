"""Detect the type of an image."""

__all__ = ["what"]

def what(file, h=None):
    if h is None:
        if isinstance(file, (str, bytes)):
            with open(file, "rb") as f:
                h = f.read(32)
        else:
            h = file.read(32)

    for tf in tests:
        res = tf(h, file)
        if res:
            return res
    return None

def test_jpeg(h, f):
    if h[6:10] in (b"JFIF", b"Exif"):
        return "jpeg"

def test_png(h, f):
    if h.startswith(b"\211PNG\r\n\032\n"):
        return "png"

def test_gif(h, f):
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"

def test_tiff(h, f):
    if h[:2] in (b"II", b"MM"):
        return "tiff"

def test_bmp(h, f):
    if h.startswith(b"BM"):
        return "bmp"

def test_webp(h, f):
    if h.startswith(b"RIFF") and h[8:12] == b"WEBP":
        return "webp"

tests = [
    test_jpeg,
    test_png,
    test_gif,
    test_tiff,
    test_bmp,
    test_webp,
]
