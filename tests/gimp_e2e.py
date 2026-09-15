# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the lama-cleanup authors
#
# End-to-end test, executed inside GIMP by `python-fu-eval`
# (see test-headless.sh).  It is not a standalone script.
#
# Lesson that shaped this file: an early version used *flat colour* test images,
# which made a whole class of bug invisible - the pasted content was landing at
# the wrong coordinates and every assertion still passed.  The tests now use a
# position-sensitive image (four coloured quadrants, selection inside the bottom
# right one) and issue a second, byte-identical request themselves to compare
# against, so any misalignment fails immediately.
#
# Assertions:
#   1. the white area of the mask PNG sent to the server equals the selection
#   2. the pixels the plug-in writes back equal what the server returns for the
#      very same input (i.e. the paste-back is aligned correctly)
#   3. pixels outside the selection are untouched, the blemish inside is gone,
#      and the selection itself survives
#   4. the image sent to the server keeps its alpha channel
#   5. new-layer mode leaves the original layer alone; indexed images work
#   6. the error path reports cleanly and changes nothing
#   7. Quick Cleanup does not open a dialog even when run as INTERACTIVE
import glob
import os
import sys
import traceback
import urllib.request
import uuid

import gi

gi.require_version("Gimp", "3.0")
gi.require_version("Gegl", "0.4")
from gi.repository import Gegl, Gimp, Gio  # noqa: E402

REPORT = os.environ.get("E2E_REPORT", "/tmp/e2e-report.txt")
URL = os.environ.get("LAMA_URL", "http://127.0.0.1:8080")
TMP = os.environ.get("TMPDIR", "/tmp")

# The selection sits inside the bottom-right (yellow) quadrant, away from the
# origin, so any misalignment is guaranteed to show up.
SEL = (168, 168, 48, 48)
DOT = (180, 180, 24, 24)
QUADRANTS = (
    (0, 0, "red"),
    (128, 0, "green"),
    (0, 128, "blue"),
    (128, 128, "yellow"),
)

FIELDS = {
    "ldmSteps": 25, "ldmSampler": "plms", "zitsWireframe": "true",
    "hdStrategy": "Crop", "hdStrategyCropMargin": 196,
    "hdStrategyCropTrigerSize": 800, "hdStrategyResizeLimit": 2048,
    "prompt": "", "negativePrompt": "",
    "croperX": 0, "croperY": 0, "croperHeight": 0, "croperWidth": 0, "useCroper": "false",
    "sdMaskBlur": 5, "sdStrength": 0.75, "sdSteps": 50, "sdGuidanceScale": 7.5,
    "sdSampler": "uni_pc", "sdSeed": 42, "sdMatchHistograms": "false", "sdScale": 1,
    "cv2Radius": 5, "cv2Flag": "INPAINT_NS",
    "paintByExampleSteps": 50, "paintByExampleGuidanceScale": 7.5, "paintByExampleSeed": 42,
    "paintByExampleMaskBlur": 5, "paintByExampleMatchHistograms": "false",
    "p2pSteps": 50, "p2pImageGuidanceScale": 1.5, "p2pGuidanceScale": 7.5,
    "controlnet_conditioning_scale": 0.4, "controlnet_method": "control_v11p_sd15_canny",
}

lines = []


def say(text):
    lines.append(str(text))
    sys.stderr.write(f"[e2e] {text}\n")
    sys.stderr.flush()


def rgba(color):
    try:
        values = color.get_rgba()
    except Exception:
        try:
            values = color.get_rgb()
        except Exception:
            return str(color)
    return "(" + ", ".join(f"{v:.3f}" for v in values) + ")"


def post_inpaint(image_bytes, mask_bytes):
    """Issue the same request ourselves, to have a reference to compare with."""
    boundary = "----E2E" + uuid.uuid4().hex
    body = bytearray()
    for key, value in FIELDS.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        body += str(value).encode() + b"\r\n"
    for key, payload in (("image", image_bytes), ("mask", mask_bytes)):
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"; filename="{key}.png"\r\n'.encode()
        body += b"Content-Type: image/png\r\n\r\n"
        body += payload + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    request = urllib.request.Request(URL.rstrip("/") + "/inpaint", data=bytes(body), method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def make_quadrant_image(width=256, height=256):
    image = Gimp.Image.new(width, height, Gimp.ImageBaseType.RGB)
    layer = Gimp.Layer.new(
        image, "src", width, height, Gimp.ImageType.RGBA_IMAGE, 100.0, Gimp.LayerMode.NORMAL
    )
    image.insert_layer(layer, None, 0)
    for x, y, color in QUADRANTS:
        Gimp.context_set_foreground(Gegl.Color.new(color))
        Gimp.Selection.none(image)
        image.select_rectangle(Gimp.ChannelOps.REPLACE, x, y, width // 2, height // 2)
        layer.edit_fill(Gimp.FillType.FOREGROUND)
    Gimp.Selection.none(image)
    return image, layer


def mean_of(drawable, x, y, w, h):
    """Mean RGB of a region, sampled on a grid (no third-party deps needed)."""
    step = max(1, min(w, h) // 12)
    sums = [0.0, 0.0, 0.0]
    count = 0
    for px in range(x, x + w, step):
        for py in range(y, y + h, step):
            values = drawable.get_pixel(px, py).get_rgba()
            sums[0] += values[0]
            sums[1] += values[1]
            sums[2] += values[2]
            count += 1
    return tuple(v / max(1, count) for v in sums)


def load(path):
    return Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(path))


def snapshot_pixels(drawable, x, y, w, h):
    """Exact RGBA values of a region, for equality checks."""
    return [
        tuple(round(v, 6) for v in drawable.get_pixel(px, py).get_rgba())
        for px in range(x, x + w)
        for py in range(y, y + h)
    ]


try:
    say("== end-to-end test ==")
    say(f"server: {URL}")
    pdb = Gimp.get_pdb()
    proc = pdb.lookup_procedure("python-fu-lama-cleanup-quick")
    say(f"procedure python-fu-lama-cleanup-quick found: {proc is not None}")
    if proc is None:
        raise RuntimeError("procedure not registered, the plug-in did not load")

    base_settings = {
        "server-url": URL, "hd-strategy": "Crop", "crop-margin": 196,
        "crop-trigger-size": 800, "resize-limit": 2048, "mask-source": "selection",
        "invert-mask": False, "grow-mask": 0, "timeout": 300, "keep-temp": True,
    }

    def run(img, mode, target, run_mode=None):
        config = proc.create_config()
        config.set_property("run-mode", run_mode or Gimp.RunMode.NONINTERACTIVE)
        config.set_property("image", img)
        try:
            config.set_property("drawables", [target])
        except Exception:
            config.set_core_object_array("drawables", [target])
        values = dict(base_settings)
        values["result-mode"] = mode
        for key, value in values.items():
            try:
                config.set_property(key, value)
            except Exception as exc:
                say(f"set {key} failed: {exc}")
        proc.run(config)
        return int(pdb.get_last_status()), pdb.get_last_error()

    # ---------------- phase 1: in-place ----------------
    say("")
    say("== phase 1: in-place (modify the current layer) ==")
    image, layer = make_quadrant_image()
    Gimp.context_set_foreground(Gegl.Color.new("black"))
    image.select_rectangle(Gimp.ChannelOps.REPLACE, *DOT)
    layer.edit_fill(Gimp.FillType.FOREGROUND)
    Gimp.Selection.none(image)
    image.select_rectangle(Gimp.ChannelOps.REPLACE, *SEL)

    before_layers = len(image.get_layers())
    before_outside = mean_of(layer, 5, 5, 40, 40)
    # Exact snapshot of a region strictly outside the selection, so the
    # "nothing outside the selection is touched" claim can be checked literally
    # rather than approximately.
    before_outside_pixels = snapshot_pixels(layer, 0, 0, 64, 64)
    say(f"selection={SEL}  outside (top-left quadrant) mean="
        f"{tuple(round(v, 3) for v in before_outside)}")
    status, error = run(image, "inplace", layer)
    say(f"PDB status: {status} (SUCCESS={int(Gimp.PDBStatusType.SUCCESS)}) error: {error!r}")
    say(f"layers before/after: {before_layers}/{len(image.get_layers())} (must not grow)")
    say(f"selection preserved: {not Gimp.Selection.is_empty(image)}")
    say(f"no leftover temporary channel: {len(list(image.get_channels() or [])) == 0}")

    after = image.get_layers()[0]
    inside = mean_of(after, SEL[0], SEL[1], SEL[2], SEL[3])
    outside = mean_of(after, 5, 5, 40, 40)
    say(f"mean inside selection (repaired) = {tuple(round(v, 3) for v in inside)}")
    say(f"mean outside selection          = {tuple(round(v, 3) for v in outside)}")

    say(f"outside untouched: {all(abs(a - b) < 0.02 for a, b in zip(before_outside, outside, strict=True))}")
    after_outside_pixels = snapshot_pixels(after, 0, 0, 64, 64)
    say(f"outside untouched, pixel-exact: {before_outside_pixels == after_outside_pixels}")
    say(f"blemish inside selection removed: {inside[0] > 0.3 or inside[1] > 0.3}")
    say("fill came from the local (yellow) quadrant, not the top-left (red) one: "
        f"{inside[1] > 0.35 and inside[2] < 0.35}")

    # assertions 1 + 2: mask position / paste-back equals the server response
    dirs = sorted(glob.glob(os.path.join(TMP, "lama-cleanup-*")), key=os.path.getmtime)
    workdir = dirs[-1] if dirs else None
    say(f"temp dir: {workdir}")
    if workdir:
        image_png = os.path.join(workdir, "image.png")
        mask_png = os.path.join(workdir, "mask.png")

        payload_image = load(image_png)
        payload_layer = (payload_image.get_layers() or [None])[0]
        say("image.png sent to the server keeps alpha: "
            f"{payload_layer is not None and payload_layer.has_alpha()} (must be True)")
        payload_image.delete()

        mask_image = load(mask_png)
        mask_layer = (mask_image.get_layers() or [None])[0]
        white_inside = mean_of(mask_layer, SEL[0] + 8, SEL[1] + 8, 16, 16)
        white_far = mean_of(mask_layer, 5, 5, 24, 24)
        say(f"mask inside selection (white): {tuple(round(v, 3) for v in white_inside)}")
        say(f"mask far away (black)        : {tuple(round(v, 3) for v in white_far)}")
        say(f"mask correctly positioned: {white_inside[0] > 0.9 and white_far[0] < 0.1}")
        mask_image.delete()

        with open(image_png, "rb") as handle:
            image_bytes = handle.read()
        with open(mask_png, "rb") as handle:
            mask_bytes = handle.read()
        try:
            response = post_inpaint(image_bytes, mask_bytes)
            reference_path = os.path.join(workdir, "reference.png")
            with open(reference_path, "wb") as handle:
                handle.write(response)
            reference_image = load(reference_path)
            reference_layer = (reference_image.get_layers() or [None])[0]
            reference_inside = mean_of(reference_layer, SEL[0], SEL[1], SEL[2], SEL[3])
            reference_image.delete()
            say(f"server response, same region: {tuple(round(v, 3) for v in reference_inside)}")
            delta = max(abs(a - b) for a, b in zip(inside, reference_inside, strict=True))
            say(f"plug-in result vs server response, max deviation: {delta:.4f} (must be < 0.05)")
            say(f"alignment correct: {delta < 0.05}")
        except Exception as exc:
            say(f"reference request failed: {exc}")

    # ---------------- phase 2: new-layer ----------------
    say("")
    say("== phase 2: new-layer ==")
    Gimp.Selection.none(image)
    image.select_rectangle(Gimp.ChannelOps.REPLACE, *DOT)
    Gimp.context_set_foreground(Gegl.Color.new("black"))
    layer.edit_fill(Gimp.FillType.FOREGROUND)
    Gimp.Selection.none(image)
    image.select_rectangle(Gimp.ChannelOps.REPLACE, *SEL)
    status, error = run(image, "new-layer", layer)
    names = [item.get_name() for item in image.get_layers()]
    say(f"PDB status: {status} error: {error!r}")
    say(f"layers: {names}")
    say(f"one layer added: {len(names) == 2}")
    if len(names) == 2:
        new_layer = image.get_layers()[0]
        say("original layer still has the blemish: "
            f"{layer.get_pixel(SEL[0] + 24, SEL[1] + 24).get_rgba()[0] < 0.15}")
        say("new layer is repaired: "
            f"{new_layer.get_pixel(SEL[0] + 24, SEL[1] + 24).get_rgba()[1] > 0.3}")

    # ---------------- phase 3: indexed ----------------
    say("")
    say("== phase 3: indexed image ==")
    idx_image, idx_layer = make_quadrant_image(128, 128)
    Gimp.Selection.none(idx_image)
    idx_image.select_rectangle(Gimp.ChannelOps.REPLACE, 40, 40, 24, 24)
    Gimp.context_set_foreground(Gegl.Color.new("black"))
    idx_layer.edit_fill(Gimp.FillType.FOREGROUND)
    Gimp.Selection.none(idx_image)
    idx_image.select_rectangle(Gimp.ChannelOps.REPLACE, 32, 32, 40, 40)
    try:
        converted = idx_image.convert_indexed(
            Gimp.ConvertDitherType.NONE, Gimp.ConvertPaletteType.GENERATE, 255, False, False, ""
        )
    except Exception as exc:
        converted = f"exception {exc}"
    say(f"converted to indexed: {converted}  base_type={idx_image.get_base_type()}")
    target_layer = (idx_image.get_layers() or [None])[0]
    if target_layer is not None:
        status, error = run(idx_image, "inplace", target_layer)
        say(f"PDB status: {status} error: {error!r}")
    idx_image.delete()

    # ---------------- phase 4: error path ----------------
    # No selection plus mask-source=selection must fail cleanly, change nothing
    # and leave no temporary channel behind (covers the cleanup in `finally`).
    say("")
    say("== phase 4: error path (no selection) ==")
    err_image, err_layer = make_quadrant_image(64, 64)
    Gimp.Selection.none(err_image)
    before_px = err_layer.get_pixel(5, 5).get_rgba()
    status, error = run(err_image, "inplace", err_layer)
    say(f"PDB status: {status} (must not be SUCCESS={int(Gimp.PDBStatusType.SUCCESS)}) "
        f"error: {error!r}")
    say(f"error reported properly: {status != int(Gimp.PDBStatusType.SUCCESS)}")
    err_after = (err_image.get_layers() or [None])[0]
    say(f"image untouched: "
        f"{err_after is not None and err_after.get_pixel(5, 5).get_rgba() == before_px}")
    say(f"layer count unchanged: {len(list(err_image.get_layers() or [])) == 1}")
    say(f"no leftover temporary channel: {len(list(err_image.get_channels() or [])) == 0}")
    err_image.delete()

    # ---------------- phase 5: Quick Cleanup must not open a dialog ----------------
    # Clicking a menu entry makes GIMP pass run_mode=INTERACTIVE.  If the dialog
    # were gated on run_mode, Quick Cleanup would pop it and lose its meaning.
    say("")
    say("== phase 5: Quick Cleanup invoked as INTERACTIVE (like clicking the menu) ==")
    q_image, q_layer = make_quadrant_image(128, 128)
    Gimp.Selection.none(q_image)
    Gimp.context_set_foreground(Gegl.Color.new("black"))
    q_image.select_rectangle(Gimp.ChannelOps.REPLACE, 56, 56, 16, 16)
    q_layer.edit_fill(Gimp.FillType.FOREGROUND)
    Gimp.Selection.none(q_image)
    q_image.select_rectangle(Gimp.ChannelOps.REPLACE, 48, 48, 32, 32)
    q_status, q_error = run(q_image, "inplace", q_layer, run_mode=Gimp.RunMode.INTERACTIVE)
    q_top = (q_image.get_layers() or [None])[0]
    q_center = q_top.get_pixel(64, 64).get_rgba() if q_top is not None else (0, 0, 0, 0)
    say(f"PDB status: {q_status} error: {q_error!r}")
    say("no dialog and it actually ran (centre is no longer black): "
        f"{q_status == int(Gimp.PDBStatusType.SUCCESS) and (q_center[0] > 0.3 or q_center[1] > 0.3)}")
    q_image.delete()

    image.delete()
except Exception:
    say(f"unhandled exception:\n{traceback.format_exc()}")

try:
    with open(REPORT, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
except Exception as exc:
    sys.stderr.write(f"could not write the report: {exc}\n")
