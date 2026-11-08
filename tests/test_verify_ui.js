#!/usr/bin/env node
/* 自检面板的前端判据：一个 /api/verify 响应里带着两家宿主的结果，界面必须逐家列出来。
 *
 * 为什么单开一条：后端把 WorkBuddy 和 DSH 合并返回后，最容易犯的错是
 * 「只渲染赢的那一家」——界面上一行绿字、看着很对，但另一家是「没读到」还是
 * 「没装」用户永远看不到，也就不知道要不要去改。
 *
 * 跑法：node tests/test_verify_ui.js（用最小 DOM 桩加载真实 web/app.js，不联网不写文件）
 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const target = process.argv[2] || path.join(__dirname, '..', 'web', 'app.js');
const src = fs.readFileSync(target, 'utf8');

function el(id) {
  const e = {
    id: id || '', textContent: '', innerHTML: '', hidden: false, scrollTop: 0,
    value: '', checked: false, disabled: false, dataset: {}, style: {}, children: [],
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; },
    removeChild() {}, remove() {}, focus() {}, blur() {}, click() {},
    setAttribute() {}, getAttribute() { return null; }, hasAttribute() { return false; },
    addEventListener() {}, removeEventListener() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    insertAdjacentHTML() {}, closest() { return null; },
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
  console, document,
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

let PASS = 0;
const FAILS = [];
function check(name, cond, detail = '') {
  if (cond) {
    PASS += 1;
    console.log('  \u2713 ' + name);
  } else {
    FAILS.push(`${name}  ->  ${String(detail).slice(0, 200)}`);
    console.log('  \u2717 ' + name + '  ->  ' + String(detail).slice(0, 200));
  }
}
function run(code) { return vm.runInContext(code, sandbox, { filename: 'probe.js' }); }

vm.runInContext(src, sandbox, { filename: 'app.js' });
check('app.js 可加载', run('typeof renderVerify === "function"'));

// ---------------------------------------------------------------- A 两家并列
const twoHosts = {
  verdict: 'ok', host: 'dsh',
  hosts: {
    workbuddy: {
      verdict: 'carried', host: 'workbuddy',
      summary: '本轮没有重新注入（3 分钟前），不等于失效',
      in_context: [{ id: 'imported', hash: 'c9b616b8' }],
      on_disk: [{ id: 'imported', hash: 'c9b616b8' }],
      turns_since: 2, segment_chars: 9449, window: 10000,
      session: './',
    },
    dsh: {
      verdict: 'no_injection', host: 'dsh',
      summary: '会话里没有任何指令链注入',
      in_context: [], on_disk: [{ id: 'imported', hash: 'c9b616b8' }],
      session: './',
    },
  },
};

run(`renderVerify(${JSON.stringify(twoHosts)})`);
const html = run('$("verify-box").innerHTML');
check('两家都出现在面板里', html.includes('WorkBuddy') && html.includes('DSH'), html.slice(0, 120));
check('WorkBuddy 显示「本轮未重注」', html.includes('本轮未重注，靠历史'), '');
check('DSH 显示「没被读取」', html.includes('没被读取'), '');
check('两种判定样式分开挂', html.includes('vb-host ok') && html.includes('vb-host danger'),
  html.match(/vb-host \w+/g) || []);
check('DSH 的取证会话文件被列出', html.includes('session.v4.jsonl.zstd'), '');

// ---------------------------------------------------------------- B 单一宿主
run(`renderVerify(${JSON.stringify({
  verdict: 'ok', host: 'dsh', summary: '读到了',
  in_context: [{ id: 'imported', hash: 'c9b616b8' }],
  on_disk: [{ id: 'imported', hash: 'c9b616b8' }],
  sources: ['~/.dsh/AGENTS.md'],
})})`);
const one = run('$("verify-box").innerHTML');
check('没有 hosts 时退回单卡', one.includes('DSH') && !one.includes('WorkBuddy'), one.slice(0, 100));
check('单卡也显示指令来源', one.includes('~/.dsh/AGENTS.md'), '');

// ---------------------------------------------------------------- C 坏数据不崩
let threw = null;
try {
  run(`renderVerify(${JSON.stringify({ verdict: 'whatever', hosts: { dsh: { summary: '' } } })})`);
} catch (e) {
  threw = e && e.message;
}
check('认不出的判定不抛异常', threw === null, threw || '');
const bad = run('$("verify-box").innerHTML');
check('认不出的判定落到「无法判定」', bad.includes('无法判定'), bad.slice(0, 100));

// ---------------------------------------------------------------- D 空宿主
run('renderVerify({ verdict: "no_session", hosts: {} })');
const empty = run('$("verify-box").innerHTML');
check('两家都没数据时不显示假卡片', !empty.includes('vb-host '), empty.slice(0, 120));

console.log('');
console.log(`${PASS} 项通过，${FAILS.length} 项失败`);
if (FAILS.length) {
  FAILS.forEach((f) => console.log('  - ' + f));
  process.exit(1);
}
