#!/usr/bin/env python3
"""Render the tracked coworker guide to PDF; optional documentation dependency."""
from html import escape
from pathlib import Path
import re
import reportlab

from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Preformatted

ROOT = Path(__file__).resolve().parents[1]
INK = colors.HexColor("#17394A")
GRAY = colors.HexColor("#54616A")


def rich(text):
    text = escape(text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)',
                  lambda m: '<link href="' + escape(m[2] if m[2].startswith('https://') else
                      'https://github.com/Nobody5252/asana-reporting/blob/main/docs/' + m[2], quote=True) +
                      '" color="#245A73">' + m[1] + '</link>', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', text)
    return re.sub(r'`([^`]+)`', r'<font name="Guide">\1</font>', text)


def diagram(source):
    """Render the guide's bounded acyclic Mermaid LR nodes and edges directly."""
    labels = {}
    def node(match):
        key, label = match.groups()
        if key in labels and labels[key] != label:
            raise ValueError('Conflicting diagram label')
        labels[key] = label
        return key
    cleaned = re.sub(r'(\w+)\["([^"\n]+)"\]', node, source)
    edges = []
    for line in cleaned.splitlines():
        line = line.strip()
        if not line or line.startswith('%%') or line == 'flowchart LR':
            continue
        match = re.fullmatch(r'(\w+)\s*-->\s*(\w+)', line)
        if not match:
            raise ValueError('Unsupported diagram syntax: ' + line)
        edges.append(match.groups())
    if not edges or any(a not in labels or b not in labels for a,b in edges):
        raise ValueError('Diagram needs defined nodes and edges')
    levels = {key: 0 for key in labels}
    for _ in labels:
        changed = False
        for a,b in edges:
            if levels[b] <= levels[a]:
                levels[b] = levels[a] + 1
                changed = True
        if not changed:
            break
    else:
        raise ValueError('Diagram must be acyclic')
    columns = [[key for key in labels if levels[key] == i]
               for i in range(max(levels.values()) + 1)]
    width, gap, box_height = 480, 14, 64
    box_width = (width-gap*(len(columns)-1))/len(columns)
    height = max(map(len, columns))*78+18
    size = 8.2 if len(columns)>3 else 9
    positions = {}
    for col, keys in enumerate(columns):
        for row,key in enumerate(keys):
            positions[key] = (col*(box_width+gap), height/2+(len(keys)-1)*39-row*78-box_height/2)
    d = Drawing(width,height)
    for a,b in edges:
        ax,ay=positions[a]; bx,by=positions[b]
        x1,y1,x2,y2=ax+box_width,ay+box_height/2,bx,by+box_height/2
        mid=(x1+x2)/2
        for coords in ((x1,y1,mid,y1),(mid,y1,mid,y2),(mid,y2,x2,y2)):
            d.add(Line(*coords,strokeColor=GRAY,strokeWidth=.8))
        d.add(Polygon([x2,y2,x2-5,y2+3,x2-5,y2-3],fillColor=GRAY,strokeColor=GRAY))
    for key,(x,y) in positions.items():
        lines=[];current=''
        for word in labels[key].split():
            trial=(current+' '+word).strip()
            if current and pdfmetrics.stringWidth(trial,'Guide',size)>box_width-12:
                lines.append(current);current=word
            else:
                current=trial
        lines.append(current)
        if len(lines)*11>box_height-8:
            raise ValueError('Diagram label does not fit: '+labels[key])
        d.add(Rect(x,y,box_width,box_height,fillColor=colors.HexColor('#F2F5F6'),strokeColor=colors.HexColor('#AEBCC3'),strokeWidth=.6))
        for i,line in enumerate(lines):
            d.add(String(x+box_width/2,y+box_height/2+(len(lines)-1)*5.5-i*11-3,
                         line,fontName='Guide',fontSize=size,textAnchor='middle',fillColor=INK))
    return d


def build():
    out = ROOT / 'output/pdf/asana-reporting-guide.pdf'
    out.parent.mkdir(parents=True,exist_ok=True)
    # Embed the bundled TrueType fonts so PDF viewers do not substitute metrics.
    font_dir=Path(reportlab.__file__).parent/'fonts'
    for name, filename in (('Guide','Vera.ttf'),('Guide-Bold','VeraBd.ttf'),
                           ('Guide-Italic','VeraIt.ttf'),('Guide-BoldItalic','VeraBI.ttf')):
        pdfmetrics.registerFont(TTFont(name,str(font_dir/filename)))
    pdfmetrics.registerFontFamily('Guide',normal='Guide',bold='Guide-Bold',italic='Guide-Italic',boldItalic='Guide-BoldItalic')
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name='GuideTitle',fontName='Helvetica-Bold',fontSize=22,leading=27,textColor=INK,spaceAfter=16))
    styles.add(ParagraphStyle(name='GuideSection',fontName='Helvetica-Bold',fontSize=16,leading=20,textColor=INK,spaceAfter=11,spaceBefore=6,keepWithNext=True))
    styles.add(ParagraphStyle(name='GuideSub',fontName='Helvetica-Bold',fontSize=11.5,leading=15,textColor=INK,spaceBefore=9,spaceAfter=5,keepWithNext=True))
    styles.add(ParagraphStyle(name='GuideBody',fontName='Helvetica',fontSize=10.2,leading=14.5,textColor=colors.HexColor('#25313A'),spaceAfter=8,alignment=TA_LEFT))
    styles.add(ParagraphStyle(name='GuideBullet',parent=styles['GuideBody'],leftIndent=12,firstLineIndent=-8,spaceAfter=5))
    styles.add(ParagraphStyle(name='GuideCode',fontName='Guide',fontSize=8,leading=11.5,
        backColor=colors.HexColor('#F3F5F7'),borderPadding=7,spaceBefore=4,spaceAfter=12))
    for name in ('GuideTitle','GuideSection','GuideSub'):
        styles[name].fontName='Guide-Bold'
    styles['GuideTitle'].fontSize=20
    styles['GuideTitle'].leading=25
    for name in ('GuideBody','GuideBullet'):
        styles[name].fontName='Guide'
        styles[name].fontSize=9.5
        styles[name].leading=13.5
    text=(ROOT/'docs/application-guide.md').read_text(encoding='utf-8')
    story=[]
    paragraph=[]
    fence=None
    fenced_lines=[]
    def flush():
        if paragraph:
            story.append(Paragraph(rich(' '.join(paragraph)),styles['GuideBody']))
            paragraph.clear()
    for line in text.splitlines():
        if fence is not None:
            if line=='```':
                source='\n'.join(fenced_lines)
                if fence=='mermaid':
                    story.extend([diagram(source),Spacer(1,9)])
                else:
                    if any(pdfmetrics.stringWidth(s,'Guide',8)>480 for s in fenced_lines):
                        raise ValueError('Code line too wide')
                    story.append(Preformatted(source,styles['GuideCode']))
                fence=None;fenced_lines=[]
            else:
                fenced_lines.append(line)
            continue
        if line.startswith('```'):
            flush();fence=line[3:];continue
        if line=='<!-- pagebreak -->':
            flush(); story.append(PageBreak()); continue
        if line.startswith('# '):
            flush();story.append(Paragraph(rich(line[2:]),styles['GuideTitle']));continue
        if line.startswith('## '):
            flush();story.append(Paragraph(rich(line[3:]),styles['GuideSection']));continue
        if line.startswith('### '):
            flush();story.append(Paragraph(rich(line[4:]),styles['GuideSub']));continue
        if line.startswith('- ') or re.match(r'^\d+\. ',line):
            flush()
            story.append(Paragraph(rich(line),styles['GuideBullet']))
            continue
        # Continuations of list items belong to that paragraph, not a separate block.
        if line.startswith('  ') and story and not paragraph:
            previous=story[-1]
            if isinstance(previous,Paragraph) and previous.style.name=='GuideBullet':
                story[-1]=Paragraph(previous.text+' '+rich(line.strip()),styles['GuideBullet']);continue
        if line.strip():
            paragraph.append(line.strip())
        else:
            flush()
    flush()
    if fence is not None:
        raise ValueError('Unclosed fenced block')
    def furniture(canvas,doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor('#CBD3D8'));canvas.setLineWidth(.5)
        canvas.line(48,39,A4[0]-48,39)
        canvas.setFont('Guide',7.5);canvas.setFillColor(GRAY)
        canvas.drawString(48,26,'Asana Reporting | Architecture and operating guide | October 18, 2026')
        canvas.drawRightString(A4[0]-48,26,str(doc.page))
        canvas.restoreState()
    doc=SimpleDocTemplate(str(out),pagesize=A4,rightMargin=48,leftMargin=48,topMargin=44,bottomMargin=52,
        title='Asana Reporting architecture and operating guide',author='Asana Reporting project')
    doc.build(story,onFirstPage=furniture,onLaterPages=furniture)
    print(out)


if __name__=='__main__':
    build()
