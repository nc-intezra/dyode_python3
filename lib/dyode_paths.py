#!/usr/bin/env python3
"""Folders DYODE's service writes to, for install.sh (1.6.0).

    python3 lib/dyode_paths.py CONFIG --side in|out|auto

Prints one "role<TAB>path" line per folder (see
dyode_common.runtime_paths).  --side auto works out which box this is from
the addresses on its interfaces: dyode_in.ip means the input box,
dyode_out.ip the output box; if neither (or both) is present it prints the
folders of both sides.  The first line is "side<TAB>in|out|both".
"""

import os
import sys


def load(config_path):
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:0] = [os.path.dirname(os.path.abspath(config_path)),
                    os.path.join(here, "..", "DYODE_v1_full")]
    import dyode_common
    return dyode_common, dyode_common.load_config(config_path)


def detect_side(common, cfg, addresses=None):
    net = cfg.get("network")
    if not net:                       # v2: one folder per side, no addresses
        return "both"
    try:
        local = set((addresses if addresses is not None
                     else common.interface_addresses()).values())
    except (OSError, ImportError, AttributeError):
        local = set()
    is_in, is_out = net["in_ip"] in local, net["out_ip"] in local
    if is_in != is_out:
        return "in" if is_in else "out"
    return "both"


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    side = argv[argv.index("--side") + 1] if "--side" in argv else "auto"
    try:
        common, cfg = load(argv[1])
        if side == "auto":
            side = detect_side(common, cfg)
        print("side\t%s" % side)
        sides = ("in", "out") if side == "both" else (side,)
        seen = set()
        for s in sides:
            for role, path in common.runtime_paths(cfg, s):
                if path not in seen:
                    seen.add(path)
                    print("%s\t%s" % (role, path))
    except Exception as err:            # reported by install.sh
        print("error: %s" % err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
