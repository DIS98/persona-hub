#!/usr/bin/env node
/**
 * 桌面外壳的更新通道测试（node，不起 Electron、不联网）。
 *
 * 为什么必须单独测：更新是**唯一一条自己替换自己的路径**，坏了之后用户不会收到任何报错，
 * 只会永远停在旧版本，还以为"已经是最新"。而且它涉及三件容易写错的事：
 *   · 下载必须先落盘再校验（早期漏了 write，大小和 sha256 都"通过"，磁盘上是 0 字节）
 *   · 校验不过必须删掉半成品、绝不运行
 *   · 静默安装必须带 /D=当前目录（0.3.6 起安装目录可自选，漏了就会装出第二份）
 * 这些都能用 mock 覆盖，不需要真下 100+ MB。
 */

const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const https = require('node:https');
const cp = require('node:child_process');
const crypto = require('node:crypto');

const updater = require(path.join(__dirname, '..', 'desktop', 'src', 'updater.js'));

let PASS = 0;
let FAIL = 0;
const FAILS = [];

function check(name, ok, detail = '') {
  if (ok) {
    PASS += 1;
    console.log('  \u2713 ' + name);
  } else {
    FAIL += 1;
    FAILS.push(`${name}  ->  ${String(detail).slice(0, 200)}`);
    console.log('  \u2717 ' + name + '  ->  ' + String(detail).slice(0, 200));
  }
}

/**
 * 造一个假的 https.get：按调用顺序返回预设响应。
 *
 * `abort: true` 表示"对端中途掐断连接"——只发 'aborted'，不发 'end' 也不发 'error'。
 * 这是必须单独模拟的一种断法：Node 在这种情况下不会触发 'error'。
 */
function mockHttps(responses) {
  const real = https.get;
  let i = 0;
  https.get = (url, opts, cb) => {
    const r = responses[Math.min(i, responses.length - 1)];
    i += 1;
    const handler = typeof opts === 'function' ? opts : cb;
    const req = {
      on: (ev, fn) => {
        if (ev === 'error') req._err = fn;
        if (ev === 'timeout') req._timeout = fn;
        return req;
      },
      destroy: (err) => { if (req._err && err) req._err(err); },
    };
    setImmediate(() => {
      const res = {
        statusCode: r.status || 200,
        headers: r.headers || { 'content-length': String((r.body || Buffer.alloc(0)).length) },
        pause: () => {},
        on: (ev, fn) => {
          if (ev === 'data' && r.body) setImmediate(() => fn(r.body));
          if (ev === 'end' && !r.abort) setImmediate(fn);
          if (ev === 'aborted' && r.abort) setImmediate(() => fn());
          if (ev === 'error') res._err = fn;
          return res;
        },
        resume: () => {},
      };
      handler(res);
    });
    return req;
  };
  return () => { https.get = real; };
}

function jsonRes(obj) {
  const body = Buffer.from(JSON.stringify(obj), 'utf8');
  return { status: 200, headers: { 'content-length': String(body.length) }, body };
}

async function main() {
  console.log('='.repeat(56));
  console.log('桌面外壳：更新通道');
  console.log('='.repeat(56));

  // ---------- A. 版本比较
  console.log('\n[A] 版本号比较');
  check('0.3.6 > 0.3.5', updater.compareVer('0.3.6', '0.3.5') > 0, updater.compareVer('0.3.6', '0.3.5'));
  check('0.3.6 == 0.3.6', updater.compareVer('0.3.6', '0.3.6') === 0);
  check('0.10.0 > 0.9.9（按数字比，不按字符串）', updater.compareVer('0.10.0', '0.9.9') > 0,
    updater.compareVer('0.10.0', '0.9.9'));
  check('解析不了的返回 null（不猜）', updater.compareVer('abc', '0.3.6') === null);

  // ---------- B. 下载地址补 ticket
  console.log('\n[B] 下载地址要带 ticket');
  check('无查询串时补 ?', updater.withticket('https://x/a.exe').includes('?ticket='));
  check('已有查询串时补 &', updater.withticket('https://x/a.exe?v=1').includes('&ticket='));
  check('已经带了就不重复补',
    updater.withticket('https://x/a.exe').split('ticket=').length === 2,
    updater.withticket('https://x/a.exe'));

  // ---------- C. 检查：缺校验信息必须拒绝
  console.log('\n[C] 检查更新：缺 sha256 就拒绝下载');
  let restore = mockHttps([jsonRes({ latest: '9.9.9', url: 'https://x/a.exe', size: 100 })]);
  let msg = '';
  try {
    await updater.check('0.3.6');
  } catch (e) {
    msg = e.message;
  } finally {
    restore();
  }
  check('有新版但缺 sha256 → 抛错', /sha256|url|size/.test(msg), msg);

  restore = mockHttps([jsonRes({ latest: '0.3.6', url: 'https://x/a.exe', sha256: 'a'.repeat(64), size: 10 })]);
  let info = null;
  try {
    info = await updater.check('0.3.6');
  } finally {
    restore();
  }
  check('源与当前同版本 → hasUpdate=false', info && info.hasUpdate === false, JSON.stringify(info));

  restore = mockHttps([jsonRes({ latest: '0.3.7', url: 'https://x/a.exe', sha256: 'b'.repeat(64), size: 10 })]);
  try {
    info = await updater.check('0.3.6');
  } finally {
    restore();
  }
  check('源更新 → hasUpdate=true 且带校验信息',
    info && info.hasUpdate === true && info.sha256.length === 64, JSON.stringify(info));

  // ---------- D. 下载：真的落盘、真的校验
  console.log('\n[D] 下载与校验');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ph-upd-'));
  const payload = Buffer.from('这是安装包的假内容', 'utf8');
  const goodSha = crypto.createHash('sha256').update(payload).digest('hex');

  const dest1 = path.join(tmp, 'ok.exe');
  restore = mockHttps([{ status: 200, headers: { 'content-length': String(payload.length) }, body: payload }]);
  let progress = [];
  let err1 = '';
  try {
    await updater.download('https://x/a.exe', dest1, payload.length, goodSha, (p) => progress.push(p));
  } catch (e) {
    err1 = e.message;
  } finally {
    restore();
  }
  check('校验通过时下载成功', !err1 && fs.existsSync(dest1), err1);
  check('**文件真的写进去了**（不是 0 字节）',
    fs.existsSync(dest1) && fs.readFileSync(dest1).equals(payload),
    fs.existsSync(dest1) ? fs.statSync(dest1).size + 'B' : '文件不存在');
  check('有进度回调', progress.length > 0, progress.length);

  const dest2 = path.join(tmp, 'bad-sha.exe');
  restore = mockHttps([{ status: 200, headers: { 'content-length': String(payload.length) }, body: payload }]);
  let err2 = '';
  try {
    await updater.download('https://x/a.exe', dest2, payload.length, 'c'.repeat(64));
  } catch (e) {
    err2 = e.message;
  } finally {
    restore();
  }
  check('sha256 不符 → 抛错', /sha256/.test(err2), err2);
  check('sha256 不符 → 不留文件', !fs.existsSync(dest2) && !fs.existsSync(dest2 + '.part'),
    fs.readdirSync(tmp).join(','));

  const dest3 = path.join(tmp, 'bad-size.exe');
  restore = mockHttps([{ status: 200, headers: { 'content-length': String(payload.length) }, body: payload }]);
  let err3 = '';
  try {
    await updater.download('https://x/a.exe', dest3, payload.length + 7, goodSha);
  } catch (e) {
    err3 = e.message;
  } finally {
    restore();
  }
  check('大小不符 → 抛错', /大小/.test(err3), err3);
  check('大小不符 → 不留文件', !fs.existsSync(dest3), fs.readdirSync(tmp).join(','));

  // 落盘写不进去（磁盘满 / 无权限 / 目录没了）：必须报"写文件失败"，
  // 不能让它冒成未捕获异常、再被 out.end 兜成一句误导的"大小不符"。
  const dest4 = path.join(tmp, 'no-such-dir', 'x.exe');
  restore = mockHttps([{ status: 200, headers: { 'content-length': String(payload.length) }, body: payload }]);
  let err4 = '';
  const stray = [];
  const onStray = (e) => stray.push(e.message);
  process.on('uncaughtException', onStray);
  try {
    await updater.download('https://x/a.exe', dest4, payload.length, goodSha);
  } catch (e) {
    err4 = e.message;
  } finally {
    restore();
    process.removeListener('uncaughtException', onStray);
  }
  check('落盘失败 → 报"写文件失败"（不是"大小不符"）', /写文件失败/.test(err4), err4);
  check('落盘失败 → 不冒未捕获异常', stray.length === 0, stray.join(' / '));

  // 对端中途掐断：只发 aborted，不发 end/error。不接这个事件就是永久悬挂。
  const dest5 = path.join(tmp, 'aborted.exe');
  restore = mockHttps([{
    status: 200,
    headers: { 'content-length': String(payload.length) },
    body: payload.subarray(0, 100),
    abort: true,
  }]);
  let err5 = '';
  let hung = false;
  try {
    await Promise.race([
      updater.download('https://x/a.exe', dest5, payload.length, goodSha),
      // 这个定时器**不能 unref**：unref 之后事件循环空了，Node 会直接退出，
      // 后面的断言一条都不执行，测试反而显示通过——第一版变异测试就这么
      // 放过了一个真漏洞（去掉 aborted 监听，测试照样绿）。
      new Promise((_, rej) => setTimeout(() => { hung = true; rej(new Error('悬挂')); }, 2500)),
    ]);
  } catch (e) {
    err5 = e.message;
  } finally {
    restore();
  }
  check('连接中途断开 → 报"被中断"而不是永久悬挂', !hung && /中断/.test(err5), err5);  check('连接中途断开 → 删掉 .part', !fs.existsSync(dest5 + '.part'), fs.readdirSync(tmp).join(','));

  // ---------- E. 交给安装包：必须带 /D=当前目录
  console.log('\n[E] 安装：装到哪儿要跟着现在这份');
  const { EventEmitter } = require('node:events');
  const realSpawn = cp.spawn;
  let captured = null;
  cp.spawn = (file, args, opts) => {
    const em = new EventEmitter();
    em.unref = () => {};
    captured = { file, args, opts, em };
    return em;
  };
  try {
    updater.runInstaller('C:\\tmp\\setup.exe', './');
    check('传了安装路径时带 /D', captured.args.some((a) => a === '/D=./'),
      JSON.stringify(captured.args));
    check('/D 是最后一个参数（NSIS 要求）',
      captured.args[captured.args.length - 1].startsWith('/D='), JSON.stringify(captured.args));
    check('路径不加引号（NSIS 会把引号当路径的一部分）',
      captured.args[captured.args.length - 1] === '/D=./',
      captured.args[captured.args.length - 1]);
    check('静默安装带 /S', captured.args.includes('/S'), JSON.stringify(captured.args));
    // 0.3.7 的坑：向导式安装包在 /S 下，"装完自动运行"只看 --force-run，
    // 漏了它就是"装完了桌面毫无动静"，用户以为更新失败。
    check('带 --force-run（否则静默装完不启动）',
      captured.args.includes('--force-run'), JSON.stringify(captured.args));
    check('--force-run 排在 /D 之前（/D 必须收尾）',
      captured.args.indexOf('--force-run') < captured.args.findIndex((a) => a.startsWith('/D=')),
      JSON.stringify(captured.args));
    check('分离进程（自己退出后安装继续）', captured.opts && captured.opts.detached === true,
      JSON.stringify(captured.opts));

    // 只断言 args 数组里的字符串是不够的：Node 在 Windows 上会给含空白的参数
    // **再加一层引号**。实测（读被启动进程的 GetCommandLineW）：
    //   /D=C:\Program Files\x  →  "/D=C:\Program Files\x"
    // NSIS 会把这对引号当成路径的一部分，装到怪目录或直接失败。
    // windowsVerbatimArguments 让它原样拼接，这才是 NSIS 要的命令行。
    check('windowsVerbatimArguments=true（否则含空格路径会被加引号）',
      captured.opts && captured.opts.windowsVerbatimArguments === true,
      JSON.stringify(captured.opts));

    updater.runInstaller('C:\\tmp\\setup.exe');
    check('没传路径时不带 /D（不瞎猜目录）',
      !captured.args.some((a) => a.startsWith('/D=')), JSON.stringify(captured.args));
    check('没传路径时也不丢 --force-run',
      captured.args.includes('--force-run'), JSON.stringify(captured.args));

    // 安装器起不来（被杀软拦、文件被删）必须能被上层知道，
    // 否则应用已经退出、后端也停了，用户面前什么都不剩。
    let okEvent = 'unset';
    updater.runInstaller('C:\\tmp\\setup.exe', './', (e) => { okEvent = e; });
    captured.em.emit('spawn');
    check('安装器起来了 → 回调 null', okEvent === null, String(okEvent));

    let errEvent = 'unset';
    updater.runInstaller('C:\\tmp\\setup.exe', './', (e) => { errEvent = e; });
    captured.em.emit('error', new Error('EPERM: 被安全软件拦截'));
    check('安装器起不来 → 回调 Error（上层据此不退出）',
      errEvent instanceof Error && /EPERM/.test(errEvent.message), String(errEvent));
  } finally {
    cp.spawn = realSpawn;
  }

  // ---------- G. 已下载的包要不要重下
  console.log('\n[G] 重复下载：对得上就别再下一次 116 MB');
  const cached = path.join(tmp, 'cached.exe');
  fs.writeFileSync(cached, payload);
  check('大小与 sha256 都对 → 复用',
    updater.verifyFile(cached, payload.length, goodSha) === true);
  check('sha256 不对 → 不复用',
    updater.verifyFile(cached, payload.length, 'd'.repeat(64)) === false);
  check('大小不对 → 不复用',
    updater.verifyFile(cached, payload.length + 1, goodSha) === false);
  check('文件不存在 → 不复用（不抛错）',
    updater.verifyFile(path.join(tmp, 'nope.exe'), 10, goodSha) === false);

  // ---------- F. 更新源本身
  console.log('\n[F] 更新源配置');
  check('FEED_URL 带 ticket（匿名访问是 403）', updater.FEED_URL.includes('ticket='),
    updater.FEED_URL);
  check('FEED_URL 指向桌面版清单，不是内核清单',
    updater.FEED_URL.includes('desktop-version.json'), updater.FEED_URL);

  // ---------- H. 失败原因要能看懂
  console.log('\n[H] 检查失败时给的是人话，不是 errno');
  {
    const dns = updater.friendlyNetError(new Error('getaddrinfo ENOTFOUND persona-hub.example'));
    check('域名解析失败 → 说"没有联网/被拦"', /域名解析|联网/.test(dns), dns);

    const to = updater.friendlyNetError(new Error('连接更新源超时'));
    check('超时 → 说"连接超时"', /超时/.test(to), to);

    const f403 = updater.friendlyNetError(new Error('更新源返回 HTTP 403（分享站匿名访问是 403，需要 ticket）'));
    check('403 → 提示分享凭证失效', /403|凭证/.test(f403), f403);

    const f500 = updater.friendlyNetError(new Error('更新源返回 HTTP 502'));
    check('5xx → 提示过一会儿再试', /过一会儿|暂时/.test(f500), f500);

    const weird = updater.friendlyNetError(new Error('某个没见过的问题'));
    check('没见过的错误 → 原样透出，不吞', /没见过/.test(weird), weird);

    check('原始信息保留（排查要用）',
      /原始信息/.test(dns) && /原始信息/.test(f403), dns);
  }

  // ---------- G. temp 里的旧安装包该删哪些
  console.log('\n[G] 过期安装包清理（temp 会按版本数线性堆积，每个 116 MB）');
  {
    const names = [
      'persona-desktop-0.3.7-setup.exe',
      'persona-desktop-0.3.9-setup.exe',
      'persona-desktop-0.3.12-setup.exe',
      'persona-desktop-0.3.13-setup.exe',
      'persona-desktop-0.3.14-setup.exe.part',
      'unrelated.txt',
    ];
    const stale = updater.staleInstallers(names, '0.3.12');
    check('比当前版本旧的都删', stale.includes('persona-desktop-0.3.7-setup.exe')
      && stale.includes('persona-desktop-0.3.9-setup.exe'), JSON.stringify(stale));
    check('同版本保留（可能是「稍后安装」留的那份）',
      !stale.includes('persona-desktop-0.3.12-setup.exe'), JSON.stringify(stale));
    check('比当前版本新的保留（刚下完还没装）',
      !stale.includes('persona-desktop-0.3.13-setup.exe'), JSON.stringify(stale));
    check('半成品 .part 一律删', stale.includes('persona-desktop-0.3.14-setup.exe.part'),
      JSON.stringify(stale));
    check('不认识的文件不碰', !stale.includes('unrelated.txt'), JSON.stringify(stale));
    check('版本读不出来时一个都不删（判据不成立宁可不删）',
      updater.staleInstallers(names, 'latest').length === 0,
      JSON.stringify(updater.staleInstallers(names, 'latest')));
    check('空目录不报错', updater.staleInstallers([], '0.3.12').length === 0);
  }

  fs.rmSync(tmp, { recursive: true, force: true });

  console.log('\n' + '='.repeat(56));
  console.log(`通过 ${PASS} 项，失败 ${FAIL} 项`);
  if (FAILS.length) {
    console.log('\n失败项：');
    FAILS.forEach((f) => console.log('  - ' + f));
    process.exit(1);
  }
}

main().catch((e) => {
  console.error('测试自身出错：', e);
  process.exit(2);
});
