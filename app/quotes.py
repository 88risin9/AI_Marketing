"""Quote values are immutable snapshots. Money always uses Decimal, never floats."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from functools import wraps
import hashlib
import json
from datetime import date

def exact_math(fn):
    @wraps(fn)
    def wrapped(*args,**kwargs):
        with localcontext() as context:
            context.prec=80
            return fn(*args,**kwargs)
    return wrapped

CURRENCIES={'USD':2,'EUR':2,'CNY':2,'GBP':2,'JPY':0,'AUD':2,'CAD':2}
def decimal(value,label,positive=False):
    try:
        if isinstance(value,bool): raise ValueError()
        result=Decimal(str(value))
        if not result.is_finite() or result<0 or (positive and result<=0) or result>Decimal('1000000000000'):raise ValueError()
        if result.as_tuple().exponent < -8: raise ValueError()
        return result
    except (InvalidOperation,ValueError,TypeError):raise ValueError(f'{label}必须是有效的{"正数" if positive else "非负数"}，最多 8 位小数。')
@exact_math
def money(value,currency):
    precision=Decimal('1').scaleb(-CURRENCIES.get(currency,2))
    return str(value.quantize(precision,rounding=ROUND_HALF_UP))
def review_fingerprint(inquiry):
    return hashlib.sha256(json.dumps({k:inquiry.get(k) for k in ['original_text','analysis','analysis_reviewed','selected_product_ids','selection_note']},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
@exact_math
def create_snapshot(payload,inquiry,products,customer,company):
    currency=str(payload.get('currency','')).upper().strip()
    if currency and currency not in CURRENCIES: raise ValueError('报价币种支持 USD/EUR/CNY/GBP/JPY/AUD/CAD。')
    snapshot={key:str(payload.get(key,'') or '').strip() for key in ['lead_time','valid_until','payment_terms','trade_terms','customer_note','internal_note']}
    snapshot.update(currency=currency,company=dict(company),customer={k:customer.get(k,'') for k in ['name','country','contact']},lines=[])
    lines=payload.get('lines',[])
    if not isinstance(lines,list) or len(lines)>100:raise ValueError('报价行必须为列表，最多 100 行。')
    total=Decimal(0); cost=Decimal(0); warnings=[]; errors=[]
    if not currency:errors.append('请填写报价币种。')
    if not lines:errors.append('请至少添加一个报价产品。')
    for n,row in enumerate(lines,1):
        if not isinstance(row,dict):raise ValueError(f'第 {n} 行报价格式不正确。')
        try:p=products[int(row.get('product_id',0))]
        except (KeyError,ValueError,TypeError):raise ValueError(f'第 {n} 行产品不存在。')
        if p.get('archived'):raise ValueError(f'第 {n} 行产品已归档，请重新选型后创建报价；历史报价不受影响。')
        item={k:p.get(k,'') for k in ['model','specs','purchase_price','unit']}
        item.update(product_id=p['id'],description=str(row.get('description') or p.get('name_en') or '').strip(),cost_currency=p.get('currency',''),source=p.get('source',''),product_updated_at=p.get('updated_at',''))
        item['unit']=str(row.get('unit',p.get('unit','')))
        if item['unit']!=p.get('unit',''):raise ValueError(f'第 {n} 行单位与产品资料不一致；请先修正资料或确认包装换算，不能直接改单位。')
        for k,label in [('quantity','数量'),('unit_price','销售单价')]:
            v=row.get(k,'');item[k]=str(v) if v is not None else ''
            if item[k]=='':errors.append(f'第 {n} 行缺少{label}。')
            else:decimal(item[k],f'第 {n} 行{label}',positive=k=='quantity')
        qty=decimal(item['quantity'],'数量',True) if item['quantity'] else None
        price=decimal(item['unit_price'],'销售单价') if item['unit_price'] else None
        if qty is not None and p.get('moq') not in ('',None):
            if qty < decimal(p['moq'],'最小起订量'):errors.append(f'第 {n} 行数量低于供应商最小起订量 {p["moq"]}。')
        if not item['unit'].strip():errors.append(f'第 {n} 行缺少计量单位。')
        if not str(item['model']).strip():errors.append(f'第 {n} 行缺少型号。')
        if not item['description']:errors.append(f'第 {n} 行缺少英文产品描述。')
        item['line_total']=money(qty*price,currency) if qty is not None and price is not None else None
        if item['line_total'] is not None:total+=Decimal(item['line_total'])
        fx=row.get('fx_rate','')
        if item['cost_currency']==currency and currency:fx='1'
        item['fx_rate']=str(fx) if fx is not None else ''
        if item['fx_rate']:decimal(item['fx_rate'],'汇率',True)
        reasons=[]
        if item['purchase_price'] in ('',None):reasons.append('采购价')
        if not item['cost_currency']:reasons.append('采购币种')
        if not item['fx_rate']:reasons.append(f'汇率（1 {item["cost_currency"] or "采购币种"} = ? {currency or "报价币种"}）')
        if qty is None:reasons.append('数量')
        if reasons:
            warnings.append(f'第 {n} 行缺少：'+ '、'.join(reasons));item['cost_total']=None
        else:
            item['cost_total']=money(qty*decimal(item['purchase_price'],'采购价')*decimal(item['fx_rate'],'汇率',True),currency)
            cost+=Decimal(item['cost_total'])
        snapshot['lines'].append(item)
    other=payload.get('other_costs','')
    if other in ('',None):
        snapshot['other_costs']='';warnings.append('其他成本尚未确认（运费、包装、手续费等）；明确没有时请填写 0。')
    else:
        snapshot['other_costs']=money(decimal(other,'其他成本'),currency);cost+=Decimal(snapshot['other_costs'])
    snapshot.update(total=money(total,currency),cost_warnings=warnings,estimated_profit=None,margin_pct=None)
    if not warnings and all(x['line_total'] is not None for x in snapshot['lines']) and currency:
        profit=total-cost;snapshot['estimated_profit']=money(profit,currency)
        if total>0:snapshot['margin_pct']=str((profit/total*100).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
    for field,label in [('lead_time','交期'),('valid_until','报价有效期'),('payment_terms','付款条件'),('trade_terms','贸易条款')]:
        if not snapshot[field]:errors.append(f'请填写{label}。')
    if snapshot['valid_until']:
        try:date.fromisoformat(snapshot['valid_until'])
        except ValueError:raise ValueError('报价有效期应为 YYYY-MM-DD 日期。')
    if not company.get('name'):errors.append('请在设置中填写出口公司英文名称。')
    if not customer.get('name'):errors.append('缺少客户名称。')
    if not inquiry.get('analysis_reviewed'):errors.append('请先审核需求并确认选型。')
    selected=inquiry.get('selected_product_ids',[])
    if any(x['product_id'] not in selected for x in snapshot['lines']):errors.append('报价包含尚未人工确认的产品。')
    return snapshot,errors

def approval_errors(quote,inquiry):
    errors=list(quote.get('validation_errors',[]))
    if quote.get('review_fingerprint')!=review_fingerprint(inquiry):errors.append('需求或选型已变更，请重新检查并保存新的报价版本。')
    if not inquiry.get('analysis_reviewed'):errors.append('需求尚未审核。')
    until=quote['snapshot'].get('valid_until')
    if until and date.fromisoformat(until)<date.today():errors.append('报价有效期已经过期，请创建新版本。')
    return list(dict.fromkeys(errors))

def customer_view(quote):
    """Allow-list is the only boundary used by both customer HTML and PDF."""
    s=quote['snapshot']
    public={k:s.get(k,'') for k in ['currency','lead_time','valid_until','payment_terms','trade_terms','customer_note','total']}
    public['company']={k:s.get('company',{}).get(k,'') for k in ['name','email','address']}
    public['customer']={k:s.get('customer',{}).get(k,'') for k in ['name','country','contact']}
    public['lines']=[{**{k:line.get(k,'') for k in ['description','model','quantity','unit','unit_price','line_total']},'specs':{k:v for k,v in (line.get('specs') or {}).items() if k in ('poles','current_a','voltage_v','curve','breaking_ka')}} for line in s['lines']]
    public.update(number=quote['number'],version=quote['version'],status=quote['status'],created_at=quote['created_at'][:10],demo=quote.get('demo',False))
    return public
