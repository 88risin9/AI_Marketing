import os
import io
import json
import time
import logging
import threading
from pathlib import Path
from datetime import date
from urllib.parse import urlsplit
from flask import Flask, request, jsonify, send_file, render_template, abort
from werkzeug.exceptions import HTTPException
from .db import Store, now, KINDS, V1_KINDS, normalize_bundle
from .backup_validation import validate_bundle
from .quotes import create_snapshot, approval_errors, customer_view, review_fingerprint, decimal
from .pdf_export import pdf_bytes, html_preview
from . import products as product_module
from . import ai
from .matching import match_products

ROOT=Path(__file__).resolve().parent.parent
STATUSES=['待补充','待报价','已报价','跟进中','成交','关闭']
WRITE_LOCK=threading.RLock()
def create_app(data_dir=None):
    app=Flask(__name__,template_folder=str(ROOT/'templates'),static_folder=str(ROOT/'static'))
    app.config.update(MAX_CONTENT_LENGTH=8*1024*1024,JSON_SORT_KEYS=False)
    app.json.ensure_ascii=False
    folder=Path(data_dir or os.environ.get('WORKBENCH_DATA_DIR',ROOT/'data'))
    stores={w:Store(folder/f'{w}.sqlite3') for w in ['live','demo']}
    app.config['STORES']=stores
    demo=stores['demo']
    if not demo.setting('demo_seeded',False):
        with demo.connect() as c:
            for product in product_module.example_products():
                product['source_updated_at']=product.get('updated_at','')
                demo.save_in(c,'products',product)
            customer=demo.save_in(c,'customers',{'name':'Northstar Electrical (Fictional)','country':'United Kingdom','contact':'buyer@example.com','source':'虚构示例邮件','notes':'此客户与询盘完全虚构，仅供练习。'})
            demo.save_in(c,'inquiries',{'customer_id':customer['id'],'title':'虚构示例 · 1,000 pcs MCB 询价','original_text':'Dear Sales Team,\nPlease quote 1000 pcs miniature circuit breakers (MCB), 1P, 16 A, 230 V AC, C curve, 6 kA breaking capacity. We need delivery within 30 days. Please advise your payment terms.\nBest regards,\nAlex (fictional customer)','status':'待补充','next_followup':date.today().isoformat(),'reason':'','analysis':None,'analysis_reviewed':False,'selected_product_ids':[],'selection_note':'','reply_draft':'','followups':[],'baseline_minutes':None,'actual_minutes':None})
            c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('company',json.dumps({'name':'Harbor Trade Demo Co., Ltd. (Fictional)','email':'sales@example.com','address':'Fictional address, Shanghai, China'})))
            c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('demo_seeded','true'))
    def workspace():
        w=request.args.get('workspace','live')
        if w not in stores:raise ValueError('资料空间无效。')
        return w
    def store():return stores[workspace()]
    def payload():
        data=request.get_json(silent=True)
        if not isinstance(data,dict):raise ValueError('请提交有效的 JSON 对象。')
        return data
    def activity(kind,inquiry_id=None,**kwargs):
        return store().save('activities',dict(kind=kind,inquiry_id=inquiry_id,**kwargs))
    def mutate(kind,id,changes):
        obj=store().get(kind,id);obj.update(changes);return store().save(kind,obj,id)
    def str_fields(data,fields):
        return {k:str(data.get(k,'') or '').strip() for k in fields}
    def valid_date(v):
        if v:
            try:date.fromisoformat(v)
            except (ValueError,TypeError):raise ValueError('日期应为 YYYY-MM-DD。')
    @app.before_request
    def protect_local():
        if request.path=='/api/restore':
            request.max_content_length=64*1024*1024
        hostname=request.host.split(':')[0]
        if hostname not in ('localhost','127.0.0.1','[::1]'):abort(403)
        if request.method not in ('GET','HEAD','OPTIONS'):
            if request.headers.get('X-Local-Request')!='1':abort(403)
            origin=request.headers.get('Origin')
            if origin and urlsplit(origin).netloc!=request.host:abort(403)
        if request.path.startswith('/api/'):workspace()
    @app.after_request
    def security_headers(response):
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='SAMEORIGIN'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Cache-Control']='no-store'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"
        return response
    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc,ValueError):return jsonify(error=str(exc)),400
        if isinstance(exc,HTTPException):return jsonify(error={413:'备份超过 64 MB 限制。' if request.path=='/api/restore' else '文件超过 8 MB 限制。',403:'请求来源不受信任，请从本机工作台重试。',404:'页面或记录不存在。'}.get(exc.code,'请求无法处理。')),exc.code
        # Never emit request bodies, provider responses, configuration, or credentials to logs.
        return jsonify(error='处理失败，原始资料已保留。请重试；若仍失败，请检查本地配置。'),500
    @app.get('/')
    def index():return render_template('index.html')
    @app.get('/api/health')
    def health():return jsonify(ok=True,version=2,role='internal')
    @app.get('/api/state')
    def state():
        s=store()
        result={kind:list(reversed(s.all(kind))) for kind in V1_KINDS};result.update(workspace=workspace(),company=s.setting('company',{'name':'','email':'','address':''}),ai=ai.get_config(),today=date.today().isoformat())
        return jsonify(result)
    @app.post('/api/company')
    def company():return jsonify(store().set_setting('company',str_fields(payload(),['name','email','address'])))
    @app.route('/api/products',methods=['POST'])
    @app.route('/api/products/<int:id>',methods=['PUT'])
    def products(id=None):
        d=payload()
        if id:
            old=store().get('products',id);old.update(d);d=old
            d['updated_at']=payload().get('source_updated_at',payload().get('updated_at',old.get('source_updated_at','')))
        if 'source_updated_at' in payload():d['updated_at']=payload()['source_updated_at']
        p=product_module.validate_product(d)
        p['source_updated_at']=d.get('updated_at','')
        # Material data changes require new selection review on active inquiry, but never touch historical quotes.
        with WRITE_LOCK:
            saved=store().save('products',p,id)
            if id:
                for inq in store().all('inquiries'):
                    if id in inq.get('selected_product_ids',[]):mutate('inquiries',inq['id'],{'analysis_reviewed':False})
        return jsonify(saved)
    @app.post('/api/import/preview')
    def preview_import():
        f=request.files.get('file')
        if not f:raise ValueError('请选择 CSV 或 XLSX 文件。')
        return jsonify(product_module.parse_import(f.filename,f.read()))
    @app.post('/api/import/commit')
    def commit_import():
        data=payload();rows=data.get('rows');replace_demo=data.get('replace_demo',False)
        if type(replace_demo) is not bool:raise ValueError('替换示例产品标记必须为布尔值。')
        if replace_demo and workspace()!='demo':raise ValueError('替换示例产品仅允许在示例测试空间使用。')
        if not isinstance(rows,list) or not rows or len(rows)>5000:raise ValueError('请提供 1 至 5000 行产品资料。')
        validated=[product_module.validate_product(p) for p in rows]
        for p in validated:p['source_updated_at']=p.get('updated_at','')
        s=store()
        archived_count=0;backup_file=None
        with WRITE_LOCK,s.connect() as c:
            if replace_demo:
                backup_file=s.backup_in(c,workspace(),folder.parent/'backups','before-product-replacement')
                existing=[json.loads(row[0]) for row in c.execute('SELECT data FROM records WHERE kind=?',('products',))]
                for old in existing:
                    # Only explicit fictional seed products qualify; real imports remain untouched.
                    if (not old.get('archived') and old.get('model','').startswith('DEMO-') and
                            ('虚构' in old.get('supplier','') or '虚构' in old.get('source',''))):
                        old.update(archived=True,archived_at=now())
                        s.save_in(c,'products',old,old['id']);archived_count+=1
            saved=[s.save_in(c,'products',p) for p in validated]
        return jsonify(count=len(saved),products=saved,archived_count=archived_count,backup_file=backup_file)
    @app.get('/api/templates/<name>')
    def templates_download(name):
        if name not in ('products.csv','products.xlsx','example.csv','example.xlsx'):abort(404)
        fmt=name.rsplit('.',1)[1]
        return send_file(io.BytesIO(product_module.template_bytes(fmt,demo=name.startswith('example'))),as_attachment=True,download_name=name,mimetype='text/csv; charset=utf-8' if fmt=='csv' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    @app.post('/api/customers')
    @app.put('/api/customers/<int:id>')
    def customers(id=None):
        p=payload();d=str_fields(p,['name','country','contact','source','notes'])
        if not d['name']:raise ValueError('请填写客户名称。')
        if id:old=store().get('customers',id);old.update(d);d=old
        return jsonify(store().save('customers',d,id))
    @app.post('/api/inquiries')
    def new_inquiry():
        p=payload();d=str_fields(p,['title','original_text','next_followup','reason'])
        try:cid=int(p.get('customer_id',0))
        except (TypeError,ValueError):raise ValueError('请选择客户。')
        store().get('customers',cid)
        if not d['original_text']:raise ValueError('请填写询盘原文。')
        if len(d['original_text'])>30000:raise ValueError('询盘最多 30,000 字符，请按产品拆分长询盘。')
        valid_date(d['next_followup'])
        d.update(customer_id=cid,status='待补充',analysis=None,analysis_reviewed=False,selected_product_ids=[],selection_note='',reply_draft='',followups=[],baseline_minutes=None,actual_minutes=None)
        return jsonify(store().save('inquiries',d))
    @app.put('/api/inquiries/<int:id>')
    def edit_inquiry(id):
        d=store().get('inquiries',id);p=payload();before=json.loads(json.dumps(d))
        for k in ['title','original_text','status','next_followup','reason','reply_draft']:
            if k in p:d[k]=str(p[k] or '')
        if d['status'] not in STATUSES:raise ValueError('处理状态无效。')
        if not d['original_text'].strip() or len(d['original_text'])>30000:raise ValueError('询盘原文不能为空且不能超过 30,000 字符。')
        valid_date(d['next_followup'])
        if 'original_text' in p and p['original_text']!=store().get('inquiries',id)['original_text']:
            d.update(analysis_reviewed=False,analysis=None,selected_product_ids=[])
        if 'analysis' in p:
            d['analysis']=ai.validate_analysis(p['analysis'],d['original_text'],mode='manual')
            d.update(analysis_reviewed=False,selected_product_ids=[])
        for k in ['baseline_minutes','actual_minutes']:
            if k in p:d[k]=None if p[k] in ('',None) else str(decimal(p[k],'处理时间',True))
        saved=store().save('inquiries',d,id);activity('human_edit',id,message='修改询盘或回复草稿',mode='manual',changes={'before':before,'after':d})
        return jsonify(saved)
    @app.post('/api/inquiries/<int:id>/analyze')
    def analyze_inquiry(id):
        d=store().get('inquiries',id);mode=payload().get('mode','live')
        if mode not in ('live','demo'):raise ValueError('AI 处理模式无效。')
        if mode=='demo' and workspace()!='demo':raise ValueError('演示提取仅可在虚构示例空间使用。')
        start=time.perf_counter()
        try:result=ai.analyze(d['original_text'],mode=mode)
        except Exception as exc:
            safe=str(exc) if isinstance(exc,(ValueError,RuntimeError)) else '模型调用失败，请检查配置后重试。'
            activity('ai_failure',id,duration_ms=round((time.perf_counter()-start)*1000),mode=mode,message=safe)
            return jsonify(error=safe),503
        with WRITE_LOCK:
            current=store().get('inquiries',id)
            if current['updated_at']!=d['updated_at'] or current['original_text']!=d['original_text']:
                activity('ai_failure',id,duration_ms=round((time.perf_counter()-start)*1000),mode=mode,message='处理期间询盘已修改，结果未覆盖资料。')
                return jsonify(error='处理期间询盘已修改，结果未覆盖资料，请重试。'),409
            saved=mutate('inquiries',id,dict(analysis=result,analysis_reviewed=False,selected_product_ids=[],reply_draft=result.get('reply_draft','')))
            activity('ai_success',id,duration_ms=round((time.perf_counter()-start)*1000),mode=mode,message='演示规则提取（非真实模型）' if mode=='demo' else '真实模型提取，待人工审核')
        return jsonify(saved)
    @app.get('/api/inquiries/<int:id>/matches')
    def matches(id):
        inquiry=store().get('inquiries',id)
        return jsonify(match_products(inquiry.get('analysis') or {},store().all('products')))
    @app.post('/api/inquiries/<int:id>/review')
    def review(id):
        d=store().get('inquiries',id);before=json.loads(json.dumps(d));p=payload();analysis=p.get('analysis',d.get('analysis'))
        if not isinstance(analysis,dict) or not isinstance(analysis.get('requirements'),list):raise ValueError('请先提取需求或填写人工需求。')
        analysis=ai.validate_analysis(analysis,d['original_text'],mode='manual')
        reqs=analysis['requirements']
        if len(reqs)>100:raise ValueError('需求条目过多，请拆分询盘。')
        for req in reqs:
            if not isinstance(req,dict) or req.get('kind') not in ('stated','inferred','missing'):raise ValueError('需求分类无效。')
            if not all(isinstance(req.get(k,''),str) for k in ('field','value','evidence')):raise ValueError('需求字段应为文字。')
            if req.get('kind')=='stated' and (not req.get('evidence') or req['evidence'] not in d['original_text']):raise ValueError('“原文已说明”的信息必须附上询盘中的原文。补充信息请标为待确认，并在审核说明中记录依据。')
            if req.get('kind')=='missing':req['value']=''
        ids=p.get('selected_product_ids',[])
        if not isinstance(ids,list) or any(not isinstance(x,int) or isinstance(x,bool) for x in ids) or len(ids)!=len(set(ids)):raise ValueError('选择产品编号无效。')
        if ids and any('多组' in str(w) for w in analysis.get('warnings',[])):
            raise ValueError('当前询盘有多组需求，请拆分为独立询盘后分别选型和报价。')
        result=match_products(analysis,store().all('products'));candidates={x['product_id']:x for x in result['candidates']}
        note=str(p.get('selection_note','')).strip()
        for pid in ids:
            if pid not in candidates:raise ValueError('所选产品不存在。')
            candidate=candidates[pid]
            if candidate['status']=='conflict':raise ValueError('存在硬性参数冲突，不能确认该型号：'+'；'.join(candidate['conflicts']))
            if candidate['status']=='pending' and not note:raise ValueError('所选产品有待确认项，请填写人工核实结果和依据。')
        d.update(analysis=analysis,analysis_reviewed=True,selected_product_ids=ids,selection_note=note,reply_draft=str(p.get('reply_draft',d.get('reply_draft',''))),status='待报价' if ids else '待补充')
        for k in ['baseline_minutes','actual_minutes']:
            if k in p:d[k]=None if p[k] in ('',None) else str(decimal(p[k],'处理时间',True))
        saved=store().save('inquiries',d,id);activity('human_edit',id,mode='manual',message='保存人工需求审核与选型',changes={'before':{k:before.get(k) for k in ['analysis','selected_product_ids','selection_note','reply_draft']},'after_requirements':reqs,'selected_product_ids':ids,'selection_note':note})
        return jsonify(saved)
    @app.post('/api/inquiries/<int:id>/followups')
    def followup(id):
        d=store().get('inquiries',id);p=payload();f=str_fields(p,['note','next_followup','status','reason'])
        if not f['note']:raise ValueError('请填写跟进记录。')
        if f['status'] not in STATUSES:raise ValueError('请选择有效状态。')
        if f['status'] in ('成交','关闭') and not f['reason']:raise ValueError('请填写成交或关闭原因。')
        valid_date(f['next_followup']);f['created_at']=now();d['followups'].append(f);d.update({k:f[k] for k in ['next_followup','status','reason']})
        saved=store().save('inquiries',d,id);activity('followup',id,message=f['note'],mode='manual');return jsonify(saved)
    @app.post('/api/quotes')
    def new_quote():
        p=payload()
        with WRITE_LOCK:
            s=store();inq=s.get('inquiries',p.get('inquiry_id'));customer=s.get('customers',inq['customer_id'])
            snapshot,errors=create_snapshot(p,inq,{x['id']:x for x in s.all('products')},customer,s.setting('company',{}))
            versions=[q for q in s.all('quotes') if q['inquiry_id']==inq['id']]
            version=max([q['version'] for q in versions],default=0)+1
            if p.get('base_quote_id') and s.get('quotes',p['base_quote_id'])['inquiry_id']!=inq['id']:raise ValueError('基础报价与当前询盘不一致。')
            q=dict(inquiry_id=inq['id'],number=f'QT-{inq["id"]:05d}',version=version,status='draft',sent_at=None,snapshot=snapshot,validation_errors=errors,review_fingerprint=review_fingerprint(inq),demo=workspace()=='demo',created_at=now())
            saved=s.save('quotes',q);activity('quote_created',inq['id'],message=f'保存报价第 {version} 版（草稿）',mode='manual');return jsonify(saved)
    @app.post('/api/quotes/<int:id>/approve')
    def approve(id):
        with WRITE_LOCK:
            s=store();q=s.get('quotes',id);inq=s.get('inquiries',q['inquiry_id'])
            errors=approval_errors(q,inq)
            if errors:return jsonify(error='无法审核：'+'；'.join(errors),validation_errors=errors),400
            q.update(status='approved',approved_at=now());saved=s.save('quotes',q,id);activity('quote_approved',inq['id'],message=f'审核报价第 {q["version"]} 版',mode='manual');return jsonify(saved)
    @app.post('/api/quotes/<int:id>/sent')
    def sent(id):
        q=store().get('quotes',id);p=payload()
        if q['status']!='approved':raise ValueError('草稿不能标记为已发送，请先完成审核。')
        if not isinstance(p.get('sent'),bool):raise ValueError('发送状态无效。')
        q['sent_at']=now() if p['sent'] else None;saved=store().save('quotes',q,id)
        if p['sent']:mutate('inquiries',q['inquiry_id'],{'status':'已报价'})
        activity('quote_sent',q['inquiry_id'],message='人工标记报价已发送' if p['sent'] else '人工取消已发送标记',mode='manual');return jsonify(saved)
    @app.get('/api/quotes/<int:id>/preview')
    def preview_quote(id):return html_preview(customer_view(store().get('quotes',id)))
    @app.get('/api/quotes/<int:id>/pdf')
    def export_quote(id):
        q=store().get('quotes',id)
        data=pdf_bytes(customer_view(q))
        return send_file(io.BytesIO(data),as_attachment=True,download_name=f'{q["number"]}-v{q["version"]}-{q["status"]}.pdf',mimetype='application/pdf')
    @app.get('/api/backup')
    def backup():
        data=json.dumps(store().export(workspace()),ensure_ascii=False,indent=2).encode()
        return send_file(io.BytesIO(data),as_attachment=True,download_name=f'trade-{workspace()}-{date.today().isoformat()}.json',mimetype='application/json')
    @app.post('/api/restore')
    def restore():
        f=request.files.get('file')
        if not f:raise ValueError('请选择备份 JSON 文件。')
        try:bundle=json.loads(f.read())
        except (ValueError,UnicodeError):raise ValueError('备份不是有效的 UTF-8 JSON 文件。')
        bundle=normalize_bundle(bundle)
        validate_bundle(bundle)
        with WRITE_LOCK,research_manager.lock:
            if research_manager.has_running(workspace()):
                raise ValueError('研究任务仍在执行。请取消任务并等待当前调用结束后再恢复，避免覆盖新取得的结果。')
            other=stores['demo' if workspace()=='live' else 'live']
            other_tokens={p.get('token') for p in other.all('buyer_pages')}
            if any(p.get('token') in other_tokens for p in bundle['records']['buyer_pages']):
                raise ValueError('买家页面链接与另一资料空间冲突，恢复已取消。')
            name=store().restore(bundle,workspace(),folder.parent/'backups')
            research_manager.recover(workspace())
        return jsonify(ok=True,backup_file=name,message='恢复完成。恢复前的数据已自动备份。')
    from .research import register_research
    from .portal import register_portal_management
    research_manager=register_research(app,stores,workspace)
    app.extensions['research_manager']=research_manager
    register_portal_management(app,stores,workspace)
    return app
