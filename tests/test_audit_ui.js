/* 用最小 DOM 桩在 node 里跑真实 web/app.js，验证"打开面板会重新挑会话"与
   "采纳后端实际统计的会话"两条前端行为。不联网、不写文件。 */
const fs = require('fs');
const vm = require('vm');

const path = require('path');
const target = process.argv[2] || path.join(__dirname, '..', 'web', 'app.js');
const src = fs.readFileSync(target, 'utf8');

function el(id) {
  const e = {
    id: id || '', textContent: '', innerHTML: '', hidden: false, scrollTop: 0,
    value: '', checked: false, disabled: false, dataset: {}, style: {},
    children: [],
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(){}, remove(){}, focus(){}, blur(){}, click(){},
    setAttribute(){}, getAttribute(){ return null; }, hasAttribute(){ return false; },
    addEventListener(){}, removeEventListener(){},
    querySelector(){ return null; }, querySelectorAll(){ return []; },
    insertAdjacentHTML(){}, closest(){ return null; },
  };
  return e;
}

const registry = new Map();
const document = {
  getElementById(id) {
    if (!registry.has(id)) registry.set(id, el(id));
    return registry.get(id);
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement() { return el(); },
  body: el('body'),
  addEventListener() {},
  documentElement: el('html'),
};

const sandbox = {
  console,
  document,
  window: { addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) },
  location: { search: '', href: 'http://127.0.0.1/', reload() {} },
  URLSearchParams,
  navigator: { sendBeacon: () => true },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  performance: { getEntriesByType: () => [] },
  setInterval: () => 0,
  clearInterval() {},
  setTimeout: () => 0,
  fetch: async () => ({ ok: true, status: 200, json: async () => ({ ok: true, data: {} }) }),
  alert() {}, confirm: () => true,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const results = [];
function check(name, cond, detail) {
  results.push({ name, cond: !!cond, detail: detail === undefined ? '' : String(detail) });
}
function run(code) { return vm.runInContext(code, sandbox, { filename: 'probe.js' }); }

vm.runInContext(src, sandbox, { filename: 'app.js' });
check('app.js 能在无 DOM 环境里加载（未 init）', run('typeof S === "object" && typeof loadAudit === "function"'));

// —— 行为 1：显式打开面板时必须解锁，重新挑"当前最新会话"
run('S.auditPin = "old-session.jsonl"; diagLoaded.audit = true;');
run('switchDiag("audit", true)');
check('显式打开（force）后钉住被清空', run('S.auditPin') === null, run('S.auditPin'));

// —— 行为 2：同一次打开内的普通页签切换不清空（否则又回到抖动）
run('S.auditPin = "kept.jsonl"; diagLoaded.audit = true;');
run('switchDiag("audit", false)');
check('页签内切换（非 force）保留钉住', run('S.auditPin') === 'kept.jsonl', run('S.auditPin'));

// —— 行为 3：后端说"没钉住"时必须采纳它实际统计的那个会话
run(`
  S.auditPin = "stale.jsonl";
  api = async () => ({ pinned: false, pin_missed: true, replies: 2,
    sessions: [{ name: "real.jsonl", replies: 2, updated: "17:44:35" }],
    metrics: [], advice: [], note: "", min_chars: 200 });
  loadAudit(null);
`);
setTimeout(() => {
  check('钉住失效时采纳后端实际会话', run('S.auditPin') === 'real.jsonl', run('S.auditPin'));

  // —— 行为 4：后端说"钉住了"时不许被覆盖（否则统计对象又漂）
  run(`
    S.auditPin = "want.jsonl";
    api = async () => ({ pinned: true, pin_missed: false, replies: 1,
      sessions: [{ name: "want.jsonl", replies: 1, updated: "17:50:00" }],
      metrics: [], advice: [], note: "", min_chars: 200 });
    loadAudit(null);
  `);
  setTimeout(() => {
    check('钉住生效时保持不动', run('S.auditPin') === 'want.jsonl', run('S.auditPin'));
    const bad = results.filter((r) => !r.cond);
    for (const r of results) console.log((r.cond ? '  ✓ ' : '  ✗ ') + r.name + (r.detail ? '  -> ' + r.detail : ''));
    console.log(`\n通过 ${results.length - bad.length} 项，失败 ${bad.length} 项`);
    process.exit(bad.length ? 1 : 0);
  }, 20);
}, 20);
