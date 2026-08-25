"""
Minimal pure-stdlib PDF writer for the signed instructor contracts.

Produces a US-Letter, Helvetica, multi-page PDF from plain text, with the
drawn signature (a PNG data URL from the signing pad) embedded as an image.
No third-party libraries: Render runs the stdlib only.
"""
import zlib, struct, base64, re

PAGE_W, PAGE_H = 612, 792          # US Letter, points
MARGIN = 54
FONT_SIZE = 10.5
LEADING = 14
CHARS_PER_LINE = 92


# ---------------------------------------------------------------- PNG decode ----
def _decode_png(data):
    """(w, h, rgb_bytes) composited over white. Handles 8-bit RGB/RGBA PNGs,
    which is what a canvas toDataURL always produces."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w = h = ct = None
    idat = b""
    pos = 8
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos+4])[0]
        tag = data[pos+4:pos+8]
        chunk = data[pos+8:pos+8+ln]
        if tag == b"IHDR":
            w, h, bd, ct = struct.unpack(">IIBB", chunk[:10])
            if bd != 8 or ct not in (2, 6):
                raise ValueError("unsupported PNG")
        elif tag == b"IDAT":
            idat += chunk
        pos += 12 + ln
    ch = 4 if ct == 6 else 3
    raw = zlib.decompress(idat)
    stride = w * ch
    out = bytearray(w * h * 3)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        f = raw[p]; p += 1
        line = bytearray(raw[p:p+stride]); p += stride
        if f == 1:
            for i in range(ch, stride): line[i] = (line[i] + line[i-ch]) & 255
        elif f == 2:
            for i in range(stride): line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i-ch] if i >= ch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i-ch] if i >= ch else 0
                b = prev[i]
                cc = prev[i-ch] if i >= ch else 0
                pa, pb, pc = abs(b-cc), abs(a-cc), abs(a+b-2*cc)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else cc)
                line[i] = (line[i] + pr) & 255
        o = y * w * 3
        for x in range(w):
            s = x * ch
            if ch == 4:
                al = line[s+3]
                out[o] = (line[s]*al + 255*(255-al)) // 255
                out[o+1] = (line[s+1]*al + 255*(255-al)) // 255
                out[o+2] = (line[s+2]*al + 255*(255-al)) // 255
            else:
                out[o], out[o+1], out[o+2] = line[s], line[s+1], line[s+2]
            o += 3
        prev = line
    return w, h, bytes(out)


# ------------------------------------------------------------------- helpers ----
def _wrap(text):
    lines = []
    for para in (text or "").replace("\r\n", "\n").split("\n"):
        if not para:
            lines.append("")
            continue
        cur = ""
        for word in para.split(" "):
            trial = (cur + " " + word).strip()
            if len(trial) <= CHARS_PER_LINE:
                cur = trial
            else:
                if cur: lines.append(cur)
                while len(word) > CHARS_PER_LINE:   # never loop on a monster token
                    lines.append(word[:CHARS_PER_LINE]); word = word[CHARS_PER_LINE:]
                cur = word
        lines.append(cur)
    return lines

def _txt(s):
    """cp1252-encode (so en dashes and curly quotes survive) and escape for PDF."""
    b = s.encode("cp1252", "replace")
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


# --------------------------------------------------------------------- build ----
def contract_pdf(text, sig_data_url=None, footer_lines=None):
    """The signed contract as PDF bytes: the contract text, then the signed-by
    block, then the drawn signature image if there is one."""
    body_lines = _wrap(text)
    tail = footer_lines or []

    sig = None
    if sig_data_url:
        m = re.match(r"^data:image/png;base64,(.*)$", sig_data_url, re.S)
        if m:
            try:
                sig = _decode_png(base64.b64decode(m.group(1)))
            except Exception:
                sig = None

    # Paginate: body first, then a divider, the footer block and the signature.
    pages = []          # each: (lines, place_signature_after_lines_bool)
    per_page = int((PAGE_H - 2*MARGIN) / LEADING)
    all_lines = body_lines + [""] + ["-"*60] + tail
    i = 0
    while i < len(all_lines):
        pages.append(all_lines[i:i+per_page])
        i += per_page
    sig_h_pt = 0
    if sig:
        w, h, _ = sig
        sig_w_pt = min(240.0, float(w))
        sig_h_pt = sig_w_pt * h / w
        # does the signature fit under the last page's text? if not, new page
        used = len(pages[-1]) * LEADING
        if used + sig_h_pt + 30 > PAGE_H - 2*MARGIN:
            pages.append([])

    # ---- objects
    objs = {}           # num -> bytes (without "N 0 obj" wrapper)
    font_num = 1
    objs[font_num] = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                      b"/Encoding /WinAnsiEncoding >>")
    img_num = None
    if sig:
        w, h, rgb = sig
        comp = zlib.compress(rgb, 9)
        img_num = 2
        objs[img_num] = (f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                         f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
                         f"/Length {len(comp)} >>\nstream\n".encode() + comp + b"\nendstream")

    next_num = 3
    page_nums, content_nums = [], []
    for pi, lines in enumerate(pages):
        stream = bytearray()
        y = PAGE_H - MARGIN
        for ln in lines:
            if ln.strip():
                stream += b"BT /F1 %.1f Tf %d %.1f Td (" % (FONT_SIZE, MARGIN, y)
                stream += _txt(ln) + b") Tj ET\n"
            y -= LEADING
        if sig and pi == len(pages) - 1:
            w, h, _ = sig
            sig_w_pt = min(240.0, float(w))
            sh = sig_w_pt * h / w
            iy = y - sh - 6
            stream += (f"q {sig_w_pt:.1f} 0 0 {sh:.1f} {MARGIN} {iy:.1f} cm /Sig Do Q\n").encode()
        comp = zlib.compress(bytes(stream), 9)
        cnum = next_num; next_num += 1
        objs[cnum] = (f"<< /Filter /FlateDecode /Length {len(comp)} >>\nstream\n".encode()
                      + comp + b"\nendstream")
        content_nums.append(cnum)
        page_nums.append(next_num); next_num += 1

    pages_num = next_num; next_num += 1
    catalog_num = next_num; next_num += 1

    for idx, pnum in enumerate(page_nums):
        res = f"/Font << /F1 {font_num} 0 R >>"
        if img_num:
            res += f" /XObject << /Sig {img_num} 0 R >>"
        objs[pnum] = (f"<< /Type /Page /Parent {pages_num} 0 R "
                      f"/MediaBox [0 0 {PAGE_W} {PAGE_H}] /Resources << {res} >> "
                      f"/Contents {content_nums[idx]} 0 R >>").encode()
    kids = " ".join(f"{n} 0 R" for n in page_nums)
    objs[pages_num] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_nums)} >>".encode()
    objs[catalog_num] = f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode()

    # ---- assemble with xref
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        body = objs[num] if isinstance(objs[num], bytes) else objs[num].encode()
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    maxnum = max(objs)
    out += f"xref\n0 {maxnum+1}\n".encode()
    out += b"0000000000 65535 f \n"
    for n in range(1, maxnum+1):
        out += (f"{offsets.get(n,0):010d} 00000 n \n").encode()
    out += (f"trailer\n<< /Size {maxnum+1} /Root {catalog_num} 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(out)
