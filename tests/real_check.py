# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the lama-cleanup authors
#
# Verify that the plug-in registered correctly in the *real* GIMP user
# directory (~/.config/GIMP/<version>), not the isolated test profile.
#
# Run it through gimp-console, e.g.:
#   XDG_RUNTIME_DIR=... flatpak run --command=gimp-console org.gimp.GIMP -i \
#       --batch-interpreter=python-fu-eval \
#       -b "exec(open('tests/real_check.py').read())" --quit
import sys

import gi

gi.require_version("Gimp", "3.0")
from gi.repository import Gimp  # noqa: E402

EXPECTED = [
    "python-fu-lama-cleanup",
    "python-fu-lama-cleanup-quick",
    "python-fu-lama-check-server",
]

pdb = Gimp.get_pdb()
print(f"[real] GIMP {Gimp.MAJOR_VERSION}.{Gimp.MINOR_VERSION}.{Gimp.MICRO_VERSION}")
print(f"[real] gimp dir = {Gimp.directory()}")
missing = []
for name in EXPECTED:
    procedure = pdb.lookup_procedure(name)
    print(f"[real] {name:<32} -> {procedure is not None}")
    if procedure is None:
        missing.append(name)
        continue
    try:
        args = ", ".join(arg.get_name() for arg in procedure.get_arguments())
        print(f"[real]     arguments: {args}")
    except Exception as exc:
        print(f"[real]     (could not list arguments: {exc})")

print(f"[real] all procedures registered: {not missing}")
sys.stderr.flush()
