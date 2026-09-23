"""Reject damaged backups before replacing any current data."""
from datetime import date
from .products import validate_product
from .quotes import decimal,CURRENCIES,money,exact_math
from decimal import Decimal, ROUND_HALF_UP
from .db import normalize_bundle

@exact_math
def validate_bundle(bundle):
    bundle=normalize_bundle(bundle)
    if not isinstance(bundle,dict):raise ValueError('备份内容无效。')
    rows=bundle.get('records')
    if not isinstance(rows,dict):raise ValueError('备份缺少记录。')
    def require(obj,keys):
        if not isinstance(obj,dict) or any(k not in obj for k in keys):raise ValueError('备份记录字段不完整，恢复已取消。')
    def text(obj,keys):
        if any(not isinstance(obj.get(k,''),str) for k in keys):raise ValueError('备份文字字段损坏，恢复已取消。')
    for p in rows.get('products',[]):
        require(p,['id','model','specs','unit']);validate_product(p)
    for c in rows.get('customers',[]):
        require(c,['id','name']);text(c,['name','country','contact','source','notes'])
    for i in rows.get('inquiries',[]):
        require(i,['id','customer_id','original_text','status','followups','analysis_reviewed','selected_product_ids'])
        text(i,['original_text','status','reply_draft','selection_note'])
        if i['status'] not in ('待补充','待报价','已报价','跟进中','成交','关闭') or not isinstance(i['followups'],list) or not isinstance(i['selected_product_ids'],list) or not isinstance(i['analysis_reviewed'],bool):raise ValueError('备份询盘记录损坏。')
        for f in i['followups']:
            require(f,['note','status','created_at']);text(f,['note','status','next_followup','reason'])
        a=i.get('analysis')
        if a is not None:
            require(a,['requirements'])
            if not isinstance(a['requirements'],list):raise ValueError('备份需求记录损坏。')
            for r in a['requirements']:
                require(r,['field','value','evidence','kind']);text(r,['field','value','evidence','kind'])
                if r['kind'] not in ('stated','missing','inferred'):raise ValueError('备份需求分类损坏。')
    for q in rows.get('quotes',[]):
        require(q,['id','inquiry_id','number','version','status','created_at','snapshot','validation_errors','review_fingerprint'])
        text(q,['number','status','created_at','review_fingerprint'])
        if q['status'] not in ('draft','approved') or not isinstance(q['version'],int) or not isinstance(q['validation_errors'],list):raise ValueError('备份报价版本损坏。')
        s=q['snapshot'];require(s,['company','customer','lines','currency','total'])
        require(s['company'],['name']);require(s['customer'],['name']);text(s['company'],['name','email','address']);text(s['customer'],['name','country','contact'])
        text(s,['currency','lead_time','valid_until','payment_terms','trade_terms','customer_note','internal_note'])
        if s['currency'] and s['currency'] not in CURRENCIES:raise ValueError('备份报价币种损坏。')
        decimal(s['total'],'报价金额')
        if s.get('valid_until'):date.fromisoformat(s['valid_until'])
        if not isinstance(s['lines'],list):raise ValueError('备份报价行损坏。')
        total=Decimal(0);cost=Decimal(0);all_costs=True
        for line in s['lines']:
            require(line,['product_id','description','model','quantity','unit','unit_price','line_total','specs'])
            text(line,['description','model','quantity','unit','unit_price'])
            if not isinstance(line['specs'],dict):raise ValueError('备份产品规格损坏。')
            for key in ['quantity','unit_price','line_total','purchase_price','fx_rate','cost_total']:
                if line.get(key) not in ('',None):decimal(line[key],'报价数值',positive=key in ('quantity','fx_rate'))
            qty=line.get('quantity');price=line.get('unit_price')
            computed=money(decimal(qty,'数量',True)*decimal(price,'售价'),s['currency']) if qty not in ('',None) and price not in ('',None) else None
            if line['line_total']!=computed:raise ValueError('备份中的行金额与数量、单价不一致，恢复已取消。')
            if computed is not None:total+=Decimal(computed)
            cp=line.get('purchase_price');fx=line.get('fx_rate')
            expected_cost=money(decimal(qty,'数量',True)*decimal(cp,'采购价')*decimal(fx,'汇率',True),s['currency']) if all(v not in ('',None) for v in [qty,cp,fx,line.get('cost_currency')]) else None
            if line.get('cost_total')!=expected_cost:raise ValueError('备份中的成本换算金额不一致，恢复已取消。')
            if expected_cost is None:all_costs=False
            else:cost+=Decimal(expected_cost)
        if s['total']!=money(total,s['currency']):raise ValueError('备份中的报价总额与行金额不一致，恢复已取消。')
        other=s.get('other_costs')
        if other not in ('',None):cost+=decimal(other,'其他成本')
        else:all_costs=False
        expected_profit=money(total-cost,s['currency']) if all_costs and s['currency'] and all(l.get('line_total') is not None for l in s['lines']) else None
        if s.get('estimated_profit')!=expected_profit:raise ValueError('备份中的预估毛利与成本不一致，恢复已取消。')
        expected_margin=str(((total-cost)/total*100).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)) if expected_profit is not None and total>0 else None
        if s.get('margin_pct')!=expected_margin:raise ValueError('备份中的毛利率不一致，恢复已取消。')
        if q['status']=='approved':
            required=('currency','lead_time','valid_until','payment_terms','trade_terms')
            if any(not s.get(k,'').strip() for k in required) or not s['company'].get('name','').strip() or not s['customer'].get('name','').strip() or not s['lines'] or q['validation_errors']:
                raise ValueError('备份中的已审核报价缺少必填信息，恢复已取消。')
            for line in s['lines']:
                if any(not str(line.get(k,'')).strip() for k in ('model','description','unit','quantity','unit_price')):
                    raise ValueError('备份中的已审核报价产品信息不完整，恢复已取消。')
    for a in rows.get('activities',[]):
        require(a,['id','kind','created_at']);text(a,['kind','message','created_at'])
    company=bundle.get('settings',{}).get('company')
    if company is not None:require(company,['name']);text(company,['name','email','address'])
    from .research import validate_research_backup
    from .portal import validate_page_backup
    validate_research_backup(rows)
    tokens=[]
    products={p['id'] for p in rows['products']}
    prospects={p['id'] for p in rows['prospects']}
    for page in rows['buyer_pages']:
        validate_page_backup(page)
        snapshots=[page['draft_snapshot']]+[v['snapshot'] for v in page['versions']]
        if page.get('public_snapshot') is not None:snapshots.append(page['public_snapshot'])
        if any(s['demo'] != (bundle.get('workspace')=='demo') for s in snapshots):
            raise ValueError('备份采购页面的示例标记与资料空间不一致。')
        tokens.append(page['token'])
        if any(pid not in products for pid in page['product_ids']):
            raise ValueError('备份采购页面的产品关联无效。')
        if page.get('prospect_id') is not None and page['prospect_id'] not in prospects:
            raise ValueError('备份采购页面的企业关联无效。')
    if len(tokens)!=len(set(tokens)):
        raise ValueError('备份中的买家页面链接重复。')
