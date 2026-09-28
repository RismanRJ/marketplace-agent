#!/usr/bin/env python3
"""Deterministic image-compliance checks for marketplace product photos.

Measures pixels only (size, background whiteness/uniformity, product bbox
occupancy). Does NOT judge promo text, badges, or claims - that's the VLM's
job in a separate pass. See CLI below.

  image_check.py check --image PATH --channel amazon|flipkart
  image_check.py selfcheck
"""
import argparse
import json
import os
import sys
import tempfile

try:
    from PIL import Image, ImageChops, ImageDraw
except ImportError:
    print("Pillow is required: pip install Pillow", file=sys.stderr)
    sys.exit(1)

BUILTIN_DEFAULTS = {
    "image_amazon_min_edge_px": 1600,
    "image_amazon_min_occupancy_pct": 85.0,
    "image_amazon_min_bg_white_pct": 95.0,
    "image_flipkart_min_edge_px": 500,
    "image_flipkart_recommended_edge_px": 1000,
    "image_flipkart_min_bg_uniformity_pct": 90.0,
}

DEFAULT_WHITE_TOLERANCE = 250
BORDER_FRACTION = 0.03
DOWNSAMPLE_MAX_DIM = 600
BG_DIFF_THRESHOLD = 24


def load_defaults():
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    cfg_path = os.path.join(root, "config", "skus.json")
    defaults = dict(BUILTIN_DEFAULTS)
    try:
        with open(cfg_path) as f:
            data = json.load(f)
        file_defaults = data.get("defaults", {})
        for key in BUILTIN_DEFAULTS:
            if key in file_defaults:
                defaults[key] = file_defaults[key]
    except (OSError, json.JSONDecodeError):
        pass
    return defaults


def _border_histogram(small_rgb):
    sw, sh = small_rgb.size
    border = max(1, round(min(sw, sh) * BORDER_FRACTION))
    crops = [small_rgb.crop((0, 0, sw, border)), small_rgb.crop((0, sh - border, sw, sh))]
    if sh - border > border:
        crops.append(small_rgb.crop((0, border, border, sh - border)))
        crops.append(small_rgb.crop((sw - border, border, sw, sh - border)))

    color_counts = {}
    total = 0
    for crop in crops:
        cw, ch = crop.size
        if cw <= 0 or ch <= 0:
            continue
        for count, color in crop.getcolors(maxcolors=cw * ch):
            color_counts[color] = color_counts.get(color, 0) + count
            total += count
    return color_counts, total


def analyze(img, white_tolerance):
    rgb = img.convert("RGB")
    w, h = rgb.size
    scale = min(1.0, DOWNSAMPLE_MAX_DIM / max(w, h))
    small = rgb.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.NEAREST) if scale < 1.0 else rgb

    color_counts, total = _border_histogram(small)
    if total == 0:
        bg_white_pct, bg_uniformity_pct, bg_color = 0.0, 0.0, (255, 255, 255)
    else:
        white_count = sum(c for color, c in color_counts.items() if all(v >= white_tolerance for v in color))
        bg_white_pct = white_count / total * 100.0
        bg_color, dominant_count = max(color_counts.items(), key=lambda kv: kv[1])
        bg_uniformity_pct = dominant_count / total * 100.0

    bg_img = Image.new("RGB", small.size, bg_color)
    diff = ImageChops.difference(small, bg_img).convert("L")
    mask = diff.point(lambda p: 255 if p > BG_DIFF_THRESHOLD else 0)
    bbox = mask.getbbox()
    sw, sh = small.size
    if bbox is None:
        occupancy_pct = 0.0
    else:
        x0, y0, x1, y1 = bbox
        occupancy_pct = (x1 - x0) * (y1 - y0) / (sw * sh) * 100.0

    return bg_white_pct, bg_uniformity_pct, occupancy_pct


def run_check(image_path, channel, white_tolerance=DEFAULT_WHITE_TOLERANCE, defaults=None):
    defaults = defaults if defaults is not None else load_defaults()

    try:
        img = Image.open(image_path)
        img.load()
    except FileNotFoundError:
        return {"pass": False, "image": image_path, "channel": channel, "checks": {},
                "failures": [f"file not found: {image_path}"], "warnings": []}
    except Exception as e:
        return {"pass": False, "image": image_path, "channel": channel, "checks": {},
                "failures": [f"unreadable or invalid image: {e}"], "warnings": []}

    width, height = img.size
    has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
    file_size_bytes = os.path.getsize(image_path)
    bg_white_pct, bg_uniformity_pct, occupancy_pct = analyze(img, white_tolerance)

    checks = {
        "format": img.format,
        "mode": img.mode,
        "width": width,
        "height": height,
        "has_alpha": has_alpha,
        "file_size_bytes": file_size_bytes,
        "longest_edge_px": max(width, height),
        "bg_white_pct": round(bg_white_pct, 2),
        "bg_uniformity_pct": round(bg_uniformity_pct, 2),
        "frame_occupancy_pct": round(occupancy_pct, 2),
    }

    failures = []
    warnings = []

    if channel == "amazon":
        min_edge = defaults["image_amazon_min_edge_px"]
        min_occ = defaults["image_amazon_min_occupancy_pct"]
        min_bg_white = defaults["image_amazon_min_bg_white_pct"]
        if checks["longest_edge_px"] < min_edge:
            failures.append(f"longest edge {checks['longest_edge_px']}px < required {min_edge}px")
        if checks["frame_occupancy_pct"] < min_occ:
            failures.append(f"frame occupancy {checks['frame_occupancy_pct']}% < required {min_occ}%")
        if checks["bg_white_pct"] < min_bg_white:
            failures.append(f"background white {checks['bg_white_pct']}% < required {min_bg_white}%")
    else:
        min_edge = defaults["image_flipkart_min_edge_px"]
        recommended_edge = defaults["image_flipkart_recommended_edge_px"]
        min_uniformity = defaults["image_flipkart_min_bg_uniformity_pct"]
        if width < min_edge or height < min_edge:
            failures.append(f"image {width}x{height} below minimum {min_edge}x{min_edge}")
        elif width < recommended_edge or height < recommended_edge:
            warnings.append(f"image {width}x{height} below recommended {recommended_edge}x{recommended_edge}")
        if checks["bg_uniformity_pct"] < min_uniformity:
            failures.append(f"background not solid: {checks['bg_uniformity_pct']}% uniform < required {min_uniformity}%")

    return {"pass": len(failures) == 0, "image": image_path, "channel": channel,
            "checks": checks, "failures": failures, "warnings": warnings}


def cmd_check(args):
    result = run_check(args.image, args.channel)
    print(json.dumps(result, indent=2))
    return 0 if result["pass"] else 1


def _make_test_image(path, size, bg_color, product_color, margin_frac):
    img = Image.new("RGB", size, bg_color)
    draw = ImageDraw.Draw(img)
    w, h = size
    mx, my = round(w * margin_frac), round(h * margin_frac)
    draw.rectangle([mx, my, w - mx, h - my], fill=product_color)
    img.save(path)


def cmd_selfcheck(_args):
    with tempfile.TemporaryDirectory() as tmp:
        compliant_path = os.path.join(tmp, "compliant.png")
        failing_path = os.path.join(tmp, "failing.png")
        _make_test_image(compliant_path, (2000, 2000), (255, 255, 255), (200, 30, 30), margin_frac=0.035)
        _make_test_image(failing_path, (300, 300), (128, 128, 128), (200, 30, 30), margin_frac=0.45)

        defaults = load_defaults()
        compliant = run_check(compliant_path, "amazon", defaults=defaults)
        failing = run_check(failing_path, "amazon", defaults=defaults)
        compliant_fk = run_check(compliant_path, "flipkart", defaults=defaults)
        failing_fk = run_check(failing_path, "flipkart", defaults=defaults)

        assert compliant["pass"] is True, f"expected compliant image to pass amazon: {compliant}"
        assert failing["pass"] is False, f"expected failing image to fail amazon: {failing}"
        assert compliant_fk["pass"] is True, f"expected compliant image to pass flipkart: {compliant_fk}"
        assert failing_fk["pass"] is False, f"expected failing image to fail flipkart: {failing_fk}"

        assert compliant["checks"]["longest_edge_px"] > failing["checks"]["longest_edge_px"]
        assert compliant["checks"]["frame_occupancy_pct"] > failing["checks"]["frame_occupancy_pct"]
        assert compliant["checks"]["bg_white_pct"] > failing["checks"]["bg_white_pct"]
        assert compliant["checks"]["frame_occupancy_pct"] >= defaults["image_amazon_min_occupancy_pct"]
        assert failing["checks"]["longest_edge_px"] < defaults["image_amazon_min_edge_px"]

    print("selfcheck ok")
    return 0


def main():
    parser = argparse.ArgumentParser(prog="image_check.py")
    sub = parser.add_subparsers(dest="command", required=True)

    check_p = sub.add_parser("check")
    check_p.add_argument("--image", required=True)
    check_p.add_argument("--channel", required=True, choices=["amazon", "flipkart"])
    check_p.set_defaults(func=cmd_check)

    selfcheck_p = sub.add_parser("selfcheck")
    selfcheck_p.set_defaults(func=cmd_selfcheck)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
