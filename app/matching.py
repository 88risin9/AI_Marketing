"""Evidence-bound MCB matching. Similar model names never establish equivalence."""
from decimal import Decimal, InvalidOperation
import re

KEY_SPECS = ('poles', 'current_a', 'voltage_v', 'curve', 'breaking_ka')
LABELS = {'product':'产品类别', 'quantity':'数量', 'unit':'单位', 'poles':'极数',
          'current_a':'额定电流', 'voltage_v':'额定电压', 'curve':'脱扣曲线',
          'breaking_ka':'分断能力', 'delivery':'交期', 'other':'其他要求'}
REQUIRED = ('product', 'quantity', 'unit') + KEY_SPECS + ('delivery',)
NUMBER = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
UNITS = {'pcs':'piece','pc':'piece','piece':'piece','pieces':'piece','个':'piece','只':'piece',
         'set':'set','sets':'set','套':'set','box':'box','boxes':'box','箱':'box','carton':'carton','cartons':'carton'}

def scalar(field, value):
    """Only an unambiguous scalar. Ranges, inequalities, AC/DC suffixes stay pending."""
    value = str(value or '').strip()
    if field == 'poles':
        m = re.fullmatch(r'([1-4])\s*(?:P|poles?)?', value, re.I)
        return m[1] if m else None
    if field == 'curve':
        m = re.fullmatch(r'(?:curve\s*)?([BCD])(?:\s*(?:curve|type))?', value, re.I)
        return m[1].upper() if m else None
    if field == 'unit': return UNITS.get(value.lower())
    suffix = {'quantity':r'(?:pcs?|pieces?|sets?|boxes|box|cartons?)?',
              'current_a':r'(?:mA|A|amps?|amperes?)?',
              'voltage_v':r'(?:kV|V|volts?)?',
              'breaking_ka':r'(?:kA|A)?'}.get(field)
    if suffix is None: return None
    m = re.fullmatch(r'(' + NUMBER + r')\s*(' + suffix + r')', value, re.I)
    if not m: return None
    try:
        amount = Decimal(m[1].replace(',', ''))
        unit = m[2].lower()
        if field == 'current_a' and unit == 'ma': amount /= 1000
        if field == 'voltage_v' and unit == 'kv': amount *= 1000
        if field == 'breaking_ka' and unit == 'a': amount /= 1000
        return amount if amount.is_finite() and amount > 0 else None
    except InvalidOperation: return None


def qualified_scalar(field, value):
    """Extract only a single numeric rating plus an explicit AC/DC qualifier.

    This is used to reject unequal numeric ratings, never to declare compatibility.
    """
    if field not in ('current_a','voltage_v','breaking_ka'):return None
    text=str(value or '').strip()
    text=re.sub(r'^(?:AC|DC)\s*','',text,flags=re.I)
    text=re.sub(r'\s*\(?\s*(?:AC|DC)\s*\)?$','',text,flags=re.I)
    return scalar(field,text)


def without_complex(field, evidence):
    """Remove expressions that must not be silently collapsed to their endpoints."""
    units={'quantity':r'(?:pcs?|pieces?|sets?|boxes|cartons?)',
           'current_a':r'(?:mA|A|amps?|amperes?)',
           'voltage_v':r'(?:kV|V|volts?)', 'breaking_ka':r'(?:kA|A)'}
    if field=='poles':
        return re.sub(r'[1-4]\s*P\s*\+\s*(?:N|[1-4]P)', lambda m:' '*len(m[0]), evidence,flags=re.I)
    if field not in units:return evidence
    unit=units[field]
    number=NUMBER
    simple=r'(?:'+number+r'\s*'+unit+r')'
    # Remove ranges, alternatives, inequalities and electrical qualifiers with their rating.
    expressions=[
        r'(?<![\w.])'+number+r'\s*(?:'+unit+r')?\s*(?:-|–|—|~|to|/|or|and|±)\s*'+number+r'\s*'+unit+r'\b',
        r'(?:>=|<=|>|<|≥|≤|at least|at most|minimum(?: of)?|maximum(?: of)?|min\.?|max\.?)\s*'+simple+r'\b',
        r'\b(?:AC|DC)\s*'+simple+r'\b',
        r'(?<![\w.])'+simple+r'\s*\(?\s*(?:AC|DC)\b\s*\)?',
        r'(?<![\w.])'+simple+r'\s*(?:±|\+/-)\s*'+number+r'\s*%?',
    ]
    text=evidence
    for expression in expressions:text=re.sub(expression,lambda m: ' '*len(m[0]),text,flags=re.I)
    return text

def evidence_values(field, evidence):
    patterns = {
        'quantity':r'(?<![\w.])(' + NUMBER + r')\s*(?:pcs?|pieces?|sets?|boxes|cartons?)\b',
        'poles':r'(?<![\w+])([1-4]\s*(?:P|poles?))(?!\w|\s*\+)',
        'current_a':r'(?<![\w.])(' + NUMBER + r'\s*(?:mA|A|amps?|amperes?))\b',
        'voltage_v':r'(?<![\w.])(' + NUMBER + r'\s*(?:kV|V|volts?))\b',
        'breaking_ka':r'(?<![\w.])(' + NUMBER + r'\s*kA)\b',
        'curve':r'\b([BCD])\s*(?:curve|type)\b|\bcurve\s*([BCD])\b',
        'unit':r'\b(pcs?|pieces?|sets?|boxes|box|cartons?)\b',
    }
    pattern = patterns.get(field)
    if not pattern: return []
    result = []
    for match in re.finditer(pattern,evidence,re.I):
        for group in match.groups():
            if group:
                parsed = scalar(field,group)
                if parsed is not None: result.append(parsed)
    # "breaking capacity 6000 A" has explicit context; plain 6000 A is current.
    if field == 'breaking_ka':
        for match in re.finditer(r'breaking\s+(?:capacity|rating)\s*(?:of|:|=)?\s*(' + NUMBER + r'\s*A)\b',evidence,re.I):
            result.append(scalar(field,match[1]))
    return result

def grounded(field, value, evidence):
    """Reject unrelated number/unit evidence even when an evidence quote is genuine."""
    v = str(value).strip()
    if not v or not evidence: return False
    if field in KEY_SPECS + ('quantity', 'unit'):
        wanted = scalar(field,v)
        if wanted is not None:
            candidates = evidence_values(field,without_complex(field,evidence))
            if candidates: return wanted in candidates
            # An exact scalar quote is valid, e.g. a manually copied table cell "16".
            return scalar(field,without_complex(field,evidence)) == wanted
        # Complex expressions are kept verbatim and remain pending in matching.
        return v in evidence
    return v in evidence


def grounded_in_original(field, value, evidence, original):
    if not evidence or evidence not in original or not grounded(field,value,evidence):return False
    if scalar(field,value) is None:return True
    # Reject cherry-picked endpoints/qualifiers: quote "16 A" from "10-16 A"
    # or "230 V" from "230 V AC" cannot establish a scalar demand.
    masked=without_complex(field,original)
    start=original.find(evidence)
    while start>=0:
        if grounded(field,value,masked[start:start+len(evidence)]):return True
        start=original.find(evidence,start+1)
    return False

def multiple_groups(text, requirements=None):
    """Conservative warning: repeated quantities or differing key values require split."""
    quantities = list(re.finditer(r'(?<![\w.])' + NUMBER + r'\s*(?:pcs?|pieces?|sets?|boxes|cartons?)\b',text,re.I))
    if len(quantities)>1: return True
    for field in ('poles','current_a','curve'):
        values = evidence_values(field,text)
        if len(set(values))>1: return True
    if requirements:
        seen = {}
        for r in requirements:
            if r.get('kind')=='stated' and r.get('field') in REQUIRED:
                k=r['field']; v=r.get('value','')
                if k in seen and seen[k]!=v: return True
                seen[k]=v
    return bool(re.search(r'\b(?:two|three|multiple|several)\s+(?:items|models|products|types)\b',text,re.I))

def is_mcb(value):
    return bool(re.search(r'\bMCBs?\b|\bminiature circuit breakers?\b|小型断路器|微型断路器',str(value),re.I))

def match_products(analysis, products):
    analysis = analysis or {}
    requirements = analysis.get('requirements',[])
    stated = {}
    duplicates = False
    for r in requirements:
        if r.get('kind')=='stated' and r.get('value') and r.get('evidence') and grounded(r.get('field'),r['value'],r['evidence']):
            if r['field'] in stated and stated[r['field']] != r['value']: duplicates=True
            stated[r['field']]=r['value']
    group_warning = duplicates or any('多组' in str(w) or 'multiple' in str(w).lower() for w in analysis.get('warnings',[]))
    candidates=[]
    for p in products:
        reasons=[]; conflicts=[]; pending=[]
        if group_warning: pending.append('询盘含多组或相互矛盾的需求；请拆分为单个产品需求后重新分析，不能合并选型。')
        category=stated.get('product','')
        if not category:pending.append('客户尚未明确产品类别。')
        elif re.search(r'\b(?:MCCB|RCCB|RCBO|ACB|contactors?|relays?)\b|塑壳断路器|漏电断路器|接触器|继电器',category,re.I) and is_mcb(p.get('category','')):
            conflicts.append('客户明确要求其他产品类别，当前 MCB 小型断路器不能直接替代。')
        elif not is_mcb(category):pending.append('第一版仅自动核对 MCB 小型断路器；当前产品类别需人工确认。')
        elif not is_mcb(p.get('category','')):conflicts.append('产品类别不是 MCB 小型断路器。')
        else:reasons.append('产品类别：MCB 小型断路器。')
        specs=p.get('specs',{}) or {}
        for field in KEY_SPECS:
            label=LABELS[field]; need=stated.get(field)
            have=specs.get(field)
            if not need:pending.append(f'客户{label}缺失或仅为推测。');continue
            if have in ('',None):pending.append(f'产品资料缺少{label}。');continue
            nv=scalar(field,need); pv=scalar(field,have)
            nn=qualified_scalar(field,need); pn=qualified_scalar(field,have)
            nq=re.search(r'\b(AC|DC)\b',str(need),re.I); pq=re.search(r'\b(AC|DC)\b',str(have),re.I)
            if nq and pq and nq[1].upper()!=pq[1].upper():
                conflicts.append(f'{label}交直流要求冲突：需求 {need}，产品资料 {have}。')
            elif (nv is None or pv is None) and nn is not None and pn is not None and nn!=pn:
                conflicts.append(f'{label}数值冲突：需求 {need}，产品资料 {have}。')
            elif nv is None or pv is None:pending.append(f'{label}存在范围、特殊接法或未支持表达：需求 {need} / 资料 {have}，须人工核实。')
            elif nv!=pv:conflicts.append(f'{label}冲突：需求 {need}，产品资料 {have}。')
            else:reasons.append(f'{label}一致：需求 {need}，资料 {have}。')
        req_unit=stated.get('unit',''); product_unit=p.get('unit','')
        nu=scalar('unit',req_unit); pu=scalar('unit',product_unit)
        units_ok=bool(nu and pu and nu==pu)
        if not nu or not pu:pending.append(f'计量单位待确认：询盘 {req_unit or "未说明"} / 产品 {product_unit or "未录入"}。')
        elif nu!=pu:pending.append(f'单位不一致：询盘 {req_unit} / 产品 {product_unit}；未提供包装换算，不能直接换算。')
        else:reasons.append(f'计量单位一致：{req_unit} / {product_unit}；保留原始单位。')
        qty=scalar('quantity',stated.get('quantity',''))
        if qty is None:pending.append('数量缺失或表达不明确。')
        if p.get('moq') in ('',None):pending.append('产品最小起订量未录入。')
        elif units_ok and qty is not None:
            moq=scalar('quantity',p['moq'])
            if moq is None:pending.append('最小起订量表达需要确认。')
            elif qty<moq:conflicts.append(f'数量 {stated["quantity"]} 低于最小起订量 {p["moq"]} {product_unit}。')
            else:reasons.append(f'数量达到最小起订量 {p["moq"]} {product_unit}。')
        if not p.get('source'):pending.append('产品资料来源尚未填写。')
        for r in requirements:
            field=r.get('field')
            if r.get('kind')=='stated' and r.get('value') and field not in ('product','quantity','unit')+KEY_SPECS:
                pending.append(f'{LABELS.get(field,field)}须人工核实：{r["value"]}。')
        if not stated.get('delivery'):pending.append('客户交期尚未明确，报价前请确认。')
        status='conflict' if conflicts else ('pending' if pending else 'eligible')
        candidates.append({'product_id':p['id'],'status':status,'reasons':reasons,'conflicts':conflicts,'pending':list(dict.fromkeys(pending))})
    candidates.sort(key=lambda c:({'eligible':0,'pending':1,'conflict':2}[c['status']],-len(c['reasons'])))
    if not candidates:message='产品库为空，请先导入产品资料。'
    elif group_warning:message='检测到多组需求，请先拆分询盘；当前候选不能作为合并匹配结果。'
    elif all(c['status']=='conflict' for c in candidates):message='暂无合适产品：现有产品均存在明确冲突，不能按相似型号替代。'
    elif any(c['status']=='eligible' for c in candidates):message='找到有资料依据的候选；最终型号仍需人工审核。'
    else:message='尚无已完整匹配的产品；以下候选有待确认项，不能视为匹配成功。'
    return {'candidates':candidates,'message':message}
