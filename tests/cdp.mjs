/**
 * 零依赖 CDP 客户端：用 Edge 自带的调试协议真实操作页面。
 *
 * 为什么不用 Playwright：本机没有，装它要下 100+MB 浏览器；而 Edge 已经装了，
 * 用 --remote-debugging-port 起一个实例就能通过 CDP 完全控制。Node 22 自带 WebSocket，
 * 所以整个工具零第三方包。
 *
 * 用法：
 *   node tests/cdp.mjs <端口> <表达式文件>
 * 表达式文件里写一段 JS（可用 await），返回值会被打印成 JSON。
 */
import { readFileSync } from 'node:fs';

const PORT = process.argv[2] || '9222';
const FILE = process.argv[3];

async function targets() {
  const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
  return r.json();
}

async function main() {
  if (!FILE) {
    console.error('用法: node cdp.mjs <调试端口> <表达式文件>');
    process.exit(2);
  }
  const expr = readFileSync(FILE, 'utf8');

  // 找到页面 target（忽略 devtools 自己的 target）
  const list = await targets();
  const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
  if (!page) {
    console.error('没找到可用的页面 target');
    process.exit(3);
  }

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();

  const send = (method, params) =>
    new Promise((resolve, reject) => {
      const mid = ++id;
      pending.set(mid, { resolve, reject });
      ws.send(JSON.stringify({ id: mid, method, params }));
    });

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result);
    }
  };

  await new Promise((res, rej) => {
    ws.onopen = res;
    ws.onerror = rej;
  });

  await send('Runtime.enable');

  const out = await send('Runtime.evaluate', {
    expression: expr,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true,   // 让 click() 之类走真实用户手势路径
  });

  if (out.exceptionDetails) {
    console.error('页面抛异常:');
    console.error(out.exceptionDetails.exception?.description || out.exceptionDetails.text);
    ws.close();
    process.exit(4);
  }

  console.log(JSON.stringify(out.result.value, null, 2));
  ws.close();
}

main().catch((e) => {
  console.error('CDP 失败:', e.message);
  process.exit(1);
});
