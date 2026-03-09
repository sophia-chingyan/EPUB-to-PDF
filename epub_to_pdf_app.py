import io
import zipfile
import re
from flask import Flask, request, send_file, jsonify, render_template_string
from bs4 import BeautifulSoup
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, HRFlowable
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont

# ── Font Registration ────────────────────────────────────────────────────────
pdfmetrics.registerFont(UnicodeCIDFont('MSung-Light'))           # Traditional Chinese (TC/HK)
pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))          # Simplified Chinese (SC)
pdfmetrics.registerFont(UnicodeCIDFont('HeiseiMin-W3'))          # Japanese serif
pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))       # Japanese sans
pdfmetrics.registerFont(UnicodeCIDFont('HYSMyeongJo-Medium'))    # Korean
pdfmetrics.registerFont(TTFont('IPAGothic', '/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf'))

FONT_EN_REGULAR = 'Times-Roman'
FONT_EN_BOLD    = 'Times-Bold'
FONT_EN_ITALIC  = 'Times-Italic'
FONT_TC         = 'MSung-Light'       # Traditional Chinese
FONT_SC         = 'STSong-Light'      # Simplified Chinese
FONT_JA         = 'HeiseiMin-W3'      # Japanese
FONT_KO         = 'HYSMyeongJo-Medium'# Korean

# ── Script Detection ─────────────────────────────────────────────────────────
def detect_script(text):
    """Return dominant script in text: 'tc', 'sc', 'ja', 'ko', or 'latin'."""
    counts = {'tc': 0, 'sc': 0, 'ja': 0, 'ko': 0}
    for ch in text:
        cp = ord(ch)
        # Korean Hangul
        if 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F:
            counts['ko'] += 1
        # Japanese Hiragana / Katakana
        elif 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF:
            counts['ja'] += 1
        # CJK Unified (shared by TC/SC/JA) — use locale hints later
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF:
            counts['tc'] += 1  # default; refined below

    total_cjk = sum(counts.values())
    if total_cjk == 0:
        return 'latin'
    dominant = max(counts, key=counts.get)
    return dominant

def script_font(text, base='latin'):
    """Pick the right font name for a piece of text."""
    script = detect_script(text)
    if script == 'latin':
        # map base hint to actual font
        return {'bold': FONT_EN_BOLD, 'italic': FONT_EN_ITALIC}.get(base, FONT_EN_REGULAR)
    return {
        'tc': FONT_TC,
        'sc': FONT_SC,
        'ja': FONT_JA,
        'ko': FONT_KO,
    }[script]

def has_non_latin(text):
    for ch in text:
        cp = ord(ch)
        if cp > 0x024F:  # Beyond Latin Extended-B
            return True
    return False

def dominant_script_in_chunks(chunks):
    """Given list of (tag, text) across chapters, return dominant script."""
    big = ' '.join(t for _, t in chunks[:200])
    return detect_script(big)

# ── EPUB Parsing ─────────────────────────────────────────────────────────────
def parse_epub(epub_bytes):
    chapters, title, author = [], 'Untitled', ''
    with zipfile.ZipFile(io.BytesIO(epub_bytes)) as z:
        names = z.namelist()
        opf_path = None
        if 'META-INF/container.xml' in names:
            soup = BeautifulSoup(z.read('META-INF/container.xml').decode('utf-8', errors='replace'), 'lxml-xml')
            rf = soup.find('rootfile')
            if rf:
                opf_path = rf.get('full-path')

        spine_ids, id_to_href, base_dir = [], {}, ''
        if opf_path and opf_path in names:
            base_dir = '/'.join(opf_path.split('/')[:-1])
            if base_dir:
                base_dir += '/'
            opf = BeautifulSoup(z.read(opf_path).decode('utf-8', errors='replace'), 'lxml-xml')
            t = opf.find('dc:title') or opf.find('title')
            if t: title = t.get_text(strip=True)
            a = opf.find('dc:creator') or opf.find('creator')
            if a: author = a.get_text(strip=True)
            for item in opf.find_all('item'):
                iid, href, mt = item.get('id',''), item.get('href',''), item.get('media-type','')
                if 'html' in mt or href.endswith(('.html','.xhtml','.htm')):
                    id_to_href[iid] = href
            for ir in opf.find_all('itemref'):
                iid = ir.get('idref','')
                if iid in id_to_href:
                    spine_ids.append(iid)

        if not spine_ids:
            html_files = sorted(n for n in names if n.endswith(('.html','.xhtml','.htm')))
            for hf in html_files:
                chapters.append(('', hf))
        else:
            for sid in spine_ids:
                href = id_to_href[sid]
                full = (base_dir + href if not href.startswith('/') else href.lstrip('/')).split('#')[0]
                path = full if full in names else (href.split('#')[0] if href.split('#')[0] in names else None)
                if path:
                    chapters.append((sid, path))

        parsed = []
        seen = set()
        for cid, path in chapters:
            if path in seen: continue
            seen.add(path)
            try:
                parsed.append(parse_html_chapter(z.read(path).decode('utf-8', errors='replace')))
            except Exception:
                pass
    return title, author, parsed

def parse_html_chapter(html_content):
    soup = BeautifulSoup(html_content, 'lxml')
    for tag in soup(['script','style','nav','aside']):
        tag.decompose()
    body = soup.find('body') or soup
    elements = []

    def process_node(node):
        if isinstance(node, str):
            t = node.strip()
            if t: elements.append(('p', t))
            return
        tag = node.name or ''
        if tag in ('h1','h2','h3','h4','h5','h6'):
            t = node.get_text(separator=' ', strip=True)
            if t: elements.append((tag, t))
        elif tag == 'p':
            t = node.get_text(separator=' ', strip=True)
            if t: elements.append(('p', t))
        elif tag == 'blockquote':
            t = node.get_text(separator=' ', strip=True)
            if t: elements.append(('blockquote', t))
        elif tag == 'li':
            t = node.get_text(separator=' ', strip=True)
            if t: elements.append(('li', '• ' + t))
        elif tag == 'hr':
            elements.append(('hr', ''))
        elif tag in ('div','section','article','main','body','span', None):
            for child in node.children:
                process_node(child) if hasattr(child, 'children') else (
                    elements.append(('p', child.strip())) if isinstance(child, str) and len(child.strip()) > 1 else None
                )
        else:
            t = node.get_text(separator=' ', strip=True)
            if t: elements.append(('p', t))

    for child in body.children:
        process_node(child)
    return elements

# ── PDF Building ─────────────────────────────────────────────────────────────
def build_pdf(title, author, chapters):
    buf = io.BytesIO()

    # Detect dominant script from title + first chapters
    sample_elems = []
    for ch in chapters[:5]:
        sample_elems.extend(ch or [])
    dom = detect_script((title or '') + (author or '') + ' '.join(t for _, t in sample_elems[:100]))
    is_cjk = dom in ('tc','sc','ja','ko')

    def base_font(variant='regular'):
        if not is_cjk:
            return {'bold': FONT_EN_BOLD, 'italic': FONT_EN_ITALIC}.get(variant, FONT_EN_REGULAR)
        f = {
            'tc': FONT_TC, 'sc': FONT_SC,
            'ja': FONT_JA, 'ko': FONT_KO,
        }[dom]
        return f

    lm = 1.72 if is_cjk else 1.55

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=3.2*cm, rightMargin=3.2*cm,
        topMargin=3*cm, bottomMargin=3*cm,
        title=title, author=author,
    )

    def ms(name, font, size, **kw):
        leading = kw.pop('leading', round(size * lm))
        return ParagraphStyle(name, fontName=font, fontSize=size, leading=leading, **kw)

    s_title = ms('Title', base_font('bold'),   24, textColor=colors.HexColor('#1a1008'), alignment=TA_CENTER, spaceAfter=8)
    s_author= ms('Author',base_font('italic'), 13, textColor=colors.HexColor('#6b5c44'), alignment=TA_CENTER, spaceAfter=28)
    s_h1    = ms('H1',    base_font('bold'),   17, textColor=colors.HexColor('#1a1008'), spaceBefore=20, spaceAfter=9)
    s_h2    = ms('H2',    base_font('bold'),   14, textColor=colors.HexColor('#2d1e0e'), spaceBefore=14, spaceAfter=7)
    s_h3    = ms('H3',    base_font('italic'), 12, textColor=colors.HexColor('#3d2a10'), spaceBefore=10, spaceAfter=5)
    s_body  = ms('Body',  base_font(),         11, textColor=colors.HexColor('#1a1008'), spaceAfter=5,
                 alignment=TA_JUSTIFY, firstLineIndent=0 if is_cjk else 14)
    s_bq    = ms('BQ',    base_font('italic'), 10, textColor=colors.HexColor('#4a3828'), spaceAfter=7, leftIndent=22, rightIndent=22)
    s_li    = ms('Li',    base_font(),         11, textColor=colors.HexColor('#1a1008'), spaceAfter=3, leftIndent=16)

    tag_map = {
        'h1':s_h1,'h2':s_h2,'h3':s_h3,'h4':s_h3,'h5':s_h3,'h6':s_h3,
        'p':s_body,'blockquote':s_bq,'li':s_li,
    }

    # Per-script font upgrade for mixed documents
    SCRIPT_FONTS = {'tc': FONT_TC, 'sc': FONT_SC, 'ja': FONT_JA, 'ko': FONT_KO}

    def best_font_for(text, base_style_font):
        if not has_non_latin(text):
            return base_style_font
        sc = detect_script(text)
        return SCRIPT_FONTS.get(sc, base_style_font)

    def safe_para(text, style):
        text = text.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
        needed = best_font_for(text, style.fontName)
        if needed != style.fontName:
            style = ParagraphStyle(style.name+'_x', parent=style,
                                   fontName=needed, leading=round(style.fontSize * 1.72))
        try:
            return Paragraph(text, style)
        except Exception:
            try:
                return Paragraph(text.encode('ascii','replace').decode(), style)
            except Exception:
                return Spacer(1, 0)

    story = [Spacer(1, 1.8*cm)]
    story.append(safe_para(title or 'Untitled', s_title))
    if author:
        story.append(safe_para(author, s_author))
    story.append(HRFlowable(width='55%', thickness=1, color=colors.HexColor('#c8882a'), hAlign='CENTER'))
    story.append(PageBreak())

    for chapter_elements in chapters:
        if not chapter_elements:
            continue
        for tag, text in chapter_elements:
            if not text and tag != 'hr':
                continue
            if tag == 'hr':
                story += [Spacer(1,4),
                          HRFlowable(width='35%',thickness=0.5,color=colors.HexColor('#c8882a'),hAlign='CENTER'),
                          Spacer(1,4)]
            else:
                story.append(safe_para(text, tag_map.get(tag, s_body)))

    if not any(chapters):
        story.append(safe_para('No readable content found in this EPUB.', s_body))

    doc.build(story)
    buf.seek(0)
    return buf.read()

# ── Flask App ────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

HTML_PAGE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EPUB → PDF Converter</title>
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
    background-image: radial-gradient(ellipse at 20% 20%, rgba(200,136,42,0.08) 0%, transparent 50%),
                      radial-gradient(ellipse at 80% 80%, rgba(139,58,18,0.06) 0%, transparent 50%);
    color: var(--ink); font-family: 'Source Serif 4', Georgia, serif;
    min-height: 100vh; display: flex; flex-direction: column;
    align-items: center; justify-content: center; padding: 2rem 1rem;
  }
  .masthead { text-align: center; margin-bottom: 2.5rem; }
  .rule { width: 120px; height: 2px; background: linear-gradient(90deg, transparent, var(--amber), transparent); margin: 0 auto 1.2rem; }
  h1 { font-family: 'Playfair Display', serif; font-size: clamp(2.4rem,6vw,4rem); font-weight: 900; line-height: 1.05; letter-spacing: -0.02em; }
  h1 span { color: var(--amber); }
  .subtitle { font-size: 1rem; color: #6b5c44; font-style: italic; margin-top: .6rem; letter-spacing: .02em; }
  /* Language badges */
  .lang-badges { display: flex; flex-wrap: wrap; gap: .4rem; justify-content: center; margin-top: 1rem; }
  .badge { font-size: .72rem; padding: .25rem .6rem; border-radius: 2px; border: 1px solid rgba(200,136,42,.3);
           background: rgba(200,136,42,.07); color: #7a5a20; letter-spacing: .04em; font-family: monospace; }
  .card { background: white; border-radius: 2px;
    box-shadow: 0 1px 2px var(--shadow), 0 4px 16px var(--shadow), 0 0 0 1px rgba(200,136,42,.12);
    width: 100%; max-width: 560px; overflow: hidden; }
  .card-header { background: var(--ink); padding: 1rem 1.5rem; display: flex; align-items: center; gap: .6rem; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--amber); opacity: .8; }
  .card-header-title { font-family: 'Playfair Display', serif; color: var(--cream); font-size: .85rem; letter-spacing: .12em; text-transform: uppercase; }
  .card-body { padding: 2rem; }
  .drop-zone { border: 2px dashed rgba(200,136,42,.35); border-radius: 2px; padding: 2.5rem 1.5rem;
    text-align: center; cursor: pointer; transition: all .25s ease; background: var(--paper); position: relative; }
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
  .divider { height: 1px; background: linear-gradient(90deg, transparent, rgba(200,136,42,.25), transparent); margin: 1.5rem 0; }
  .btn-convert { width: 100%; padding: .95rem; background: var(--ink); color: var(--amber-light);
    font-family: 'Playfair Display', serif; font-size: 1rem; font-weight: 700; letter-spacing: .08em;
    text-transform: uppercase; border: none; border-radius: 2px; cursor: pointer; transition: all .2s ease; }
  .btn-convert:hover:not(:disabled) { background: #2d1e0e; color: var(--amber); transform: translateY(-1px); box-shadow: 0 4px 12px rgba(26,16,8,.25); }
  .btn-convert:disabled { opacity: .55; cursor: not-allowed; transform: none; }
  .progress-wrap { display: none; margin-top: 1.2rem; }
  .progress-wrap.visible { display: block; }
  .progress-label { font-size: .82rem; color: #6b5c44; font-style: italic; margin-bottom: .5rem; display: flex; justify-content: space-between; }
  .progress-bar-bg { height: 4px; background: var(--cream); border-radius: 2px; overflow: hidden; }
  .progress-bar-fill { height: 100%; background: linear-gradient(90deg, var(--amber-dark), var(--amber)); border-radius: 2px; width: 0%; transition: width .4s ease; animation: shimmer 1.5s infinite; }
  @keyframes shimmer { 0%,100% { opacity:1; } 50% { opacity:.65; } }
  .result-box { display: none; margin-top: 1.2rem; padding: 1rem; border-radius: 2px; font-size: .9rem; }
  .result-box.success { display: block; background: #f0faf0; border-left: 3px solid #4a9e5c; color: #2d6a3a; }
  .result-box.error   { display: block; background: #fdf0ee; border-left: 3px solid var(--rust); color: var(--rust); }
  .btn-download { display: inline-flex; align-items: center; gap: .5rem; margin-top: .8rem;
    padding: .6rem 1.2rem; background: var(--amber); color: var(--ink);
    font-family: 'Playfair Display', serif; font-weight: 700; font-size: .88rem; letter-spacing: .06em;
    text-decoration: none; border-radius: 2px; transition: all .2s; }
  .btn-download:hover { background: var(--amber-dark); color: white; transform: translateY(-1px); }
  .footer-note { margin-top: 2rem; font-size: .78rem; color: #9a8060; font-style: italic; text-align: center; }
  .ornament { color: var(--amber); opacity: .5; margin: 0 .5rem; }
</style>
</head>
<body>
<div class="masthead">
  <div class="rule"></div>
  <h1>EPUB <span>→</span> PDF</h1>
  <p class="subtitle">Multilingual eBook converter</p>
  <div class="lang-badges">
    <span class="badge">🇬🇧 English</span>
    <span class="badge">🇹🇼 繁體中文</span>
    <span class="badge">🇨🇳 简体中文</span>
    <span class="badge">🇯🇵 日本語</span>
    <span class="badge">🇰🇷 한국어</span>
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
      <span class="drop-icon">📖</span>
      <div class="drop-label">Drop your EPUB here</div>
      <div class="drop-hint">or click to browse &mdash; up to 50 MB</div>
    </div>
    <div class="file-info" id="fileInfo">
      <span>📄</span>
      <span class="file-name" id="fileName"></span>
      <span class="file-size" id="fileSize"></span>
    </div>
    <div class="divider"></div>
    <button class="btn-convert" id="convertBtn" disabled onclick="convertFile()">✦ &nbsp; Convert to PDF &nbsp; ✦</button>
    <div class="progress-wrap" id="progressWrap">
      <div class="progress-label"><span id="progressText">Processing…</span><span id="progressPct">0%</span></div>
      <div class="progress-bar-bg"><div class="progress-bar-fill" id="progressFill"></div></div>
    </div>
    <div class="result-box" id="resultBox"></div>
  </div>
</div>

<p class="footer-note">
  <span class="ornament">✦</span>
  Auto-detects language &middot; Processed server-side &middot; No data retained
  <span class="ornament">✦</span>
</p>

<script>
const fileInput=document.getElementById('fileInput'), dropZone=document.getElementById('dropZone'),
  fileInfo=document.getElementById('fileInfo'), fileName=document.getElementById('fileName'),
  fileSize=document.getElementById('fileSize'), convertBtn=document.getElementById('convertBtn'),
  progressWrap=document.getElementById('progressWrap'), progressFill=document.getElementById('progressFill'),
  progressText=document.getElementById('progressText'), progressPct=document.getElementById('progressPct'),
  resultBox=document.getElementById('resultBox');
let selectedFile=null;

function fmtSize(b){return b<1024?b+' B':b<1048576?(b/1024).toFixed(1)+' KB':(b/1048576).toFixed(1)+' MB';}
function setFile(f){
  if(!f||!f.name.endsWith('.epub')){alert('Please select a valid .epub file.');return;}
  selectedFile=f; fileName.textContent=f.name; fileSize.textContent=fmtSize(f.size);
  fileInfo.classList.add('visible'); convertBtn.disabled=false;
  resultBox.className='result-box'; resultBox.innerHTML='';
}
fileInput.addEventListener('change',e=>setFile(e.target.files[0]));
dropZone.addEventListener('dragover',e=>{e.preventDefault();dropZone.classList.add('dragover');});
dropZone.addEventListener('dragleave',()=>dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop',e=>{e.preventDefault();dropZone.classList.remove('dragover');setFile(e.dataTransfer.files[0]);});

function animProg(to,dur){
  const from=parseFloat(progressFill.style.width)||0, t0=performance.now();
  (function step(now){const t=Math.min((now-t0)/dur,1),v=from+(to-from)*t;
    progressFill.style.width=v+'%'; progressPct.textContent=Math.round(v)+'%';
    if(t<1)requestAnimationFrame(step);})(t0);
}

async function convertFile(){
  if(!selectedFile)return;
  convertBtn.disabled=true; progressWrap.classList.add('visible');
  resultBox.className='result-box'; resultBox.innerHTML='';
  progressFill.style.width='0%'; progressText.textContent='Uploading…'; animProg(30,800);
  const fd=new FormData(); fd.append('file',selectedFile);
  try{
    progressText.textContent='Parsing EPUB…'; animProg(55,600);
    const res=await fetch('/convert',{method:'POST',body:fd});
    progressText.textContent='Generating PDF…'; animProg(85,500);
    if(!res.ok){const e=await res.json();throw new Error(e.error||'Conversion failed');}
    const blob=await res.blob(); animProg(100,300); progressText.textContent='Done!';
    const url=URL.createObjectURL(blob);
    const base=selectedFile.name.replace(/\.epub$/i,'');
    resultBox.className='result-box success';
    resultBox.innerHTML=`<strong>✓ Conversion successful!</strong><br>Your PDF is ready for download.<br>
      <a class="btn-download" href="${url}" download="${base}.pdf">⬇ &nbsp; Download PDF</a>`;
  }catch(err){
    animProg(100,200); progressText.textContent='Error';
    resultBox.className='result-box error';
    resultBox.innerHTML=`<strong>✗ Error:</strong> ${err.message}`;
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
    if not f.filename.endswith('.epub'):
        return jsonify({'error': 'Only .epub files are supported'}), 400
    try:
        title, author, chapters = parse_epub(f.read())
        pdf_bytes = build_pdf(title, author, chapters)
    except Exception as e:
        return jsonify({'error': f'Conversion error: {str(e)}'}), 500
    safe = re.sub(r'[^\w\s-]', '', title or 'output').strip().replace(' ','_')[:60] or 'output'
    return send_file(io.BytesIO(pdf_bytes), mimetype='application/pdf',
                     as_attachment=True, download_name=f'{safe}.pdf')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
