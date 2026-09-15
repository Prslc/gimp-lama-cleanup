<div align="center">

# gimp-lama-cleanup

<img src="images/icon.png" alt="Icon" width="150" height="150"><br>

English | [Chinese](docs/README_CN.md)

</div>

## Overview

A GIMP 3 plug-in that drives a [lama-cleaner](https://github.com/Sanster/lama-cleaner)
server from inside the canvas. Select an area, run the plug-in, and the pixels
inside the selection are replaced by a LaMa inpaint of their surroundings —
nothing outside the selection is modified.

No export / upload / download / paste round trip, and no re-encoding of the rest
of the image. The request it sends is pixel-equivalent to what the lama-cleaner
web UI uploads (same pixels, same alpha channel, same parameters), so the result
matches what you would get in the browser.

## Screenshots

| Before — the text to remove | After — only the selection was inpainted |
|-----------------------------------|-------------------------------------|
| ![Before](images/before.png) | ![After](images/after.png) |

## Features

- **In-place by default** — the current layer is modified directly. A
  non-destructive `New layer` mode is available too.
- **Selection-only** — the server response is pasted clipped to your selection,
  so nothing else moves. Verified pixel-by-pixel in the test suite.
- **Alpha-aware** — transparency survives. Sprites, icons and cut-outs behave the
  same as in the web UI.
- **No extra dependencies** — GIMP's bundled Python 3 + PyGObject is enough; no
  `pip install`, no virtualenv.
- **Parameter parity with the web UI** — HD strategy, crop margin, crop trigger,
  mask grow, invert.
- **Dialog-free "Quick Cleanup"** — bind it to a key and the workflow becomes
  *select → press*.
- **Headless test suite** — mask geometry, paste alignment, alpha handling, error
  paths and dialog suppression, all without a GUI.

## Requirements

- **GIMP 3.x** — developed and tested against 3.2.6. GIMP 2.10 is not supported
  (completely different plug-in API).
- **Python 3 + PyGObject** — bundled with GIMP; nothing to install.
  Tested with the Flatpak build, which ships Python 3.13.
- **A lama-cleaner server** — you install and run it yourself; this plug-in only
  talks to it. Default `http://127.0.0.1:8080`, reached over plain HTTP, so it may
  equally live on another machine.

## Quick Start

Prerequisites: GIMP 3 and a lama-cleaner server that is already running and
answering on `http://127.0.0.1:8080` (or wherever you put it — the plug-in has a
**Server URL** parameter).

GIMP 3 requires plug-ins to live in a **subdirectory** of `plug-ins/`; a bare
`.py` file directly inside `plug-ins/` is silently skipped.

```bash
git clone https://github.com/<you>/gimp-lama-cleanup.git
cd gimp-lama-cleanup

mkdir -p ~/.config/GIMP/3.2/plug-ins/lama-cleanup
install -m 0755 lama-cleanup.py \
    ~/.config/GIMP/3.2/plug-ins/lama-cleanup/lama-cleanup.py
```

The path above is the Linux one. In general you want
`<config-root>/GIMP/<version>/plug-ins/lama-cleanup/`, where the config root
depends on how GIMP was installed:

| Install | Config root |
| --- | --- |
| Linux, distro package | `~/.config` (or `$XDG_CONFIG_HOME`) |
| Linux, Flatpak | **the same** `~/.config` — see below |
| macOS | `~/Library/Application Support` |
| Windows | `%APPDATA%` |

**Why Flatpak is not `~/.var/app`.** GIMP's Flatpak manifest grants
`filesystems=xdg-config/GIMP:create`, which bind-mounts the host `~/.config/GIMP`
into the sandbox, so Flatpak's usual `~/.var/app/<app-id>/config` redirection
does not apply to that one directory. `~/.var/app/org.gimp.GIMP/config/GIMP`
does exist, but it stays empty — installing there will not work.

If your GIMP keeps its config somewhere else, ask GIMP itself where it is:

```bash
gimp-console -i --batch-interpreter=python-fu-eval \
    -b 'print(Gimp.directory())' --quit
# Flatpak: prefix the above with
#   flatpak run --command=gimp-console org.gimp.GIMP
```

Then **quit GIMP completely and start it again** — GIMP only scans for plug-ins
at startup. Open any image and run **Filters → Lama Cleanup → Check Server** to
confirm the plug-in can reach your server.

## Usage

| Action | Where |
|--------|-------|
| Parameter dialog, then cleanup | `Filters` / `Tools` → `Lama Cleanup` → **Lama Cleanup…** |
| Run again with the saved settings, no dialog | … → **Quick Cleanup (last settings)** |
| Check the server | … → **Check Server** |

Select the area to erase with any select tool, then run **Lama Cleanup…**. The
selection itself is restored afterwards, so you can refine it and run again
immediately.

To use it as a one-press tool, bind a key to the dialog-free action:

```
Edit → Keyboard Shortcuts → search "lama" → assign e.g. Ctrl+Shift+L
```

`Filters → Repeat Last` (default `Ctrl+F`) re-runs the last filter as well.

### There is no dedicated toolbox tool

GIMP 3.2 accepts exactly these menu roots:

```
<Image> <Layers> <Channels> <Paths> <Colormap> <Brushes> <Dynamics>
<MyPaintBrushes> <Gradients> <Palettes> <Patterns> <ToolPresets> <Fonts> <Buffers>
```

`<Toolbox>` no longer exists — registering there fails with
`invalid menu location "<Toolbox>/..."`. Putting a real tool into the toolbox
icon grid would require implementing a `GimpTool` in C inside GIMP itself, which
the plug-in API does not expose. Hence the `Tools` menu entry plus a
shortcut-bound `Quick Cleanup`, which in practice behaves like a tool.

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| **Server URL** | `http://127.0.0.1:8080` | Where lama-cleaner listens. |
| **HD strategy** | `Crop` | Large-image handling. `Crop` sends only the area around the mask to the model (fast, recommended); `Original` sends the whole image; `Resize` downscales the longer side first. |
| **Crop margin** | `196` | Extra pixels kept around the mask in `Crop` mode. |
| **Crop trigger size** | `800` | `Crop` is only used when the image is larger than this. |
| **Resize limit** | `2048` | Target length of the longer side in `Resize` mode. |
| **Mask source** | `Auto` | `Auto` uses the selection, falling back to the layer alpha when there is none. `Selection` requires a selection; `Layer alpha` erases transparent areas. |
| **Grow mask** | `0` | Grow (positive) or shrink (negative) the mask, in pixels — handy for the halo around an object. |
| **Invert mask** | off | Invert the mask. |
| **Result** | `In-place (current layer)` | `In-place` edits the current layer; `New layer` duplicates it first and edits the copy. Both only touch the selection. |
| **Timeout (s)** | `300` | HTTP timeout; raise it for large images on CPU. |
| **Keep temp files** | off | Keep the temporary PNGs and log the directory (debugging). Never persisted. |

Whatever you select is what gets erased: the plug-in renders the selection as a
black/white mask (white = erase) and sends it along with the image, exactly like
painting in the web UI.

## Configuration

The parameters confirmed in the dialog are saved to
`<gimp-dir>/lama-cleanup.json` (`~/.config/GIMP/3.2/lama-cleanup.json` for the
default profile) and reused by `Quick Cleanup`:

```json
{
  "server-url": "http://127.0.0.1:8080",
  "hd-strategy": "Crop",
  "crop-margin": 196,
  "crop-trigger-size": 800,
  "resize-limit": 2048,
  "mask-source": "auto",
  "invert-mask": false,
  "grow-mask": 0,
  "result-mode": "inplace",
  "timeout": 300,
  "keep-temp": false
}
```

Unknown keys and unknown choice values are ignored, so an older or hand-edited
file cannot break the plug-in. `keep-temp` is never persisted.

## How it works

```
GIMP image (selection = the area to erase)
   ├─ selection ─┬─ mask.png        white = erase, sent to the server
   │             └─ kept as a channel, later used to clip the paste-back
   ├─ duplicate + merge visible layers → image.png   (alpha preserved)
   │
   └─ POST http://127.0.0.1:8080/inpaint   (multipart/form-data)
            ↓
        result.png
            ↓
   copy the same rectangle out of the result,
   paste it into the target layer clipped to the selection
            ↓
   only pixels inside the selection changed
```

The plug-in speaks the lama-cleaner 1.2.5 HTTP API directly: `POST /inpaint`
(note: the *root* path, not `/api/v1/inpaint`) with the complete field set the
web frontend sends. That backend reads parameters with `request.form[...]` and
answers a bare `400 Bad Request` when any one of them is missing, which is why
the field list is long.

## Notes

**The clipboard is used.** With GIMP 3 there is no other way to move pixels
between two images: a layer that already belongs to an image cannot be attached
to another, `remove_layer` destroys instead of detaching, and `file-png-load` has
no "load into an existing image" parameter. The selection is preserved, but
whatever you had copied before is gone.

**Adding an alpha channel.** If the target layer has none, the plug-in adds one —
required to composite the repaired pixels correctly. It changes the layer type
(Background → Layer), not its content.

**Indexed images** work, but the result is quantised to the palette. Convert to
RGB (`Image → Mode → RGB`) first for smooth inpainting.

**The menu is missing after installing.** GIMP only scans at startup; quit it
fully and reopen. A plug-in file placed directly in `plug-ins/` is skipped —
GIMP logs `plug-ins must be installed in subdirectories`.

**"Cannot connect".** Check `curl http://127.0.0.1:8080/model`; a `404` means
something else owns that port. The Flatpak build ships with `--share=network`, so
network access normally just works.

**Check Server is greyed out.** It is registered as an image procedure (GIMP 3
only allows procedures carrying the standard image arguments under the `<Image>`
menu tree), so it needs an open image — any image will do.

**Slow on CPU.** Keep `HD strategy` on `Crop`, lower `Resize limit`, or raise
`Timeout`.

## Development

```
lama-cleanup.py         the plug-in (the only file GIMP loads)
test-headless.sh        entry point for the headless end-to-end test
tests/gimp_e2e.py       the actual test, executed inside GIMP
tests/real_check.py     verifies procedure registration in the real profile
```

Run the tests with the server up:

```bash
./test-headless.sh
LAMA_URL=http://127.0.0.1:8080 ./test-headless.sh   # explicit URL
```

It drives `gimp-console` with its **own** GIMP user directory
(`GIMP3_DIRECTORY`), so your real profile is never touched, and asserts:

| Check | Why it exists |
|-------|---------------|
| mask white area == selection | `edit_copy` copies only the selection and the clipboard paste offset is unreliable; building the mask from absolute coordinates avoids a misaligned mask. |
| pasted pixels == server response | `edit_paste(paste_into=True)` aligns to the selection's **top-left corner**, not to the original coordinates, so the copy rectangle and the paste rectangle must be identical. |
| outside the selection untouched, pixel-exact | The whole point of the plug-in. |
| upload keeps alpha | Flattening first changes the payload, and therefore the result. |
| no leftover channel, selection preserved | Covers the `finally`-block cleanup. |
| error path changes nothing | Same, for the failure case. |
| `Quick Cleanup` as `INTERACTIVE` opens no dialog | Clicking a menu entry makes GIMP pass `INTERACTIVE`; gating the dialog on `run_mode` would defeat the action's purpose. |

The report lands in `e2e-report.txt`, the raw output in `e2e.log`. The fixtures
are deliberately **position-sensitive** (four coloured quadrants, selection away
from the origin) — a flat-colour fixture once hid an alignment bug completely.

Lint:

```bash
pip install ruff
ruff check --target-version py313 \
    --select E,F,W,B,UP,SIM,C4,RET,ARG --ignore E501 .
```

## Related projects

Other GIMP/Krita plug-ins solve neighbouring problems. None of them talks to a
lama-cleaner server, and this plug-in was written independently of them — the
shared shape (export → HTTP → import) is dictated by GIMP's plug-in API and by
the fact that LaMa cannot run inside GIMP's bundled Python.

- **[moebius-gimp](https://github.com/Daniel-Steinberger/moebius-gimp)** — GIMP 3
  client/server inpainting against a self-hosted Moebius server. Closest in
  architecture. It inserts the result as a **new layer** and flattens the export
  (which drops alpha); this plug-in writes back **in place**, preserves alpha,
  and crops to the selection instead of uploading the whole image.
- **[krita-iopaint](https://github.com/chayleaf/krita-iopaint)** — the same idea
  for Krita: talks to IOPaint at `127.0.0.1:8080`, hard-coded. Krita only, no
  dialog, no configuration.
- **[deep_erase](https://github.com/mamipi972/deep_erase)** — GIMP 3 + LaMa, but
  runs ONNX **locally** in a self-managed venv instead of calling a server. Pick
  that one if you would rather not run lama-cleaner at all.

## Credit

- **[lama-cleaner](https://github.com/Sanster/lama-cleaner)** (now IOPaint) by
  Sanster and contributors — the inpainting service this plug-in drives.
- **[GIMP](https://www.gimp.org/)** — its bundled Python 3 + PyGObject make a
  dependency-free plug-in possible.
- **[LaMa](https://arxiv.org/abs/2109.07161)** — Resolution-robust Large Mask
  Inpainting with Fourier Convolutions, the model behind the default server.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE). This matches the GIMP ecosystem's
convention; the plug-in uses GIMP's introspection bindings at runtime.
