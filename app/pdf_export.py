"""Customer-only output. This module never receives the internal quote object."""
from io import BytesIO
from html import escape
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pathlib import Path
import os
import reportlab

SPEC_LABELS={'poles':'Poles','current_a':'Rated current (A)','voltage_v':'Rated voltage (V)','curve':'Trip curve','breaking_ka':'Breaking capacity (kA)'}
def spec_text(specs):
    return '; '.join(f'{SPEC_LABELS.get(k,k)}: {v}' for k,v in (specs or {}).items() if v not in ('',None))
def pdf_bytes(q):
    buf=BytesIO()
    font='TradeDeskUnicode'
    if font not in pdfmetrics.getRegisteredFontNames():
        paths=[os.environ.get('QUOTE_FONT_PATH',''),'/System/Library/Fonts/Supplemental/Arial Unicode.ttf','/Library/Fonts/Arial Unicode.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',str(Path(reportlab.__file__).parent/'fonts'/'Vera.ttf')]
        path=next((p for p in paths if p and Path(p).is_file()),None)
        if not path:raise ValueError('找不到报价字体，请配置 QUOTE_FONT_PATH 为可用的 TTF 字体路径。')
        pdfmetrics.registerFont(TTFont(font,path))
    styles=getSampleStyleSheet()
    for s in styles.byName.values():s.fontName=font
    styles.add(ParagraphStyle(name='SmallBody',fontName=font,fontSize=9,leading=14,textColor=colors.HexColor('#334155')))
    styles.add(ParagraphStyle(name='QuoteTitle',fontName=font,fontSize=28,leading=34,textColor=colors.HexColor('#123c45'),spaceAfter=12))
    def p(text,style='SmallBody'):return Paragraph(escape(str(text or '')).replace('\n','<br/>'),styles[style])
    doc=SimpleDocTemplate(buf,pagesize=(595.28,841.89),rightMargin=42,leftMargin=42,topMargin=40,bottomMargin=45,title=q['number'],author=q['company'].get('name',''))
    story=[p(q['company'].get('name') or 'Company name pending','Heading2'),p(q['company'].get('address')),p(q['company'].get('email')),Spacer(1,24),p('QUOTATION','QuoteTitle')]
    if q['demo']:story += [p('FICTIONAL SAMPLE - For workflow testing only','Heading3')]
    if q['status']!='approved':story += [p('DRAFT - NOT APPROVED','Heading3')]
    info=[[p(f'Quote no. {q["number"]} / v{q["version"]}'),p(f'Date: {q["created_at"]}')],[p(f'To: {q["customer"].get("name","")}'),p(f'Valid until: {q["valid_until"] or "To be confirmed"}')],[p(q['customer'].get('contact')),p(q['customer'].get('country'))]]
    t=Table(info,colWidths=[310,201]);t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),7)]));story+=[t,Spacer(1,18)]
    rows=[[p(x) for x in ['Item / specification','Qty','Unit','Unit price','Amount']]]
    for line in q['lines']:
        description=f'{line["description"]}\nModel: {line["model"]}\n{spec_text(line.get("specs"))}'
        rows.append([p(description),p(line['quantity'] or 'TBC'),p(line['unit'] or 'TBC'),p(line['unit_price'] if line['unit_price']!='' else 'TBC'),p(line['line_total'] if line['line_total'] is not None else 'TBC')])
    if not q['lines']:rows.append([p('No items added'),p(''),p(''),p(''),p('')])
    table=Table(rows,colWidths=[247,45,45,82,92],repeatRows=1,hAlign='LEFT',splitInRow=1)
    table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e3eeed')),('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),10),('BOTTOMPADDING',(0,0),(-1,-1),10),('LINEBELOW',(0,0),(-1,-1),.4,colors.HexColor('#dce3e6'))]))
    story += [table,Spacer(1,15),p(f'TOTAL  {q["currency"] or "TBC"} {q["total"]}','Heading2'),Spacer(1,20)]
    for label,key in [('Lead time','lead_time'),('Payment terms','payment_terms'),('Trade terms','trade_terms')]:story += [p(f'{label}: {q[key] or "To be confirmed"}'),Spacer(1,7)]
    if q['customer_note']:story += [Spacer(1,8),p('Notes','Heading3'),p(q['customer_note'])]
    story += [Spacer(1,22),p('Please confirm your acceptance and any outstanding details before order placement.')]
    def footer(canvas,doc):
        canvas.setFont(font,9);canvas.setFillColor(colors.HexColor('#64748b'));canvas.drawString(42,25,q['number']+' / v'+str(q['version']));canvas.drawRightString(553,25,f'Page {doc.page}')
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    return buf.getvalue()

def html_preview(q):
    e=lambda x:escape(str(x or ''))
    rows=''.join(f'<tr><td><strong>{e(x["description"])}</strong><br>Model: {e(x["model"])}<small>{e(spec_text(x.get("specs")))}</small></td><td>{e(x["quantity"])}</td><td>{e(x["unit"])}</td><td>{e(x["unit_price"])}</td><td>{e(x["line_total"])}</td></tr>' for x in q['lines'])
    badge=('<div class="badge">FICTIONAL SAMPLE - For workflow testing only</div>' if q['demo'] else '')+('<div class="badge">DRAFT - NOT APPROVED</div>' if q['status']!='approved' else '')
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(q['number'])}</title><style>body{{font:15px/1.6 Arial,sans-serif;background:#f0f4f5;color:#243d45;margin:0;padding:32px}}article{{max-width:850px;background:white;padding:50px;margin:auto}}h1{{font-size:36px;letter-spacing:2px;margin-top:36px}}.badge{{color:#97540b;background:#fff3d9;padding:8px;margin:10px 0}}small{{display:block;color:#64748b}}table{{width:100%;border-collapse:collapse;margin:30px 0}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #dce3e6;vertical-align:top}}th{{background:#e3eeed}}.total{{text-align:right;font-size:22px}}@media print{{body{{padding:0;background:white}}article{{padding:20px}}}}</style><article><h2>{e(q['company']['name'])}</h2><div>{e(q['company']['address'])}<br>{e(q['company']['email'])}</div><h1>QUOTATION</h1>{badge}<p>Quote no. {e(q['number'])} / v{q['version']}<br>Date: {e(q['created_at'])}<br>Valid until: {e(q['valid_until']) or 'To be confirmed'}</p><p>To: <strong>{e(q['customer']['name'])}</strong><br>{e(q['customer']['country'])}<br>{e(q['customer']['contact'])}</p><table><thead><tr><th>Item / specification</th><th>Qty</th><th>Unit</th><th>Unit price</th><th>Amount</th></tr></thead><tbody>{rows}</tbody></table><p class="total">TOTAL {e(q['currency'])} {e(q['total'])}</p><p>Lead time: {e(q['lead_time']) or 'To be confirmed'}<br>Payment terms: {e(q['payment_terms']) or 'To be confirmed'}<br>Trade terms: {e(q['trade_terms']) or 'To be confirmed'}</p><p>{e(q['customer_note'])}</p><small>Please confirm your acceptance and any outstanding details before order placement.</small></article></html>'''
