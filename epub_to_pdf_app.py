import os
import io
import zipfile
import re
from flask import Flask, request, send_file, jsonify, render_template_string
from bs4 import BeautifulSoup
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, HRFlowable
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

HTML_PAGE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EPUB → PDF Converter</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700;900&family=Source+Serif+4:ital,wght@0,300;0,400;0,600;1,300;1,400&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --ink: #1a1008;
    --paper: #f5f0e8;
    --cream: #ede6d6;
    --amber: #c8882a;
    --amber-light: #e8a84a;
    --amber-dark: #9a6010;
    --rust: #8b3a12;
    --shadow: rgba(26,16,8,0.15);
  }

  body {
    background-color: var(--paper);
    color: var(--ink);
    font-family: 'Source Serif 4', Georgia, serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 2rem 1rem;
    background-image: 
      radial-gradient(ellipse at 20% 20%, rgba(200,136,42,0.08) 0%, transparent 50%),
      radial-gradient(ellipse at 80% 80%, rgba(139,58,18,0.06) 0%, transparent 50%);
  }

  .masthead {
    text-align: center;
    margin-bottom: 2.5rem;
  }

  .masthead-rule {
    width: 120px;
    height: 2px;
    background: linear-gradient(90deg, transparent, var(--amber), transparent);
    margin: 0 auto 1.2rem;
  }

  h1 {
    font-family: 'Playfair Display', Georgia, serif;
    font-size: clamp(2.4rem, 6vw, 4rem);
    font-weight: 900;
    line-height: 1.05;
    letter-spacing: -0.02em;
    color: var(--ink);
  }

  h1 span {
    color: var(--amber);
  }

  .subtitle {
    font-size: 1rem;
    color: #6b5c44;
    font-style: italic;
    margin-top: 0.6rem;
    letter-spacing: 0.02em;
  }

  .card {
    background: white;
    border-radius: 2px;
    box-shadow: 
      0 1px 2px var(--shadow),
      0 4px 16px var(--shadow),
      0 0 0 1px rgba(200,136,42,0.12);
    width: 100%;
    max-width: 560px;
    overflow: hidden;
  }

  .card-header {
    background: var(--ink);
    padding: 1rem 1.5rem;
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }

  .card-header-dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    background: var(--amber);
    opacity: 0.8;
  }

  .card-header-title {
    font-family: 'Playfair Display', serif;
    color: var(--cream);
    font-size: 0.85rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .card-body {
    padding: 2rem;
  }

  /* Drop Zone */
  .drop-zone {
    border: 2px dashed rgba(200,136,42,0.35);
    border-radius: 2px;
    padding: 2.5rem 1.5rem;
    text-align: center;
    cursor: pointer;
    transition: all 0.25s ease;
    background: var(--paper);
    position: relative;
  }

  .drop-zone:hover, .drop-zone.dragover {
    border-color: var(--amber);
    background: #fdf6e8;
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(200,136,42,0.15);
  }

  .drop-zone input[type="file"] {
    position: absolute; inset: 0;
    opacity: 0; cursor: pointer;
    width: 100%; height: 100%;
  }

  .drop-icon {
    font-size: 2.8rem;
    line-height: 1;
    margin-bottom: 0.8rem;
    display: block;
  }

  .drop-label {
    font-family: 'Playfair Display', serif;
    font-size: 1.1rem;
    font-weight: 700;
    color: var(--ink);
    margin-bottom: 0.3rem;
  }

  .drop-hint {
    font-size: 0.82rem;
    color: #8a7055;
    font-style: italic;
  }

  .file-info {
    display: none;
    margin-top: 1.2rem;
    padding: 0.8rem 1rem;
    background: #fdf6e8;
    border-left: 3px solid var(--amber);
    font-size: 0.88rem;
    color: var(--ink);
  }

  .file-info.visible { display: flex; align-items: center; gap: 0.6rem; }
  .file-name { font-weight: 600; word-break: break-all; }
  .file-size { color: #8a7055; font-style: italic; white-space: nowrap; }

  .divider {
    height: 1px;
    background: linear-gradient(90deg, transparent, rgba(200,136,42,0.25), transparent);
    margin: 1.5rem 0;
  }

  /* Convert Button */
  .btn-convert {
    width: 100%;
    padding: 0.95rem;
    background: var(--ink);
    color: var(--amber-light);
    font-family: 'Playfair Display', serif;
    font-size: 1rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    border: none;
    border-radius: 2px;
    cursor: pointer;
    transition: all 0.2s ease;
    position: relative;
    overflow: hidden;
  }

  .btn-convert:hover:not(:disabled) {
    background: #2d1e0e;
    color: var(--amber);
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(26,16,8,0.25);
  }

  .btn-convert:disabled {
    opacity: 0.55;
    cursor: not-allowed;
    transform: none;
  }

  /* Progress */
  .progress-wrap {
    display: none;
    margin-top: 1.2rem;
  }
  .progress-wrap.visible { display: block; }

  .progress-label {
    font-size: 0.82rem;
    color: #6b5c44;
    font-style: italic;
    margin-bottom: 0.5rem;
    display: flex;
    justify-content: space-between;
  }

  .progress-bar-bg {
    height: 4px;
    background: var(--cream);
    border-radius: 2px;
    overflow: hidden;
  }

  .progress-bar-fill {
    height: 100%;
    background: linear-gradient(90deg, var(--amber-dark), var(--amber));
    border-radius: 2px;
    width: 0%;
    transition: width 0.4s ease;
    animation: shimmer 1.5s infinite;
  }

  @keyframes shimmer {
    0% { opacity: 1; }
    50% { opacity: 0.7; }
    100% { opacity: 1; }
  }

  /* Result */
  .result-box {
    display: none;
    margin-top: 1.2rem;
    padding: 1rem;
    border-radius: 2px;
    font-size: 0.9rem;
  }

  .result-box.success {
    display: block;
    background: #f0faf0;
    border-left: 3px solid #4a9e5c;
    color: #2d6a3a;
  }

  .result-box.error {
    display: block;
    background: #fdf0ee;
    border-left: 3px solid var(--rust);
    color: var(--rust);
  }

  .btn-download {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    margin-top: 0.8rem;
    padding: 0.6rem 1.2rem;
    background: var(--amber);
    color: var(--ink);
    font-family: 'Playfair Display', serif;
    font-weight: 700;
    font-size: 0.88rem;
    letter-spacing: 0.06em;
    text-decoration: none;
    border-radius: 2px;
    transition: all 0.2s;
  }

  .btn-download:hover {
    background: var(--amber-dark);
    color: white;
    transform: translateY(-1px);
  }

  .footer-note {
    margin-top: 2rem;
    font-size: 0.78rem;
    color: #9a8060;
    font-style: italic;
    text-align: center;
  }

  .ornament {
    color: var(--amber);
    opacity: 0.5;
    margin: 0 0.5rem;
  }
</style>
</head>
<body>

<div class="masthead">
  <div class="masthead-rule"></div>
  <h1>EPUB <span>→</span> PDF</h1>
  <p class="subtitle">Transform your eBooks into beautiful, readable documents</p>
  <div class="masthead-rule" style="margin-top:1.2rem;"></div>
</div>

<div class="card">
  <div class="card-header">
    <div class="card-header-dot"></div>
    <div class="card-header-dot" style="background:#d4a044;opacity:0.5"></div>
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

    <button class="btn-convert" id="convertBtn" disabled onclick="convertFile()">
      ✦ &nbsp; Convert to PDF &nbsp; ✦
    </button>

    <div class="progress-wrap" id="progressWrap">
      <div class="progress-label">
        <span id="progressText">Processing…</span>
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
  <span class="ornament">✦</span>
  Processed entirely on this server &middot; No data retained after download
  <span class="ornament">✦</span>
</p>

<script>
const fileInput = document.getElementById('fileInput');
const dropZone = document.getElementById('dropZone');
const fileInfo = document.getElementById('fileInfo');
const fileName = document.getElementById('fileName');
const fileSize = document.getElementById('fileSize');
const convertBtn = document.getElementById('convertBtn');
const progressWrap = document.getElementById('progressWrap');
const progressFill = document.getElementById('progressFill');
const progressText = document.getElementById('progressText');
const progressPct = document.getElementById('progressPct');
const resultBox = document.getElementById('resultBox');

let selectedFile = null;

function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB';
  return (bytes/(1024*1024)).toFixed(1) + ' MB';
}

function setFile(file) {
  if (!file || !file.name.endsWith('.epub')) {
    alert('Please select a valid .epub file.');
    return;
  }
  selectedFile = file;
  fileName.textContent = file.name;
  fileSize.textContent = formatSize(file.size);
  fileInfo.classList.add('visible');
  convertBtn.disabled = false;
  resultBox.className = 'result-box';
  resultBox.innerHTML = '';
}

fileInput.addEventListener('change', e => setFile(e.target.files[0]));

dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('dragover');
  setFile(e.dataTransfer.files[0]);
});

function animateProgress(targetPct, duration) {
  const start = parseFloat(progressFill.style.width) || 0;
  const startTime = performance.now();
  function step(now) {
    const t = Math.min((now - startTime) / duration, 1);
    const val = start + (targetPct - start) * t;
    progressFill.style.width = val + '%';
    progressPct.textContent = Math.round(val) + '%';
    if (t < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

async function convertFile() {
  if (!selectedFile) return;

  convertBtn.disabled = true;
  progressWrap.classList.add('visible');
  resultBox.className = 'result-box';
  resultBox.innerHTML = '';
  progressFill.style.width = '0%';
  progressText.textContent = 'Uploading…';
  animateProgress(30, 800);

  const formData = new FormData();
  formData.append('file', selectedFile);

  try {
    progressText.textContent = 'Parsing EPUB…';
    animateProgress(55, 600);

    const response = await fetch('/convert', { method: 'POST', body: formData });

    progressText.textContent = 'Generating PDF…';
    animateProgress(85, 500);

    if (!response.ok) {
      const err = await response.json();
      throw new Error(err.error || 'Conversion failed');
    }

    const blob = await response.blob();
    animateProgress(100, 300);
    progressText.textContent = 'Done!';

    const url = URL.createObjectURL(blob);
    const baseName = selectedFile.name.replace(/\.epub$/i, '');

    resultBox.className = 'result-box success';
    resultBox.innerHTML = `
      <strong>✓ Conversion successful!</strong><br>
      Your PDF is ready for download.
      <br>
      <a class="btn-download" href="${url}" download="${baseName}.pdf">
        ⬇ &nbsp; Download PDF
      </a>
    `;

  } catch (err) {
    animateProgress(100, 200);
    progressText.textContent = 'Error';
    resultBox.className = 'result-box error';
    resultBox.innerHTML = `<strong>✗ Error:</strong> ${err.message}`;
  } finally {
    convertBtn.disabled = false;
  }
}
</script>
</body>
</html>'''


def parse_epub(epub_bytes):
    """Parse EPUB file and return (title, author, chapters[])"""
    chapters = []
    title = "Untitled"
    author = ""

    with zipfile.ZipFile(io.BytesIO(epub_bytes)) as z:
        names = z.namelist()

        # Parse container.xml to find OPF
        opf_path = None
        if 'META-INF/container.xml' in names:
            container_xml = z.read('META-INF/container.xml').decode('utf-8', errors='replace')
            soup = BeautifulSoup(container_xml, 'lxml-xml')
            rootfile = soup.find('rootfile')
            if rootfile:
                opf_path = rootfile.get('full-path')

        # Parse OPF for metadata and spine
        spine_ids = []
        id_to_href = {}
        base_dir = ''

        if opf_path and opf_path in names:
            base_dir = '/'.join(opf_path.split('/')[:-1])
            if base_dir:
                base_dir += '/'
            opf_content = z.read(opf_path).decode('utf-8', errors='replace')
            opf_soup = BeautifulSoup(opf_content, 'lxml-xml')

            # Metadata
            title_tag = opf_soup.find('dc:title') or opf_soup.find('title')
            if title_tag:
                title = title_tag.get_text(strip=True)

            author_tag = opf_soup.find('dc:creator') or opf_soup.find('creator')
            if author_tag:
                author = author_tag.get_text(strip=True)

            # Manifest: id -> href
            for item in opf_soup.find_all('item'):
                item_id = item.get('id', '')
                href = item.get('href', '')
                media_type = item.get('media-type', '')
                if 'html' in media_type or href.endswith('.html') or href.endswith('.xhtml') or href.endswith('.htm'):
                    id_to_href[item_id] = href

            # Spine order
            for itemref in opf_soup.find_all('itemref'):
                idref = itemref.get('idref', '')
                if idref in id_to_href:
                    spine_ids.append(idref)

        # If no spine found, just get all HTML files in order
        if not spine_ids:
            html_files = [n for n in names if n.endswith(('.html', '.xhtml', '.htm'))]
            html_files.sort()
            for hf in html_files:
                chapters.append(('', hf))
        else:
            for sid in spine_ids:
                href = id_to_href[sid]
                # Resolve relative path
                if not href.startswith('/') and base_dir:
                    full_path = base_dir + href
                else:
                    full_path = href.lstrip('/')

                # Handle fragment identifiers
                full_path = full_path.split('#')[0]

                if full_path in names:
                    chapters.append((sid, full_path))
                elif href.split('#')[0] in names:
                    chapters.append((sid, href.split('#')[0]))

        # Extract text from HTML files
        parsed_chapters = []
        seen = set()
        for cid, path in chapters:
            if path in seen:
                continue
            seen.add(path)
            try:
                content = z.read(path).decode('utf-8', errors='replace')
                parsed_chapters.append(parse_html_chapter(content))
            except Exception:
                pass

    return title, author, parsed_chapters


def parse_html_chapter(html_content):
    """Parse HTML chapter into list of (tag, text) tuples"""
    soup = BeautifulSoup(html_content, 'lxml')
    
    # Remove scripts/styles
    for tag in soup(['script', 'style', 'nav', 'aside']):
        tag.decompose()

    body = soup.find('body') or soup
    elements = []

    def process_node(node):
        if isinstance(node, str):
            text = node.strip()
            if text:
                elements.append(('p', text))
            return

        tag = node.name if node.name else ''

        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            text = node.get_text(separator=' ', strip=True)
            if text:
                elements.append((tag, text))
        elif tag in ('p',):
            text = node.get_text(separator=' ', strip=True)
            if text:
                elements.append(('p', text))
        elif tag in ('blockquote',):
            text = node.get_text(separator=' ', strip=True)
            if text:
                elements.append(('blockquote', text))
        elif tag in ('li',):
            text = node.get_text(separator=' ', strip=True)
            if text:
                elements.append(('li', '• ' + text))
        elif tag in ('hr',):
            elements.append(('hr', ''))
        elif tag in ('div', 'section', 'article', 'main', 'body', 'span', None):
            for child in node.children:
                if hasattr(child, 'children'):
                    process_node(child)
                elif isinstance(child, str):
                    text = child.strip()
                    if text and len(text) > 1:
                        elements.append(('p', text))
        else:
            text = node.get_text(separator=' ', strip=True)
            if text:
                elements.append(('p', text))

    for child in body.children:
        process_node(child)

    return elements


def build_pdf(title, author, chapters):
    """Build PDF from parsed chapters, return bytes"""
    buf = io.BytesIO()

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=3.5*cm,
        rightMargin=3.5*cm,
        topMargin=3*cm,
        bottomMargin=3*cm,
        title=title,
        author=author,
    )

    styles = getSampleStyleSheet()

    # Custom styles
    style_title = ParagraphStyle(
        'BookTitle',
        fontName='Times-Bold',
        fontSize=28,
        leading=34,
        textColor=colors.HexColor('#1a1008'),
        alignment=TA_CENTER,
        spaceAfter=10,
    )
    style_author = ParagraphStyle(
        'BookAuthor',
        fontName='Times-Italic',
        fontSize=13,
        leading=18,
        textColor=colors.HexColor('#6b5c44'),
        alignment=TA_CENTER,
        spaceAfter=30,
    )
    style_h1 = ParagraphStyle(
        'H1',
        fontName='Times-Bold',
        fontSize=18,
        leading=24,
        textColor=colors.HexColor('#1a1008'),
        spaceBefore=22,
        spaceAfter=10,
        alignment=TA_LEFT,
    )
    style_h2 = ParagraphStyle(
        'H2',
        fontName='Times-Bold',
        fontSize=14,
        leading=20,
        textColor=colors.HexColor('#2d1e0e'),
        spaceBefore=16,
        spaceAfter=8,
    )
    style_h3 = ParagraphStyle(
        'H3',
        fontName='Times-BoldItalic',
        fontSize=12,
        leading=17,
        textColor=colors.HexColor('#3d2a10'),
        spaceBefore=12,
        spaceAfter=6,
    )
    style_body = ParagraphStyle(
        'Body',
        fontName='Times-Roman',
        fontSize=11,
        leading=17,
        textColor=colors.HexColor('#1a1008'),
        spaceAfter=6,
        alignment=TA_JUSTIFY,
        firstLineIndent=14,
    )
    style_blockquote = ParagraphStyle(
        'BlockQuote',
        fontName='Times-Italic',
        fontSize=10.5,
        leading=16,
        textColor=colors.HexColor('#4a3828'),
        spaceAfter=8,
        leftIndent=24,
        rightIndent=24,
        borderPadding=(4, 0, 4, 10),
    )
    style_li = ParagraphStyle(
        'ListItem',
        fontName='Times-Roman',
        fontSize=11,
        leading=16,
        textColor=colors.HexColor('#1a1008'),
        spaceAfter=3,
        leftIndent=18,
    )

    tag_to_style = {
        'h1': style_h1,
        'h2': style_h2,
        'h3': style_h3,
        'h4': style_h3,
        'h5': style_h3,
        'h6': style_h3,
        'p': style_body,
        'blockquote': style_blockquote,
        'li': style_li,
    }

    def safe_para(text, style):
        # Escape XML special chars except we want to allow basic formatting
        text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        try:
            return Paragraph(text, style)
        except Exception:
            return Paragraph('', style)

    story = []

    # Title page
    story.append(Spacer(1, 2*cm))
    story.append(safe_para(title or 'Untitled', style_title))
    if author:
        story.append(safe_para(author, style_author))
    story.append(HRFlowable(width='60%', thickness=1, color=colors.HexColor('#c8882a'), hAlign='CENTER'))
    story.append(PageBreak())

    # Chapters
    for chapter_elements in chapters:
        if not chapter_elements:
            continue
        for tag, text in chapter_elements:
            if not text and tag != 'hr':
                continue
            if tag == 'hr':
                story.append(Spacer(1, 4))
                story.append(HRFlowable(width='40%', thickness=0.5, color=colors.HexColor('#c8882a'), hAlign='CENTER'))
                story.append(Spacer(1, 4))
            elif tag in tag_to_style:
                story.append(safe_para(text, tag_to_style[tag]))
            else:
                story.append(safe_para(text, style_body))

    if not any(chapters):
        story.append(safe_para('No readable content found in this EPUB file.', style_body))

    doc.build(story)
    buf.seek(0)
    return buf.read()


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

    epub_bytes = f.read()

    try:
        title, author, chapters = parse_epub(epub_bytes)
        pdf_bytes = build_pdf(title, author, chapters)
    except Exception as e:
        return jsonify({'error': f'Conversion error: {str(e)}'}), 500

    safe_title = re.sub(r'[^\w\s-]', '', title or 'output').strip().replace(' ', '_')[:60] or 'output'
    filename = f'{safe_title}.pdf'

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=filename
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
