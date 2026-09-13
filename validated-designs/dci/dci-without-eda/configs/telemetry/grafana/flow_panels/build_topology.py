#!/usr/bin/env python3
# © 2025 Nokia - BSD-3-Clause
#
# Regenerate the Grafana flow-panel topology artifacts from dci-topology.drawio.
#
# Workflow:  edit dci-topology.drawio  ->  run this script  ->  refresh Grafana.
#
# Steps performed:
#   1. Recompute every `mid:` node to the exact midpoint of its two endpoint
#      ports, so each link's two half-edges are collinear (STRAIGHT links).
#   2. Export the .drawio to .svg via the rlespinasse/drawio-desktop-headless
#      container (no local draw.io needed).
#   3. Post-process the SVG into the format the andrewbmchugh-flow-panel plugin
#      needs and that renders on Grafana's dark theme:
#        - data-cell-id="X"  ->  id="cell-X"     (plugin matches id="cell-...")
#        - drop style="...light-dark()..."        (else colors render black)
#        - black -> grey, white label boxes -> none, link strokes thickened to 3
#   4. Inline the SVG into the flow panel's options.svg in dci-topology.json.
#
# Re-run whenever you change the drawio (move/add nodes, etc.).

import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DRAWIO = os.path.join(HERE, "dci-topology.drawio")
SVG = os.path.join(HERE, "dci-topology.svg")
DASHBOARD = os.path.normpath(
    os.path.join(HERE, "..", "dashboards", "dci-topology.json"))
DRAWIO_RENDER_IMAGE = "rlespinasse/drawio-desktop-headless"
PANEL_TYPE = "andrewbmchugh-flow-panel"


def _parse(raw):
    """Return (geom, edges): geom[id]=(parent,x,y,w,h); edges=set((src,tgt))."""
    t = ET.fromstring(raw)
    geom, edges = {}, set()

    def geo(mc):
        g = mc.find("mxGeometry")
        if g is None:
            return None
        f = lambda k: float(g.get(k)) if g.get(k) else 0.0
        return (mc.get("parent"), f("x"), f("y"), f("width"), f("height"))

    for el in t.iter():
        if el.tag == "object":
            mc = el.find("mxCell")
            if mc is None:
                continue
            g = geo(mc)
            if g:
                geom[el.get("id")] = g
            if mc.get("edge") == "1":
                edges.add((mc.get("source"), mc.get("target")))
        elif el.tag == "mxCell":
            if el.get("edge") == "1":
                edges.add((el.get("source"), el.get("target")))
                continue
            idv = el.get("id")
            if idv and idv not in geom:
                g = geo(el)
                if g:
                    geom[idv] = g
    return geom, edges


def _abs_center(idv, geom):
    parent, x, y, w, h = geom[idv]
    p = parent
    while p in geom and p not in ("0", "1"):
        _, px, py, _, _ = geom[p]
        x += px
        y += py
        p = geom[p][0]
    return (x + w / 2.0, y + h / 2.0)


def straighten_midpoints(raw):
    """Move every `mid:` node onto the line between its two ports."""
    geom, edges = _parse(raw)
    mid_ports = {}
    for s, tgt in edges:
        if tgt and s and tgt.startswith("mid:") and s in geom and tgt in geom:
            mid_ports.setdefault(tgt, set()).add(s)
    moved = 0
    for mid, ports in mid_ports.items():
        if len(ports) != 2:
            continue
        pa, pb = list(ports)
        ax, ay = _abs_center(pa, geom)
        bx, by = _abs_center(pb, geom)
        cx, cy = (ax + bx) / 2.0, (ay + by) / 2.0
        parent, _, _, w, h = geom[mid]
        ox = oy = 0.0
        p = parent
        while p in geom and p not in ("0", "1"):
            _, px, py, _, _ = geom[p]
            ox += px
            oy += py
            p = geom[p][0]
        lx, ly = cx - ox - w / 2.0, cy - oy - h / 2.0
        i = raw.find('id="%s"' % mid)
        gs = raw.find("<mxGeometry", i)
        ge = raw.find("/>", gs)
        block = raw[gs:ge]
        block = re.sub(r'\bx="[-\d.]+"', 'x="%.3f"' % lx, block, 1)
        block = re.sub(r'\by="[-\d.]+"', 'y="%.3f"' % ly, block, 1)
        raw = raw[:gs] + block + raw[ge:]
        moved += 1
    return raw, moved


def render_svg():
    # Render with a writable HOME (electron-store needs it). The output file
    # ends up root-owned, so write_text() below removes-then-recreates it.
    subprocess.run(
        ["docker", "run", "--rm", "-e", "HOME=/tmp", "-v", "%s:/d" % HERE,
         "-w", "/d", DRAWIO_RENDER_IMAGE, "-x", "-f", "svg",
         "-o", "/d/dci-topology.svg", "/d/dci-topology.drawio"],
        check=True)


def write_text(path, text):
    """Overwrite even if the existing file is owned by another uid (root)."""
    if os.path.exists(path):
        try:
            os.remove(path)
        except PermissionError:
            subprocess.run(["sudo", "rm", "-f", path], check=False)
    with open(path, "w") as f:
        f.write(text)


def postprocess_svg(svg):
    svg = svg.replace(' data-cell-id="', ' id="cell-')
    svg = re.sub(r'\s+style="[^"]*"', "", svg)
    svg = (svg.replace('stroke="#000000"', 'stroke="#98a2ae"')
              .replace('fill="#000000"', 'fill="#c7d0d9"')
              .replace('fill="#ffffff"', 'fill="none"'))
    svg = svg.replace('stroke-miterlimit="10" pointer-events="stroke"',
                      'stroke-width="3" stroke-miterlimit="10" pointer-events="stroke"')
    return svg


def inline_into_dashboard(svg):
    d = json.load(open(DASHBOARD))
    n = 0
    for p in d.get("panels", []):
        if p.get("type") == PANEL_TYPE:
            p.setdefault("options", {})["svg"] = svg
            n += 1
    json.dump(d, open(DASHBOARD, "w"), indent=2)
    return n


def main():
    raw = open(DRAWIO).read()
    raw, moved = straighten_midpoints(raw)
    open(DRAWIO, "w").write(raw)
    print("1. straightened %d link midpoints" % moved)

    render_svg()
    print("2. rendered %s" % os.path.basename(SVG))

    svg = postprocess_svg(open(SVG).read())
    write_text(SVG, svg)
    cells = svg.count('id="cell-')
    thick = svg.count('stroke-width="3" stroke-miterlimit="10" pointer-events="stroke"')
    print("3. post-processed svg (cells=%d, thick-links=%d, light-dark=%d)"
          % (cells, thick, svg.count("light-dark")))

    panels = inline_into_dashboard(svg)
    json.load(open(DASHBOARD))  # validate
    print("4. inlined into %s (%d flow panel(s)); %d svg bytes"
          % (os.path.basename(DASHBOARD), panels, len(svg)))
    print("Done. Refresh the Grafana 'DCI Fabric Topology' dashboard.")


if __name__ == "__main__":
    sys.exit(main())
