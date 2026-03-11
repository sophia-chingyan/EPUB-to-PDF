import io
import os
import zipfile
import re
import posixpath
from flask import Flask, request, send_file, jsonify, render_template_string
from bs4 import BeautifulSoup, NavigableString
from PIL import Image as PILImage
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak,
    HRFlowable, Image as RLImage, KeepTogether
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ── Font Registration ────────────────────────────────────────────────────────
# Use embedded TTF fonts for all CJK scripts so PDFs render universally
# without requiring the viewer to have CID/CMaps installed.
# WenQuanYi Zen Hei covers Traditional Chinese, Simplified Chinese, and Korean.
# IPAGothic covers Japanese (hiragana, katakana, kanji).
pdfmetrics.registerFont(TTFont('WQYZenHei',
    '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc', subfontIndex=0))
pdfmetrics.registerFont(TTFont('IPAGothic',
    '/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf'))

FONT_EN_REGULAR = 'Times-Roman'
FONT_EN_BOLD    = 'Times-Bold'
FONT_EN_ITALIC  = 'Times-Italic'
FONT_TC = 'WQYZenHei'   # Traditional Chinese — embedded TTF, universal rendering
FONT_SC = 'WQYZenHei'   # Simplified Chinese  — same pan-CJK font
FONT_JA = 'IPAGothic'   # Japanese
FONT_KO = 'WQYZenHei'   # Korean — WQY covers Hangul

PAGE_W, PAGE_H = A4
MARGIN_L = MARGIN_R = 3.2 * cm
MARGIN_T = MARGIN_B = 3.0 * cm
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R

# ── Script Detection ─────────────────────────────────────────────────────────
def detect_script(text):
    counts = {'tc': 0, 'sc': 0, 'ja': 0, 'ko': 0}
    for ch in text:
        cp = ord(ch)
        if 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F:
            counts['ko'] += 1
        elif 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF:
            counts['ja'] += 1
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF:
            counts['tc'] += 1
    return 'latin' if sum(counts.values()) == 0 else max(counts, key=counts.get)

def has_non_latin(text):
    return any(ord(c) > 0x024F for c in text)

SCRIPT_FONTS = {'tc': FONT_TC, 'sc': FONT_SC, 'ja': FONT_JA, 'ko': FONT_KO}

# ── Path Helpers ─────────────────────────────────────────────────────────────
def resolve_path(base_dir, href):
    href = href.split('#')[0].split('?')[0]
    if href.startswith('/'):
        return href.lstrip('/')
    if base_dir:
        return posixpath.normpath(posixpath.join(base_dir, href)).lstrip('/')
    return href

# ── Link Helpers ─────────────────────────────────────────────────────────────
def is_external_url(href):
    """Return True if the href is an absolute external URL."""
    return href and (href.startswith('http://') or href.startswith('https://') or href.startswith('mailto:'))

def escape_xml(text):
    """Escape characters that are special in ReportLab's para XML."""
    return (text
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))

def build_linked_text(node, base_url_map=None):
    """
    Walk a BS4 node and return a ReportLab-compatible XML string that
    preserves <a href> as <link href="..."> for external URLs, or as
    plain underlined text for internal anchors.
    """
    parts = []
    for child in node.descendants if hasattr(node, 'descendants') else []:
        # Only process direct-child text and <a> tags at any depth;
        # we'll reconstruct the string manually.
        pass

    # Simpler approach: render the whole node with inline link markup
    def render(n):
        if isinstance(n, NavigableString):
            return escape_xml(str(n))
        tag = (n.name or '').lower()
        if tag == 'a':
            href = (n.get('href') or '').strip()
            text = escape_xml(n.get_text())
            if not text:
                return ''
            if is_external_url(href):
                # External link → clickable blue underlined
                return f'<link href="{escape_xml(href)}" color="blue"><u>{text}</u></link>'
            elif href.startswith('#') or (href and not href.startswith('http')):
                # Internal/chapter link → underlined, no external URL
                return f'<u>{text}</u>'
            else:
                return text
        elif tag in ('b', 'strong'):
            inner = ''.join(render(c) for c in n.children)
            return f'<b>{inner}</b>' if inner else ''
        elif tag in ('i', 'em'):
            inner = ''.join(render(c) for c in n.children)
            return f'<i>{inner}</i>' if inner else ''
        elif tag in ('u',):
            inner = ''.join(render(c) for c in n.children)
            return f'<u>{inner}</u>' if inner else ''
        elif tag == 'br':
            return '<br/>'
        elif tag in ('script', 'style', 'img', 'svg'):
            return ''
        else:
            return ''.join(render(c) for c in n.children)

    return render(node)


# ── EPUB Parsing ─────────────────────────────────────────────────────────────
def parse_epub(epub_bytes):
    chapters, title, author = [], 'Untitled', ''
    image_map = {}

    with zipfile.ZipFile(io.BytesIO(epub_bytes)) as z:
        names_set = set(z.namelist())

        # Load all images eagerly (raster + SVG containers)
        IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif', '.svg'}
        for name in z.namelist():
            if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                try:
                    data = z.read(name)
                    image_map[name] = data
                    # Also index by lowercased path for case-insensitive href matching
                    lname = name.lower()
                    if lname != name:
                        image_map.setdefault(lname, data)
                except Exception:
                    pass

        # container.xml -> OPF
        opf_path = None
        if 'META-INF/container.xml' in names_set:
            soup = BeautifulSoup(
                z.read('META-INF/container.xml').decode('utf-8', errors='replace'), 'lxml-xml')
            rf = soup.find('rootfile')
            if rf:
                opf_path = rf.get('full-path')

        spine_ids, id_to_href, base_dir = [], {}, ''

        if opf_path and opf_path in names_set:
            base_dir = posixpath.dirname(opf_path)
            opf = BeautifulSoup(
                z.read(opf_path).decode('utf-8', errors='replace'), 'lxml-xml')
            t = opf.find('dc:title') or opf.find('title')
            if t: title = t.get_text(strip=True)
            a = opf.find('dc:creator') or opf.find('creator')
            if a: author = a.get_text(strip=True)

            for item in opf.find_all('item'):
                iid  = item.get('id', '')
                href = item.get('href', '')
                mt   = item.get('media-type', '')
                resolved = resolve_path(base_dir, href)
                if 'html' in mt or href.lower().endswith(('.html', '.xhtml', '.htm')):
                    id_to_href[iid] = resolved

            for ir in opf.find_all('itemref'):
                iid = ir.get('idref', '')
                if iid in id_to_href:
                    spine_ids.append(iid)

        if not spine_ids:
            for hf in sorted(n for n in z.namelist()
                             if n.lower().endswith(('.html', '.xhtml', '.htm'))):
                chapters.append(('', hf))
        else:
            for sid in spine_ids:
                path = id_to_href[sid]
                if path in names_set:
                    chapters.append((sid, path))

        parsed = []
        seen = set()
        for cid, path in chapters:
            if path in seen:
                continue
            seen.add(path)
            try:
                html = z.read(path).decode('utf-8', errors='replace')
                chapter_base = posixpath.dirname(path)
                parsed.append(parse_html_chapter(html, chapter_base, image_map))
            except Exception:
                pass

    return title, author, parsed, image_map


def parse_html_chapter(html_content, chapter_base, image_map):
    soup = BeautifulSoup(html_content, 'lxml')
    for tag in soup(['script', 'style', 'nav']):
        tag.decompose()

    body = soup.find('body') or soup
    elements = []

    def css_to_pt(val):
        if not val:
            return None
        val = str(val).strip().lower()
        m = re.match(r'^([\d.]+)(px|pt|em|rem|cm|mm|in|%)?$', val)
        if not m:
            return None
        v, u = float(m.group(1)), (m.group(2) or 'px')
        return {'px':0.75,'pt':1.0,'em':12.0,'rem':12.0,'cm':28.35,'mm':2.835,'in':72.0}.get(u)

    def get_align(node):
        style = node.get('style', '') if hasattr(node, 'get') else ''
        cls   = ' '.join(node.get('class', [])) if hasattr(node, 'get') else ''
        m = re.search(r'text-align\s*:\s*(\w+)', style)
        if m: return m.group(1)
        for kw in ('center','right','left','justify'):
            if kw in cls.lower(): return kw
        return None

    def resolve_src(src):
        if not src:
            return None
        # data: URI — store inline bytes under a synthetic key
        if src.startswith('data:'):
            return src  # handled directly in make_image_flowable
        src = src.split('?')[0].split('#')[0]
        candidates = [
            src,
            resolve_path(chapter_base, src),
            resolve_path(chapter_base, src).lstrip('/'),
        ]
        for candidate in candidates:
            if candidate in image_map:
                return candidate
            # Case-insensitive fallback
            if candidate.lower() in image_map:
                return candidate.lower()
        # Last resort: match by basename (case-insensitive)
        basename = posixpath.basename(src).lower()
        for k in image_map:
            if posixpath.basename(k).lower() == basename:
                return k
        return None

    def node_has_links(node):
        """Check if a node contains any <a href> tags."""
        return bool(node.find('a', href=True))

    def process(node, list_depth=0):
        if isinstance(node, NavigableString):
            t = str(node).strip()
            if t:
                elements.append({'type': 'para', 'text': escape_xml(t), 'align': None, 'rich': False})
            return

        tag = (node.name or '').lower()
        style_attr = node.get('style', '') if hasattr(node, 'get') else ''

        # Page break
        if 'page-break-before: always' in style_attr or 'break-before: page' in style_attr:
            elements.append({'type': 'pagebreak'})

        # Image
        if tag == 'img':
            # Try src, then data-src, then first entry in srcset
            src = node.get('src') or node.get('data-src') or ''
            if not src:
                srcset = node.get('srcset', '')
                if srcset:
                    src = srcset.split(',')[0].strip().split()[0]
            path = resolve_src(src)
            if path:
                w_hint = css_to_pt(node.get('width'))
                h_hint = css_to_pt(node.get('height'))
                if not w_hint:
                    wm = re.search(r'width\s*:\s*([^;]+)', style_attr)
                    if wm: w_hint = css_to_pt(wm.group(1))
                if not h_hint:
                    hm = re.search(r'height\s*:\s*([^;]+)', style_attr)
                    if hm: h_hint = css_to_pt(hm.group(1))
                parent_align = get_align(node.parent) if node.parent else None
                elements.append({'type':'img','path':path,'alt':node.get('alt',''),
                                  'align': parent_align or 'center',
                                  'width_hint': w_hint, 'height_hint': h_hint})
            return

        # SVG container — extract any raster <image> tags inside instead of skipping
        if tag == 'svg':
            for img_node in node.find_all('image'):
                href = (img_node.get('xlink:href') or img_node.get('href')
                        or img_node.get('src') or '')
                path = resolve_src(href)
                if path:
                    w_hint = css_to_pt(img_node.get('width'))
                    h_hint = css_to_pt(img_node.get('height'))
                    parent_align = get_align(node.parent) if node.parent else None
                    elements.append({'type':'img','path':path,'alt':'',
                                     'align': parent_align or 'center',
                                     'width_hint': w_hint, 'height_hint': h_hint})
            return

        if tag == 'figure':
            for child in node.children: process(child, list_depth)
            return

        if tag == 'figcaption':
            t = node.get_text(separator=' ', strip=True)
            if t: elements.append({'type':'caption','text':escape_xml(t), 'rich': False})
            return

        if tag in ('h1','h2','h3','h4','h5','h6'):
            for img in node.find_all('img'): process(img, list_depth)
            # Headings: preserve links inside headings too
            if node_has_links(node):
                rich_text = build_linked_text(node)
                if rich_text.strip():
                    elements.append({'type':'heading','level':int(tag[1]),'text':rich_text,'rich':True})
            else:
                t = node.get_text(separator=' ', strip=True)
                if t: elements.append({'type':'heading','level':int(tag[1]),'text':escape_xml(t),'rich':False})
            return

        if tag == 'p':
            imgs = node.find_all('img')
            for img in imgs: process(img, list_depth)
            for img in imgs: img.decompose()

            if node_has_links(node):
                # Rich path: preserve link markup
                rich_text = build_linked_text(node)
                if rich_text.strip():
                    elements.append({'type':'para','text':rich_text,'align':get_align(node),'rich':True})
            else:
                t = node.get_text(separator=' ', strip=True)
                if t: elements.append({'type':'para','text':escape_xml(t),'align':get_align(node),'rich':False})
            return

        if tag == 'blockquote':
            if node_has_links(node):
                rich_text = build_linked_text(node)
                if rich_text.strip():
                    elements.append({'type':'blockquote','text':rich_text,'rich':True})
            else:
                t = node.get_text(separator=' ', strip=True)
                if t: elements.append({'type':'blockquote','text':escape_xml(t),'rich':False})
            return

        if tag in ('ul','ol'):
            for i, li in enumerate(node.find_all('li', recursive=False)):
                bullet = f'{i+1}.' if tag == 'ol' else '•'
                if node_has_links(li):
                    rich_text = build_linked_text(li)
                    if rich_text.strip():
                        elements.append({'type':'li','text':f'{bullet} {rich_text}','depth':list_depth,'rich':True})
                else:
                    t = li.get_text(separator=' ', strip=True)
                    if t: elements.append({'type':'li','text':escape_xml(f'{bullet} {t}'),'depth':list_depth,'rich':False})
            return

        if tag == 'li':
            if node_has_links(node):
                rich_text = build_linked_text(node)
                if rich_text.strip():
                    elements.append({'type':'li','text':f'• {rich_text}','depth':list_depth,'rich':True})
            else:
                t = node.get_text(separator=' ', strip=True)
                if t: elements.append({'type':'li','text':escape_xml(f'• {t}'),'depth':list_depth,'rich':False})
            return

        if tag == 'hr':
            elements.append({'type':'hr'})
            return

        if tag == 'table':
            for row in node.find_all('tr'):
                cells = [td.get_text(separator=' ', strip=True)
                         for td in row.find_all(['td','th'])]
                line = '  |  '.join(c for c in cells if c)
                if line.strip():
                    elements.append({'type':'para','text':escape_xml(line),'align':None,'rich':False})
            return

        # generic containers
        cls = ' '.join(node.get('class', [])) if hasattr(node, 'get') else ''
        if 'pagebreak' in cls or 'page-break' in cls:
            elements.append({'type':'pagebreak'})
            return

        if tag in ('div','section','article','main','header','footer','body',
                   'span','a','em','strong','i','b','u','small','big',
                   'center','font',None,''):
            for child in node.children:
                process(child, list_depth)
            return

        # fallback
        for img in node.find_all('img'): process(img, list_depth)
        for img in node.find_all('img'): img.decompose()
        t = node.get_text(separator=' ', strip=True)
        if t: elements.append({'type':'para','text':escape_xml(t),'align':None,'rich':False})

    for child in body.children:
        process(child)

    return elements


# ── PDF Building ─────────────────────────────────────────────────────────────
def build_pdf(title, author, chapters, image_map):
    buf = io.BytesIO()

    sample = [(title or ''), (author or '')]
    for ch in chapters[:5]:
        for el in (ch or [])[:80]:
            if el.get('type') in ('para','heading','caption','li','blockquote'):
                sample.append(el.get('text',''))
    dom = detect_script(' '.join(sample))
    is_cjk = dom in ('tc','sc','ja','ko')

    def base_font(variant='regular'):
        if not is_cjk:
            return {'bold':FONT_EN_BOLD,'italic':FONT_EN_ITALIC}.get(variant, FONT_EN_REGULAR)
        return {'tc':FONT_TC,'sc':FONT_SC,'ja':FONT_JA,'ko':FONT_KO}[dom]

    lm = 1.72 if is_cjk else 1.55

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN_L, rightMargin=MARGIN_R,
        topMargin=MARGIN_T,  bottomMargin=MARGIN_B,
        title=title, author=author,
    )

    def ms(name, font, size, **kw):
        leading = kw.pop('leading', round(size * lm))
        return ParagraphStyle(name, fontName=font, fontSize=size, leading=leading, **kw)

    s_title   = ms('Title',  base_font('bold'),   24, textColor=colors.HexColor('#1a1008'), alignment=TA_CENTER, spaceAfter=8)
    s_author  = ms('Author', base_font('italic'), 13, textColor=colors.HexColor('#6b5c44'), alignment=TA_CENTER, spaceAfter=28)
    s_h = [None,
        ms('H1', base_font('bold'),   17, textColor=colors.HexColor('#1a1008'), spaceBefore=20, spaceAfter=9),
        ms('H2', base_font('bold'),   14, textColor=colors.HexColor('#2d1e0e'), spaceBefore=14, spaceAfter=7),
        ms('H3', base_font('italic'), 12, textColor=colors.HexColor('#3d2a10'), spaceBefore=10, spaceAfter=5),
        ms('H4', base_font('italic'), 11, textColor=colors.HexColor('#4a3828'), spaceBefore=8,  spaceAfter=4),
        ms('H5', base_font('italic'), 10, textColor=colors.HexColor('#4a3828'), spaceBefore=6,  spaceAfter=3),
        ms('H6', base_font('italic'), 10, textColor=colors.HexColor('#4a3828'), spaceBefore=6,  spaceAfter=3),
    ]
    s_body    = ms('Body', base_font(), 11, textColor=colors.HexColor('#1a1008'), spaceAfter=5,
                   alignment=TA_JUSTIFY, firstLineIndent=0 if is_cjk else 14)
    s_bq      = ms('BQ', base_font('italic'), 10, textColor=colors.HexColor('#4a3828'),
                   spaceAfter=7, leftIndent=22, rightIndent=22, spaceBefore=4)
    s_li      = ms('Li', base_font(), 11, textColor=colors.HexColor('#1a1008'), spaceAfter=3, leftIndent=16)
    s_caption = ms('Cap', base_font('italic'), 9, textColor=colors.HexColor('#6b5c44'),
                   alignment=TA_CENTER, spaceBefore=2, spaceAfter=10)

    def best_font_for(text, base_style_font):
        # Strip XML tags for script detection
        plain = re.sub(r'<[^>]+>', '', text)
        if not has_non_latin(plain):
            return base_style_font
        sc = detect_script(plain)
        return SCRIPT_FONTS.get(sc, base_style_font)

    def safe_para(text, style, is_rich=False):
        """
        Build a Paragraph.
        - is_rich=True  → text already contains XML markup (links, bold, etc.)
        - is_rich=False → text is plain (already XML-escaped), wrap as-is
        """
        needed = best_font_for(text, style.fontName)
        if needed != style.fontName:
            style = ParagraphStyle(style.name+'_x', parent=style,
                                   fontName=needed, leading=round(style.fontSize * 1.72))
        try:
            return Paragraph(text, style)
        except Exception:
            # Fallback: strip all markup and try plain ASCII
            plain = re.sub(r'<[^>]+>', '', text)
            try:
                return Paragraph(escape_xml(plain), style)
            except Exception:
                try:
                    return Paragraph(plain.encode('ascii','replace').decode(), style)
                except Exception:
                    return Spacer(1, 0)

    def make_image_flowable(el):
        path   = el.get('path','')
        align  = (el.get('align') or 'center').lower()
        w_hint = el.get('width_hint')
        h_hint = el.get('height_hint')

        # ── Resolve raw bytes ────────────────────────────────────────────────
        if path.startswith('data:'):
            # Inline data: URI  e.g. data:image/png;base64,....
            import base64 as _b64
            m = re.match(r'data:image/[^;]+;base64,(.+)', path, re.DOTALL)
            if not m:
                return None
            try:
                img_bytes = _b64.b64decode(m.group(1))
            except Exception:
                return None
        else:
            img_bytes = image_map.get(path)
            if not img_bytes:
                return None

        try:
            # ── SVG: convert to PNG via PIL/resvg fallback ───────────────────
            ext = os.path.splitext(path)[1].lower() if not path.startswith('data:') else ''
            if ext == '.svg' or path.startswith('data:image/svg'):
                # Try to render SVG by extracting any embedded raster href
                try:
                    svg_soup = BeautifulSoup(img_bytes, 'lxml-xml')
                    img_tag = svg_soup.find('image')
                    if img_tag:
                        href = (img_tag.get('xlink:href') or img_tag.get('href') or '')
                        if href.startswith('data:'):
                            import base64 as _b64
                            m2 = re.match(r'data:image/[^;]+;base64,(.+)', href, re.DOTALL)
                            if m2:
                                img_bytes = _b64.b64decode(m2.group(1))
                            else:
                                return None
                        elif href:
                            resolved = resolve_src(href)
                            img_bytes = image_map.get(resolved) if resolved else None
                            if not img_bytes:
                                return None
                        else:
                            return None
                    else:
                        return None  # SVG with no embedded raster
                except Exception:
                    return None

            # ── Open with PIL ────────────────────────────────────────────────
            pil = PILImage.open(io.BytesIO(img_bytes))
            pil.load()  # force decode so errors surface here, not later
            orig_w, orig_h = pil.size
            if orig_w == 0 or orig_h == 0:
                return None

            # ── Normalise to RGB/L PNG for ReportLab ─────────────────────────
            fmt = (pil.format or '').upper()
            if pil.mode == 'RGBA':
                # Preserve transparency by compositing onto white
                bg = PILImage.new('RGB', pil.size, (255, 255, 255))
                bg.paste(pil, mask=pil.split()[3])
                pil = bg
            elif pil.mode == 'LA':
                bg = PILImage.new('L', pil.size, 255)
                bg.paste(pil.split()[0], mask=pil.split()[1])
                pil = bg
            elif pil.mode == 'P':
                # Palette: may have transparency
                pil = pil.convert('RGBA')
                bg = PILImage.new('RGB', pil.size, (255, 255, 255))
                bg.paste(pil, mask=pil.split()[3])
                pil = bg
            elif pil.mode == 'CMYK':
                pil = pil.convert('RGB')
            elif pil.mode == 'YCbCr':
                pil = pil.convert('RGB')
            elif pil.mode == '1':
                pil = pil.convert('L')
            elif pil.mode not in ('RGB', 'L'):
                pil = pil.convert('RGB')

            # Re-encode as PNG (handles WebP, GIF, BMP, TIFF, CMYK JPEG, etc.)
            tmp = io.BytesIO()
            pil.save(tmp, format='PNG')
            img_bytes = tmp.getvalue()
            orig_w, orig_h = pil.size  # update after mode conversion (size unchanged but be safe)

            # ── Scale to fit page ────────────────────────────────────────────
            max_w = CONTENT_W
            max_h = PAGE_H * 0.75

            if w_hint and h_hint:
                draw_w = min(w_hint, max_w)
                draw_h = h_hint * (draw_w / w_hint)
            elif w_hint:
                draw_w = min(w_hint, max_w)
                draw_h = orig_h * (draw_w / orig_w)
            elif h_hint:
                draw_h = min(h_hint, max_h)
                draw_w = min(orig_w * (draw_h / orig_h), max_w)
                draw_h = orig_h * (draw_w / orig_w)
            else:
                scale  = min(max_w / orig_w, max_h / orig_h, 1.0)
                draw_w = orig_w * scale
                draw_h = orig_h * scale

            if draw_h > max_h:
                draw_w *= max_h / draw_h
                draw_h  = max_h

            rl_align = {'left': 'LEFT', 'right': 'RIGHT'}.get(align, 'CENTER')
            return RLImage(io.BytesIO(img_bytes), width=draw_w, height=draw_h, hAlign=rl_align)

        except Exception as exc:
            import traceback; traceback.print_exc()
            return None

    # ── Story assembly ────────────────────────────────────────────────────────
    story = [Spacer(1, 1.8*cm)]
    story.append(safe_para(escape_xml(title or 'Untitled'), s_title))
    if author:
        story.append(safe_para(escape_xml(author), s_author))
    story.append(HRFlowable(width='55%', thickness=1,
                             color=colors.HexColor('#c8882a'), hAlign='CENTER'))
    story.append(PageBreak())

    for chapter_elements in chapters:
        if not chapter_elements:
            continue
        i = 0
        while i < len(chapter_elements):
            el = chapter_elements[i]
            etype = el.get('type','')
            is_rich = el.get('rich', False)

            if etype == 'heading':
                lvl = max(1, min(6, el.get('level', 1)))
                story.append(safe_para(el['text'], s_h[lvl], is_rich))

            elif etype == 'para':
                text = el.get('text','').strip()
                if text:
                    align = el.get('align')
                    if align == 'center':
                        style = ParagraphStyle('bc', parent=s_body, alignment=TA_CENTER, firstLineIndent=0)
                    elif align == 'right':
                        style = ParagraphStyle('br', parent=s_body, alignment=TA_RIGHT, firstLineIndent=0)
                    else:
                        style = s_body
                    story.append(safe_para(text, style, is_rich))

            elif etype == 'blockquote':
                story.append(safe_para(el['text'], s_bq, is_rich))

            elif etype == 'li':
                indent = el.get('depth', 0) * 10
                li_style = ParagraphStyle('lid', parent=s_li, leftIndent=16+indent)
                story.append(safe_para(el['text'], li_style, is_rich))

            elif etype == 'hr':
                story += [Spacer(1,4),
                           HRFlowable(width='35%',thickness=0.5,
                                      color=colors.HexColor('#c8882a'),hAlign='CENTER'),
                           Spacer(1,4)]

            elif etype == 'pagebreak':
                story.append(PageBreak())

            elif etype == 'img':
                img_flow = make_image_flowable(el)
                # Peek for caption
                next_el = chapter_elements[i+1] if i+1 < len(chapter_elements) else None
                has_caption = next_el and next_el.get('type') == 'caption'

                if img_flow:
                    block = [Spacer(1, 8), img_flow, Spacer(1, 4)]
                    if has_caption:
                        block.append(safe_para(next_el['text'], s_caption, next_el.get('rich', False)))
                        i += 1
                    story.append(KeepTogether(block))
                elif has_caption:
                    i += 1  # skip orphaned caption

            elif etype == 'caption':
                story.append(safe_para(el['text'], s_caption, is_rich))

            i += 1

    if not any(chapters):
        story.append(safe_para('No readable content found in this EPUB.', s_body))

    doc.build(story)
    buf.seek(0)
    return buf.read()


# ── Flask App ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

HTML_PAGE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EPUB to PDF Converter</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700;900&family=Source+Serif+4:ital,wght@0,300;0,400;0,600;1,300;1,400&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --ink: #1a1008; --paper: #f5f0e8; --cream: #ede6d6;
    --amber: #c8882a; --amber-light: #e8a84a; --amber-dark: #9a6010;
    --rust: #8b3a12; --shadow: rgba(26,16,8,0.15);
  }
  body {
    background: var(--paper);
    background-image: radial-gradient(ellipse at 20% 20%, rgba(200,136,42,.08) 0%, transparent 50%),
                      radial-gradient(ellipse at 80% 80%, rgba(139,58,18,.06) 0%, transparent 50%);
    color: var(--ink); font-family: 'Source Serif 4', Georgia, serif;
    min-height: 100vh; display: flex; flex-direction: column;
    align-items: center; justify-content: center; padding: 2rem 1rem;
  }
  .masthead { text-align: center; margin-bottom: 2.5rem; }
  .rule { width: 120px; height: 2px; background: linear-gradient(90deg,transparent,var(--amber),transparent); margin: 0 auto 1.2rem; }
  h1 { font-family: 'Playfair Display', serif; font-size: clamp(2.4rem,6vw,4rem); font-weight: 900; line-height: 1.05; letter-spacing: -.02em; }
  h1 span { color: var(--amber); }
  .subtitle { font-size: 1rem; color: #6b5c44; font-style: italic; margin-top: .6rem; }
  .lang-badges { display: flex; flex-wrap: wrap; gap: .4rem; justify-content: center; margin-top: 1rem; }
  .badge { font-size: .72rem; padding: .25rem .6rem; border-radius: 2px; border: 1px solid rgba(200,136,42,.3);
           background: rgba(200,136,42,.07); color: #7a5a20; font-family: monospace; }
  .card { background: white; border-radius: 2px;
    box-shadow: 0 1px 2px var(--shadow), 0 4px 16px var(--shadow), 0 0 0 1px rgba(200,136,42,.12);
    width: 100%; max-width: 560px; overflow: hidden; }
  .card-header { background: var(--ink); padding: 1rem 1.5rem; display: flex; align-items: center; gap: .6rem; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--amber); opacity: .8; }
  .card-header-title { font-family: 'Playfair Display', serif; color: var(--cream); font-size: .85rem; letter-spacing: .12em; text-transform: uppercase; }
  .card-body { padding: 2rem; }
  .drop-zone { border: 2px dashed rgba(200,136,42,.35); border-radius: 2px; padding: 2.5rem 1.5rem;
    text-align: center; cursor: pointer; transition: all .25s; background: var(--paper); position: relative; }
  .drop-zone:hover, .drop-zone.dragover { border-color: var(--amber); background: #fdf6e8; transform: translateY(-1px); box-shadow: 0 4px 12px rgba(200,136,42,.15); }
  .drop-zone input[type="file"] { position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%; }
  .drop-icon { font-size: 2.8rem; line-height: 1; margin-bottom: .8rem; display: block; }
  .drop-label { font-family: 'Playfair Display', serif; font-size: 1.1rem; font-weight: 700; color: var(--ink); margin-bottom: .3rem; }
  .drop-hint { font-size: .82rem; color: #8a7055; font-style: italic; }
  .file-info { display: none; margin-top: 1.2rem; padding: .8rem 1rem; background: #fdf6e8;
    border-left: 3px solid var(--amber); font-size: .88rem; color: var(--ink); }
  .file-info.visible { display: flex; align-items: center; gap: .6rem; }
  .file-name { font-weight: 600; word-break: break-all; }
  .file-size { color: #8a7055; font-style: italic; white-space: nowrap; }
  .divider { height: 1px; background: linear-gradient(90deg,transparent,rgba(200,136,42,.25),transparent); margin: 1.5rem 0; }
  .btn-convert { width: 100%; padding: .95rem; background: var(--ink); color: var(--amber-light);
    font-family: 'Playfair Display', serif; font-size: 1rem; font-weight: 700; letter-spacing: .08em;
    text-transform: uppercase; border: none; border-radius: 2px; cursor: pointer; transition: all .2s; }
  .btn-convert:hover:not(:disabled) { background: #2d1e0e; color: var(--amber); transform: translateY(-1px); box-shadow: 0 4px 12px rgba(26,16,8,.25); }
  .btn-convert:disabled { opacity: .55; cursor: not-allowed; transform: none; }
  .progress-wrap { display: none; margin-top: 1.2rem; }
  .progress-wrap.visible { display: block; }
  .progress-label { font-size: .82rem; color: #6b5c44; font-style: italic; margin-bottom: .5rem; display: flex; justify-content: space-between; }
  .progress-bar-bg { height: 4px; background: var(--cream); border-radius: 2px; overflow: hidden; }
  .progress-bar-fill { height: 100%; background: linear-gradient(90deg,var(--amber-dark),var(--amber));
    border-radius: 2px; width: 0%; transition: width .4s; animation: shimmer 1.5s infinite; }
  @keyframes shimmer { 0%,100%{opacity:1}50%{opacity:.65} }
  .result-box { display: none; margin-top: 1.2rem; padding: 1rem; border-radius: 2px; font-size: .9rem; }
  .result-box.success { display: block; background: #f0faf0; border-left: 3px solid #4a9e5c; color: #2d6a3a; }
  .result-box.error   { display: block; background: #fdf0ee; border-left: 3px solid var(--rust); color: var(--rust); }
  .btn-download { display: inline-flex; align-items: center; gap: .5rem; margin-top: .8rem;
    padding: .6rem 1.2rem; background: var(--amber); color: var(--ink);
    font-family: 'Playfair Display', serif; font-weight: 700; font-size: .88rem;
    text-decoration: none; border-radius: 2px; transition: all .2s; }
  .btn-download:hover { background: var(--amber-dark); color: white; transform: translateY(-1px); }
  .footer-note { margin-top: 2rem; font-size: .78rem; color: #9a8060; font-style: italic; text-align: center; }
  .ornament { color: var(--amber); opacity: .5; margin: 0 .5rem; }
</style>
</head>
<body>
<div class="masthead">
  <div class="rule"></div>
  <h1>EPUB <span>&#8594;</span> PDF</h1>
  <p class="subtitle">Multilingual eBook converter &middot; Images, Layout &amp; Links Preserved</p>
  <div class="lang-badges">
    <span class="badge">&#127468;&#127463; English</span>
    <span class="badge">&#127481;&#127484; &#32321;&#39636;&#20013;&#25991;</span>
    <span class="badge">&#127464;&#127475; &#31616;&#20307;&#20013;&#25991;</span>
    <span class="badge">&#127471;&#127477; &#26085;&#26412;&#35486;</span>
    <span class="badge">&#127472;&#127479; &#54620;&#44397;&#50612;</span>
    <span class="badge">&#128444; Images</span>
    <span class="badge">&#128279; Links</span>
    <span class="badge">&#128208; Layout</span>
  </div>
  <div class="rule" style="margin-top:1.2rem"></div>
</div>

<div class="card">
  <div class="card-header">
    <div class="dot"></div>
    <div class="dot" style="background:#d4a044;opacity:.5"></div>
    <span class="card-header-title">Conversion Studio</span>
  </div>
  <div class="card-body">
    <div class="drop-zone" id="dropZone">
      <input type="file" id="fileInput" accept=".epub">
      <span class="drop-icon">&#128218;</span>
      <div class="drop-label">Drop your EPUB here</div>
      <div class="drop-hint">or click to browse &mdash; up to 100&thinsp;MB</div>
    </div>
    <div class="file-info" id="fileInfo">
      <span>&#128196;</span>
      <span class="file-name" id="fileName"></span>
      <span class="file-size" id="fileSize"></span>
    </div>
    <div class="divider"></div>
    <button class="btn-convert" id="convertBtn" disabled onclick="convertFile()">
      &#10022; &nbsp; Convert to PDF &nbsp; &#10022;
    </button>
    <div class="progress-wrap" id="progressWrap">
      <div class="progress-label">
        <span id="progressText">Processing&hellip;</span>
        <span id="progressPct">0%</span>
      </div>
      <div class="progress-bar-bg">
        <div class="progress-bar-fill" id="progressFill"></div>
      </div>
    </div>
    <div class="result-box" id="resultBox"></div>
  </div>
</div>

<p class="footer-note">
  <span class="ornament">&#10022;</span>
  Auto-detects language &middot; Preserves images, layout &amp; hyperlinks &middot; No data retained
  <span class="ornament">&#10022;</span>
</p>

<script>
const fileInput=document.getElementById('fileInput'),dropZone=document.getElementById('dropZone'),
  fileInfo=document.getElementById('fileInfo'),fileName=document.getElementById('fileName'),
  fileSize=document.getElementById('fileSize'),convertBtn=document.getElementById('convertBtn'),
  progressWrap=document.getElementById('progressWrap'),progressFill=document.getElementById('progressFill'),
  progressText=document.getElementById('progressText'),progressPct=document.getElementById('progressPct'),
  resultBox=document.getElementById('resultBox');
let selectedFile=null;
function fmtSize(b){return b<1024?b+' B':b<1048576?(b/1024).toFixed(1)+' KB':(b/1048576).toFixed(1)+' MB';}
function setFile(f){
  if(!f||!f.name.toLowerCase().endsWith('.epub')){alert('Please select a valid .epub file.');return;}
  selectedFile=f;fileName.textContent=f.name;fileSize.textContent=fmtSize(f.size);
  fileInfo.classList.add('visible');convertBtn.disabled=false;
  resultBox.className='result-box';resultBox.innerHTML='';
}
fileInput.addEventListener('change',e=>setFile(e.target.files[0]));
dropZone.addEventListener('dragover',e=>{e.preventDefault();dropZone.classList.add('dragover');});
dropZone.addEventListener('dragleave',()=>dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop',e=>{e.preventDefault();dropZone.classList.remove('dragover');setFile(e.dataTransfer.files[0]);});
function animProg(to,dur){
  const from=parseFloat(progressFill.style.width)||0,t0=performance.now();
  (function step(now){const t=Math.min((now-t0)/dur,1),v=from+(to-from)*t;
    progressFill.style.width=v+'%';progressPct.textContent=Math.round(v)+'%';
    if(t<1)requestAnimationFrame(step);})(t0);
}
async function convertFile(){
  if(!selectedFile)return;
  convertBtn.disabled=true;progressWrap.classList.add('visible');
  resultBox.className='result-box';resultBox.innerHTML='';
  progressFill.style.width='0%';progressText.textContent='Uploading\u2026';animProg(28,900);
  const fd=new FormData();fd.append('file',selectedFile);
  try{
    progressText.textContent='Parsing EPUB \u0026 images\u2026';animProg(55,1200);
    const res=await fetch('/convert',{method:'POST',body:fd});
    progressText.textContent='Rendering PDF\u2026';animProg(88,800);
    if(!res.ok){const e=await res.json();throw new Error(e.error||'Conversion failed');}
    const blob=await res.blob();animProg(100,300);progressText.textContent='Done!';
    const url=URL.createObjectURL(blob);
    const base=selectedFile.name.replace(/\.epub$/i,'');
    resultBox.className='result-box success';
    resultBox.innerHTML='<strong>&#10003; Conversion successful!</strong><br>Images, layout &amp; links preserved.<br>'
      +'<a class="btn-download" href="'+url+'" download="'+base+'.pdf">'
      +'&#8659;&nbsp;&nbsp;Download PDF</a>';
  }catch(err){
    animProg(100,200);progressText.textContent='Error';
    resultBox.className='result-box error';
    resultBox.innerHTML='<strong>&#10007; Error:</strong> '+err.message;
  }finally{convertBtn.disabled=false;}
}
</script>
</body>
</html>'''


@app.route('/')
def index():
    return render_template_string(HTML_PAGE)


@app.route('/convert', methods=['POST'])
def convert():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.epub'):
        return jsonify({'error': 'Only .epub files are supported'}), 400
    try:
        title, author, chapters, image_map = parse_epub(f.read())
        pdf_bytes = build_pdf(title, author, chapters, image_map)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': f'Conversion error: {str(e)}'}), 500
    safe = re.sub(r'[^\w\s-]', '', title or 'output').strip().replace(' ','_')[:60] or 'output'
    return send_file(io.BytesIO(pdf_bytes), mimetype='application/pdf',
                     as_attachment=True, download_name=f'{safe}.pdf')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
