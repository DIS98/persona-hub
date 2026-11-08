/**
 * 「使用指引只看一次」的回归测试。
 *
 * 真实事故（2026-11-02 用户报告）：桌面版每次打开都弹使用指引，点过 8 次跳过。
 * 根因：外壳每次启动向系统要随机空闲端口，页面地址是 http://127.0.0.1:<随机端口>，
 * **端口不同 = origin 不同 = localStorage 各存各的**。用户点的那 8 次，写进了 8 个
 * 互不相认的 origin（leveldb 里能数出来）。所以判定不能只看 localStorage。
 *
 * 这里不跑浏览器，直接拿 web/app.js 里的两个函数在 vm 里跑，模拟"换端口"这件事：
 * localStorage 每次换一个空对象，而外壳侧的标记（phUI）跨端口保持。
 *
 * 判据：换端口后仍然认得出"看过了"。给一个只读 localStorage 的变异，必须报红。
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..');
const SRC = path.join(ROOT, 'web', 'app.js');

let pass = 0;
let fail = 0;
function check(name, cond, extra) {
  if (cond) {
    pass++;
    console.log(`  OK   ${name}`);
  } else {
    fail++;
    console.log(`  FAIL ${name}${extra ? '  ← ' + extra : ''}`);
  }
}

/** 从 app.js 里切出 guideSeen / markGuideSeen，带一个最小 localStorage 桩 */
function load(mutateSrc) {
  let src = fs.readFileSync(SRC, 'utf8');
  const a = src.indexOf('async function guideSeen');
  const b = src.indexOf('function endGuide');
  if (a < 0 || b < 0 || b < a) throw new Error('app.js 里找不到 guideSeen/endGuide，锚点变了');
  let body = src.slice(a, b);
  if (mutateSrc) body = mutateSrc(body);

  const makeStore = (init) => {
    const m = new Map(Object.entries(init || {}));
    return {
      getItem: (k) => (m.has(k) ? m.get(k) : null),
      setItem: (k, v) => m.set(k, String(v)),
      _dump: () => Object.fromEntries(m),
    };
  };

  return { code: `const GUIDE_KEY = 'ph-guide-v1-done';\n${body}\n;this.__seen = guideSeen; this.__mark = markGuideSeen;`, makeStore };
}

function run(loader, { phUI, store, setFlagSink }) {
  const sandbox = {
    window: phUI === undefined ? {} : { phUI },
    localStorage: store,
  };
  if (phUI && !phUI.setFlag && setFlagSink) phUI.setFlag = setFlagSink;
  vm.runInNewContext(loader.code, sandbox);
  return sandbox;
}

(async () => {
console.log('=== A 组：判定路径 ===');

{
  const L = load();
  // A1 外壳里已有标记，localStorage 空的（换过端口）→ 认
  const box = run(L, {
    phUI: { getFlag: async () => '1', setFlag: async () => true },
    store: L.makeStore({}),
  });
  check('A1 外壳有标记、本地为空 → 已看过', await box.__seen() === true);
}

{
  const L = load();
  // A2 两边都空 → 该弹
  const box = run(L, {
    phUI: { getFlag: async () => null, setFlag: async () => true },
    store: L.makeStore({}),
  });
  check('A2 两边都没有 → 未看过（首次该弹）', await box.__seen() === false);
}

{
  const L = load();
  // A3 浏览器直连（没有外壳）→ 退回 localStorage
  const box = run(L, { store: L.makeStore({ 'ph-guide-v1-done': '1' }) });
  check('A3 无外壳 + 本地有标记 → 已看过', await box.__seen() === true);
}

{
  const L = load();
  // A4 外壳接口抛错（版本不匹配之类）时不能把页面弄崩，退回本地判定
  const box = run(L, {
    phUI: { getFlag: async () => { throw new Error('boom'); }, setFlag: async () => true },
    store: L.makeStore({ 'ph-guide-v1-done': '1' }),
  });
  let ok = false;
  try {
    ok = (await box.__seen()) === true;
  } catch {
    ok = false;
  }
  check('A4 外壳接口抛错 → 不崩，退回本地判定', ok);
}

{
  const L = load();
  // A5 localStorage 被禁用（隐私模式）→ 不崩
  const box = run(L, {
    phUI: { getFlag: async () => null, setFlag: async () => true },
    store: {
      getItem: () => { throw new Error('denied'); },
      setItem: () => { throw new Error('denied'); },
    },
  });
  let ok = false;
  try {
    ok = (await box.__seen()) === false;
  } catch {
    ok = false;
  }
  check('A5 本地存储被禁 → 不崩，判为未看过', ok);
}

console.log('=== B 组：换端口这件事本身 ===');

{
  const L = load();
  // 模拟真实场景：第一次打开 → 点跳过 → 关掉 → 换端口再打开
  const shellFlags = {}; // 外壳侧，跨端口共享
  const phUI = {
    getFlag: async (k) => (k in shellFlags ? shellFlags[k] : null),
    setFlag: async (k, v) => { shellFlags[k] = v; return true; },
  };

  const originA = L.makeStore({}); // 端口 50065
  const boxA = run(L, { phUI, store: originA });
  check('B1 第一次打开 → 未看过（会弹）', await boxA.__seen() === false);
  boxA.__mark(); // 用户点「跳过」

  const originB = L.makeStore({}); // 端口 51914，换了个空 localStorage
  const boxB = run(L, { phUI, store: originB });
  check('B2 换端口再打开 → 仍认已看过（不弹）', await boxB.__seen() === true,
    '这就是用户报的那个 bug');

  check('B3 标记同时写了两处（外壳 + 本地）',
    shellFlags['ph-guide-v1-done'] === '1' && originA.getItem('ph-guide-v1-done') === '1',
    JSON.stringify({ shellFlags, local: originA._dump() }));
}

console.log('=== C 组：变异必须报红 ===');

{
  // 变异 1：退回事故前的写法——只看 localStorage
  const L = load((body) => {
    const out = body.replace(
      /let shell = null;[\s\S]*?return !!shell \|\| !!local;/,
      'return !!localStorage.getItem(GUIDE_KEY);'
    );
    if (out === body) throw new Error('变异 1 锚点没命中');
    return out;
  });
  const shellFlags = { 'ph-guide-v1-done': '1' };
  const phUI = { getFlag: async (k) => shellFlags[k] || null, setFlag: async () => true };
  const box = run(L, { phUI, store: L.makeStore({}) });
  const seen = await box.__seen();
  check('M1 只读 localStorage → 换端口后判定为未看过（报红）', seen === false);
}

{
  // 变异 2：写的时候只写本地，不写外壳（BUG 的另一种形态：判定修了、写入没修）
  const L = load((body) => {
    const out = body.replace(
      /if \(window\.phUI && window\.phUI\.setFlag\) window\.phUI\.setFlag\(GUIDE_KEY, '1'\);/,
      '/* 故意不写外壳 */'
    );
    if (out === body) throw new Error('变异 2 锚点没命中');
    return out;
  });
  const shellFlags = {};
  const phUI = {
    getFlag: async (k) => (k in shellFlags ? shellFlags[k] : null),
    setFlag: async (k, v) => { shellFlags[k] = v; return true; },
  };
  const boxA = run(L, { phUI, store: L.makeStore({}) });
  boxA.__mark();
  const boxB = run(L, { phUI, store: L.makeStore({}) }); // 换端口
  check('M2 只写本地不写外壳 → 换端口后仍弹（报红）', await boxB.__seen() === false);
}

console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);

})();
