'use strict';

// V2 is an additive workspace. V1 keeps ownership of navigation, dialogs and API headers.
window.V2 = (() => {
  const ui = {workspace:null, research:null, pages:null, loading:false, error:'', tab:'tasks', taskId:null, filter:'all', loadedAt:0};
  const taskLabels = {draft:'尚未开始',running:'正在研究',needs_configuration:'需要配置',paused:'已暂停 · 可恢复',cancelled:'已取消',failed:'执行失败',completed:'研究完成'};
  const reviewLabels = {pending:'待我审核',accepted:'已认可',excluded:'已排除'};
  const contactLabels = {new:'尚未联系',contacted:'已实际联系',replied:'已收到回复',qualified:'已形成有效询盘',unsuitable:'不合适'};
  const classificationLabels = {potential_buyer:'潜在买家',supplier:'供应商',competitor:'竞争者',directory:'目录平台',unknown:'身份待核验',irrelevant:'明显不相关'};
  const levelLabels = {high:'高',medium:'中',low:'低',unknown:'未知'};
  const reviewOptions = Object.entries(reviewLabels).map(([value,label])=>({value,label}));
  const contactOptions = Object.entries(contactLabels).map(([value,label])=>({value,label}));
  const priorityOptions = Object.entries({high:'高优先级',medium:'中优先级',low:'低优先级'}).map(([value,label])=>({value,label}));
  const arr = value => Array.isArray(value) ? value : [];
  const textValue = value => typeof value === 'object' ? '' : value || '';
  const vbtn = (label,action,cls='',id='',extra='') => `<button type="button" class="btn ${cls}" data-v2-action="${e(action)}" ${id!==''?`data-id="${e(id)}"`:''} ${extra}>${label}</button>`;
  const list = (items,emptyText='暂无记录') => arr(items).length ? `<ul class="v2-list">${items.map(item=>`<li>${e(textValue(item))}</li>`).join('')}</ul>` : `<p class="small-text muted">${e(emptyText)}</p>`;
  const safeUrl = value => {try {const u=new URL(value);return ['https:','http:'].includes(u.protocol)&&!u.username&&!u.password?u.href:'';}catch(_){return '';}};
  const link = (url,label) => safeUrl(url)?`<a href="${e(safeUrl(url))}" target="_blank" rel="noopener noreferrer">${e(label||url)} ${icon('external')}</a>`:`<span class="muted">${e(label||url||'待核验')}</span>`;
  const provenance = value => pill(({web:'真实网页研究',manual:'人工资料',demo:'演示资料',live:'真实模型草稿',manual_template:'人工辅助模板'})[value]||'来源待确认',value==='web'||value==='live'?'blue':'amber');
  const prospectProvenance = p => p.provenance==='web'&&!p.ai_processed?pill('真实网页读取 · 未 AI 研判','blue'):provenance(p.provenance);
  const materialProvenance = m => m.provenance==='manual'?provenance('manual_template'):provenance(m.provenance);
  const pickedTask = () => arr(ui.research?.tasks).find(x=>x.id===ui.taskId);
  const pickedProspect = id => arr(ui.research?.prospects).find(x=>x.id===Number(id));
  const pickedMaterial = id => arr(ui.research?.materials).find(x=>x.id===Number(id));
  const pickedPage = id => arr(ui.pages?.pages).find(x=>x.id===Number(id));
  const currentProducts = () => arr(state.data?.products);
  const availableProducts = () => currentProducts().filter(p=>!p.archived);
  const productNames = ids => arr(ids).map(id=>currentProducts().find(p=>p.id===Number(id))?.model||`产品 #${id}`).join('、')||'待确认';
  const formEnd = (label,other='') => `<div class="form-actions">${other}${btn('取消','close-modal')}<button type="submit" class="btn primary">${e(label)}</button></div>`;
  function resetWorkspace(){
    if(ui.workspace===state.workspace)return;
    Object.assign(ui,{workspace:state.workspace,research:null,pages:null,error:'',taskId:null,loadedAt:0,tab:'tasks',filter:'all'});
  }
  function isView(view){return view==='research'||view==='buyer-pages';}
  async function load(force=false){
    resetWorkspace();
    if(ui.loading||(!force&&Date.now()-ui.loadedAt<5000))return;
    ui.loading=true;const activeWorkspace=state.workspace;
    try {
      const [research,pages]=await Promise.all([api('/api/research/state'),api('/api/buyer-pages')]);
      if(state.workspace!==activeWorkspace)return;
      ui.research=research;ui.pages=pages;ui.loadedAt=Date.now();ui.error='';
      if(!pickedTask())ui.taskId=arr(research.tasks)[0]?.id||null;
    } catch(error){if(state.workspace===activeWorkspace)ui.error=error.message;}
    finally {ui.loading=false;if(isView(state.view)&&!$('#modal').open)render();}
  }
  async function reload(){ui.loadedAt=0;await load(true);}
  function capabilityPanel(){
    const c=ui.research?.capabilities||{};
    const missing=arr(c.missing);
    return `<section class="v2-capabilities" aria-label="研究能力配置"><div class="v2-capability-item"><span class="v2-dot ${c.model?'ready':''}"></span><strong>模型</strong><span>${c.model?'已配置 · 待实测':'未接入'}</span></div><div class="v2-capability-item"><span class="v2-dot ${c.search?'ready':''}"></span><strong>联网搜索</strong><span>${c.search?'已配置 · 待实测':'未接入'}</span></div><div class="v2-capability-item"><span class="v2-dot ${c.reader?'ready':''}"></span><strong>网页阅读</strong><span>${c.reader?'可用 · 视网站可访问性':'未启用'}</span></div><details><summary>配置与边界</summary><div class="v2-capability-note">${missing.length?`<strong>待配置：</strong>${e(missing.join('；'))}<br>`:''}模型复用本机 .env；搜索配置请参考 .env.example 与 docs/RESEARCH.md。密钥仅在服务端读取。缺少配置时可保存任务、提供企业网址或人工资料。<br>调用次数含失败尝试；重试沿用剩余预算。未配置可靠价格时不估算费用。网页内容仅作为资料，不作为指令。</div></details></section>`;
  }
  function statsPanel(){
    const ps=arr(ui.research?.prospects), s=ui.research?.stats||{};
    const count=(key,fallback)=>Number.isFinite(Number(s[key]))?Number(s[key]):fallback;
    const counts={researched:count('researched',ps.length),accepted:count('accepted',ps.filter(p=>p.review_status==='accepted').length),contacted:count('contacted',ps.filter(p=>['contacted','replied','qualified'].includes(p.contact_status)).length),replied:count('replied',ps.filter(p=>['replied','qualified'].includes(p.contact_status)).length),qualified:count('qualified',ps.filter(p=>p.contact_status==='qualified').length)};
    const ratio=(n,d)=>d?`${(n/d*100).toFixed(1)}%`:'暂无数据';
    return `<div class="v2-metrics">${[['研究企业',counts.researched,'按去重后的企业档案统计'],['我已认可',counts.accepted,`认可数 / 研究数：${ratio(counts.accepted,counts.researched)}`],['实际联系',counts.contacted,'仅人工记录，生成草稿不计入'],['收到回复',counts.replied,`回复数 / 联系数：${ratio(counts.replied,counts.contacted)}`],['有效询盘',counts.qualified,`有效询盘数 / 联系数：${ratio(counts.qualified,counts.contacted)}`]].map(([label,n,foot])=>`<div class="metric"><div class="metric-label">${label}</div><div class="metric-number">${n}</div><div class="metric-foot">${foot}</div></div>`).join('')}</div><p class="v2-stats-note">${state.workspace==='demo'?'当前为虚构示例空间；所有数量仅用于练习，不代表真实业务成果。':'统计范围为当前正式资料空间。网页研究与人工资料在卡片中分别标记；“发现企业”不表示获得客户。'} 联系、回复及有效询盘根据人工状态记录累计，后续阶段包含前序阶段。</p>`;
  }
  function renderResearch(){
    const taskCount=arr(ui.research?.tasks).length, pending=arr(ui.research?.prospects).filter(p=>p.review_status==='pending').length;
    return pageHead('BUYER DEVELOPMENT','先了解买家，再开始联系。','从供货资料出发，保存研究过程、来源依据和待确认项。每封开发邮件都由您审核并自行发送。',vbtn('人工录入企业','manual-prospect','', '',taskCount?'':'disabled')+vbtn('创建研究任务','new-task','primary'))+capabilityPanel()+statsPanel()+`<div class="v2-workspace-tabs" role="tablist" aria-label="买家开发"><button class="tab ${ui.tab==='tasks'?'active':''}" data-v2-action="tab" data-tab="tasks" role="tab" aria-selected="${ui.tab==='tasks'}">研究任务 <span class="count-tag">${taskCount}</span></button><button class="tab ${ui.tab==='prospects'?'active':''}" data-v2-action="tab" data-tab="prospects" role="tab" aria-selected="${ui.tab==='prospects'}">候选企业 ${pending?`<span class="count-tag">${pending} 待审核</span>`:''}</button><button class="tab ${ui.tab==='materials'?'active':''}" data-v2-action="tab" data-tab="materials" role="tab" aria-selected="${ui.tab==='materials'}">开发材料 <span class="count-tag">${arr(ui.research?.materials).length}</span></button><span class="v2-refresh">${ui.loading?'正在更新…':'资料本地保存'} ${vbtn('刷新','refresh','ghost small')}</span></div>${ui.tab==='tasks'?taskWorkspace():ui.tab==='prospects'?prospectWorkspace():materialWorkspace()}`;
  }
  function taskWorkspace(){
    const tasks=arr(ui.research?.tasks),t=pickedTask();
    if(!tasks.length)return `<section class="card">${empty('还没有买家研究任务','先选择已有产品、目标市场和调用上限，再开始研究。您也可以在任务中补充企业官网网址。') }<div class="v2-empty-action">${vbtn('创建第一个研究任务','new-task','primary')}</div></section>`;
    return `<div class="v2-task-layout"><section class="card v2-task-list"><div class="card-head"><h2>研究任务</h2><span class="tiny muted">${tasks.length} 个</span></div>${tasks.map(x=>`<button class="v2-task-row ${x.id===t?.id?'active':''}" data-v2-action="select-task" data-id="${x.id}"><span class="v2-task-heading"><strong>${e(x.title||'未命名研究')}</strong>${pill(taskLabels[x.status]||x.status,x.status==='running'?'blue':x.status==='completed'?'green':['failed','needs_configuration'].includes(x.status)?'amber':'gray')}</span><span class="small-text muted">${e(x.market||'市场待确认')} · ${x.test_market?'测试市场假设':'已选择目标市场'}</span><span class="tiny muted">${timeText(x.updated_at)} · 目标 ${e(x.target_count)} 家</span></button>`).join('')}</section><div>${t?taskDetail(t):''}</div></div>`;
  }
  function taskDetail(t){
    const results=arr(ui.research.prospects).filter(p=>arr(p.task_ids).includes(t.id));
    const usage=t.usage||{}, events=arr(t.events);
    const budget=(label,key,limit)=>`<div><span>${label}</span><strong>${Number(usage[key]||0)} <small>/ ${e(limit??0)}</small></strong><meter min="0" max="${Math.max(1,Number(limit||0))}" value="${Math.min(Number(usage[key]||0),Math.max(1,Number(limit||0)))}" aria-label="${label}用量"></meter></div>`;
    const action=t.status==='running'?vbtn('取消任务','cancel-task','danger small',t.id):vbtn(['failed','needs_configuration'].includes(t.status)?'重试剩余步骤':t.status==='draft'?'开始研究':'恢复 / 继续','start-task','primary small',t.id,`data-retry="${['failed','needs_configuration'].includes(t.status)}"`);
    return `<section class="card"><div class="card-head"><div><h2>${e(t.title||'买家研究任务')}</h2><p>${e(t.market)} · ${e(arr(t.buyer_types).join('、')||'买家类型待确认')} ${t.test_market?'· 明确标注的测试市场假设':''}</p></div><div class="actions">${action}${vbtn('补充人工资料','manual-prospect','small',t.id)}</div></div><div class="card-body"><div class="v2-task-facts"><div><span>产品范围</span><strong>${e(productNames(t.product_ids||arr(t.product_snapshots).map(p=>p.id)))}</strong></div><div><span>已确认供货条件</span><p>${e(t.confirmed_conditions||'暂无已确认条件')}</p></div><div><span>尚未确认</span><p>${e(t.unknown_conditions||'未填写；产品资质、价格与交期须另外核实')}</p></div></div><div class="v2-budget">${budget('搜索','search',t.search_limit)}${budget('模型','model',t.model_limit)}${budget('网页','pages',t.page_limit)}</div><p class="field-help">次数含失败尝试；取消、重试和恢复不重置预算。已保存的企业和来源继续保留。费用暂无可靠价格配置。</p>${arr(t.gaps).length?`<div class="v2-gaps"><strong>需要处理 / 本次研究缺口</strong>${list(t.gaps)}</div>`:''}${t.error||t.last_error?`<div class="banner error">${e(t.error||t.last_error)}</div>`:''}<details class="v2-details" open><summary>执行记录 <span class="tiny muted">${events.length} 条</span></summary>${events.length?`<ol class="v2-events">${events.slice(-10).map(ev=>`<li><time>${e(timeText(ev.at||ev.created_at))}</time><span>${e(ev.message||ev.stage||'已保存进度')}</span></li>`).join('')}</ol>`:'<p class="small-text muted">启动后会保存查询计划、阶段进度和异常记录。</p>'}${events.length>10?`<p class="field-help">显示最近 10 条，完整记录保存在任务备份中。</p>`:''}</details>${arr(t.queries||t.query_plan).length?`<details class="v2-details"><summary>搜索计划</summary>${list((t.queries||t.query_plan).map(q=>typeof q==='string'?q:q.query||q.text||''))}</details>`:''}${arr(t.seed_urls).length?`<details class="v2-details"><summary>人工提供的起始网址</summary><ul class="v2-list">${t.seed_urls.map(url=>`<li>${link(url)}</li>`).join('')}</ul></details>`:''}</div></section><div class="section-heading"><h3>本任务候选企业 <span class="count-tag">${results.length}</span></h3><span class="tiny muted">目标 ${e(t.target_count)} 家 · 不足时如实保留缺口</span></div>${results.length?`<div class="v2-prospect-grid">${results.map(prospectCard).join('')}</div>`:`<section class="card"><div class="empty-state compact"><h3>暂未取得候选企业</h3><p>研究完成前会逐步保存结果。缺少搜索或模型配置时，可以先提供官网和人工核实的资料。</p></div></section>`}`;
  }
  function prospectCard(p){
    return `<article class="card v2-prospect-card"><div class="v2-company-top"><div class="v2-company-mark">${e((p.name||'?').slice(0,1).toUpperCase())}</div><div><h3>${e(p.name||'企业名称待核验')}</h3><p>${e(p.country||'所在地待核验')} · ${e(p.business_type||'业务待核验')}</p></div></div><div class="v2-pill-row">${prospectProvenance(p)}${pill(reviewLabels[p.review_status]||'待我审核',p.review_status==='accepted'?'green':p.review_status==='excluded'?'gray':'amber')}${pill(classificationLabels[p.classification]||'身份待核验','gray')}</div><p class="v2-company-reason">${e(p.why_fit||'还没有足够资料说明匹配原因，请先核验企业经营范围。')}</p><div class="v2-mini-scores">${[['匹配',p.fit],['证据',p.evidence],['联系',p.contactability]].map(([label,score])=>`<span>${label}<strong>${e(levelLabels[score?.level]||'未知')}</strong></span>`).join('')}</div><div class="v2-card-footer"><span class="small-text muted">${e(contactLabels[p.contact_status]||'尚未联系')} · ${e(levelLabels[p.priority]||'未知')}优先级</span>${vbtn('查看与审核','prospect','small',p.id)}</div></article>`;
  }
  function prospectWorkspace(){
    const ps=arr(ui.research?.prospects).filter(p=>ui.filter==='all'||p.review_status===ui.filter);
    return `<div class="v2-filter-row"><label for="v2-prospect-filter">审核状态</label><select id="v2-prospect-filter"><option value="all">全部候选企业</option>${reviewOptions.map(o=>`<option value="${o.value}" ${ui.filter===o.value?'selected':''}>${o.label}</option>`).join('')}</select><span class="small-text muted">${ps.length} 家 · 按企业名称与官网域名去重</span></div>${ps.length?`<div class="v2-prospect-grid">${ps.map(prospectCard).join('')}</div>`:`<section class="card">${empty('暂无符合条件的企业','创建研究任务，或为已有任务录入人工核实的企业资料。')}</section>`}`;
  }
  function materialWorkspace(){
    const ms=arr(ui.research?.materials);
    return `<div class="banner info">${icon('file')}开发材料只保存为草稿；审核或复制邮件不会将企业标记为已联系。实际发送后，请在企业卡片中单独记录。</div>${ms.length?`<section class="card"><div class="table-wrap"><table><thead><tr><th>对象 / 主题</th><th>生成方式</th><th>审核</th><th>更新日期</th><th></th></tr></thead><tbody>${ms.map(m=>`<tr><td><strong>${e(pickedProspect(m.prospect_id)?.name||'企业待核验')}</strong><span class="sub">${e(m.subject||'主题待填写')}</span></td><td>${materialProvenance(m)}</td><td>${pill(m.reviewed?'已人工审核':'待我审核',m.reviewed?'green':'amber')}</td><td class="small-text muted">${e(timeText(m.updated_at))}</td><td>${vbtn('编辑草稿','material','small',m.id)}</td></tr>`).join('')}</tbody></table></div></section>`:`<section class="card">${empty('先选定一家企业','在候选企业卡片中查看来源、确认匹配，再准备具体的开发材料。')}</section>`}`;
  }
  function productSelector(selected=[]){
    return `<div class="field full"><span class="field-label">关联产品 <span class="required">*</span></span>${availableProducts().length?`<div class="v2-product-picker">${availableProducts().map(p=>`<label class="v2-product-choice"><input type="checkbox" name="product_ids" value="${p.id}" ${selected.includes(p.id)?'checked':''}><span><strong>${e(p.model||'型号待确认')}</strong><small>${e(p.name_en||p.name||'名称待确认')} · ${e(p.unit||'单位未知')}</small>${p.missing?.length?`<small class="v2-unknown">资料缺 ${p.missing.length} 项，未知值不会自动补全</small>`:''}</span></label>`).join('')}</div>`:`<div class="banner">请先到“产品资料”录入产品，再创建任务或采购页。</div>`}</div>`;
  }
  function taskModal(){
    modal('创建买家研究任务','先明确产品、市场和边界。系统在您设置的调用上限内研究，所有市场由您选择。',`<form data-v2 id="v2-task-form"><div class="error-box"></div><div class="form-grid">${field('任务名称','title','',{required:true,placeholder:'例如：MCB 经销商研究 — 第一批',full:true})}${productSelector()}${field('目标国家或地区','market','',{required:true,placeholder:'请明确填写目标市场，不会自动选择'})}${field('买家类型','buyer_types','经销商、进口商',{required:true,help:'用逗号、顿号或换行分隔，例如：经销商、进口商、设备制造商'})}<div class="inline-check full"><input id="v2-test-market" name="test_market" type="checkbox" ${state.workspace==='demo'?'checked':''}><label for="v2-test-market">本任务的市场是测试假设，仅用于验证流程</label></div>${field('已确认的供货条件','confirmed_conditions','',{area:true,rows:3,placeholder:'仅填写有依据且已经确认的条件；没有可留空'})}${field('尚未确认的供货条件','unknown_conditions','产品资质、价格、交期、目标市场合规要求尚待确认。',{area:true,rows:3})}</div><div class="section-title">任务预算</div><div class="v2-budget-form">${boundedNumber('目标企业数','target_count',3,1,20)}${boundedNumber('搜索调用上限','search_limit',5,0,20)}${boundedNumber('模型调用上限','model_limit',10,0,40)}${boundedNumber('网页读取上限','page_limit',10,0,50)}</div><p class="field-help">失败调用也计数；到达上限即停止并列出缺口。重试和恢复复用已取得的结果与剩余预算，不承诺凑足企业数量。</p><div class="space-top">${field('可选：已知企业官网或公开资料网址','seed_urls','',{area:true,rows:3,full:true,placeholder:'https://…\n每行一个网址，可在缺少搜索配置时使用',help:'网页访问和 AI 分析仍受本任务网页、模型上限控制。只提供公开业务资料网址。'})}</div><div class="inline-check space-top"><input id="v2-start-now" name="start_now" type="checkbox" checked><label for="v2-start-now">保存后开始研究；缺少能力时保存任务并显示配置提示</label></div>${formEnd('保存研究任务')}</form>`,true);
  }
  function boundedNumber(label,name,value,min,max){return `<div class="field"><label for="${name}">${e(label)}</label><input type="number" id="${name}" name="${name}" min="${min}" max="${max}" step="1" value="${value}" required><span class="field-help">${min}–${max} ${name==='target_count'?'家':'次'}</span></div>`;}
  function manualModal(taskId){
    const tasks=arr(ui.research?.tasks);if(!tasks.length){toast('请先创建研究任务，再为任务录入企业资料。',true);return;}
    modal('人工录入企业资料','这些内容会明确标记为人工资料；企业经营信息只是采购需求假设的依据。',`<form data-v2 id="v2-manual-form"><div class="error-box"></div><div class="form-grid">${selectField('所属研究任务','task_id',taskId||ui.taskId||tasks[0].id,tasks.map(t=>({value:t.id,label:t.title})))}${field('企业名称','name','',{required:true})}${field('官网网址','website','',{type:'url',placeholder:'https://…'})}${field('国家或地区','country','',{placeholder:'未知可留空'})}${field('业务类型','business_type','',{placeholder:'填写已经核实的经营类型'})}${field('资料来源网址','source_url','',{type:'url',placeholder:'https://…'})}${field('来源原文 / 人工提供的资料','source_text','',{required:true,area:true,rows:5,full:true,help:'粘贴支持判断的原文；没有依据的条件留作待确认。'})}${field('内部研究备注','notes','',{area:true,rows:3,full:true})}</div>${formEnd('保存人工资料')}</form>`,true);
  }
  function scoreDetail(label,value){return `<div class="v2-score"><span>${label}</span><strong>${e(levelLabels[value?.level]||'未知')}</strong>${list(value?.reasons,'暂无足够依据')}</div>`;}
  function factEvidence(p){
    const facts=Object.entries(p.facts||{}).filter(([,f])=>f&&f.value);if(!facts.length)return '';
    return `<details class="v2-details"><summary>企业事实与逐项原文依据 (${facts.length})</summary><div class="table-wrap"><table><thead><tr><th>判断字段</th><th>来源中的信息</th><th>支持原文</th></tr></thead><tbody>${facts.map(([k,f])=>`<tr><td>${e(({name:'企业名称',country:'所在地',business_type:'经营业务',product_signal:'产品线索'})[k]||k)}</td><td>${e(f.value)}</td><td><blockquote class="v2-fact-quote">${e(f.excerpt||'原文待补充')}</blockquote>${f.source_url?link(f.source_url,'查看来源'):''}</td></tr>`).join('')}</tbody></table></div><p class="field-help">摘录经过原文校验；网页身份、时效与企业当前情况仍须复核。</p></details>`;
  }
  function prospectModal(id){
    const p=pickedProspect(id);if(!p)return;
    const sources=arr(p.sources),ms=arr(ui.research.materials).filter(m=>m.prospect_id===p.id);
    modal(p.name||'候选企业','请核对来源、未知条件与不匹配因素。公开经营信息不能当作已确认的采购意向。',`<div class="v2-pill-row">${prospectProvenance(p)}${pill(classificationLabels[p.classification]||'身份待核验')}${pill(reviewLabels[p.review_status]||'待我审核',p.review_status==='accepted'?'green':p.review_status==='excluded'?'gray':'amber')}</div><div class="v2-company-meta">${link(p.website,p.domain||p.website||'官网待核验')}<span>${e(p.country||'国家或地区待核验')}</span><span>${e(p.business_type||'业务类型待核验')}</span></div><div class="v2-scores">${scoreDetail('产品匹配程度',p.fit)}${scoreDetail('证据充分程度',p.evidence)}${scoreDetail('联系可行性',p.contactability)}</div><p class="v2-priority-reason"><strong>${e(levelLabels[p.priority]||'未知')}优先级：</strong>${e(p.priority_reason||p.review_reason||'综合产品匹配、证据和联系可行性，需人工核验后决定。')}${p.ai_processed?' 企业分类为 AI 推断。':''}</p>${factEvidence(p)}<div class="v2-research-grid"><section><h3>为什么可能适合</h3><p>${e(p.why_fit||'暂无足够依据')}</p><h3>推荐产品</h3><p>${e(productNames(p.product_ids))}</p><h3>可能的采购需求 · 假设</h3><p>${e(p.demand_hypothesis||'需求尚待直接确认')}</p></section><section><h3>尚未确认的信息</h3>${list(p.unknowns,'请与企业直接核实需求、采购角色与技术要求')}<h3>不匹配因素</h3>${list(p.mismatches,'暂无已识别冲突；这不表示已确认匹配')}<h3>建议下一步</h3><p>${e(p.next_action||'核验官网与业务，再确认是否经营相关品类。')}</p></section></div><div class="section-title">公开联系入口</div>${arr(p.contacts).length?`<ul class="v2-contacts">${p.contacts.map(c=>`<li><strong>${e(({general_email:'公司通用邮箱',contact_page:'官方联系页面',phone:'公开电话'})[c.type]||'公开联系信息')}</strong><span>${c.type==='contact_page'?link(c.value):e(c.value)}</span><small>${c.type==='general_email'?'未验证可送达 · 不代表采购负责人联系方式':''}</small>${c.source_url?`<small>来源：${link(c.source_url,'查看原文')}</small>`:''}</li>`).join('')}</ul>`:'<p class="muted small-text">未取得可核实的公开联系入口，请继续检查官网。</p>'}<div class="section-title">关键来源与原文</div>${sources.length?sources.map((s,index)=>`<details class="v2-source" ${index===0?'open':''}><summary><span>${e(s.title||`来源 ${index+1}`)}</span>${pill(({official:'企业官网',directory:'行业目录',manual:'人工资料'})[s.kind]||'公开网页')}</summary><div class="v2-source-body">${s.url?link(s.url):'<span class="muted">未提供网址</span>'}<p class="tiny muted">查询日期：${e(s.retrieved_at?new Date(s.retrieved_at).toLocaleString('zh-CN'):'未记录')}</p><blockquote>${e(s.excerpt||'缺少支持判断的原文片段')}</blockquote>${arr(s.issues).length?`<div class="v2-gaps">${list(s.issues)}</div>`:''}</div></details>`).join(''):'<div class="banner">还没有来源证据，当前判断均需人工核验。</div>'}<div class="section-title">我的判断与实际跟进</div><form data-v2 id="v2-prospect-form" data-id="${p.id}"><div class="error-box"></div><div class="form-grid">${selectField('审核决定','review_status',p.review_status||'pending',reviewOptions)}${selectField('优先级','priority',p.priority||'low',priorityOptions)}${field('认可、排除或调整原因','review_reason',p.review_reason,{area:true,rows:2,full:true,help:'后续任务可以参考这些反馈；少量反馈不会被描述为已经训练出的模型。'})}${selectField('实际联系结果','contact_status',p.contact_status||'new',contactOptions,'仅在已发生后记录；生成邮件不算已联系。')}${field('实际结果备注','contact_note',p.contact_note,{area:true,rows:2,help:'例如发送日期、回复要点、询盘关联或不合适的原因'})}</div><div class="form-actions"><button type="submit" class="btn primary">保存审核与跟进</button></div></form><div class="section-title">准备有针对性的开发材料</div><div class="actions">${vbtn('AI 生成开发草稿','generate-material','primary',p.id,ui.research?.capabilities?.model?'':'disabled title="请先在本机 .env 配置模型"')}${vbtn('使用人工辅助模板','manual-material','',p.id)}${vbtn('创建买家采购页草稿','page-for-prospect','',p.id)}</div><p class="field-help">AI 生成会计入关联研究任务的模型预算。只使用有依据的企业和产品资料；草稿不会自动发送，也不承诺资质、库存、价格或交期。</p>${ms.length?`<div class="v2-material-links">${ms.map(m=>vbtn(`${m.reviewed?'已审核':'待审核'} · ${e(m.subject||'开发草稿')}`,'material','small',m.id)).join('')}</div>`:''}${arr(p.history).length?`<details class="v2-details"><summary>人工修改记录 (${p.history.length})</summary><ul class="v2-events">${p.history.slice(-8).reverse().map(h=>`<li><time>${e(timeText(h.at||h.created_at))}</time><span>${e(h.message||h.reason||h.note||h.review_reason||h.contact_note||h.after?.review_reason||h.after?.contact_note||'已保存人工修改')}</span></li>`).join('')}</ul></details>`:''}`,true);
  }
  function materialModal(id){
    const m=pickedMaterial(id);if(!m)return;
    modal(`开发草稿 · ${pickedProspect(m.prospect_id)?.name||'候选企业'}`,'内容可编辑。每次修改都需重新审核；复制或审核不等于已发送。',`<div class="v2-pill-row">${materialProvenance(m)}${pill(m.reviewed?'已人工审核':'尚未审核',m.reviewed?'green':'amber')}</div><form data-v2 id="v2-material-form" data-id="${m.id}"><div class="error-box"></div><div class="form-grid">${field('联系理由','reason',m.reason,{area:true,rows:2,full:true})}<div class="field full"><label>推荐产品</label><p class="v2-static-value">${e(productNames(m.product_ids))}</p></div>${field('英文邮件主题','subject',m.subject,{required:true,full:true})}${field('英文邮件正文','body',m.body,{area:true,rows:12,required:true,full:true})}${field('低门槛的下一步','next_step',m.next_step,{area:true,rows:2,full:true})}</div><div class="inline-check space-top"><input type="checkbox" id="v2-material-reviewed" name="reviewed"><label for="v2-material-reviewed">我已核对企业事实、推荐产品和所有表述，确认这份草稿可由我自行发送</label></div>${formEnd('保存开发草稿',vbtn('复制主题与正文','copy-material','small',m.id))}</form>`,true);
  }
  function pageSnapshot(snapshot){
    const s=snapshot||{};
    return `<div class="v2-public-preview"><span class="v2-preview-label">买家可见内容 · 本地草稿预览</span><h2>${e(s.title||'采购需求页面')}</h2><p class="v2-intro">${e(s.intro||'页面介绍尚未填写')}</p>${s.buyer_type?`<p class="small-text muted">适用对象：${e(s.buyer_type)}</p>`:''}${s.demo?'<div class="banner">FICTIONAL SAMPLE · 本页产品来自虚构示例，仅供练习。</div>':''}<div class="v2-public-products">${arr(s.products).map(p=>`<article><h3>${e(p.name_en||p.model||'Product')}</h3><strong class="mono">${e(p.model||'Model to be confirmed')}</strong><p>${e(p.category||'')} ${p.unit?`· Unit: ${e(p.unit)}`:''}</p><dl>${Object.entries(p.specs||{}).map(([k,v])=>`<dt>${e(({poles:'Poles',current_a:'Rated current (A)',voltage_v:'Rated voltage (V)',curve:'Tripping curve',breaking_ka:'Breaking capacity (kA)'})[k]||k)}</dt><dd>${e(v||'To be confirmed')}</dd>`).join('')}</dl><p class="tiny muted">Source updated: ${e(p.source_updated_at||'Unknown')}</p></article>`).join('')}</div><div class="v2-preview-form"><strong>Request a quotation</strong><p>买家可填写型号、需求、联系方式，并上传采购清单。提交后进入内部“客户与询盘”，由您审核和处理。</p></div></div>`;
  }
  function renderPages(){
    const pages=arr(ui.pages?.pages);
    return pageHead('BUYER PROCUREMENT PAGE','把兴趣，接成一份清楚的需求。','只展示经您确认的对外产品资料。买家无需注册即可提交需求；私人资料与报价保留在内部工作台。',vbtn('创建采购页草稿','new-page','primary'))+`<div class="banner info">${icon('external')}<div><strong>当前仅支持本机预览，尚未上线。</strong> 本地链接不能供海外买家访问。公开部署、HTTPS 与域名配置完成后，才能用于独立站、嵌入或社媒分享；详见 docs/PORTAL.md。</div></div><div class="v2-page-grid">${pages.length?pages.map(p=>`<article class="card v2-page-card"><div class="card-head"><div><h2>${e(p.title||'采购页草稿')}</h2><p>${e(p.buyer_type||'适用买家类型待填写')} · ${arr(p.product_ids).length} 个产品</p></div>${pill(p.status==='approved'?'已批准对外内容':'草稿 · 不可公开读取',p.status==='approved'?'green':'amber')}</div><div class="card-body"><p class="v2-page-intro">${e(p.intro||p.draft_snapshot?.intro||'未填写页面介绍')}</p><p class="small-text muted">${provenance(p.generation_mode||'manual_template')} · ${arr(p.versions).length} 个批准版本</p><div class="actions">${vbtn('编辑草稿','edit-page','small',p.id)}${vbtn(p.status==='approved'?'查看已批准内容':'审核对外内容','review-page','small',p.id)}${p.status==='approved'?`<a class="btn small primary" href="${e(safeUrl(p.url))}" target="_blank" rel="noopener noreferrer">打开本地采购页 ${icon('external')}</a>`:''}</div>${p.status==='approved'?`<div class="v2-page-links"><label>本地分享链接（尚未公开部署）</label><code>${e(p.url)}</code><div class="actions">${vbtn('复制本地链接','copy-page-link','ghost small',p.id)}${vbtn('复制嵌入入口','copy-page-embed','ghost small',p.id)}${vbtn('撤回对外内容','unpublish-page','danger small',p.id)}</div></div>`:`<p class="field-help">请先核对对外内容并批准，才能打开买家侧预览。修改已批准页面会撤回原公开内容并要求重新审核。</p>`}</div></article>`).join(''):`<section class="card v2-page-empty">${empty('还没有采购页面','从产品库选择适合对外介绍的产品，生成页面草稿，再逐项核对并批准。')}</section>`}</div><section class="card"><div class="card-body"><h2>收到需求后，沿用原有询盘流程</h2><p class="muted small-text">买家提交的原文与采购清单内容先保存到本地询盘。进入“客户与询盘”后使用现有 AI 提取、人工选型、报价与跟进流程。买家端没有读取客户、报价、供应商成本或研究记录的接口。</p><a class="btn small" href="#inquiries">打开客户与询盘 ${icon('arrow')}</a></div></section>`;
  }
  function pageModal(id,prospectId){
    const p=id?pickedPage(id):{},prospect=prospectId?pickedProspect(prospectId):null;
    modal(id?'编辑买家采购页草稿':'创建买家采购页草稿','产品取自现有产品库；可选择谨慎模板或已配置的 AI 辅助生成。请用适合对外展示的英文填写标题和介绍。',`<form data-v2 id="v2-page-form" data-id="${id||''}" data-prospect="${prospectId||p?.prospect_id||''}"><div class="error-box"></div>${id?'<div class="banner">保存修改会将页面恢复为草稿并停止公开读取，审核通过后才能重新预览。</div>':''}<div class="form-grid">${!id?selectField('页面草稿生成方式','mode','manual',[{value:'manual',label:'人工辅助模板'},...(ui.research?.capabilities?.model?[{value:'live',label:'AI 辅助草稿 · 单独调用 1 次模型'}]:[])],'AI 生成单独计为 1 次模型调用，不占研究任务预算；仍须人工审核。'):''}${field('对外标题（英文）','title',p?.title||'Electrical Product Sourcing Inquiry',{required:true,full:true})}${field('适用买家类型','buyer_type',p?.buyer_type||'',{placeholder:'例如：Distributors / Importers',full:true})}${productSelector(p?.product_ids||prospect?.product_ids||[])}${field('页面介绍（英文，可留空使用谨慎模板）','intro',p?.intro||p?.draft_snapshot?.intro||'',{area:true,rows:4,full:true,help:'只陈述已确认的事实；不填写未经确认的认证、现货、价格优势或交付承诺。'})}</div><p class="small-text muted">草稿和批准版本分别保存。对外产品字段限定为名称、型号、分类、技术参数、原始单位和资料日期，未填参数保持未知。</p>${formEnd('保存采购页草稿')}</form>`,true);
  }
  function pageReview(id){
    const p=pickedPage(id);if(!p)return;
    const approved=p.status==='approved';
    modal(approved?'已批准的买家可见内容':'审核买家可见内容','下方为买家端读取的独立资料快照。采购成本、供应商信息、研究判断及其他客户资料不会出现在这里。',`${pageSnapshot(approved?p.public_snapshot:p.draft_snapshot)}${approved?`<div class="form-actions">${btn('关闭','close-modal')}<a class="btn primary" href="${e(safeUrl(p.url))}" target="_blank" rel="noopener noreferrer">打开本地采购页</a></div>`:`<form data-v2 id="v2-page-approve-form" data-id="${id}" data-version="${e(p.updated_at)}"><div class="error-box"></div><div class="inline-check space-top"><input id="v2-page-confirm" name="confirmed" type="checkbox" required><label for="v2-page-confirm">我已逐项核对以上产品资料与对外表述，批准本版在买家页展示；未知条件仍需另行确认</label></div>${formEnd('批准本版对外内容')}</form>`}`,true);
  }
  async function copyText(value){
    if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(value);toast('已复制。');return;}
    modal('复制内容','请选择下方内容并复制。',`<textarea class="v2-copy-box" readonly rows="8">${e(value)}</textarea><div class="form-actions">${btn('关闭','close-modal')}</div>`);$('.v2-copy-box').select();
  }
  document.addEventListener('click',async event=>{
    const el=event.target.closest('[data-v2-action]');if(!el||el.disabled)return;
    const action=el.dataset.v2Action,id=Number(el.dataset.id);
    if(action==='new-task')return taskModal();
    if(action==='manual-prospect')return manualModal(id||ui.taskId);
    if(action==='select-task'){ui.taskId=id;return render();}
    if(action==='tab'){ui.tab=el.dataset.tab;return render();}
    if(action==='prospect')return prospectModal(id);
    if(action==='material')return materialModal(id);
    if(action==='new-page')return pageModal();
    if(action==='edit-page')return pageModal(id);
    if(action==='review-page')return pageReview(id);
    if(action==='page-for-prospect')return pageModal(null,id);
    await work(el,async()=>{
      if(action==='refresh'){await reload();return;}
      if(action==='start-task'||action==='cancel-task'){
        const endpoint=action==='cancel-task'?'cancel':el.dataset.retry==='true'?'retry':'start';
        await api(`/api/research/tasks/${id}/${endpoint}`,{method:'POST',body:{}});await reload();toast(action==='cancel-task'?'已请求取消；已取得的结果会保留。':'任务已提交，执行状态和缺口会持续保存。');
      } else if(action==='generate-material'||action==='manual-material'){
        let m;try{m=await api(`/api/research/prospects/${id}/materials`,{method:'POST',body:{mode:action==='generate-material'?'live':'manual'}});}catch(error){await reload();throw error;}await reload();materialModal(m.id);toast('开发草稿已保存，请核对事实与表述。');
      } else if(action==='copy-material'){
        const f=$('#v2-material-form');await copyText(`Subject: ${$('[name=subject]',f).value}\n\n${$('[name=body]',f).value}`);
      } else if(action==='copy-page-link')await copyText(pickedPage(id)?.url||'');
      else if(action==='copy-page-embed')await copyText(pickedPage(id)?.embed_code||'');
      else if(action==='unpublish-page'){
        await api(`/api/buyer-pages/${id}/unpublish`,{method:'POST',body:{}});await reload();toast('已撤回，买家端不再读取该页面。');
      }
    });
  });
  document.addEventListener('submit',async event=>{
    const f=event.target;if(!(f instanceof HTMLFormElement)||!f.hasAttribute('data-v2'))return;
    event.preventDefault();const id=Number(f.dataset.id),button=event.submitter||$('button[type=submit]',f);
    await work(button,async()=>{
      $('.error-box',f)?.replaceChildren();const data=values(f);
      if(f.id==='v2-task-form'){
        data.product_ids=$$('[name=product_ids]:checked',f).map(x=>Number(x.value));if(!data.product_ids.length)throw new Error('请至少选择一个已有产品。');
        data.buyer_types=data.buyer_types.split(/[,，、\n]/).map(x=>x.trim()).filter(Boolean);data.seed_urls=data.seed_urls.split(/\n/).map(x=>x.trim()).filter(Boolean);data.test_market=$('[name=test_market]',f).checked;
        for(const k of ['target_count','search_limit','model_limit','page_limit'])data[k]=Number(data[k]);
        const start=$('[name=start_now]',f).checked;delete data.start_now;
        const t=await api('/api/research/tasks',{method:'POST',body:data});ui.taskId=t.id;ui.tab='tasks';
        let startError='';if(start){try{await api(`/api/research/tasks/${t.id}/start`,{method:'POST',body:{}});}catch(err){startError=err.message;}}
        closeModal();await reload();render();toast(startError?`任务已保存；启动未完成：${startError}`:'研究任务已保存。',!!startError);
      } else if(f.id==='v2-manual-form'){
        const taskId=Number(data.task_id);delete data.task_id;const p=await api(`/api/research/tasks/${taskId}/manual`,{method:'POST',body:data});await reload();prospectModal(p.id);toast('人工资料已保存，相同企业会复用档案。');
      } else if(f.id==='v2-prospect-form'){
        await api(`/api/research/prospects/${id}`,{method:'PATCH',body:data});await reload();prospectModal(id);toast('审核与实际结果已保存。');
      } else if(f.id==='v2-material-form'){
        const reviewed=$('[name=reviewed]',f).checked;delete data.reviewed;await api(`/api/research/materials/${id}`,{method:'PUT',body:{...data,reviewed:false}});if(reviewed)await api(`/api/research/materials/${id}`,{method:'PUT',body:{reviewed:true}});closeModal();await reload();render();toast(reviewed?'开发草稿及人工审核已保存；尚未发送。':'修改已保存为待审核草稿。');
      } else if(f.id==='v2-page-form'){
        data.product_ids=$$('[name=product_ids]:checked',f).map(x=>Number(x.value));if(!data.product_ids.length)throw new Error('请至少选择一个产品。');
        if(f.dataset.prospect)data.prospect_id=Number(f.dataset.prospect);if(!id&&!data.intro.trim())delete data.intro;
        const p=await api(id?`/api/buyer-pages/${id}`:'/api/buyer-pages',{method:id?'PUT':'POST',body:data});await reload();pageReview(p.id);toast('采购页草稿已保存，请审核对外内容。');
      } else if(f.id==='v2-page-approve-form'){
        await api(`/api/buyer-pages/${id}/approve`,{method:'POST',body:{confirmed_public:true,expected_updated_at:f.dataset.version}});closeModal();await reload();if(state.view!=='buyer-pages')navigate('buyer-pages');else render();toast('本版对外内容已批准，可在本机预览。当前尚未公开部署。');
      }
    });
  });
  document.addEventListener('change',event=>{if(event.target.id==='v2-prospect-filter'){ui.filter=event.target.value;render();}});
  $('#modal').addEventListener('close',()=>{if(isView(state.view))render();});
  setInterval(()=>{if(isView(state.view)&&!$('#modal').open&&arr(ui.research?.tasks).some(t=>t.status==='running'))load(true);},3000);
  return {isView,render(view){
    resetWorkspace();
    if(!ui.research||!ui.pages){if(!ui.loading&&!ui.error)setTimeout(()=>load(true),0);return `${pageHead('BUYER DEVELOPMENT',view==='research'?'买家开发':'买家采购页','正在打开本地业务资料…')}<section class="card"><div class="loading-state">${ui.error?`${e(ui.error)}<div class="space-top">${vbtn('重新加载','refresh','primary')}</div>`:'正在读取任务、企业与页面…'}</div></section>`;}
    const error=ui.error?`<div class="banner error">${e(ui.error)} ${vbtn('重试','refresh','small')}</div>`:'';
    return `<div class="v2-workspace">${error}${view==='research'?renderResearch():renderPages()}</div>`;
  },reload};
})();
