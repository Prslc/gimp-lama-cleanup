#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the lama-cleanup authors
"""GIMP 3 plug-in: send the current image plus a mask to a lama-cleaner server
and write the inpainted result back into the current layer.

Target environment
------------------
* GIMP 3.2 (developed and tested against the Flatpak build ``org.gimp.GIMP``,
  which bundles Python 3 and PyGObject)
* lama-cleaner reachable over HTTP, by default ``http://127.0.0.1:8080``.
  How that server is installed and run is up to the user; this plug-in only
  talks to it.

Menu registration
-----------------
GIMP 3.2 only accepts these menu roots::

    <Image> <Layers> <Channels> <Paths> <Colormap> <Brushes> <Dynamics>
    <MyPaintBrushes> <Gradients> <Palettes> <Patterns> <ToolPresets>
    <Fonts> <Buffers>

``<Toolbox>`` is gone, so a plug-in can neither add an entry to the toolbox
menu nor add a real toolbox tool (that would need a ``GimpTool`` implemented in
C inside GIMP itself).  This plug-in therefore registers under
``<Image>/Filters`` and ``<Image>/Tools`` and ships a dialog-free "Quick
Cleanup" action that can be bound to a keyboard shortcut.

Server API contract
-------------------
lama-cleaner 1.2.5 exposes the inpainting endpoint at the *root* path
(``POST /inpaint``, not ``/api/v1/inpaint``) and reads every parameter with
``request.form[...]`` / ``request.files[...]``.  A missing key raises
``BadRequestKeyError``, which the server reports as a bare HTTP 400, so the
plug-in deliberately sends the complete field set that the web frontend sends.
The mask must be a same-size image where *white marks the area to erase*.
"""

import contextlib
import json
import os
import shutil
import sys
import tempfile
import traceback
import urllib.error
import urllib.request
import uuid

import gi

gi.require_version("Gegl", "0.4")
gi.require_version("Gimp", "3.0")
from gi.repository import Gegl, Gimp, Gio, GLib, GObject  # noqa: E402

PLUGIN_ID = "lama-cleanup"
PLUGIN_VERSION = "1.0.0"

PROC_MAIN = "python-fu-lama-cleanup"
PROC_QUICK = "python-fu-lama-cleanup-quick"
PROC_CHECK = "python-fu-lama-check-server"

MENU_FILTERS = "<Image>/Filters/Lama Cleanup"
MENU_TOOLS = "<Image>/Tools/Lama Cleanup"

SETTINGS_BASENAME = "lama-cleanup.json"

# Defaults mirror the lama-cleaner web UI so that both frontends behave the same.
DEFAULTS = {
    "server-url": "http://127.0.0.1:8080",
    "hd-strategy": "Crop",
    "crop-margin": 196,
    "crop-trigger-size": 800,
    "resize-limit": 2048,
    "mask-source": "auto",
    "invert-mask": False,
    "grow-mask": 0,
    "result-mode": "inplace",
    "timeout": 300,
    "keep-temp": False,
}

HD_STRATEGIES = ["Original", "Crop", "Resize"]
MASK_SOURCES = ["auto", "selection", "alpha"]
RESULT_MODES = ["inplace", "new-layer"]

# Every field the web frontend posts to /inpaint.  Values match its defaults.
INPAINT_FIELDS = {
    "ldmSteps": 25,
    "ldmSampler": "plms",
    "zitsWireframe": "true",
    "prompt": "",
    "negativePrompt": "",
    "croperX": 0,
    "croperY": 0,
    "croperHeight": 0,
    "croperWidth": 0,
    "useCroper": "false",
    "sdMaskBlur": 5,
    "sdStrength": 0.75,
    "sdSteps": 50,
    "sdGuidanceScale": 7.5,
    "sdSampler": "uni_pc",
    "sdSeed": 42,
    "sdMatchHistograms": "false",
    "sdScale": 1,
    "cv2Radius": 5,
    "cv2Flag": "INPAINT_NS",
    "paintByExampleSteps": 50,
    "paintByExampleGuidanceScale": 7.5,
    "paintByExampleSeed": 42,
    "paintByExampleMaskBlur": 5,
    "paintByExampleMatchHistograms": "false",
    "p2pSteps": 50,
    "p2pImageGuidanceScale": 1.5,
    "p2pGuidanceScale": 7.5,
    "controlnet_conditioning_scale": 0.4,
    "controlnet_method": "control_v11p_sd15_canny",
}


class LamaError(Exception):
    """An error that is safe and useful to show to the user."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _log(message):
    """Write a diagnostic line to stderr.

    Plug-in stderr ends up in the gimp-console output and, in the GUI, in
    ``Windows > Dockable Dialogs > Error Console``.  Logging must never be able
    to take the plug-in down, hence the guard.
    """
    with contextlib.suppress(Exception):
        sys.stderr.write(f"[lama-cleanup] {message}\n")
        sys.stderr.flush()


def _cfg(config, name, default=None):
    """Read a ProcedureConfig property, tolerating ``-`` and ``_`` spellings."""
    for key in (name, name.replace("-", "_")):
        try:
            return config.get_property(key)
        except Exception:
            continue
    return default


def _cfg_choice(config, name, options, default):
    """Read a choice argument.

    GIMP 3 stores the choice *nick* as a string; the integer-id fallback keeps
    this working if a future version exposes the id instead.
    """
    value = _cfg(config, name, default)
    if isinstance(value, str) and value in options:
        return value
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < len(options):
        return options[value]
    return default


def _settings_path():
    """Path of the settings file inside the GIMP user directory."""
    try:
        base = Gimp.directory()
    except Exception:
        base = None
    if not base:
        base = os.path.join(GLib.get_user_config_dir(), "GIMP")
    return os.path.join(base, SETTINGS_BASENAME)


def _load_settings():
    path = _settings_path()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            merged = dict(DEFAULTS)
            merged.update({k: v for k, v in data.items() if k in DEFAULTS})
            return merged
    except FileNotFoundError:
        pass
    except Exception as exc:
        # A corrupt settings file must not make the plug-in unusable.
        _log(f"could not read settings: {exc}")
    return dict(DEFAULTS)


def _save_settings(values):
    path = _settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(values, handle, indent=2, ensure_ascii=False)
        _log(f"saved settings to {path}")
    except Exception as exc:
        _log(f"could not save settings: {exc}")


def _collect_settings(config):
    return {
        "server-url": str(_cfg(config, "server-url", DEFAULTS["server-url"]) or "").strip(),
        "hd-strategy": _cfg_choice(config, "hd-strategy", HD_STRATEGIES, DEFAULTS["hd-strategy"]),
        "crop-margin": int(_cfg(config, "crop-margin", DEFAULTS["crop-margin"]) or 0),
        "crop-trigger-size": int(_cfg(config, "crop-trigger-size", DEFAULTS["crop-trigger-size"]) or 0),
        "resize-limit": int(_cfg(config, "resize-limit", DEFAULTS["resize-limit"]) or 0),
        "mask-source": _cfg_choice(config, "mask-source", MASK_SOURCES, DEFAULTS["mask-source"]),
        "invert-mask": bool(_cfg(config, "invert-mask", DEFAULTS["invert-mask"])),
        "grow-mask": int(_cfg(config, "grow-mask", DEFAULTS["grow-mask"]) or 0),
        "result-mode": _cfg_choice(config, "result-mode", RESULT_MODES, DEFAULTS["result-mode"]),
        "timeout": int(_cfg(config, "timeout", DEFAULTS["timeout"]) or 300),
        "keep-temp": bool(_cfg(config, "keep-temp", DEFAULTS["keep-temp"])),
    }


# ---------------------------------------------------------------------------
# HTTP: talk to lama-cleaner
# ---------------------------------------------------------------------------

def _multipart(fields, files):
    """Encode a multipart/form-data body without pulling in third-party deps."""
    boundary = f"----GimpLamaCleanup{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += str(value).encode() + b"\r\n"
    for name, (filename, payload, content_type) in files.items():
        body += f"--{boundary}\r\n".encode()
        body += (
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        ).encode()
        body += f"Content-Type: {content_type}\r\n\r\n".encode()
        body += payload + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _inpaint_fields(opts):
    fields = dict(INPAINT_FIELDS)
    fields.update(
        {
            "hdStrategy": opts["hd-strategy"],
            "hdStrategyCropMargin": opts["crop-margin"],
            "hdStrategyCropTrigerSize": opts["crop-trigger-size"],
            "hdStrategyResizeLimit": opts["resize-limit"],
        }
    )
    return fields


def _post_inpaint(server_url, image_bytes, mask_bytes, opts):
    """POST the image and mask, return the raw PNG returned by the server."""
    url = server_url.rstrip("/") + "/inpaint"
    body, content_type = _multipart(
        _inpaint_fields(opts),
        {
            "image": ("image.png", image_bytes, "image/png"),
            "mask": ("mask.png", mask_bytes, "image/png"),
        },
    )
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    request.add_header("Accept", "image/png, application/json")
    request.add_header("User-Agent", f"gimp-lama-cleanup/{PLUGIN_VERSION}")
    timeout = max(5, int(opts.get("timeout", 300)))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            media_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            seed = response.headers.get("X-Seed")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400].decode("utf-8", "replace").strip()
        raise LamaError(
            f"lama-cleaner answered HTTP {exc.code}.\n"
            f"Server response: {detail or '(empty)'}\n"
            f"Check the server version and that the server URL is right: {url}"
        ) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise LamaError(
                f"Request timed out after {timeout}s. "
                f"Raise 'Timeout' in the dialog for large images on CPU."
            ) from None
        raise LamaError(
            f"Cannot reach lama-cleaner: {exc.reason}\n"
            f"URL: {url}\n"
            f"Make sure your lama-cleaner server is running and that "
            f"'Server URL' points at it."
        ) from None
    except TimeoutError:
        raise LamaError(
            f"Request timed out after {timeout}s. "
            f"Raise 'Timeout' in the dialog for large images on CPU."
        ) from None

    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        snippet = payload[:400].decode("utf-8", "replace").strip()
        raise LamaError(
            f"The server did not return a PNG (Content-Type={media_type}).\n{snippet}"
        )
    _log(f"received {len(payload)} bytes, seed={seed}")
    return payload


def _server_info(server_url, timeout=10):
    """Probe the server.  Returns ``(ok, description)``."""
    base = server_url.rstrip("/")
    try:
        with urllib.request.urlopen(base + "/model", timeout=timeout) as response:
            model = response.read().decode("utf-8", "replace").strip()
    except Exception as exc:
        return False, f"cannot connect to {base}: {exc}"
    detail = f"model: {model or 'unknown'}"
    with contextlib.suppress(Exception):
        with urllib.request.urlopen(base + "/server_config", timeout=timeout) as response:
            config = json.loads(response.read().decode("utf-8", "replace"))
        plugins = ", ".join(config.get("plugins") or []) or "none"
        detail += f", plugins: {plugins}"
    return True, detail


# ---------------------------------------------------------------------------
# GIMP side: export the source, build the mask, write the result back
# ---------------------------------------------------------------------------

def _pick_drawable(image, drawables):
    """Pick the pixel layer to work on.

    Only real pixel layers qualify: a ``GroupLayer`` is also a ``Layer``
    subclass, but ``add_alpha``/``edit_paste`` on one either fails or does
    something surprising, so group layers are skipped.
    """
    candidates = [d for d in (drawables or []) if d is not None]
    if not candidates:
        candidates = list(image.get_selected_drawables() or [])
    if not candidates:
        candidates = list(image.get_selected_layers() or [])
    if not candidates:
        candidates = list(image.get_layers() or [])
    for drawable in candidates:
        if drawable.is_layer() and not drawable.is_group_layer():
            return drawable
    # The selection may hold nothing but group layers; fall back to the whole
    # layer stack instead of failing with a misleading "no usable layer".
    for drawable in list(image.get_layers() or []):
        if drawable.is_layer() and not drawable.is_group_layer():
            return drawable
    return None


def _export_flat_png(image, workdir):
    """Flatten the visible layers into a PNG without touching the user's image.

    ``merge_visible_layers`` is used on purpose instead of ``flatten()``:
    ``flatten()`` composites transparent areas onto GIMP's *background colour*
    and drops the alpha channel, so the picture sent to the server no longer
    matches the original file the web UI would upload.  Measured on a UI asset
    with transparency that was a 20% pixel difference (transparent pixels turned
    from black to white), which changes the inpainting result.  Merging the
    visible layers keeps both the alpha channel and the RGB values underneath
    transparent pixels; measured pixel-identical to the source file (AE=0).
    """
    duplicate = image.duplicate()
    try:
        try:
            merged = duplicate.merge_visible_layers(Gimp.MergeType.EXPAND_AS_NECESSARY)
        except Exception as exc:
            # e.g. every layer is hidden; flatten at least produces something
            _log(f"merge_visible_layers failed ({exc}), falling back to flatten")
            try:
                merged = duplicate.flatten()
            except Exception as inner:
                raise LamaError(
                    f"Cannot export the image: no visible layer to merge ({inner})."
                ) from None
        if merged is None:
            raise LamaError("Cannot export the image: no visible layer.")
        path = os.path.join(workdir, "image.png")
        if not Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, duplicate, Gio.File.new_for_path(path)):
            raise LamaError("Failed to export the source image.")
        return path
    finally:
        duplicate.delete()


def _select_mask_region(image, drawable, opts):
    """Turn the current selection into the "area to repair" mask.

    This rewrites ``image``'s selection; the caller is responsible for saving
    and restoring it.
    """
    source = opts["mask-source"]
    has_selection = not Gimp.Selection.is_empty(image)

    if source == "selection" and not has_selection:
        raise LamaError(
            "There is no selection. Mark the area you want to erase with any "
            "select tool, or set Mask source to 'Layer alpha'."
        )

    if source == "auto" and not has_selection:
        if drawable is None or not drawable.has_alpha():
            raise LamaError(
                "The image has no selection and the active layer has no alpha "
                "channel, so the mask cannot be inferred.\n"
                "Create a selection (recommended), or add an alpha channel and "
                "erase the area to a transparent hole."
            )
        source = "alpha"

    if source == "alpha":
        if drawable is None or not drawable.has_alpha():
            raise LamaError("The active layer has no alpha channel to build a mask from.")
        Gimp.Selection.none(image)
        if not image.select_item(Gimp.ChannelOps.REPLACE, drawable):
            raise LamaError("Failed to derive a selection from the layer alpha.")
        # "Select from alpha" also selects fully transparent pixels of the layer
        # bounds; nothing to correct here, the mask is used as-is.

    grow = int(opts.get("grow-mask", 0) or 0)
    if grow > 0:
        Gimp.Selection.grow(image, grow)
    elif grow < 0:
        Gimp.Selection.shrink(image, -grow)
    if opts.get("invert-mask"):
        Gimp.Selection.invert(image)
    return source


def _selection_bbox(image):
    """Return the selection as ``(x, y, w, h)``, or ``None`` if there is none.

    ``Gimp.Selection.bounds()`` returns ``(success, non_empty, x1, y1, x2, y2)``
    on 3.2.6; the shorter shape is accepted too so a different GIMP build does
    not silently break the geometry.
    """
    try:
        bounds = Gimp.Selection.bounds(image)
    except Exception:
        return None
    values = list(bounds) if isinstance(bounds, (tuple, list)) else [bounds]
    if len(values) >= 6:
        non_empty, x1, y1, x2, y2 = values[1:6]
    elif len(values) == 5:
        non_empty, x1, y1, x2, y2 = values[0:5]
    else:
        return None
    try:
        if not non_empty or x2 <= x1 or y2 <= y1:
            return None
        return int(x1), int(y1), int(x2 - x1), int(y2 - y1)
    except Exception:
        return None


def _mask_png_from_selection(image, workdir):
    """Render the current selection as a black/white mask PNG.

    White marks the area to erase, matching lama-cleaner's convention.

    The clipboard is deliberately avoided here: ``edit_copy`` copies only the
    selected pixels when a selection exists, and the offset computed when
    pasting it back is wrong.  Measured with a selection at (265, 50) the white
    block of the exported mask landed at (155, 47), so the server repaired the
    wrong region.  Filling a whole layer black and then filling only the
    selection white relies on absolute coordinates only.
    """
    width, height = image.get_width(), image.get_height()
    duplicate = image.duplicate()  # duplicate() preserves the selection
    try:
        # The layer type has to match the image base type or insert_layer
        # refuses it (and a later export can even crash the file-png plug-in).
        base = duplicate.get_base_type()
        if base == Gimp.ImageBaseType.GRAY:
            layer_type = Gimp.ImageType.GRAYA_IMAGE
        else:
            if base == Gimp.ImageBaseType.INDEXED and not duplicate.convert_rgb():
                raise LamaError("Cannot convert the indexed image to RGB to build a mask.")
            layer_type = Gimp.ImageType.RGBA_IMAGE

        mask_layer = Gimp.Layer.new(
            duplicate, "lama-mask", width, height,
            layer_type, 100.0, Gimp.LayerMode.NORMAL,
        )
        if mask_layer is None:
            raise LamaError("Failed to create the mask layer.")
        if not duplicate.insert_layer(mask_layer, None, 0):
            raise LamaError("Failed to add the mask layer to the image.")
        # Drop the original layers, keeping the mask (insert first, then remove,
        # so the image always holds at least one layer).
        for layer in list(duplicate.get_layers() or []):
            if layer.get_id() != mask_layer.get_id():
                duplicate.remove_layer(layer)

        Gimp.context_push()
        try:
            # drawable.fill() covers the whole drawable and ignores the selection
            Gimp.context_set_background(Gegl.Color.new("black"))
            mask_layer.fill(Gimp.FillType.BACKGROUND)
            # edit_fill() only fills the selection
            Gimp.context_set_foreground(Gegl.Color.new("white"))
            mask_layer.edit_fill(Gimp.FillType.FOREGROUND)
        finally:
            Gimp.context_pop()

        path = os.path.join(workdir, "mask.png")
        if not Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, duplicate, Gio.File.new_for_path(path)):
            raise LamaError("Failed to export the mask.")
        return path
    finally:
        duplicate.delete()


def _insert_result(image, drawable, png_path, opts, mask_channel, mask_bounds):
    """Write the repaired pixels back, limited to the masked area.

    Alignment notes (learned the hard way): GIMP 3's
    ``edit_paste(paste_into=True)`` does *not* paste at the content's original
    coordinates, it aligns to the top-left corner of the selection.  Copying and
    pasting the *same* rectangle (``mask_bounds``) makes the result correct no
    matter which rule GIMP applies.

    Moving pixels between images has no clean alternative either: GIMP 3 refuses
    to attach a layer that already belongs to an image
    ("has already been added to an image"), ``remove_layer`` destroys the layer
    instead of detaching it, and ``file-png-load`` has no "load into an existing
    image" parameter.  The clipboard is therefore the only way.
    """
    result = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(png_path))
    if result is None:
        raise LamaError("Cannot read back the image returned by lama-cleaner.")
    try:
        # Do not flatten a single-layer image: flatten drops alpha, and if the
        # server returns a PNG with transparency the transparent parts would be
        # composited onto the background colour, skewing the colours we paste.
        layers = list(result.get_layers() or [])
        if not layers:
            raise LamaError("The image returned by the server has no layer.")
        layer = result.flatten() if len(layers) > 1 else layers[0]

        # Sizes must match: mask_bounds is expressed in the source image's
        # coordinates, so a differently sized response would silently paste to
        # the wrong place.  Failing loudly is much better.
        expected = (image.get_width(), image.get_height())
        actual = (result.get_width(), result.get_height())
        if actual != expected:
            raise LamaError(
                f"The server returned a {actual[0]}x{actual[1]} image but the source "
                f"is {expected[0]}x{expected[1]}; aborting to avoid pasting to the "
                f"wrong position."
            )

        mode = opts.get("result-mode", "inplace")
        target = drawable

        if mode == "new-layer" and drawable is not None:
            # Duplicate the current layer first; only the copy gets modified.
            # gimp-edit-copy copies just the selected pixels when a selection is
            # active, so the selection must be cleared first or the copy would be
            # transparent outside of it.
            before_ids = {item.get_id() for item in (image.get_layers() or [])}
            Gimp.Selection.none(image)
            Gimp.edit_copy([drawable])
            floating = Gimp.edit_paste(drawable, False)
            if not floating:
                raise LamaError("Failed to duplicate the current layer.")
            Gimp.floating_sel_to_layer(floating[0])
            target = None
            for item in (image.get_layers() or []):
                if item.get_id() not in before_ids:
                    target = item
                    break
            if target is None:
                raise LamaError("Failed to create the new layer.")
            with contextlib.suppress(Exception):
                target.set_name("Lama Cleanup")

        if target is None:
            raise LamaError("The image has no usable layer.")

        # The result may contain transparent pixels (when the masked area had
        # some), and anchoring onto a layer without alpha would push them to the
        # background colour.  Adding alpha only adds a channel, it does not
        # change the layer content.
        with contextlib.suppress(Exception):
            if not target.has_alpha():
                target.add_alpha()

        if mask_channel is None or mask_bounds is None:
            raise LamaError("Internal error: no mask selection; aborting to avoid "
                            "modifying pixels outside it.")

        x, y, width, height = mask_bounds

        # 1) Copy the same rectangle from the result (copy region == paste
        #    region, so the alignment is correct by construction).
        if not result.select_rectangle(Gimp.ChannelOps.REPLACE, x, y, width, height):
            raise LamaError(f"Cannot locate the masked area {mask_bounds} in the response.")
        if not Gimp.edit_copy([layer]):
            raise LamaError("Failed to copy the repaired pixels.")

        # 2) Restore the mask selection on the target and paste into it.
        if not image.select_item(Gimp.ChannelOps.REPLACE, mask_channel):
            raise LamaError("Failed to restore the mask selection.")
        floating = Gimp.edit_paste(target, True)
        if not floating:
            raise LamaError("Failed to paste the repaired pixels into the selection.")
        Gimp.floating_sel_anchor(floating[0])
        return True
    finally:
        result.delete()


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def _run_cleanup(run_mode, image, drawables, config):
    if image is None:
        raise LamaError("There is no image to process.")

    opts = _collect_settings(config)
    if not opts["server-url"]:
        raise LamaError("Server URL must not be empty.")
    _log(f"options: {json.dumps(opts, ensure_ascii=False)}")

    drawable = _pick_drawable(image, drawables)
    if drawable is None:
        raise LamaError("The image has no usable pixel layer.")

    workdir = tempfile.mkdtemp(prefix="lama-cleanup-")
    keep = bool(opts.get("keep-temp"))
    user_channel = None
    mask_channel = None
    mask_bounds = None
    grouped = False
    selection_touched = False
    try:
        # 1) Back up the user's selection first.  This has to happen before
        #    anything that can raise (progress_init, undo_group_start), otherwise
        #    the restore in `finally` would see user_channel == None and clear a
        #    selection the user had set.
        try:
            if not Gimp.Selection.is_empty(image):
                user_channel = Gimp.Selection.save(image)
        except Exception as exc:
            _log(f"could not back up the selection: {exc}")

        Gimp.progress_init("Lama Cleanup…")
        Gimp.progress_update(0.05)
        grouped = bool(image.undo_group_start())

        # 2) Build the mask selection on the image and keep it as a channel.
        #    Both the mask PNG and the paste-back region come from this one
        #    channel, so what the server repairs and what gets overwritten can
        #    never drift apart.
        selection_touched = True
        used = _select_mask_region(image, drawable, opts)
        mask_bounds = _selection_bbox(image)
        if mask_bounds is None:
            raise LamaError("The mask is empty: mark the area to erase with a select tool.")
        mask_channel = Gimp.Selection.save(image)
        if mask_channel is None:
            raise LamaError("Failed to save the selection as a channel.")

        # 3) Export the source image (visible layers merged, original untouched)
        image_path = _export_flat_png(image, workdir)
        Gimp.progress_update(0.2)

        # 4) Export the black/white mask
        mask_path = _mask_png_from_selection(image, workdir)
        _log(f"mask source={used} bbox={mask_bounds} -> {mask_path}")

        with open(image_path, "rb") as handle:
            image_bytes = handle.read()
        with open(mask_path, "rb") as handle:
            mask_bytes = handle.read()
        _log(
            f"uploading image={len(image_bytes)}B mask={len(mask_bytes)}B "
            f"-> {opts['server-url']}"
        )

        payload = _post_inpaint(opts["server-url"], image_bytes, mask_bytes, opts)
        Gimp.progress_update(0.85)

        result_path = os.path.join(workdir, "result.png")
        with open(result_path, "wb") as handle:
            handle.write(payload)

        # 5) Replace only the pixels inside the selection
        _insert_result(image, drawable, result_path, opts, mask_channel, mask_bounds)

        Gimp.displays_flush()
        Gimp.progress_update(1.0)
        if run_mode != Gimp.RunMode.NONINTERACTIVE:
            Gimp.message("Lama Cleanup finished: only pixels inside the selection were changed.")
    finally:
        # Whatever happened: restore the user's selection, drop the temporary
        # channels, close the undo group and clean up the temp directory.  Each
        # step is guarded on its own so cleanup cannot mask the real failure.
        if selection_touched:
            try:
                if user_channel is not None:
                    image.select_item(Gimp.ChannelOps.REPLACE, user_channel)
                else:
                    Gimp.Selection.none(image)
            except Exception as exc:
                _log(f"could not restore the selection: {exc}")
        for channel in (mask_channel, user_channel):
            if channel is not None:
                with contextlib.suppress(Exception):
                    image.remove_channel(channel)
        if grouped:
            with contextlib.suppress(Exception):
                image.undo_group_end()
        with contextlib.suppress(Exception):
            Gimp.progress_end()
        if keep:
            _log(f"temporary files kept in {workdir}")
        else:
            _rmtree(workdir)


def _rmtree(path):
    with contextlib.suppress(Exception):
        shutil.rmtree(path, ignore_errors=True)


def _interactive(procedure, config):
    """Show the parameter dialog.  Returns True when the user pressed OK."""
    gi.require_version("GimpUi", "3.0")
    from gi.repository import GimpUi

    GimpUi.init(PLUGIN_ID)
    dialog = GimpUi.ProcedureDialog(procedure=procedure, config=config)
    dialog.fill(None)
    try:
        return bool(dialog.run())
    finally:
        dialog.destroy()


def _run_common(procedure, run_mode, image, drawables, config, allow_dialog, save_on_success):
    """Shared entry point for the image procedures.

    ``allow_dialog=False`` procedures (Quick Cleanup) never show a dialog.  This
    cannot be decided from ``run_mode``: clicking any menu entry makes GIMP pass
    ``INTERACTIVE``, so a "quick" action gated on run_mode would pop the dialog
    anyway and defeat its whole purpose.
    """
    try:
        if allow_dialog and run_mode == Gimp.RunMode.INTERACTIVE:
            if not _interactive(procedure, config):
                return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
            if save_on_success:
                values = _collect_settings(config)
                # keep-temp is a debugging switch; persisting it would make every
                # later Quick Cleanup leave temp files behind.
                values["keep-temp"] = False
                _save_settings(values)
        _run_cleanup(run_mode, image, drawables, config)
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
    except LamaError as exc:
        _log(f"error: {exc}")
        Gimp.message(str(exc))
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(exc)))
    except Exception as exc:
        # Last-resort handler so the plug-in never fails silently.
        detail = traceback.format_exc()
        _log(f"unexpected error: {exc}\n{detail}")
        Gimp.message(f"Lama Cleanup internal error: {exc}\nSee the GIMP error console for details.")
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(exc)))


# ---------------------------------------------------------------------------
# Procedure definitions
# ---------------------------------------------------------------------------

def _choice(options):
    choice = Gimp.Choice.new()
    for index, (nick, label, help_text) in enumerate(options):
        choice.add(nick, index, label, help_text)
    return choice


def _add_common_arguments(procedure):
    procedure.add_string_argument(
        "server-url", "Server URL", "lama-cleaner endpoint", DEFAULTS["server-url"],
        GObject.ParamFlags.READWRITE,
    )
    procedure.add_choice_argument(
        "hd-strategy", "HD strategy", "How large images are handled (server-side hdStrategy)",
        _choice([
            ("Original", "Original", "Send the whole image to the model; good below ~2K"),
            ("Crop", "Crop", "Only crop the area around the mask (recommended)"),
            ("Resize", "Resize", "Downscale the longer side, then inpaint"),
        ]),
        DEFAULTS["hd-strategy"], GObject.ParamFlags.READWRITE,
    )
    procedure.add_int_argument(
        "crop-margin", "Crop margin", "Extra pixels kept around the mask in Crop mode",
        0, 8192, DEFAULTS["crop-margin"], GObject.ParamFlags.READWRITE,
    )
    procedure.add_int_argument(
        "crop-trigger-size", "Crop trigger size", "Only use Crop when the image is larger than this",
        0, 100000, DEFAULTS["crop-trigger-size"], GObject.ParamFlags.READWRITE,
    )
    procedure.add_int_argument(
        "resize-limit", "Resize limit", "Target size of the longer side in Resize mode",
        0, 100000, DEFAULTS["resize-limit"], GObject.ParamFlags.READWRITE,
    )
    procedure.add_choice_argument(
        "mask-source", "Mask source", "Where the mask comes from",
        _choice([
            ("auto", "Auto", "Use the selection, or the layer alpha when there is none"),
            ("selection", "Selection", "Use the selection only; fail when there is none"),
            ("alpha", "Layer alpha", "Erase the transparent areas of the layer"),
        ]),
        DEFAULTS["mask-source"], GObject.ParamFlags.READWRITE,
    )
    procedure.add_int_argument(
        "grow-mask", "Grow mask", "Grow (positive) or shrink (negative) the mask, in pixels",
        -256, 256, DEFAULTS["grow-mask"], GObject.ParamFlags.READWRITE,
    )
    procedure.add_boolean_argument(
        "invert-mask", "Invert mask", "Invert the mask",
        DEFAULTS["invert-mask"], GObject.ParamFlags.READWRITE,
    )
    procedure.add_choice_argument(
        "result-mode", "Result", "How the result is written back (both only touch the selection)",
        _choice([
            ("inplace", "In-place (current layer)", "Modify the current layer directly (recommended)"),
            ("new-layer", "New layer", "Duplicate the current layer first, leave the original alone"),
        ]),
        DEFAULTS["result-mode"], GObject.ParamFlags.READWRITE,
    )
    procedure.add_int_argument(
        "timeout", "Timeout (s)", "HTTP request timeout in seconds",
        5, 3600, DEFAULTS["timeout"], GObject.ParamFlags.READWRITE,
    )
    procedure.add_boolean_argument(
        "keep-temp", "Keep temp files", "Keep the temporary files (debugging)",
        DEFAULTS["keep-temp"], GObject.ParamFlags.READWRITE,
    )


class LamaCleanupPlugIn(Gimp.PlugIn):
    # ---- GIMP plug-in interface -------------------------------------------

    def do_query_procedures(self):
        return [PROC_MAIN, PROC_QUICK, PROC_CHECK]

    def do_set_i18n(self, name):  # noqa: ARG002 - signature fixed by GIMP
        # The vfunc is declared as -> (bool, gettext_domain, catalog_dir),
        # so it has to return a 3-tuple even when there is no translation.
        return False, None, None

    def do_create_procedure(self, name):
        if name == PROC_CHECK:
            return self._create_check()
        if name == PROC_QUICK:
            return self._create_quick()
        if name == PROC_MAIN:
            return self._create_main()
        return None

    # ---- procedures --------------------------------------------------------

    def _create_main(self):
        procedure = Gimp.ImageProcedure.new(
            self, PROC_MAIN, Gimp.PDBProcType.PLUGIN, self.run_main, None
        )
        procedure.set_image_types("RGB*, GRAY*, INDEXED*")
        procedure.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.DRAWABLE)
        procedure.set_menu_label("Lama Cleanup…")
        procedure.add_menu_path(MENU_FILTERS)
        procedure.add_menu_path(MENU_TOOLS)
        procedure.set_documentation(
            "Repair the current image with lama-cleaner",
            "Sends the image and the selection to the lama-cleaner server and writes "
            "the result back into the current layer, changing only pixels inside the "
            "selection.",
            None,
        )
        procedure.set_attribution("gimp-lama-cleanup", "gimp-lama-cleanup", "2026")
        _add_common_arguments(procedure)
        return procedure

    def _create_quick(self):
        procedure = Gimp.ImageProcedure.new(
            self, PROC_QUICK, Gimp.PDBProcType.PLUGIN, self.run_quick, None
        )
        procedure.set_image_types("RGB*, GRAY*, INDEXED*")
        procedure.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.DRAWABLE)
        procedure.set_menu_label("Quick Cleanup (last settings)")
        procedure.add_menu_path(MENU_FILTERS)
        procedure.add_menu_path(MENU_TOOLS)
        procedure.set_documentation(
            "Run one cleanup without any dialog, using the last saved settings",
            "Parameters come from the last confirmed Lama Cleanup dialog.  Meant to be "
            "bound to a keyboard shortcut and used as a one-press tool.",
            None,
        )
        procedure.set_attribution("gimp-lama-cleanup", "gimp-lama-cleanup", "2026")
        _add_common_arguments(procedure)
        return procedure

    def _create_check(self):
        # Must be an ImageProcedure: GIMP 3 only lets procedures carrying the
        # standard <Image> arguments (run-mode/image/drawables) register under
        # the <Image> menu tree.
        procedure = Gimp.ImageProcedure.new(
            self, PROC_CHECK, Gimp.PDBProcType.PLUGIN, self.run_check, None
        )
        procedure.set_image_types("RGB*, GRAY*, INDEXED*")
        procedure.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.NO_DRAWABLES)
        procedure.set_menu_label("Check Server")
        procedure.add_menu_path(MENU_FILTERS)
        procedure.add_menu_path(MENU_TOOLS)
        procedure.set_documentation(
            "Check whether the lama-cleaner server is reachable",
            "Reads /model and /server_config from the server and reports what it found.",
            None,
        )
        procedure.set_attribution("gimp-lama-cleanup", "gimp-lama-cleanup", "2026")
        procedure.add_string_argument(
            "server-url", "Server URL", "lama-cleaner endpoint", DEFAULTS["server-url"],
            GObject.ParamFlags.READWRITE,
        )
        return procedure

    # ---- run callbacks -----------------------------------------------------

    def run_main(self, procedure, run_mode, image, drawables, config, *_rest):
        return _run_common(procedure, run_mode, image, drawables, config, True, True)

    def run_quick(self, procedure, run_mode, image, drawables, config, *_rest):
        # By definition this is the "no dialog" action, and clicking a menu entry
        # makes run_mode INTERACTIVE, so the dialog has to be disabled explicitly.
        if run_mode != Gimp.RunMode.NONINTERACTIVE:
            saved = _load_settings()
            for key, value in saved.items():
                try:
                    config.set_property(key, value)
                except Exception:
                    with contextlib.suppress(Exception):
                        config.set_property(key.replace("-", "_"), value)
        return _run_common(procedure, run_mode, image, drawables, config, False, False)

    def run_check(self, procedure, run_mode, image, drawables, config, *_rest):  # noqa: ARG002
        # A pure check: no dialog, and when invoked from the menu it reuses the
        # URL saved by the main dialog.
        if run_mode != Gimp.RunMode.NONINTERACTIVE:
            url = str(_load_settings().get("server-url") or "").strip()
        else:
            url = str(_cfg(config, "server-url", DEFAULTS["server-url"]) or "").strip()
        if not url:
            url = DEFAULTS["server-url"]
        ok, detail = _server_info(url)
        if ok:
            Gimp.message(f"lama-cleaner is ready.\n{url}\n{detail}")
            return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
        Gimp.message(f"lama-cleaner is not reachable.\n{detail}")
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(detail))


Gimp.main(LamaCleanupPlugIn.__gtype__, sys.argv)
