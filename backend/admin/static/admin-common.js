/* 复盘运营台 · 页面公共脚本（所有后台独立页共用）
 * 依赖：页面 <body data-base="__ADMIN_BASE__">，服务端渲染时替换占位符。
 * 提供：q/esc/toast/api/fmtDate/dur/rel、顶栏时钟、当前用户、
 *       采集任务/触发来源的中文词表、分页条渲染。
 * 后台为单账号，角色体系已移除；用户名由 ADMIN_USER 配置。
 */
(function(){
  function init(){
  const BASE = document.body.dataset.base || '';
  window.ADMIN_BASE = BASE;
  window.q = s => document.querySelector(s);
  window.esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  let _toastT = null;
  window.toast = function(msg, type){
    const t = q('#toast'); if(!t) return;
    q('#toastMsg').textContent = msg;
    t.className = 'toast show ' + (type === 'err' ? 'err' : '');
    clearTimeout(_toastT); _toastT = setTimeout(() => t.className = 'toast', 2600);
  };

  let _lastApiErr = 0;
  function apiErr(msg){
    // 轮询场景：同类错误 15 秒内只提示一次
    const now = Date.now();
    if(now - _lastApiErr < 15000) return;
    _lastApiErr = now;
    try{ toast(msg, 'err'); }catch(e){}
  }
  window.apiErr = apiErr;

  window.api = async function(url, opt){
    let r;
    try{ r = await fetch(BASE + url, opt); }
    catch(e){ apiErr('网络异常，请检查服务连接'); throw e; }
    if(r.status === 401){ location.href = BASE + '/login'; throw new Error('未登录'); }
    if(!r.ok){
      let msg = '请求失败 (' + r.status + ')';
      try{ const d = await r.json(); msg = d.detail || d.msg || msg; }catch(e){}
      apiErr(msg);
      throw new Error(msg);
    }
    return r.json();
  };

  window.fmtDate = d => d ? String(d).slice(0, 10) : '—';
  window.dur = function(a, b){
    if(!a || !b) return '—';
    const s = (new Date(String(b).replace(' ', 'T')) - new Date(String(a).replace(' ', 'T'))) / 1000;
    if(!isFinite(s) || s < 0) return '—';
    return s < 60 ? s.toFixed(1) + 's' : (s / 60).toFixed(1) + 'm';
  };
  window.rel = function(ts){
    if(!ts) return '—';
    const d = new Date(String(ts).replace(' ', 'T'));
    const diff = (Date.now() - d.getTime()) / 1000;
    if(diff < 0) return fmtDate(ts);
    if(diff < 60) return Math.max(1, Math.round(diff)) + ' 秒前';
    if(diff < 3600) return Math.round(diff / 60) + ' 分钟前';
    if(diff < 86400) return Math.round(diff / 3600) + ' 小时前';
    if(diff < 86400 * 30) return Math.round(diff / 86400) + ' 天前';
    return fmtDate(ts);
  };

  /* ---------- 采集任务词表 ---------- */
  /* 数据库里存的是英文 key，后台一律翻成中文并给出解释（title 悬浮） */
  window.TASK_TYPE = {
    full:     { name:'收盘全量', cls:'full',
                desc:'收盘后跑完整流程：指数、成交额、资金流、涨停池、情绪分，并落定当日复盘快照' },
    intraday: { name:'盘中轻量', cls:'intraday',
                desc:'盘中定时刷新指数与成交额，只更新当日实时字段，不落复盘快照' }
  };
  window.TYPE_OPTIONS = [
    { v:'',         name:'全部类型' },
    { v:'full',     name:'收盘全量' },
    { v:'intraday', name:'盘中轻量' }
  ];
  window.taskType = function(v){
    const m = TASK_TYPE[v];
    return m ? m : { name: v || '未知任务', cls:'mute', desc: v ? ('未收录的任务类型：' + v) : '任务类型缺失' };
  };

  /* ---------- 触发来源 ----------
     库里 trigger_by 取值很杂：scheduler / admin / manual / manual-est /
     manual-fund / backfill / backfill_year / backfill_history / backfill_estimated …
     统一归成四类，原始值放在 tooltip 里备查。 */
  window.SRC_OPTIONS = [
    { v:'daily',     name:'日常任务', hint:'排除历史回补' },
    { v:'',          name:'全部来源', hint:'含历史回补' },
    { v:'scheduler', name:'定时调度', hint:'内置调度器按交易日历自动执行' },
    { v:'admin',     name:'后台手动', hint:'在「采集任务」页点开始采集' },
    { v:'manual',    name:'手动补采', hint:'命令行工具补采' },
    { v:'backfill',  name:'历史回补', hint:'历史数据回填工具批量写入' }
  ];
  window.triggerSrc = function(v){
    const raw = String(v || '');
    if(!raw) return { name:'未知来源', cls:'mute', raw:'—' };
    if(raw === 'scheduler') return { name:'定时调度', cls:'auto', raw };
    if(raw === 'admin')     return { name:'后台手动', cls:'manual', raw };
    if(raw.indexOf('manual') === 0)   return { name:'手动补采', cls:'manual', raw };
    if(raw.indexOf('backfill') === 0) return { name:'历史回补', cls:'backfill', raw };
    return { name: raw, cls:'mute', raw };
  };

  window.statusText = s => s === 'success' ? '成功' : s === 'failed' ? '失败' : s === 'running' ? '运行中' : (s || '—');

  /* 触发来源后面跟一段原始值：scheduler/admin 与中文名一一对应就不必显示，
     manual-est / backfill_year 这类细分值留在旁边，排查时能看出是谁写的数据 */
  window.rawSuffix = function(raw){
    const v = String(raw || '');
    if(!v || v === 'scheduler' || v === 'admin' || v === 'manual' || v === 'backfill') return '';
    return `<span class="raw">${esc(v)}</span>`;
  };

  /* 任务耗时：优先用库里落好的 duration_s（无 started/finished 时只能靠它），
     缺失再按起止时间算。 */
  window.logDur = function(r){
    const v = r && r.duration_s;
    if(v != null && v !== '' && isFinite(Number(v))){
      const s = Number(v);
      return s < 60 ? s.toFixed(1) + 's' : (s / 60).toFixed(1) + 'm';
    }
    return dur(r && r.started_at, r && r.finished_at);
  };

  /* ---------- 分页条 ----------
     用法：renderPager(el, {page,pages,total,page_size}, p => load(p))
     页码窗口：首页、末页、当前页 ±1，其余折叠成省略号 */
  window.renderPager = function(el, meta, onGo){
    if(!el) return;
    const { page, pages, total } = meta;
    if(!total){ el.innerHTML = ''; return; }
    const btn = (label, p, opt) => {
      const o = opt || {};
      const dis = o.disabled ? ' disabled' : '';
      const on = o.active ? ' on' : '';
      return `<button class="pg-btn${on}"${dis} data-p="${p}">${label}</button>`;
    };
    const nums = [];
    const push = p => nums.push(p);
    const near = new Set([1, pages, page - 1, page, page + 1].filter(p => p >= 1 && p <= pages));
    const sorted = [...near].sort((a, b) => a - b);
    let prev = 0;
    sorted.forEach(p => {
      if(prev && p - prev > 1) push('…');
      push(p);
      prev = p;
    });
    const body = nums.map(p => p === '…'
      ? '<span class="pg-gap">…</span>'
      : btn(p, p, { active: p === page })
    ).join('');
    el.innerHTML = `<span class="pg-info">共 <b>${total}</b> 条 · 第 ${page}/${pages} 页</span>
      <span class="pg-ctrl">
        ${btn('‹ 上一页', page - 1, { disabled: page <= 1 })}
        ${body}
        ${btn('下一页 ›', page + 1, { disabled: page >= pages })}
      </span>`;
    el.onclick = e => {
      const b = e.target.closest('.pg-btn'); if(!b || b.disabled) return;
      const p = Number(b.dataset.p);
      if(p && p !== page) onGo(p);
    };
  };

  /* 顶栏时钟 */
  window.tickClock = function(){
    const el = q('#clock'); if(!el) return;
    const d = new Date();
    const wk = '日一二三四五六'[d.getDay()];
    const p = n => String(n).padStart(2, '0');
    el.innerHTML = `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} 周${wk} <b>${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}</b>`;
  };

  /* 当前登录用户（后台只有一个账号，只显示名字，无角色徽标） */
  window.loadMe = async function(){
    let d = null;
    try{ d = await api('/api/me'); }catch(e){ return null; }
    if(!d || !d.username) return d;
    const nm = q('#userName'), av = q('#userAv');
    if(nm) nm.textContent = d.username;
    if(av) av.textContent = d.username.slice(0, 1).toUpperCase();
    return d;
  };

  /* 退出登录：走 POST 并撤销服务端会话，避免 GET 被跨站导航强制登出 */
  document.addEventListener('click', async function(e){
    const a = e.target && e.target.closest ? e.target.closest('[data-logout]') : null;
    if(!a) return;
    e.preventDefault();
    try{ await fetch(BASE + '/logout', {method:'POST', credentials:'same-origin'}); }
    catch(err){}
    location.href = BASE + '/login';
  });
  }
  // 防御：本脚本若被放回 <head>（body 尚不存在），挂到 DOMContentLoaded 再初始化
  if(document.body) init();
  else document.addEventListener('DOMContentLoaded', init);
})();
