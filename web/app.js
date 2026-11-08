/* 人格中枢 — 前端逻辑
   无框架，原生 DOM。所有请求走统一 api() 封装，错误一律 toast + 状态栏。 */

const S = {
  personas: [],
  adapters: [],
  selPersona: null,
  selAdapters: new Set(),
  vars: {},
  preview: null,
  applied: false,
  auditPin: null,     // 钉住的统计对象：会话名。不清掉就不会自己换人
  editingId: null,
  injections: [],
  injSummary: null,
  injSelected: new Set(),
  guideStep: 1,
  version: '',
  budget: [],
};

const GUIDE_KEY = 'ph-guide-v1-done';
const $ = (id) => document.getElementById(id);
const qs = new URLSearchParams(location.search);

/* ------------------------------------------------------------------ 请求 */
async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      // 没有超时的请求会永远挂着：后端卡在磁盘或网络时，界面上表现为
      // 无限 loading，用户点第二次、第三次，问题被叠成一片。
      signal: AbortSignal.timeout ? AbortSignal.timeout(20000) : undefined,
      ...opts,
    });
  } catch (e) {
    // fetch 抛的是 TypeError: Failed to fetch —— 对用户等于没说。
    // 这个错误只有一个意思：连不上后端（进程已退出 / 端口没了）。
    markServiceDown();
    throw new Error('连不上后台服务，进程可能已经退出。关掉这个窗口，从桌面或开始菜单重新打开人格中枢即可。');
  }
  let payload;
  try {
    payload = await res.json();
  } catch {
    throw new Error(`服务返回非 JSON（HTTP ${res.status}）`);
  }
  if (!payload.ok) {
    const e = payload.error || {};
    throw new Error(`[${e.code || 'ERROR'}] ${e.message || '未知错误'}`);
  }
  return payload.data;
}

// 后台服务没了的时候，页面必须自己说出来。
// 之前的表现是每个页签各甩一句 "Failed to fetch" —— 用户不知道是网络、是软件、
// 还是自己点错了。这条横幅只出现一次，恢复后自动消失。
let svcDownShown = false;
let svcDownEl = null;

function markServiceDown() {
  if (svcDownShown || !document.body) return;
  svcDownShown = true;
  const d = document.createElement('div');
  d.textContent = '后台服务已退出，这个窗口里的操作都不会生效。关掉窗口，重新打开人格中枢即可。';
  d.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:9999;padding:10px 16px;'
    + 'background:#7f1d1d;color:#fff;font-size:13px;text-align:center;cursor:default';
  document.body.appendChild(d);
  svcDownEl = d;          // 持引用而不是按 id 查：工具会按 id 校验元素是否存在，动态节点必然报“不存在”
}

function clearServiceDown() {
  if (!svcDownShown) return;
  svcDownShown = false;
  if (svcDownEl) { svcDownEl.remove(); svcDownEl = null; }
}

function status(msg, kind = 'ok') {
  $('status').textContent = msg;
  const dot = $('status-dot');
  dot.className = 'status-dot' + (kind === 'busy' ? ' busy' : kind === 'err' ? ' err' : '');
}

let toastTimer = null;function toast(msg, isErr = false) {
  const t = $('toast');
  t.textContent = msg;
  t.hidden = false;
  t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = 'toast'; }, isErr ? 5600 : 2800);
}

function busy(btn, on) {
  if (!btn) return;
  btn.classList.toggle('loading', on);
  btn.disabled = on;
}

/* ------------------------------------------------------------------ 模态确认 */
let modalResolve = null;
function confirmModal({ title, body, okText = '确定', danger = false }) {
  $('modal-title').textContent = title;
  $('modal-body').innerHTML = body;
  const ok = $('modal-ok');
  ok.textContent = okText;
  ok.className = 'btn ' + (danger ? 'danger' : 'primary');
  $('modal').hidden = false;
  return new Promise((resolve) => { modalResolve = resolve; });
}
function closeModal(v) {
  $('modal').hidden = true;
  if (modalResolve) { modalResolve(v); modalResolve = null; }
}

/* ------------------------------------------------------------------ 主题 */
function applyTheme(mode) {
  document.documentElement.dataset.theme = mode;
  localStorage.setItem('ph-theme', mode);
}
function initTheme() {
  applyTheme(localStorage.getItem('ph-theme') || 'auto');
  $('theme-btn').onclick = () => {
    const cur = document.documentElement.dataset.theme;
    applyTheme(cur === 'auto' ? 'light' : cur === 'light' ? 'dark' : 'auto');
  };
}

/* ------------------------------------------------------------------ 标签页 */
function activatePanel(name) {
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.panel === name));
  document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('active', p.id === 'panel-' + name));
  // 面板只剩三个：人格编辑已并入注入页的抽屉，不再是一个独立页面
  const hint = {
    apply: '选人格、选目标，预览后写入',
    injections: '扫描并管理本机所有注入',
    vault: '每次写入前自动存档',
  }[name];
  if (name === 'vault') loadVault();
  if (name === 'injections') loadInjections();
  if (hint) status(hint);
}
function initTabs() {
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.onclick = () => activatePanel(btn.dataset.panel);
  });
  const start = qs.get('panel');
  // 兼容旧链接：panel=persona 已经并入注入页，改成直接打开编辑抽屉
  if (start === 'persona') {
    activatePanel('apply');
    setTimeout(() => openEditor(S.selPersona), 400);
    return;
  }
  if (start && document.querySelector(`.tab[data-panel="${start}"]`)) activatePanel(start);
}

/* ------------------------------------------------------------------ 引导 */
function showGuide(step) {
  S.guideStep = step;
  $('guide').hidden = false;
  document.querySelectorAll('.guide-step').forEach((el) => {
    el.hidden = Number(el.dataset.g) !== step;
  });
  const total = document.querySelectorAll('.guide-step').length;
  $('guide-dots').innerHTML = Array.from({ length: total }, (_, i) =>
    `<i class="${i + 1 === step ? 'on' : ''}"></i>`).join('');
  $('guide-next').textContent = step === total ? '开始使用' : '下一步';
}
/**
 * 「引导看过了」这件事存在哪儿，比它存在不在重要得多。
 *
 * localStorage 按 origin 隔离，而桌面外壳每次启动都会让系统随机分配端口
 * （127.0.0.1:50065、:51914、:53667……），**每个端口都是一个新的 origin**，
 * 于是上一次写的标记下一次根本读不到 —— 表现就是"每次打开都弹使用指引"。
 * 实测桌面版的 leveldb 里躺着 8 个不同端口的 origin，每个都各写了一份标记。
 *
 * 所以外壳在的时候优先问主进程（存 userData，与端口无关）；
 * 浏览器直连时没有 phUI，退回 localStorage —— 那种场景端口是固定的，不会分家。
 */
async function guideSeen() {
  let shell = null;
  try {
    if (window.phUI && window.phUI.getFlag) shell = await window.phUI.getFlag(GUIDE_KEY);
  } catch {
    /* 外壳接口不可用就只看 localStorage */
  }
  let local = null;
  try {
    local = localStorage.getItem(GUIDE_KEY);
  } catch {
    /* 隐私模式禁用了存储 */
  }
  return !!shell || !!local;
}

function markGuideSeen() {
  try {
    if (window.phUI && window.phUI.setFlag) window.phUI.setFlag(GUIDE_KEY, '1');
  } catch {
    /* 外壳接口不可用只写本地 */
  }
  try {
    localStorage.setItem(GUIDE_KEY, '1');
  } catch {
    /* 存不了就算了，不影响本次使用 */
  }
}

function endGuide() {
  $('guide').hidden = true;
  markGuideSeen();
}

/* ------------------------------------------------------------------ 侧栏 */
function initSide() {
  $('side-toggle').onclick = () => {
    const side = $('side');
    const btn = $('side-toggle');
    const collapsed = side.classList.toggle('collapsed');
    btn.classList.toggle('collapsed', collapsed);
    btn.title = collapsed ? '展开侧栏' : '收起侧栏';
  };
}

/* ------------------------------------------------------------------ 渲染：侧栏 */
function renderPersonas() {
  const ul = $('persona-list');
  ul.innerHTML = '';
  if (!S.personas.length) {
    ul.innerHTML = '<li class="pick"><div class="pick-sub">还没有人格。点上面「+ 新建」建一个，或「导入 .md」把现成的收编进来。</div></li>';
    return;
  }
  for (const p of S.personas) {
    const li = document.createElement('li');
    li.className = 'pick has-edit' + (S.selPersona === p.id ? ' on' : '');
    li.innerHTML = `
      <div class="pick-title">${esc(p.name)}<span class="badge">${p.chars} 字</span></div>
      <div class="pick-sub">${esc(p.category)}${p.desc ? ' · ' + esc(p.desc) : ''}</div>
      <button class="pick-edit" title="改内容" aria-label="编辑 ${esc(p.name)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 20h4L20 8l-4-4L4 16z"/><path d="M14 6l4 4"/>
        </svg>
      </button>`;
    // 点条目 = 选为注入对象；点铅笔 = 改内容。两件事分开，不互相干扰
    li.onclick = () => selectPersona(p.id);
    li.querySelector('.pick-edit').onclick = (ev) => {
      ev.stopPropagation();
      openEditor(p.id);
    };
    ul.appendChild(li);
  }
}

/* 预算行：这个目标能读进去多少、已经用了多少、自己的规则还能不能读到。
   以前这条信息只有"注入了没反应"时才会被发现，现在直接摆在列表里。 */
function budgetLine(a) {
  const b = S.budget.find((x) => x.adapter === a.id);
  if (!b) return '';
  const over = b.total > b.limit;
  const cls = b.user_starved ? 'starved' : over ? 'over' : '';
  const overBy = b.total - b.limit;
  let text;
  if (b.user_starved) {
    // 最严重：人格把窗口占满，你自己的规则一个字都读不到
    text = `⚠ 已用 ${b.total}（超 ${overBy}）· 你的规则 ${b.user_chars} 字，0 字可读`;
  } else if (over) {
    text = `⚠ 已用 ${b.total}（超 ${overBy}）· 你的规则 ${b.user_chars} 字里 ${b.user_chars - b.readable_user} 字读不到`;
  } else if (b.persona_chars > 0) {
    // 关键：把「人格还能加多少」直接给出来，别让人自己减
    text = `窗口 ${b.limit} · 规则 ${b.user_chars} · 人格 ${b.persona_chars}/${b.persona_budget}`;
  } else {
    text = `窗口 ${b.limit} · 规则 ${b.user_chars} · 人格可放 ${b.persona_budget}`;
  }
  return `<div class="pick-budget ${cls}" title="窗口是所有内容共享的：你的规则 + 所有人格 + 标记开销">${esc(text)}</div>`;
}

function renderAdapters() {
  const ul = $('adapter-list');
  ul.innerHTML = '';
  let detected = 0;
  for (const a of S.adapters) {
    if (a.detected) detected++;
    const li = document.createElement('li');
    const on = S.selAdapters.has(a.id);
    li.className = 'pick' + (on ? ' on' : '') + (a.custom ? ' has-del' : '');
    li.innerHTML = `
      <div class="pick-title">
        <span class="dot ${a.detected ? 'on' : 'off'}"></span>
        ${esc(a.name)}
        ${a.detected ? `<span class="badge">${a.targets.length} 处</span>` : '<span class="badge">未检测到</span>'}
        ${a.verified === false ? '<span class="badge warn">路径未实测</span>' : ''}
        ${a.custom ? '<span class="badge">自定义</span>' : ''}
      </div>
      <div class="pick-sub">${esc(a.evidence)}</div>
      ${budgetLine(a)}
      ${a.custom ? `<button class="pick-del" title="移除这个自定义目标" aria-label="移除 ${esc(a.name)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
          <path d="M6 6l12 12M18 6L6 18"/>
        </svg>
      </button>` : ''}`;
    li.onclick = () => {
      if (!a.detected) {
        toast(`${a.name}：${a.evidence}`, true);
        return;
      }
      if (S.selAdapters.has(a.id)) S.selAdapters.delete(a.id);
      else S.selAdapters.add(a.id);
      renderAdapters();
      refreshApplyBar();
      updateSteps();
    };
    if (a.custom) {
      li.querySelector('.pick-del').onclick = (ev) => {
        ev.stopPropagation();
        removeCustomTarget(a);
      };
    }
    ul.appendChild(li);
  }
  $('target-counter').textContent = `${detected} 可用 / 共 ${S.adapters.length}`;
}

function selectPersona(id) {
  S.selPersona = id;
  S.vars = {};
  S.applied = false;
  S.preview = null;      // 不清就会拿 A 的预览去点 B 的「应用」
  const p = S.personas.find((x) => x.id === id);
  renderPersonas();
  renderVars(p);
  refreshApplyBar();
  updateSteps();
  status(`已选人格：${p ? p.name : id}`);
}

function renderVars(p) {
  const bar = $('vars-bar');
  const fields = $('vars-fields');
  fields.innerHTML = '';
  if (!p || !p.variables || !p.variables.length) { bar.hidden = true; return; }
  bar.hidden = false;
  for (const v of p.variables) {
    const label = document.createElement('label');
    label.innerHTML = `<span>${esc(v)}</span>`;
    const input = document.createElement('input');
    input.placeholder = '值';
    input.value = S.vars[v] || '';
    input.oninput = () => { S.vars[v] = input.value; };
    label.appendChild(input);
    fields.appendChild(label);
  }
}

function refreshApplyBar() {
  const ok = !!S.selPersona && S.selAdapters.size > 0;
  $('btn-apply').disabled = !ok;
  if (S.selPersona) {
    const p = S.personas.find((x) => x.id === S.selPersona);
    const pname = p ? p.name : S.selPersona;
    $('apply-title').textContent = S.selAdapters.size
      ? `${pname} → ${S.selAdapters.size} 个目标`
      : `${pname}　·　还没选目标`;
    $('apply-sub').textContent = S.selAdapters.size
      ? '点「预览差异」看清每一处改动，确认无误再写入。'
      : '在左边「目标客户端」里点一个（只有绿点才是本机确实可用的）。';
  } else {
    $('apply-title').textContent = '选人格 → 选目标 → 预览 → 写入';
    $('apply-sub').textContent = '预览会显示每一处将要改动的内容，确认后才真正写入。';
  }
}

/* 步骤条：1 选人格 2 选目标 3 看差异 4 写入 */
function updateSteps() {
  const done = [
    !!S.selPersona,
    S.selAdapters.size > 0,
    !!S.preview,
    S.applied,
  ];
  const firstUndone = done.findIndex((x) => !x);
  document.querySelectorAll('#steps .step').forEach((el, i) => {
    el.classList.toggle('done', done[i]);
    el.classList.toggle('active', i === firstUndone);
  });
}

/* ------------------------------------------------------------------ 预览 / 写入 */
function diffHtml(text) {
  if (!text) return '<span class="l-meta">（无差异）</span>';
  return text.split('\n').map((line) => {
    const e = esc(line);
    if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')) return `<span class="l-meta">${e}</span>`;
    if (line.startsWith('+')) return `<span class="l-add">${e}</span>`;
    if (line.startsWith('-')) return `<span class="l-del">${e}</span>`;
    return `<span class="l-ctx">${e}</span>`;
  }).join('\n');
}

async function doPreview() {
  if (!S.selPersona || !S.selAdapters.size) return;
  const btn = $('btn-preview');
  busy(btn, true);
  status('正在计算差异…', 'busy');
  try {
    const d = await api('/api/preview', {
      method: 'POST',
      body: JSON.stringify({
        persona_id: S.selPersona,
        adapters: [...S.selAdapters],
        vars: S.vars,
        keep_others: keepOthers(),
      }),
    });
    S.preview = d;
    renderPreview(d);
    updateSteps();
    status(`预览完成：${d.files.length} 个文件，+${d.total_added} / -${d.total_removed} 行`);
  } catch (e) {
    toast(e.message, true);
    status('预览失败', 'err');
  } finally {
    busy(btn, false);
  }
}

function renderPreview(d) {
  const area = $('diff-area');
  const sum = $('plan-summary');
  sum.hidden = false;

  const changed = d.files.filter((f) => f.action !== 'unchanged');
  sum.innerHTML = `
    <span><span class="k">人格</span> <span class="v">${esc(d.persona_id)}</span></span>
    <span><span class="k">渲染后</span> <span class="v">${d.persona_chars}</span> 字</span>
    <span><span class="k">将改动</span> <span class="v">${changed.length}</span> 个文件</span>
    <span><span class="k add">+${d.total_added}</span> / <span class="k del">-${d.total_removed}</span></span>
    ${d.warnings.length ? `<span class="warn-line">⚠ ${d.warnings.map(esc).join('；')}</span>` : ''}`;

  area.innerHTML = '';
  if (!changed.length) {
    area.innerHTML = `<div class="empty-state">
      <p class="empty-title">所有目标内容已一致</p>
      <p class="empty-sub">这份人格在这些目标里已经是当前版本，无需重复写入。</p></div>`;
    return;
  }
  for (const f of changed) {
    const card = document.createElement('div');
    card.className = 'file-card';
    card.innerHTML = `
      <div class="file-head">
        <span class="tag ${f.action}">${f.action}</span>
        <span class="file-path">${esc(f.path)}</span>
        <span class="muted">${esc(f.summary)}</span>
      </div>
      <pre class="diff">${diffHtml(f.diff)}</pre>`;
    area.appendChild(card);
  }
}

/* 写入方式：并存 / 替换。
   两条都是合法语义，选错的代价不同：
   - 替换：同通道只留这一份，之前的人物没了（用户报过"旧人格被覆盖"）
   - 并存：同通道多个区块，宿主会同时读到；两份内容冲突时表现是"人格不稳定"
   所以默认「并存」，并把代价写在旁边的提示里，而不是替用户决定。 */
function injectMode() {
  const el = document.querySelector('input[name="injectmode"]:checked');
  return el ? el.value : 'keep';
}

function keepOthers() {
  return injectMode() === 'keep';
}

function renderInjectModeHint() {
  const hint = $('inject-mode-hint');
  if (!hint) return;
  hint.innerHTML = keepOthers()
    ? '<b>并存</b>：已注入的其他人物保留，同一条通道里会有多个区块，宿主会同时读到它们。想只要当前这一份，切到「替换」。'
    : '<b>替换</b>：同一条通道只留当前这一份，之前注入的其他人物会被移除（「注入管理」里还能看到，也能从存档恢复）。';
}

/* 写入完成后的结果面板。
   为什么不再逐条弹红框：这些是"写成功了、但要你知道"的信息，不是错误。
   拿报错的样式展示正常结果，用户第一反应是"是不是失败了"——实测被这么问过。 */
function renderApplyResult(d) {
  const box = $('apply-result');
  if (!box) return;
  const ws = d.warnings || [];
  if (!ws.length) {
    box.hidden = true;
    box.innerHTML = '';
    return;
  }
  const files = (d.steps || [])
    .filter((s) => s.status === 'written')
    .map((s) => `<li><code>${esc(String(s.path).split(/[\\/]/).pop())}</code> 已写入</li>`)
    .join('');
  box.hidden = false;
  box.innerHTML = `
    <div class="ar-head">写入完成 · ${d.written} 个文件${d.snapshot_id ? ` · 快照 ${esc(d.snapshot_id)}` : ''}</div>
    ${files ? `<ul class="ar-files">${files}</ul>` : ''}
    <ul class="ar-warns">${ws.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>`;
}

async function doApply() {
  if (!S.selPersona || !S.selAdapters.size) return;
  const btn = $('btn-apply');

  // 没预览就直接写 → 先确认，避免盲写
  if (!S.preview) {
    const go = await confirmModal({
      title: '还没预览过差异',
      body: '建议先点「预览差异」看清改动再写入。<br><br>仍然直接写入吗？写入前会自动存档，出问题可以还原。',
      okText: '仍然写入',
    });
    if (!go) return;
  }

  busy(btn, true);
  status('正在写入…', 'busy');
  try {
    const d = await api('/api/apply', {
      method: 'POST',
      body: JSON.stringify({
        persona_id: S.selPersona,
        adapters: [...S.selAdapters],
        vars: S.vars,
        keep_others: keepOthers(),
      }),
    });
    if (d.ok) {
      S.applied = true;
      toast(`写入完成：${d.written} 个文件，已自动存档`);
      status(`完成 · 快照 ${d.snapshot_id}`);
      // 提示不再逐条弹红框，统一放进结果面板（见 renderApplyResult 的注释）。
      renderApplyResult(d);
      // 只要写的是身份文件，宿主就不保证下一轮重读——状态栏直说要重开，
      // 别让用户以为"等一下就好"（这条已经猜错过一次）。
      const needsRestart = (d.warnings || []).some((w) => w.includes('重开'));
      if (needsRestart) status('已写入 · 需要完全退出 WorkBuddy 重开才生效', 'warn');
      else if ((d.warnings || []).length) status('已写入 · 下方有需要你确认的提示', 'warn');
      else status(`完成 · 快照 ${d.snapshot_id}`);
      await loadState();
      await doPreview();
      await loadInjections();
    } else {
      toast(`写入失败并已回滚：${d.message}`, true);
      status('已回滚', 'err');
    }
    updateSteps();
  } catch (e) {
    toast(e.message, true);
    status('写入失败', 'err');
  } finally {
    busy(btn, false);
  }
}

/* ------------------------------------------------------ 人格编辑器（侧滑抽屉）
   以前「人格库」是一个独立页面，"点新建 → 跳到别处 → 保存 → 不知道发生了什么"
   很容易让人以为新建失败或覆盖了别的人格。现在编辑与注入同屏：
   抽屉从右侧滑出，保存后侧栏和注入区立刻同步，当前人格自动被选中。 */

/* 六节标准骨架。刻意写成"带问句的占位"，因为：
   ① 用户只要把问句换成自己的话，结构就保住了；
   ② 不写 HTML 注释——注释会随正文原样注入客户端，污染人格内容。 */
const SKELETON = `【身份】
一句话说清：你是谁，服务什么目标，不兼任什么。

【完成判据】
什么情况算做完了。写成能自查的条件——每条必须包含什么、必须指到什么位置。

【方法与硬约束】
必须：
绝不：

【输出规格】
格式、长度、结构、字段（写具体数字，不写"适当"）：

【不做的事】
-

【不确定时】
信息不足时怎么办：先问 / 自行补足最具体版本 / 标注未确认。
`;

function openEditor(id) {
  S.editingId = id || null;
  const isNew = !id;
  $('editor-title').textContent = isNew ? '新建人格' : '编辑人格';
  $('editor-sub').textContent = isNew
    ? '标识留空会自动生成；保存后直接选为注入对象。'
    : '改完点保存，侧栏与注入区会立刻同步。';
  $('btn-delete-persona').hidden = isNew;

  if (isNew) resetEditor();
  else loadPersonaToEditor(id);

  $('editor-backdrop').hidden = false;
  setTimeout(() => $('p-name').focus(), 70);
}

function closeEditor() {
  $('editor-backdrop').hidden = true;
  S.editingId = null;
}

function resetEditor() {
  $('p-id').value = '';
  $('p-name').value = '';
  $('p-category').value = '通用';
  $('p-extends').value = '';
  $('p-desc').value = '';
  $('p-body').value = '';
  $('p-issues').hidden = true;
  $('p-issues').innerHTML = '';
  updatePersonaStat();
}

function loadPersonaToEditor(id) {
  S.editingId = id;
  status('加载人格…', 'busy');
  api('/api/personas/' + encodeURIComponent(id)).then((p) => {
    $('p-id').value = p.id;
    $('p-name').value = p.name;
    $('p-category').value = p.category;
    $('p-extends').value = p.extends || '';
    $('p-desc').value = p.desc || '';
    $('p-body').value = p.body;
    updatePersonaStat();
    renderIssues(p.issues || []);
    status(`编辑中：${p.name}`);
  }).catch((e) => {
    toast(e.message, true);
    status('加载失败', 'err');
  });
}

function renderIssues(issues) {
  const box = $('p-issues');
  if (!issues.length) { box.hidden = true; box.innerHTML = ''; return; }
  box.hidden = false;
  box.innerHTML = '⚠ ' + issues.map(esc).join('<br>⚠ ');
}

/* 编辑器的字数行。
   除了字数，还要当场说出「这个字数在目标里装不装得下」。
   目标文件是定长窗口（USER.md 1 万、MEMORY.md 4 千），超出部分被截断，
   而截断是静默的——保存成功、注入成功、界面全绿，只有人格没生效。
   所以判据放在打字的位置，不放进去之后。
   取"最紧的那个目标"：几个目标同时注入时，能不能读全取决于窗口最小的那个。 */
function personaBudgetHint(n) {
  if (!S.budget || !S.budget.length) return '';
  const picked = S.selAdapters && S.selAdapters.size ? [...S.selAdapters] : null;
  const rows = picked ? S.budget.filter((b) => picked.includes(b.adapter)) : S.budget;
  if (!rows.length) return '';
  const tight = rows.reduce((a, b) => (b.persona_budget < a.persona_budget ? b : a));
  const cap = tight.persona_budget;
  if (!cap) return '';
  if (n > cap) {
    return ` · <span class="over">窗口只装 ${cap} 字，超出 ${n - cap} 字不会被读到</span>`;
  }
  return ` · 窗口可放 ${cap} 字`;
}

function updatePersonaStat() {
  const n = $('p-body').value.length;
  const vars = [...new Set([...$('p-body').value.matchAll(/\{\{\s*([A-Za-z0-9_\-]+)\s*\}\}/g)].map((m) => m[1]))];
  $('p-stat').innerHTML = `${n} 字` + (vars.length ? ` · 变量 ${esc(vars.join(', '))}` : '') + personaBudgetHint(n);
}

async function savePersona() {
  const typedId = $('p-id').value.trim();
  // 没名字不算错：只写了一整段正文却卡在"至少填一个名称"上，是纯粹的堵路。
  // 给个能认出来的默认名，让人继续往下走。
  const name = $('p-name').value.trim() || '未命名人格';
  const isCreate = !S.editingId;

  const btn = $('btn-save-persona');
  busy(btn, true);
  try {
    const res = await api(
      '/api/personas' + (isCreate ? '' : '/' + encodeURIComponent(S.editingId)),
      {
        method: isCreate ? 'POST' : 'PUT',
        body: JSON.stringify({
          id: typedId,
          name,
          category: $('p-category').value || '通用',
          desc: $('p-desc').value,
          extends: $('p-extends').value || null,
          body: $('p-body').value,
        }),
      });

    S.editingId = res.id;
    $('p-id').value = res.id;
    await loadState();          // 先刷新列表，新人格才在 S.personas 里
    selectPersona(res.id);      // 立刻选为注入对象，不用再回侧栏找一次
    closeEditor();
    toast(isCreate ? `已新建「${name}」，并已选为注入对象` : `已保存「${name}」`);
    status(isCreate ? `已新建并选中：${name}` : `已保存：${name}`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy(btn, false);
  }
}

async function deletePersona() {
  const id = S.editingId;
  if (!id) return;
  const p = S.personas.find((x) => x.id === id);
  const ok = await confirmModal({
    title: '删除人格',
    body: `确定删除「<b>${esc(p ? p.name : id)}</b>」？<br><br>`
      + `文件会从人格库移除。<b>已经注入到客户端的内容不会自动消失</b>——`
      + `那部分请去「注入管理」开关或移除，那里有存档可还原。`,
    okText: '删除',
    danger: true,
  });
  if (!ok) return;
  try {
    await api('/api/personas/' + encodeURIComponent(id), { method: 'DELETE' });
    if (S.selPersona === id) S.selPersona = null;
    closeEditor();
    await loadState();
    toast(`已删除：${p ? p.name : id}`);
    status('已删除人格');
  } catch (e) {
    toast(e.message, true);
  }
}

/* ------------------------------------------------------ 自定义目标客户端
   自动检测只认已知路径。客户端装在奇怪位置、或者只想写进某个随便的 .md，
   就在这儿直接指定——两者并列显示，互不影响。 */

function openCustom() {
  $('custom-modal').hidden = false;
  $('ct-name').value = '';
  $('ct-paths').value = '';
  const st = $('ct-state');
  st.hidden = true;
  st.className = 'upd-state';
  setTimeout(() => $('ct-name').focus(), 70);
}

async function saveCustom() {
  const name = $('ct-name').value.trim() || '自定义目标';
  const paths = $('ct-paths').value
    .split('\n').map((s) => s.trim()).filter(Boolean);
  const st = $('ct-state');

  if (!paths.length) {
    st.hidden = false; st.className = 'upd-state bad';
    st.textContent = '至少要填一个路径。一行一个。';
    return;
  }

  const btn = $('ct-save');
  busy(btn, true);
  try {
    const d = await api('/api/custom', {
      method: 'POST',
      body: JSON.stringify({ name, paths }),
    });
    $('custom-modal').hidden = true;
    await loadState();
    // 加完直接选中，省得还要在列表里再点一次
    S.selAdapters.add(d.id);
    renderAdapters(); refreshApplyBar(); updateSteps();
    toast(`已添加目标「${d.name}」，${d.count} 个路径，已选中`);
    status(`自定义目标：${d.name}`);
  } catch (e) {
    st.hidden = false; st.className = 'upd-state bad';
    st.textContent = e.message;
  } finally {
    busy(btn, false);
  }
}

async function removeCustomTarget(a) {
  const ok = await confirmModal({
    title: '移除自定义目标',
    body: `确定不再把「<b>${esc(a.name)}</b>」作为注入目标？<br><br>`
      + `这只删掉这里的配置，<b>已经写进那个文件的内容不会动</b>——`
      + `要清掉请去「注入管理」。`,
    okText: '移除',
    danger: true,
  });
  if (!ok) return;
  try {
    await api('/api/custom/remove', {
      method: 'POST',
      body: JSON.stringify({ id: a.id }),
    });
    S.selAdapters.delete(a.id);
    await loadState();
    renderAdapters(); refreshApplyBar(); updateSteps();
    toast(`已移除：${a.name}`);
  } catch (e) {
    toast(e.message, true);
  }
}

/* ------------------------------------------------------------------ 注入管理 */
function injTitle(f) {
  if (f.kind === 'self') return f.persona_id || '本工具注入';
  if (f.scope === 'dir') return f.extra.dir_name || '托管目录';
  // 不在这里截字：交给 CSS 省略号，悬停时 title 还能看全
  return (f.evidence || '').split('｜')[0] || f.source_name;
}

async function loadInjections() {
  const list = $('inj-list');
  if (!S.injections.length) {
    list.innerHTML = `<div class="empty-state"><p class="empty-sub">正在扫描本机客户端…</p></div>`;
  }
  try {
    const d = await api('/api/injections');
    S.injections = d.items;
    S.injSummary = d.summary;
    S.injSelected.clear();
    renderInjections();
    renderInjBadge();
  } catch (e) {
    toast('扫描失败：' + e.message, true);
    list.innerHTML = `<div class="empty-state"><p class="empty-sub">扫描失败：${esc(e.message)}</p></div>`;
  }
}

function renderInjBadge() {
  const n = S.injSummary ? S.injSummary.third_party : 0;
  const b = $('tab-inj-badge');
  b.hidden = n === 0;
  b.textContent = n;
}

function renderInjections() {
  const s = S.injSummary || { total: 0, self: 0, self_on: 0, self_off: 0, third_party: 0, high: 0, sources: [] };

  $('scan-summary').innerHTML = `
    <div class="stat-card ${s.third_party ? 'hot' : 'calm'}">
      <span class="n">${s.third_party}</span>
      <span class="l">第三方注入${s.sources.length ? ' · ' + esc(s.sources.join('、')) : ''}</span>
    </div>
    <div class="stat-card">
      <span class="n">${s.self}</span>
      <span class="l">本工具注入（${s.self_on} 开 / ${s.self_off} 关）</span>
    </div>
    <div class="stat-card">
      <span class="n">${s.high}</span>
      <span class="l">高危项</span>
    </div>
    <div class="stat-card">
      <span class="n">${s.total}</span>
      <span class="l">全部痕迹</span>
    </div>`;

  const removable = S.injections.filter((f) => f.deletable);
  $('inj-toolbar').hidden = removable.length === 0;

  const list = $('inj-list');
  list.innerHTML = '';

  if (!S.injections.length) {
    list.innerHTML = `<div class="empty-state">
      <p class="empty-title">没有发现任何注入</p>
      <p class="empty-sub">本机客户端都是干净的。装了什么新工具之后回来重新扫描一次就能看到。</p></div>`;
    return;
  }

  // 同一个人格的多条通道合成一组：一个 group 一张卡。
  // 依据是后端 extra.group（只有同人格命中 ≥2 条通道时才给），单通道的按 id 自成一组。
  // 不合成的话，用户会看到两行几乎一样的人格，以为自己注入了两个。
  const groups = new Map();
  for (const f of S.injections) {
    const key = (f.extra && f.extra.group) || f.id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(f);
  }

  for (const [gkey, items] of groups) {
    const f = items[0];
    const card = document.createElement('div');
    const cls = f.kind === 'self' ? 'self' : f.kind === 'anomaly' ? 'anomaly' : 'third';
    const allOff = items.every((x) => x.state === 'off');
    card.className = `inj-card ${cls}` + (allOff ? ' disabled' : '');
    card.dataset.id = gkey;
    card.dataset.members = items.map((x) => x.id).join(',');

    const toggleables = items.filter((x) => x.toggleable);
    const deletables = items.filter((x) => x.deletable);
    const canToggle = toggleables.length > 0;
    const canDelete = deletables.length > 0;
    const isOn = !allOff;
    const multi = items.length > 1;
    const channelNames = multi
      ? items.map((x) => (x.extra && x.extra.channel) || x.path.split(/[\\/]/).pop())
      : [];

    card.innerHTML = `
      <div class="inj-head">
        ${canDelete ? `<input type="checkbox" class="inj-check" data-check="${gkey}">` : '<span style="width:15px"></span>'}
        <span class="src-badge ${cls}">${esc(f.source_name)}</span>
        <span class="inj-title">${esc(injTitle(f))}</span>
        <span class="sev ${f.severity}">${f.severity === 'high' ? '高危' : f.severity === 'warn' ? '提醒' : '信息'}</span>
        ${multi ? `<span class="badge">${items.length} 条通道</span>` : ''}
        ${allOff ? '<span class="badge">已停用</span>' : ''}
        <div class="inj-actions">
          ${canToggle ? `
            <label class="switch" title="${isOn ? '点击停用（内容移出文件，可再开）' : '点击启用（把内容放回文件）'}">
              <input type="checkbox" ${isOn ? 'checked' : ''} data-toggle="${gkey}">
              <span class="track"></span>
              <span class="sw-label">${isOn ? '开' : '关'}</span>
            </label>` : ''}
          ${canDelete ? `<button class="btn ghost" data-remove="${gkey}">移除</button>` : ''}
        </div>
      </div>
      <div class="inj-meta">
        <div class="inj-path">${
          multi
            ? `<span class="inj-channels">同一人格写在 ${items.length} 处：${esc(channelNames.join(' · '))}（宿主按会话类型二选一注入，所以一个人格两条路）</span>`
            : `${esc(f.path)}${f.line_start ? ':' + f.line_start + (f.line_end > f.line_start ? '–' + f.line_end : '') : ''}`
        }</div>
        ${multi ? '' : `<div class="inj-evidence">${esc(f.evidence)}</div>`}
      </div>
      ${multi ? `<div class="inj-channels-list">${items.map((x) => `
        <div class="inj-channel-row">
          <span class="inj-channel-name">${esc((x.extra && x.extra.channel) || x.path.split(/[\\\\/]/).pop())}</span>
          <span class="inj-channel-path">${esc(x.path)}${x.line_start ? ':' + x.line_start + (x.line_end > x.line_start ? '–' + x.line_end : '') : ''}</span>
          ${x.state === 'off' ? '<span class="badge">已停用</span>' : ''}
        </div>`).join('')}</div>` : ''}
      ${f.preview ? `<pre class="inj-preview">${esc(f.preview)}</pre>` : ''}
      ${!canToggle && !canDelete ? '<div class="inj-note">此项只能查看，不能一键处理——自动切除会破坏文件语法或切错位置。</div>' : ''}
      ${!canToggle && canDelete ? '<div class="inj-note">结构型条目：只能整体移除，不能停用（停用会让文件语法失效）。</div>' : ''}
    `;
    list.appendChild(card);
  }

  // 事件委托：勾选 / 开关 / 移除
  // 勾选框绑定的是「组」，但选中集合里存的是每条痕迹的 id —— 批量移除要按 id 走。
  const membersOf = groupMemberIds;

  list.querySelectorAll('[data-check]').forEach((cb) => {
    cb.onchange = () => {
      const ids = membersOf(cb.dataset.check);
      for (const id of ids) {
        if (cb.checked) S.injSelected.add(id); else S.injSelected.delete(id);
      }
      refreshInjSelection();
    };
  });
  list.querySelectorAll('[data-toggle]').forEach((sw) => {
    sw.onchange = () => doToggleGroup(membersOf(sw.dataset.toggle), sw.checked);
  });
  list.querySelectorAll('[data-remove]').forEach((btn) => {
    btn.onclick = () => doRemove(membersOf(btn.dataset.remove));
  });

  refreshInjSelection();
}

function groupMemberIds(gkey) {
  // 一个组 = 同一个人格的多条通道。批量操作要落到「每条痕迹」上，
  // 所以组 key 必须先展开成成员 id。
  const card = document.querySelector(`.inj-card[data-id="${gkey}"]`);
  return card && card.dataset.members ? card.dataset.members.split(',') : [];
}

function refreshInjSelection() {
  const n = S.injSelected.size;
  $('inj-selected-count').textContent = `已选 ${n} 处`;
  $('btn-bulk-remove').disabled = n === 0;
  const boxes = [...document.querySelectorAll('[data-check]')];
  const all = $('inj-check-all');
  all.checked = boxes.length > 0 && boxes.every((b) => b.checked);
  all.indeterminate = n > 0 && !all.checked;
}

async function doToggleGroup(ids, enable) {
  // 一个人格可能同时写在多条通道上，开关要一起动——只关一条会让「人格还在」
  // 和「人格没了」同时成立，比不做更糟。
  const targets = S.injections.filter((x) => ids.includes(x.id) && x.toggleable);
  if (!targets.length) return;
  status(enable ? '正在启用…' : '正在停用…', 'busy');
  let done = 0;
  try {
    for (const f of targets) {
      await api('/api/injections/toggle', {
        method: 'POST',
        body: JSON.stringify({ id: f.id, enable }),
      });
      done += 1;
    }
    const suffix = targets.length > 1 ? `（${done} 条通道）` : '';
    toast((enable ? '已启用' : '已停用') + suffix);
    status(enable ? '已启用' : '已停用');
    await loadInjections();
    await loadState();
  } catch (e) {
    toast(e.message, true);
    status('操作失败', 'err');
    await loadInjections();
    renderInjections();   // 回滚开关的视觉状态
  }
}

async function doRemove(ids) {
  const targets = S.injections.filter((f) => ids.includes(f.id));
  if (!targets.length) return;

  const lines = targets.map((f) =>
    `<li><code>${esc(f.path.split(/[\\/]/).pop())}</code> — ${esc(injTitle(f))}</li>`).join('');
  const go = await confirmModal({
    title: `确认移除 ${targets.length} 项注入？`,
    body: `<p>以下内容会从文件里被切掉：</p><ul>${lines}</ul>
           <p style="margin-top:10px">动手前会自动存档，事后可以在「备份」页完整还原。</p>`,
    okText: '移除',
    danger: true,
  });
  if (!go) return;

  const btn = $('btn-bulk-remove');
  busy(btn, true);
  status('正在移除…', 'busy');
  try {
    const r = await api('/api/injections/remove', {
      method: 'POST',
      body: JSON.stringify({ ids }),
    });
    toast(`已移除 ${r.removed} 项${r.skipped.length ? `，跳过 ${r.skipped.length} 项` : ''}`);
    status(`移除完成 · 快照 ${r.snapshot_id || '—'}`);
    S.injSelected.clear();
    await loadInjections();
    await loadState();
  } catch (e) {
    toast(e.message, true);
    status('移除失败', 'err');
  } finally {
    busy(btn, false);
  }
}

/* ------------------------------------------------------------------ 备份 */
async function loadVault() {
  try {
    const d = await api('/api/snapshots');
    $('vault-stats').innerHTML = `
      <div class="stat"><span class="n">${d.stats.snapshots}</span><span class="l">快照</span></div>
      <div class="stat"><span class="n">${d.stats.objects}</span><span class="l">去重对象</span></div>
      <div class="stat"><span class="n">${(d.stats.object_bytes / 1024).toFixed(0)} KB</span><span class="l">占用</span></div>
      <div class="stat"><span class="n" style="font-size:12.5px">${d.stats.latest || '—'}</span><span class="l">最近一次</span></div>`;

    const ul = $('snap-list');
    ul.innerHTML = '';
    if (!d.items.length) {
      ul.innerHTML = `<li class="snap"><div class="snap-main">
        <div class="snap-title">还没有快照</div>
        <div class="snap-sub">执行一次写入或移除后，会自动生成第一个快照。</div></div></li>`;
    }
    for (const s of d.items) {
      const li = document.createElement('li');
      li.className = 'snap';
      li.innerHTML = `
        <div class="snap-main">
          <div class="snap-title">${esc(s.label)} <span class="badge">${s.files} 个文件</span></div>
          <div class="snap-sub">${esc(s.time)} · ${esc(s.id)}</div>
        </div>
        <button class="btn ghost" data-verify>校验</button>
        <button class="btn ghost" data-restore>还原到此</button>`;
      li.querySelector('[data-verify]').onclick = async () => {
        try {
          const v = await api(`/api/snapshots/${encodeURIComponent(s.id)}/verify`);
          toast(v.ok ? '当前磁盘状态与该快照一致' : '有文件与该快照不同（可用还原恢复）', !v.ok);
        } catch (e) { toast(e.message, true); }
      };
      li.querySelector('[data-restore]').onclick = async () => {
        const go = await confirmModal({
          title: '还原到这个快照？',
          body: `<p>将把 <b>${s.files} 个文件</b>恢复到 <code>${esc(s.time)}</code> 的状态。</p>
                 <p style="margin-top:8px">当前内容会被覆盖；还原前会先为当前状态再拍一个快照，所以这一步本身也可撤销。</p>`,
          okText: '还原',
          danger: true,
        });
        if (!go) return;
        try {
          const r = await api('/api/restore', {
            method: 'POST',
            body: JSON.stringify({ snapshot_id: s.id }),
          });
          toast(r.ok ? '还原完成' : '部分文件还原失败，请查看日志', !r.ok);
          await loadVault();
          await loadState();
          await loadInjections();
        } catch (e) { toast(e.message, true); }
      };
      ul.appendChild(li);
    }

    const lg = await api('/api/logs');
    $('log-box').textContent = lg.lines.length ? lg.lines.join('\n') : '（暂无）';
  } catch (e) {
    toast(e.message, true);
  }
}

/* ------------------------------------------------------------------ 在线升级 */
function initUpdate() {
  const dlg = $('upd');
  let info = null;

  function setState(text, kind = '') {
    const el = $('upd-state');
    el.textContent = text;
    el.className = 'upd-state' + (kind ? ' ' + kind : '');
  }

  function showNotes(notes) {
    const box = $('upd-notes');
    if (!notes || !notes.length) { box.hidden = true; box.innerHTML = ''; return; }
    box.hidden = false;
    box.innerHTML = '<ul>' + notes.map((n) => `<li>${esc(n)}</li>`).join('') + '</ul>';
  }

  async function check() {
    const btn = $('upd-check');
    busy(btn, true);
    setState('正在检查…', 'busy');
    $('upd-download').disabled = true;
    try {
      const src = ($('upd-source').value || '').trim();
      const d = await api('/api/update/check' + (src ? '?source=' + encodeURIComponent(src) : ''));
      info = d;
      $('upd-current').textContent = d.current;
      $('upd-latest').textContent = d.latest || '—';
      $('upd-latest').className = 'upd-num' + (d.has_update ? ' new' : '');

      if (d.error) {
        setState(d.error, 'bad');
        showNotes([]);
        return;
      }
      showNotes(d.notes);

      if (d.has_update) {
        setState(`发现新版本 ${d.latest}。下载后会存进暂存区，关闭再打开程序即生效。`, 'ok');
        $('upd-download').disabled = false;
      } else if (d.pending) {
        setState(`版本 ${d.pending.version} 已下载，关闭程序后重新打开即可生效。`, 'busy');
      } else {
        setState(`已是最新版本（${d.current}）。`, 'ok');
      }
    } catch (e) {
      setState(e.message, 'bad');
    } finally {
      busy(btn, false);
    }
  }

  async function download() {
    if (!info || !info.has_update) return;
    const btn = $('upd-download');
    busy(btn, true);
    setState('正在下载并校验…', 'busy');
    try {
      const d = await api('/api/update/download', {
        method: 'POST',
        body: JSON.stringify({ source: ($('upd-source').value || '').trim() || undefined }),
      });
      setState(
        `已就绪：版本 ${d.version}，${d.files} 个文件，${(d.bytes / 1024).toFixed(0)} KB。\n` +
        '现在关闭本程序，再双击 run.bat 打开，新版本会自动生效。', 'ok');
      btn.disabled = true;
      toast(`版本 ${d.version} 已下载，重启后生效`);
    } catch (e) {
      setState(e.message, 'bad');
      toast(e.message, true);
    } finally {
      busy(btn, false);
    }
  }

  function open() {
    dlg.hidden = false;
    $('upd-current').textContent = S.version || '—';
    $('upd-latest').textContent = '—';
    $('upd-latest').className = 'upd-num';
    showNotes([]);
    if (!$('upd-source').value) $('upd-source').value = '';
    check();
  }

  // 桌面外壳会把这两个按钮移除（更新走标题栏、退出走原生 ✕）。
  // 直接 `$('update-btn').onclick = ...` 在元素不存在时抛 TypeError，
  // init() 会从这一行中断 —— 后面几十个绑定全不执行，界面看着正常但全部失效。
  const updateBtn = $('update-btn');
  if (updateBtn) updateBtn.onclick = open;
  $('upd-close').onclick = () => { dlg.hidden = true; };
  $('upd-check').onclick = check;
  $('upd-download').onclick = download;
  dlg.onclick = (e) => { if (e.target === dlg) dlg.hidden = true; };

  // Esc 也能关
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !dlg.hidden) dlg.hidden = true;
  });
}

/* ------------------------------------------------------------------ 初始化 */
async function loadState() {
  try {
    const d = await api('/api/state');
    S.version = d.version;
    S.personas = d.personas;
    S.adapters = d.adapters;
    // 各目标的读取窗口占用：让「写进去但读不到」这件事在界面上就能看见
    try {
      S.budget = (await api('/api/budget')).items || [];
    } catch {
      S.budget = [];
    }
    if (S.selPersona && !S.personas.some((p) => p.id === S.selPersona)) S.selPersona = null;
    renderPersonas();
    renderAdapters();
    refreshApplyBar();
    updateSteps();
    $('data-root').textContent = d.data_root;
  } catch (e) {
    toast('加载失败：' + e.message, true);
    status('后端未响应', 'err');
  }
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function init() {
  initTheme();
  initTabs();
  initSide();
  initUpdate();

  $('btn-preview').onclick = doPreview;
  $('btn-apply').onclick = doApply;

  // 切换写入方式时，提示文字与已有的预览都要跟着变——
  // 否则用户切了「替换」却看着「并存」时生成的预览去点写入，看到的和写进去的不是一回事。
  document.querySelectorAll('input[name="injectmode"]').forEach((r) => {
    r.onchange = () => {
      renderInjectModeHint();
      if (S.preview) doPreview();
    };
  });
  renderInjectModeHint();
  $('btn-refresh-vault').onclick = loadVault;

  // 自检与云记忆：都挂在「注入管理」上，因为它们回答的是同一类问题——
  // 「我写进去的东西，宿主到底用了没」
  // 自检 / 格式遵从 / 云记忆：三块都进同一个右侧抽屉，点按钮 = 打开 + 重新统计。
  // 以前它们内嵌在列表上方，展开就把人格列表挤出屏幕。
  $('btn-verify-inj').onclick = () => openDiag('verify', true);
  $('btn-cloudmem').onclick = () => openDiag('cloudmem', true);
  $('btn-audit').onclick = () => openDiag('audit', true);
  $('btn-tpl').onclick = () => openDiag('tpl', true);
  $('diag-close').onclick = closeDiag;
  $('diag-backdrop').onclick = (ev) => {
    if (ev.target.id === 'diag-backdrop') closeDiag();
  };
  document.querySelectorAll('.diag-tab').forEach((t) => {
    t.onclick = () => switchDiag(t.dataset.diag, false);
  });
  $('btn-rescan').onclick = async () => {
    const b = $('btn-rescan');
    busy(b, true);
    status('正在重新扫描…', 'busy');
    await loadInjections();
    busy(b, false);
    status('扫描完成');
    toast(`扫描完成：${S.injections.length} 项痕迹`);
  };

  $('btn-save-persona').onclick = savePersona;
  $('p-body').addEventListener('input', updatePersonaStat);

  // 新建 / 编辑人格：开抽屉，不换页
  $('new-persona').onclick = () => {
    openEditor(null);
    status('新建人格：填好内容点保存，保存后会自动选中');
  };
  $('btn-edit-persona').onclick = () => {
    if (!S.selPersona) { toast('先在左边选一个人格', true); return; }
    openEditor(S.selPersona);
  };
  $('editor-close').onclick = closeEditor;
  // 点抽屉外的暗色区域关闭；点抽屉内部不关
  $('editor-backdrop').onclick = (ev) => {
    if (ev.target.id === 'editor-backdrop') closeEditor();
  };
  $('btn-delete-persona').onclick = deletePersona;

  // 写作要点默认展开（第一次写的人正需要它），关掉后记住选择，
  // 否则熟练用户每次都要多点一下收起。
  const wg = document.querySelector('.write-guide');
  if (wg) {
    if (localStorage.getItem('ph-wg-open') === '0') wg.open = false;
    else wg.open = true;
    wg.addEventListener('toggle', () => localStorage.setItem('ph-wg-open', wg.open ? '1' : '0'));
  }

  // 插入骨架：正文非空时先确认，别把已经写好的东西冲掉
  $('btn-skeleton').onclick = async () => {
    if ($('p-body').value.trim()) {
      const ok = await confirmModal({
        title: '插入标准骨架',
        body: '当前正文不是空的，插入会<b>覆盖</b>现有内容。'
          + '<br><br>如果那段是你写的，先复制出去再点确定。',
        okText: '覆盖插入',
        danger: true,
      });
      if (!ok) return;
    }
    $('p-body').value = SKELETON;
    updatePersonaStat();
    $('p-body').focus();
    status('已插入六节骨架，把问句换成你自己的话');
  };

  // 自定义目标客户端
  $('new-adapter').onclick = openCustom;
  $('ct-cancel').onclick = () => { $('custom-modal').hidden = true; };
  $('ct-save').onclick = saveCustom;
  // Ctrl+Enter 直接保存（在路径框里也能用）
  $('ct-paths').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) saveCustom();
  });
  // 正文框里 Ctrl+S 保存，符合编辑器肌肉记忆
  $('p-body').addEventListener('keydown', (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 's') {
      ev.preventDefault();
      savePersona();
    }
  });

  $('btn-import').onclick = async () => {
    const path = prompt('要导入的 .md 文件完整路径：\n（例如别的工具留下的 persona.md）');
    if (!path) return;
    try {
      const d = await api('/api/personas/import', {
        method: 'POST',
        body: JSON.stringify({ path }),
      });
      toast('已导入：' + d.id);
      await loadState();
      loadPersonaToEditor(d.id);
    } catch (e) { toast(e.message, true); }
  };

  $('btn-gc').onclick = async () => {
    const go = await confirmModal({
      title: '清理旧快照？',
      body: '删除 30 个以外的旧快照，并回收没有任何快照引用的对象。最近的 30 个不受影响。',
      okText: '清理',
      danger: true,
    });
    if (!go) return;
    try {
      const d = await api('/api/vault/gc', { method: 'POST', body: '{}' });
      toast(`已删 ${d.removed_snapshots} 个旧快照，回收 ${(d.freed_bytes / 1024).toFixed(0)} KB`);
      await loadVault();
    } catch (e) { toast(e.message, true); }
  };

  // 批量选择
  $('inj-check-all').onchange = (e) => {
    S.injSelected.clear();
    document.querySelectorAll('[data-check]').forEach((cb) => {
      cb.checked = e.target.checked;
      if (e.target.checked) {
        for (const id of groupMemberIds(cb.dataset.check)) S.injSelected.add(id);
      }
    });
    refreshInjSelection();
  };
  $('btn-bulk-remove').onclick = () => doRemove([...S.injSelected]);

  // 模态
  $('modal-cancel').onclick = () => closeModal(false);
  $('modal-ok').onclick = () => closeModal(true);
  $('modal').onclick = (e) => { if (e.target.id === 'modal') closeModal(false); };

  // 引导
  $('help-btn').onclick = () => showGuide(1);
  $('guide-skip').onclick = endGuide;
  $('guide-next').onclick = () => {
    const total = document.querySelectorAll('.guide-step').length;
    if (S.guideStep >= total) endGuide();
    else showGuide(S.guideStep + 1);
  };

  // 显式退出：比关窗口可靠。关窗口依赖 beforeunload + sendBeacon，
  // 而关掉最后一个浏览器窗口时那个回调不一定来得及执行，服务会残留。
  const quitBtn = $('quit-btn');
  if (quitBtn) quitBtn.onclick = async () => {
    const go = await confirmModal({
      title: '退出人格中枢？',
      body: '会结束后台服务进程。<br><br>已经写入的内容都已存档，不受影响；下次打开还在。',
      okText: '退出',
      danger: true,
    });
    if (!go) return;
    await fetch('/api/bye', { method: 'POST' }).catch(() => {});
    document.body.innerHTML = `
      <div class="bye-screen">
        <div>
          <div class="tick">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <path d="m5 12.5 4.5 4.5L19 7.5"/>
            </svg>
          </div>
          <h2>已安全退出</h2>
          <p>后台服务已结束，现在可以关闭这个窗口了。<br>已写入的内容都已存档，下次打开还在。</p>
        </div>
      </div>`;
  };

  // 键盘：Esc 关闭浮层（后打开的优先）
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!$('custom-modal').hidden) $('custom-modal').hidden = true;
    else if (!$('diag-backdrop').hidden) closeDiag();
    else if (!$('editor-backdrop').hidden) closeEditor();
    else if (!$('modal').hidden) closeModal(false);
    else if (!$('guide').hidden) endGuide();
  });

  loadState().then(async () => {
    // 默认选中第一个人格，开箱就能预览。
    // 但不再「预加载编辑器」：抽屉是关着的，预载会让人点「新建」的瞬间
    // 看到上一份内容一闪而过，以为没清干净。
    if (S.personas.length && !S.selPersona) selectPersona(S.personas[0].id);
    loadVault();
    loadInjections();
    checkTplOnBoot();

    // ?autopreview=1 自动选中第一个可用目标并生成预览，便于外部核验 diff 渲染
    if (qs.get('autopreview') === '1') {
      const avail = S.adapters.find((a) => a.detected);
      if (avail && S.selPersona) {
        S.selAdapters.add(avail.id);
        renderAdapters();
        refreshApplyBar();
        updateSteps();
        doPreview();
      }
    }
  });

  // 首次使用显示指引；?guide=1 强制再次查看，?noguide=1 明确跳过。
  // 判定要走 guideSeen()，它先问外壳再问 localStorage —— 只看 localStorage
  // 在桌面版里必然每次为假（端口一变 origin 就变），等于每次都弹。
  const wantGuide = qs.get('guide') === '1';
  const skipGuide = qs.get('noguide') === '1';
  if (!skipGuide) {
    if (wantGuide) {
      setTimeout(() => showGuide(1), 500);
    } else {
      guideSeen().then((seen) => {
        if (!seen && $('guide') && $('guide').hidden) setTimeout(() => showGuide(1), 500);
      });
    }
  }

  // ?update=1 直接打开升级窗口（便于核验版面）
  if (qs.get('update') === '1') {
    // 桌面外壳里这个按钮不存在（走标题栏的「更新」），别让它抛
    setTimeout(() => { const b = $('update-btn'); if (b) b.click(); }, 700);
  }

  // ?custom=1 直接打开自定义目标弹窗（便于核验版面）
  if (qs.get('custom') === '1') {
    setTimeout(() => openCustom(), 700);
  }

  // 心跳：兜住后端异常退出。
  // 5 秒一次够密，但**页面被浏览器冻结时心跳也会停**（Chrome 的 Memory Saver 会在
  // 后台丢弃标签，setInterval 整个不跑）。所以后端不能把"没有请求"当成"窗口关了"，
  // 判定见 core 里 IDLE_LIMIT 的注释。
  // 带 page=1：后端据此判断"还有窗口在看"，再启动一次时才不会重复开窗。
  const beat = () => fetch('/api/ping?page=1')
    .then((r) => { if (r.ok) clearServiceDown(); })
    .catch(() => markServiceDown());
  setInterval(beat, 5000);

  // 从冻结 / 最小化恢复时立刻补一次：恢复那一瞬间用户最可能马上点东西，
  // 等下一个 5 秒心跳就来不及了，页面会先甩他一句红字。
  document.addEventListener('visibilitychange', () => { if (!document.hidden) beat(); });
  window.addEventListener('pageshow', beat);

  // 关窗口时主动通知服务退出。用 sendBeacon 而不是 fetch：
  // 页面卸载阶段发起的普通请求会被浏览器取消，beacon 是专门为此设计的。
  //
  // 但"页面卸载"不等于"窗口关了"：按 F5 刷新、前进后退，同样是卸载。
  // 之前每次刷新都把服务送走，刷新后整页 Failed to fetch，看起来像软件坏了。
  // 所以只把 reload / back_forward 放过去，它们之后页面还会回来。
  window.addEventListener('beforeunload', (e) => {
    const nav = performance.getEntriesByType('navigation')[0];
    const type = (e && e.currentTarget && nav && nav.type) || 'navigate';
    if (type === 'reload' || type === 'back_forward') return;
    navigator.sendBeacon('/api/bye');
  });
}

document.addEventListener('DOMContentLoaded', init);

/* ------------------------------------------------------ 生效自检 与 云记忆
   两块都不是"再列一堆数据"，而是回答两个用户反复问的问题：
     1. 这一轮，宿主到底读到我的人格了吗？
     2. 宿主自己那份账号记忆（每轮注入）里现在是什么？敢不敢动？
*/

const VERDICT_STYLE = {
  ok: { cls: 'ok', label: '本轮已注入' },
  carried: { cls: 'ok', label: '本轮未重注，靠历史' },
  stale: { cls: 'warn', label: '读到的是旧版本' },
  partial: { cls: 'warn', label: '只对上一部分' },
  missing: { cls: 'warn', label: '写了但没进去' },
  no_injection: { cls: 'danger', label: '没被读取' },
  no_session: { cls: 'muted', label: '无法判定' },
};

/* 一家宿主的取证卡片。
   为什么不把两家合成一张：取证位置、通道、失败形态都不一样，
   合起来只能写"部分有效"，用户看完不知道该去改哪一边。 */
function verifyCard(hostName, d) {
  const st = VERDICT_STYLE[d.verdict] || VERDICT_STYLE.no_session;
  const fmt = (list) => (list || [])
    .map((x) => `<code>${esc(x.id)}</code> <span class="mono-dim">${esc(x.hash)}</span>`)
    .join('、') || '（空）';
  const ctx = fmt(d.in_context);
  const disk = fmt(d.on_disk);
  const sources = (d.sources || []).filter(Boolean);
  const extra = [];
  if (typeof d.turns_since === 'number') {
    extra.push(`<div><span class="vb-k">距上次注入</span><span class="vb-v mono-dim">${d.turns_since} 轮</span></div>`);
  }
  if (d.segment_chars) {
    const win = d.window ? ` / ${d.window}` : '';
    extra.push(`<div><span class="vb-k">注入段长度</span><span class="vb-v mono-dim">${d.segment_chars}${win} 字符${d.truncated ? '（超窗，尾部会被截断）' : ''}</span></div>`);
  }
  if (sources.length) {
    extra.push(`<div><span class="vb-k">指令来源</span><span class="vb-v mono-dim">${sources.map(esc).join(' · ')}</span></div>`);
  }
  if (d.session) {
    extra.push(`<div><span class="vb-k">取证的会话文件</span><span class="vb-v mono-dim break">${esc(d.session)}</span></div>`);
  }
  return `
    <div class="vb-host ${st.cls}">
      <div class="vb-head"><span class="vb-host-name">${esc(hostName)}</span>
        <span class="vb-tag ${st.cls}">${st.label}</span>
        <span class="vb-summary">${esc(d.summary || '')}</span></div>
      <div class="vb-grid">
        <div><span class="vb-k">宿主这一轮读到的</span><span class="vb-v">${ctx}</span></div>
        <div><span class="vb-k">磁盘上现在挂着的</span><span class="vb-v">${disk}</span></div>
        ${extra.join('')}
      </div>
    </div>`;
}

const HOST_LABEL = { workbuddy: 'WorkBuddy', dsh: 'DSH（DeepSeek Harness）' };

function renderVerify(d) {
  const box = $('verify-box');
  if (!box) return;
  const st = VERDICT_STYLE[d.verdict] || VERDICT_STYLE.no_session;

  let inner;
  if (d.hosts) {
    const order = ['workbuddy', 'dsh'];
    const names = order.filter((k) => d.hosts[k]);
    inner = names.map((k) => verifyCard(HOST_LABEL[k] || k, d.hosts[k])).join('');
  } else {
    inner = verifyCard(HOST_LABEL[d.host] || '宿主', d);
  }

  box.hidden = false;
  box.className = `verify-box ${st.cls}`;
  box.innerHTML = `
    <div class="vb-note">顶层判定取自「这一轮真去读了」的那一家；两家都读了就看谁的会话更新。下面逐家列出，缺数据的显示为无法判定。</div>
    ${inner}`;
}

async function doVerifyInjection(btn) {
  busy(btn, true);
  status('正在从宿主会话记录里核对…', 'busy');
  try {
    const d = await api('/api/verify');
    renderVerify(d);
    const good = d.verdict === "ok" || d.verdict === "carried";
    const who = HOST_LABEL[d.host] || '宿主';
    status(good ? `自检完成：${who} 人格在位` : `${who} 自检有结论，见下方`, good ? "ok" : "warn");
  } catch (e) {
    toast('自检失败：' + e.message, true);
    status('自检失败', 'err');
  } finally {
    busy(btn, false);
  }
}

/* ------------------------------------------------------ 诊断抽屉
   自检 / 格式遵从度 / 云记忆三块共用右侧抽屉。

   为什么不再内嵌在列表上方：那三块动辄一两屏高，一展开就把「注入了哪些人格」
   推出视口——而列表才是这一页的主体。诊断是阶段性结论，看一眼就走，
   跟主体抢同一块纵向空间，永远是诊断赢、列表消失。

   懒加载：只有第一次进某个页签才打对应接口，避免打开抽屉就并发三个请求。 */
const DIAG_META = {
  verify: {
    title: '自检：这一轮读到了吗',
    sub: '从宿主自己的会话记录里读回「这一轮到底注入了什么」，再与磁盘逐项比对。',
  },
  audit: {
    title: '格式遵从度',
    sub: '统计人格的格式要求被兑现了几成。测的是格式，不是内容边界。',
  },
  cloudmem: {
    title: '云记忆',
    sub: '宿主自己维护的账号级记忆，每轮注入。写入前自动备份，可逐字节还原。',
  },
  tpl: {
    title: '宿主模板',
    sub: '专家会话默认只读 USER.md。这里给专家模板补 SOUL 插槽，人格放 SOUL.md 才两边都生效、又不跟专家团抢文件。',
  },
};
const diagLoaded = { verify: false, audit: false, cloudmem: false, tpl: false };

function openDiag(kind, force) {
  const bd = $('diag-backdrop');
  if (!bd) return;
  bd.hidden = false;
  switchDiag(kind || 'verify', !!force);
}

function closeDiag() {
  const bd = $('diag-backdrop');
  if (bd) bd.hidden = true;
}

function switchDiag(kind, force) {
  const meta = DIAG_META[kind];
  if (!meta) return;
  if (force) diagLoaded[kind] = false;
  // 每次"显式打开"都重新挑会话。
  // 不重置的话，钉住会跨打开一直生效：换了对话再点破甲度，看到的还是上一次那个会话的
  // 数字 —— 正是"打开这个对话却看到别的对话的统计"。
  // 钉住只在同一次打开、同一次渲染内起作用，用来挡两个窗口交替写入造成的抖动。
  if (force && kind === 'audit') S.auditPin = null;
  $('diag-title').textContent = meta.title;
  $('diag-sub').textContent = meta.sub;
  document.querySelectorAll('.diag-tab').forEach((t) => {
    const on = t.dataset.diag === kind;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('.diag-sec').forEach((s) => {
    s.hidden = s.id !== `diag-sec-${kind}`;
  });
  $('diag-body').scrollTop = 0;
  if (diagLoaded[kind]) return;
  diagLoaded[kind] = true;
  if (kind === 'verify') doVerifyInjection(null);
  else if (kind === 'audit') loadAudit(null);
  else if (kind === 'tpl') loadTpl();
  else loadCloudMem();
}

/* ------------------------------------------------------ 宿主模板
   专家会话默认只有 USER.md 一条身份通道（模板里没有 SOUL 插槽）。
   人格写 USER.md 就会和专家团 prompt 抢解释权；补上插槽后人格可以放 SOUL.md，
   两边各用各的文件。升级会覆盖模板，所以这里要能一眼看出"插槽还在不在"。 */
const TPL_STATE_TEXT = {
  patched: { cls: 'ok', label: '已含 SOUL 插槽' },
  missing: { cls: 'bad', label: '没有 SOUL 插槽' },
  absent: { cls: 'warn', label: '找不到这个模板' },
  unknown: { cls: 'warn', label: '读取失败' },
};

/* 开机静默检查：补过的插槽还在不在。
   这是"升级提醒"的唯一入口——WorkBuddy 升级会整份覆盖模板，不报错、不提示，
   表现只是"人格在专家会话里突然不生效了"。等到用户自己发现，已经浪费好几轮对话。 */
async function checkTplOnBoot() {
  try {
    const d = await api('/api/tpl');
    const expert = d.expert || {};
    if (d.stale || (d.record && expert.state !== 'patched')) {
      status('宿主模板被覆盖：人格在专家会话里已失效', 'warn');
      toast('WorkBuddy 升级后专家模板被覆盖，SOUL 插槽没了。去「注入管理 → 宿主模板」点一下补回。', true);
    }
  } catch (e) {
    /* 静默：开机检查失败不该弹窗打扰，用户需要时去「宿主模板」页能自己看到 */
  }
}

async function loadTpl() {
  const box = $('tpl-box');
  if (!box) return;
  box.innerHTML = '<div class="cm-loading">正在检查宿主模板…</div>';
  let d;
  try {
    d = await api('/api/tpl');
  } catch (e) {
    box.innerHTML = `<div class="cm-loading err">检查失败：${esc(e.message)}</div>`;
    return;
  }
  const host = d.host || {};
  const rows = (d.templates || []).map((t) => {
    const st = TPL_STATE_TEXT[t.state] || TPL_STATE_TEXT.unknown;
    return `<tr>
      <td><code class="break">${esc(t.name)}</code></td>
      <td><span class="tpl-badge ${st.cls}">${esc(st.label)}</span></td>
      <td class="mono-dim">${t.size ? t.size + ' 字节' : '—'}</td>
    </tr>`;
  }).join('');

  const expert = d.expert || {};
  const patched = expert.state === 'patched';
  const warn = (d.warnings || []).map((w) => `<div class="tpl-warn">${esc(w)}</div>`).join('');

  box.innerHTML = `
    <div class="cm-head"><b>宿主身份模板</b>
      <span class="cm-meta">WorkBuddy ${esc(host.appVersion || '版本未知')}${
        host.generatedAt ? ' ｜ 安装于 ' + esc(String(host.generatedAt).slice(0, 10)) : ''
      }</span>
    </div>
    <p class="cm-note">
      普通会话注入 SOUL + IDENTITY + USER；<b>专家会话只有 USER</b>——因为专家模板里没有
      <code>{{ SoulContent }}</code> 插槽。人格写在 USER.md 就会和专家团的人设抢同一个文件；
      补上插槽后，人格放 SOUL.md，普通会话和专家会话都能读到，USER.md 还给画像用。
    </p>
    ${warn}
    <table class="au-table"><tbody>${rows}</tbody></table>
    <div class="tpl-actions">
      <button class="btn ${patched ? 'ghost' : 'primary'}" id="tpl-apply">${patched ? '重新确认插槽' : '补上 SOUL 插槽'}</button>
      <button class="btn ghost" id="tpl-revert" ${patched ? '' : 'disabled'}>还原成原版</button>
      <span class="mono-dim" id="tpl-hint">${
        patched
          ? '补完之后需要完全退出 WorkBuddy（含托盘）再打开才生效。升级后这里会重新出现「没有 SOUL 插槽」，点一下就能补回。'
          : '点「补上 SOUL 插槽」会先备份原文件再写入，随时可以还原。'
      }</span>
    </div>`;

  $('tpl-apply').onclick = async () => {
    const btn = $('tpl-apply');
    busy(btn, true);
    try {
      const r = await api('/api/tpl/apply', { method: 'POST', body: '{}' });
      toast(r.patched ? '插槽已补上，请完全退出 WorkBuddy 再打开' : '当前已经是补好的状态');
      await loadTpl();
    } catch (e) {
      toast('补插槽失败：' + e.message, true);
    } finally {
      busy(btn, false);
    }
  };
  $('tpl-revert').onclick = async () => {
    if (!(await confirmModal({
      title: '还原宿主模板？',
      body: '会把专家模板恢复成 WorkBuddy 原版（去掉 SOUL 插槽）。<br>还原后专家会话不再读人格，需要重开客户端才生效。',
      okText: '还原', danger: true,
    }))) return;
    try {
      await api('/api/tpl/revert', { method: 'POST', body: '{}' });
      toast('已还原成原版');
      await loadTpl();
    } catch (e) {
      toast('还原失败：' + e.message, true);
    }
  };
}

async function loadCloudMem() {
  const box = $('cloudmem-box');
  if (!box) return;
  box.hidden = false;
  box.innerHTML = `<div class="cm-loading">正在读取云记忆…</div>`;
  let d;
  try {
    d = await api('/api/cloudmem');
  } catch (e) {
    box.innerHTML = `<div class="cm-loading err">读取失败：${esc(e.message)}</div>`;
    return;
  }
  if (!d.exists) {
    box.innerHTML = `<div class="cm-loading">本机没有云记忆文件（客户端还没生成账号记忆）。</div>`;
    return;
  }
  const over = d.chars > d.limit;
  const backs = (d.backups || [])
    .map((b) => `<li><code class="break">${esc(b.name)}</code>
        <span class="mono-dim">${(b.size / 1024).toFixed(0)} KB</span>
        <button class="btn ghost tiny" data-restore="${esc(b.path)}">还原</button></li>`)
    .join('');

  box.innerHTML = `
    <div class="cm-head">
      <b>账号级云记忆</b>
      <span class="cm-meta">版本 ${d.version} ｜ ${d.chars} / ${d.limit} 字符${over ? ' <b class="danger">超窗</b>' : ''}
        ｜ 双写一致：${d.raw_ok ? '是' : '<b class="danger">否（宿主同步的是另一份）</b>'}</span>
    </div>
    <p class="cm-note">
      这一层由宿主自己维护、<b>每轮注入</b>。改写后请在客户端里发一句话，再点「自检」确认它真的进去了。
      写入前会自动备份，还原按钮就在下面。
    </p>
    <div class="cm-fill">
      <span class="mono-dim">从人格库填入：</span>
      <select id="cm-persona" class="cm-select">
        ${(S.personas || []).map((x) => `<option value="${esc(x.id)}">${esc(x.name || x.id)}</option>`).join('')}
      </select>
      <button class="btn ghost tiny" id="cm-fill">填入正文</button>
      <span class="mono-dim" id="cm-fill-hint"></span>
    </div>
    <textarea id="cm-text" class="cm-text" rows="7" placeholder="留空表示清空云记忆">${esc(d.block)}</textarea>
    <div class="cm-actions">
      <button class="btn primary" id="cm-save">写入云记忆</button>
      <span class="mono-dim break">${esc(d.path)}</span>
    </div>
    <details class="cm-backs"><summary>备份（${(d.backups || []).length}）</summary>
      <ul>${backs || '<li class="mono-dim">暂无备份</li>'}</ul>
    </details>`;

  // 从人格库填正文：省掉手动复制粘贴，也避免粘错人
  $('cm-fill').onclick = async () => {
    const pid = $('cm-persona').value;
    const btn = $('cm-fill');
    busy(btn, true);
    try {
      const r = await api(`/api/personas/${encodeURIComponent(pid)}/body`);
      $('cm-text').value = r.body;
      $('cm-fill-hint').textContent = `已填入 ${r.chars} 字符`;
      status(`已把「${pid}」的正文填进来，还没写入`);
    } catch (e) {
      toast('取正文失败：' + e.message, true);
    } finally {
      busy(btn, false);
    }
  };

  $('cm-save').onclick = async () => {
    const block = $('cm-text').value;
    if (d.block && !(await confirmModal({
      title: '覆盖已有云记忆？',
      body: `云记忆里现在有 <b>${d.chars} 字符</b>内容，写入会把它替换掉（写前自动备份）。<br><br>继续吗？`,
      okText: '覆盖写入', danger: true,
    }))) return;
    const btn = $('cm-save');
    busy(btn, true);
    try {
      const r = await api('/api/cloudmem', { method: 'POST', body: JSON.stringify({ block, force: true }) });
      if (r.ok) {
        toast(`云记忆已写入（版本 ${r.version}，${r.chars} 字符），备份：${String(r.backup).split(/[\\/]/).pop()}`);
        (r.warnings || []).forEach((w) => toast(w, true));
        await loadCloudMem();
      } else {
        toast(r.message || '写入失败', true);
      }
    } catch (e) {
      toast(e.message, true);
    } finally {
      busy(btn, false);
    }
  };

  box.querySelectorAll('[data-restore]').forEach((b) => {
    b.onclick = async () => {
      const ok = await confirmModal({
        title: '从这个备份还原？',
        body: '会先把你当前的云记忆另存一份，再用备份覆盖。',
        okText: '还原',
      });
      if (!ok) return;
      try {
        // 后端漏 return 时 d 会是 null —— 那样这里会抛 TypeError，
        // 用户看到的是 "Cannot read properties of null"，比不提示更糟。
        const r0 = await api('/api/cloudmem/restore', {
          method: 'POST',
          body: JSON.stringify({ path: b.dataset.restore }),
        });
        const r = r0 || {};
        if (r.ok) {
          toast('已还原（当前这份也留了安全副本）');
          await loadCloudMem();
        } else {
          toast(r.message || '还原失败', true);
        }
      } catch (e) {
        toast(e.message, true);
      }
    };
  });
}


/* ------------------------------------------------------ 格式遵从度
   和「生效自检」互补：自检答"注入进去了没"，这里答"进去之后起效了多少"。
   刻意不给总分——marker/禁用词命中是"没执行"，拒绝语命中是"边界还在"，
   方向相反，相加得到的数字没有意义。 */
async function loadAudit(btn) {
  const box = $('audit-box');
  if (!box) return;
  if (btn) busy(btn, true);
  box.hidden = false;
  box.innerHTML = '<div class="cm-loading">正在统计最近一个会话…</div>';
  try {
    // 首次自动挑一个会话，之后一直用它。
    // 不钉的话：宿主同时开着两个窗口，两个会话文件交替写入，谁后写谁就是"最新"，
    // 于是相隔几秒刷两次，样本数在 10 和 2 之间跳，而用户什么都没做。
    const q = S.auditPin ? `&session=${encodeURIComponent(S.auditPin)}` : '';
    const d = await api(`/api/audit?sessions=1${q}`);
    // 采纳"后端实际统计的那一个"，而不是"我请求的那一个"。
    // d.pinned=false 有两种情况：首次没传（正常）、传了但会话文件已不在（要跟随）。
    if (!d.pinned && d.sessions && d.sessions[0]) S.auditPin = d.sessions[0].name;
    if (!d.replies) {
      box.innerHTML = '<div class="cm-loading">这个会话里还没有足够长的回复可统计（阈值 200 字）。</div>';
      return;
    }
    // 按覆盖域分组：四项测格式，一项只看已发生的拒绝。
    // 分组的理由：混在一张表里，读者会以为"破甲度"测的是内容边界，实际一项都不测。
    const GROUPS = [
      { key: 'format', label: '格式遵从', hint: '人格写的格式要求被兑现了几成' },
      { key: 'boundary', label: '边界观察', hint: '只记录已发生的拒绝，不主动试探' },
    ];
    const cell = (m) => {
      const good = m.direction === 'hit_is_good';
      const safe = m.direction === 'hit_is_safe';
      const tone = safe
        ? (m.hit > 0 ? 'ok' : 'warn')
        : good
        ? (m.hit > 0 ? 'ok' : 'bad')
        : (m.hit > 0 ? 'warn' : 'ok');
      const detail = m.detail
        ? `<div class="au-detail">${Object.entries(m.detail).map(([k, v]) => `${esc(k)} ${v}`).join(' · ')}</div>`
        : '';
      // 剔除的"引用讨论"条数必须显示：不显示的话，用户会以为这些条数凭空消失了
      const skipped = m.skipped_self_ref
        ? `<div class="au-detail">另有 ${m.skipped_self_ref} 条在讨论规则时引用了这些词，已剔除不计</div>`
        : '';
      // 样本不足时不给百分比：n 太小的比例是噪声，写出来会被当结论
      const pct = m.enough_sample === false
        ? '<span class="au-weak">样本不足</span>'
        : `${m.rate}%`;
      return `<tr class="au-row ${tone}">
          <td>${esc(m.label)}</td>
          <td class="au-num"><b>${m.hit}</b>/${m.total}</td>
          <td class="au-pct">${pct}</td>
          <td class="au-expect">${esc(m.expect)}${detail}${skipped}</td>
        </tr>`;
    };
    const body = GROUPS.map((g) => {
      const items = (d.metrics || []).filter((m) => (m.coverage || 'format') === g.key);
      if (!items.length) return '';
      return `<tr class="au-group"><td colspan="4">${esc(g.label)}
          <span class="au-group-hint">${esc(g.hint)}</span></td></tr>
        ${items.map(cell).join('')}`;
    }).join('');
    const which = (d.sessions || [])
      .map((x) => `${esc(String(x.name).slice(0, 8))} (${x.replies})`
        + (x.updated ? ` · 更新 ${esc(x.updated)}` : ''))
      .join('、');
    box.innerHTML = `
      <div class="cm-head"><b>格式遵从度</b>
        <span class="cm-meta">样本 ${d.replies} 条实质回复 · 会话 ${which}${d.pinned ? ' · 已锁定' : ''}${d.pin_missed ? ' · 原会话已不存在，已切到最新' : ''}</span>
      </div>
      <p class="cm-warn-line">前四项测的是<b>格式遵从</b>，不是内容边界；第五项只记录已发生的拒绝。四项里没有一项能回答"内容限制有没有被突破"。</p>
      <table class="au-table">${body}</table>
      ${(d.advice || []).length ? `<div class="au-advice">
        <div class="au-advice-head">下一步改什么</div>
        ${d.advice.map((a) => `<div class="au-advice-item ${esc(a.level)}">
            <div class="au-advice-title">${esc(a.title)}</div>
            <div class="au-advice-text">${esc(a.text)}</div>
          </div>`).join('')}
      </div>` : ''}
      <p class="cm-note">${esc(d.note || '')}</p>`;
    status('统计完成');
  } catch (e) {
    box.innerHTML = `<div class="cm-loading err">统计失败：${esc(e.message)}</div>`;
  } finally {
    if (btn) busy(btn, false);
  }
}

