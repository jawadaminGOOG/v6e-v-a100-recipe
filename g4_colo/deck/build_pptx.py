#!/usr/bin/env python3
"""Build a 16:9 .pptx for Google Slides from the deck's static key frames.

    /tmp/pptx-venv/bin/python g4_colo/deck/build_pptx.py [out.pptx]      # needs python-pptx

One slide per HTML slide: keyframes/slide-N.png (2x, final static frame) as a full-bleed picture, the slide's speaker
notes (index.html <aside class="notes">, sources included) in the notes pane, and the slide-2 footnote as a real
hyperlink text box over the image. Run export_frames.py first.
"""
import html
import json
import re
import sys
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Emu, Pt

DECK = Path(__file__).resolve().parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else DECK / "deck.pptx"
TITLE = "Maximizing CPU Utilization on GCP GPU and TPU machines with GKE"
LINK = "https://medium.com/google-cloud/the-idle-half-of-a-gpu-pod-8ec3ffaddc4c"
W, H = Emu(12192000), Emu(6858000)  # 13.333 x 7.5 in
PX = W / 1920  # EMU per deck pixel


def deck_data():
    t = (DECK / "js" / "deck_data.js").read_text()
    return json.loads(t[t.index("=") + 1:].rstrip().rstrip(";"))


DD = deck_data()
CFG = DD.get("kpi", {}).get("cfg", {})
GUIDE = (DD.get("pricing") or {}).get("guidance") or {}


def fill(body):
    """Same substitutions deck.js makes at load time, so notes match the slides after a regenerate."""
    body = re.sub(r'<span data-c="(\w+)">[^<]*</span>', lambda m: str(CFG.get(m.group(1), m.group(0))), body)

    def g(m):
        host, f = m.group(1).split(".")
        v = ((GUIDE.get(host) or {}).get("3-yr CUD") or {}).get("measured", {}).get(f)
        if v is None:
            return m.group(0)
        return f"${v:,.0f}" if f == "usd_mo" else f"{v:g} %" if f == "pct_host" else f"{v:g}"
    body = re.sub(r'<span data-g="([\w.]+)">[^<]*</span>', g, body)

    def k(m):
        h, f = m.group(1).split(".")
        v = (DD.get("kpi", {}).get(h) or {}).get(f)
        if v is None:
            return m.group(0)
        if isinstance(v, (int, float)):
            return f"{v:,.1f}" if "ready_pct" in f and float(v) != int(v) else f"{v:,.0f}"
        return str(v)
    return re.sub(r'<span data-k="([\w.]+)">[^<]*</span>', k, body)


def notes_text(section):
    m = re.search(r'<aside class="notes">(.*?)</aside>', section, re.S)
    if not m:
        return ""
    body = fill(m.group(1))
    items = re.findall(r"<li>(.*?)</li>", body, re.S) or [body]
    out = []
    for it in items:
        t = re.sub(r"<[^>]+>", "", it)
        out.append("• " + " ".join(html.unescape(t).split()))
    return "\n".join(out)


def main():
    src = (DECK / "index.html").read_text()
    sections = re.findall(r'<section class="slide".*?</section>', src, re.S)
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H
    prs.core_properties.title = TITLE
    blank = prs.slide_layouts[6]
    for i, sec in enumerate(sections, 1):
        img = DECK / "keyframes" / f"slide-{i}.png"
        s = prs.slides.add_slide(blank)
        s.shapes.add_picture(str(img), 0, 0, width=W, height=H)
        label = re.search(r'aria-label="([^"]+)"', sec)
        s.name = label.group(1) if label else f"Slide {i}"
        if 'medium.com/google-cloud/the-idle-half' in sec:
            # real hyperlink over the footnote line (deck px: x 128, y 952, h 28)
            tb = s.shapes.add_textbox(int(128 * PX), int(951 * PX), int(900 * PX), int(30 * PX))
            tb.fill.solid()
            tb.fill.fore_color.rgb = RGBColor(0xFF, 0xFD, 0xF7)
            tf = tb.text_frame
            tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
            p = tf.paragraphs[0]
            r1 = p.add_run()
            r1.text = "¹ "
            r1.font.size = Pt(9)
            r1.font.color.rgb = RGBColor(0x11, 0x13, 0x14)
            r2 = p.add_run()
            r2.text = LINK.replace("https://", "")
            r2.font.size = Pt(9)
            r2.font.color.rgb = RGBColor(0x11, 0x13, 0x14)
            r2.hyperlink.address = LINK
        s.notes_slide.notes_text_frame.text = notes_text(sec)
    prs.save(OUT)
    print("wrote", OUT, len(sections), "slides")


if __name__ == "__main__":
    main()
