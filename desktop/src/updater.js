/**
 * 在线更新：检查 → 下载 → 校验 → 交给安装包。
 *
 * 为什么不用 electron-updater：
 * 分享站对匿名请求返回 403，必须带 `?ticket=...`，而 electron-updater 的
 * generic provider 只会拼 `url + 文件名`，加不了查询参数。所以这里自己做，
 * 契约只有三样：一个 JSON、一个 exe、一个 sha256。
 *
 * 三条原则：
 *   1. 只信 sha256。下载完先算哈希，对不上就删文件、报错，绝不运行。
 *   2. 不偷偷装。检查是后台的，下载和安装都要用户点。
 *   3. 失败可解释。每种失败给一句话说明，不要"更新失败"四个字了事。
 */
const fs = require('node:fs');
const path = require('node:path');
const https = require('node:https');
const crypto = require('node:crypto');
// 整模块引入而不是解构：解构会把 spawn 的引用当场固定下来，
// 测试就没法替换它来断言参数（/D 传没传、顺序对不对）。
const cp = require('node:child_process');

const APP_ID = 'persona-desktop';

/** 更新源。指向分享页本站（2026-11-07 回迁）：腾讯云云通道已废弃，不再有 /persona/ 路径前缀。必须带 ticket，否则 403。 */
const FEED_URL = 'https://updates.example.com';
const ticket = '<REDACTED>';

function parseVer(v) {
  const m = String(v || '').trim().match(/^(\d+)\.(\d+)\.(\d+)/);
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

/** a 比 b 新返回正数，相同返回 0，解析不了返回 null（调用方据此报错，不猜）。 */
function compareVer(a, b) {
  const x = parseVer(a);
  const y = parseVer(b);
  if (!x || !y) return null;
  for (let i = 0; i < 3; i += 1) {
    if (x[i] !== y[i]) return x[i] - y[i];
  }
  return 0;
}

/**
 * 把 ticket 拼到下载地址上。
 *
 * 分享站对匿名请求返回 403，只有带 ?ticket= 才放行。
 * 更新源里的 url 故意不写 ticket（一处配置改，全链路生效），
 * 由这里统一补——实测漏这一步的表现就是"检查能过、下载 403"。
 */
function withticket(url) {
  if (!url) return url;
  if (url.includes('ticket=')) return url;
  return url + (url.includes('?') ? '&' : '?') + 'ticket=' + ticket;
}

function getJson(url, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { timeout: timeoutMs }, (res) => {
      if (res.statusCode !== 200) {
        res.resume();
        reject(new Error(`更新源返回 HTTP ${res.statusCode}（分享站匿名访问是 403，需要 ticket）`));
        return;
      }
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
        } catch {
          reject(new Error('更新源不是合法 JSON'));
        }
      });
    });
    req.on('timeout', () => req.destroy(new Error('连接更新源超时')));
    req.on('error', (e) => reject(new Error(`连不上更新源：${e.message}`)));
  });
}

/** 检查有没有新版本。current 由调用方传入（通常是 app.getVersion()）。 */
async function check(current) {
  const data = await getJson(FEED_URL);
  const latest = String(data.latest || '').trim();
  if (!latest) throw new Error('更新源缺少 latest 字段');
  const cmp = compareVer(latest, current);
  if (cmp === null) throw new Error(`版本号无法比较：latest=${latest} current=${current}`);

  const url = String(data.url || '').trim();
  const sha256 = String(data.sha256 || '').trim().toLowerCase();
  const size = Number(data.size || 0);
  // 有新版本但缺校验信息 → 拒绝，不下载没有哈希的安装包
  if (cmp > 0 && (!url || !sha256 || !size)) {
    throw new Error('更新源缺少 url / sha256 / size，拒绝下载无法校验的安装包');
  }
  return {
    current,
    latest,
    hasUpdate: cmp > 0,
    url,
    sha256,
    size,
    published: String(data.published || ''),
    notes: Array.isArray(data.notes) ? data.notes.map(String) : [],
  };
}

/**
 * 下载并按 sha256 校验。
 *
 * 异常路径比成功路径更需要照顾 —— 这里的每一条都是实测出来的：
 *
 *   · 写流要挂 error。createWriteStream 打不开目标（目录没了 / 无权限 / 磁盘满）时，
 *     错误只发到流的 'error' 上；没人听就冒成 uncaughtException，而 out.end() 的回调
 *     仍然会被调用，于是**用户看到的是「大小不符」**——真正的原因（ENOENT / EACCES）
 *     被彻底盖住。实测就是这样：reject 消息写着"大小不符：期望 4096，实际 1000"。
 *   · 要挂 aborted。对端中途掐断连接时 Node 发的是 'aborted'，既不是 'error' 也不是
 *     'end'；不接这个事件，Promise 永远不 resolve —— 界面停在"下载 37%"直到用户重启。
 *   · 要管背压。write 返回 false 表示内核缓冲满了，继续灌数据就是拿内存换速度。
 *     116 MB 在慢盘上足够堆出一次可感知的卡顿。
 */
function download(url, dest, expectSize, expectSha, onProgress) {
  return new Promise((resolve, reject) => {
    const tmp = `${dest}.part`;
    const out = fs.createWriteStream(tmp);
    const hash = crypto.createHash('sha256');
    let got = 0;
    let settled = false;

    const fail = (err) => {
      if (settled) return;
      settled = true;
      try {
        out.destroy();
      } catch {
        /* 已经关了 */
      }
      try {
        fs.unlinkSync(tmp);
      } catch {
        /* 删不掉就算了，下次覆盖 */
      }
      reject(err);
    };

    const done = () => {
      if (settled) return;
      settled = true;
      try {
        fs.renameSync(tmp, dest);
        resolve(dest);
      } catch (e) {
        try {
          fs.unlinkSync(tmp);
        } catch {
          /* 忽略 */
        }
        reject(new Error(`写文件失败（目标目录不可用）：${e.message}`));
      }
    };

    // 写不进去（磁盘满、无权限、路径不存在）必须在这里停住，别让它冒充"大小不符"
    out.on('error', (e) => fail(new Error(`写文件失败（${tmp}）：${e.message}`)));

    const fullUrl = withticket(url);
    const req = https.get(fullUrl, { timeout: 30000 }, (res) => {
      if (res.statusCode !== 200) {
        res.resume();
        fail(new Error(`下载返回 HTTP ${res.statusCode}`));
        return;
      }
      const total = Number(res.headers['content-length'] || expectSize || 0);
      res.on('data', (c) => {
        // 这一行写漏过：只累加计数和哈希，没把数据落到文件里，
        // 结果"大小与 sha256 都通过"但磁盘上是个 0 字节文件。
        const ok = out.write(c);
        got += c.length;
        hash.update(c);
        if (onProgress) onProgress({ got, total });
        // 缓冲满了先停读，等 drain 再继续——否则慢盘上是纯内存堆积
        if (!ok && typeof res.pause === 'function') {
          res.pause();
          out.once('drain', () => {
            if (typeof res.resume === 'function') res.resume();
          });
        }
      });
      res.on('aborted', () => fail(new Error(`下载被中断：连接在 ${got} 字节处断开`)));
      res.on('end', () => {
        if (settled) return;
        out.end(() => {
          if (settled) return;
          if (got !== expectSize) {
            fail(new Error(`大小不符：期望 ${expectSize}，实际 ${got}`));
            return;
          }
          const actual = hash.digest('hex');
          if (actual !== expectSha) {
            fail(new Error(`sha256 不符：期望 ${expectSha.slice(0, 12)}…，实际 ${actual.slice(0, 12)}…`));
            return;
          }
          done();
        });
      });
      res.on('error', (e) => fail(new Error(`下载中断：${e.message}`)));
    });
    req.on('timeout', () => req.destroy(new Error('下载超时（30 秒无数据）')));
    req.on('error', (e) => fail(new Error(`下载失败：${e.message}`)));
  });
}

/**
 * 已经下过的包还对不对——对就不要再下一次 116 MB。
 *
 * 用户点「稍后」、或者装到一半取消了，下次点更新不该重头再来。
 * 只认大小 + sha256，不认文件名：文件可能被手工替换过。
 */
function verifyFile(file, expectSize, expectSha) {
  try {
    const st = fs.statSync(file);
    if (!st.isFile() || st.size !== expectSize) return false;
    const h = crypto.createHash('sha256');
    const fd = fs.openSync(file, 'r');
    try {
      const buf = Buffer.allocUnsafe(1 << 20);
      let n = 0;
      while ((n = fs.readSync(fd, buf, 0, buf.length, null)) > 0) h.update(buf.subarray(0, n));
    } finally {
      fs.closeSync(fd);
    }
    return h.digest('hex') === expectSha;
  } catch {
    return false;
  }
}


/**
 * 交给安装包，然后退出自己。
 *
 * **装到哪儿必须跟着"现在这份"走。** 0.3.6 起安装包改成了向导式，用户可以把程序装到
 * 任意目录（本机就装在 `./`）。这时如果升级只丢一个 `/S`，
 * NSIS 会退到默认的 `%LOCALAPPDATA%\Programs\persona-desktop` —— 结果是装出第二份，
 * 桌面图标还指着旧的那份，用户会在"更新完了怎么还是旧版本"里反复打转。
 *
 * NSIS 的约定：`/D=` 必须是**最后一个**参数，且路径**不能加引号**（含空格也不加，
 * 它会把整行剩余部分当成路径）。所以这里不做引号处理，顺序也固定。
 *
 * **`--force-run` 不能省。** 我们是 oneClick:false 的向导式安装包，
 * electron-builder 的 installSection.nsh 里写着：
 *
 *     # for assisted installer run only if silent, because assisted installer
 *     # has run after finish option
 *     ${if} ${isForceRun}
 *     ${andIf} ${Silent}
 *       !insertmacro doStartApp
 *     ${endIf}
 *
 * 也就是说：**静默安装（`/S`）时，"装完自动运行"这个选项根本不参与判断，
 * 只看有没有 `--force-run`。** 漏掉它的实际表现是装完了桌面毫无动静，
 * 用户以为更新失败，或以为装到了别的地方——0.3.7 的"更新后崩了/没装上"
 * 就是这么来的。
 *
 * **`windowsVerbatimArguments` 也不能省。** NSIS 规定 `/D=` 的路径不能带引号，
 * 但 Node 在 Windows 上会给含空白的参数自动加引号。实测（读被启动进程的
 * GetCommandLineW）：
 *
 *     /D=C:\Program Files\persona-desktop   →  "/D=C:\Program Files\persona-desktop"
 *
 * 引号进了命令行，NSIS 就把它当路径的一部分——装在 `C:\Program Files` 的用户
 * 会更新失败或装到怪目录。本机装的是 `./`（无空格），
 * 所以这个坑一直没暴露；换台机器或换个目录就踩。
 *
 * onEvent(err)：进程真正创建起来回调 null，起不来回调 Error。
 * 传它是为了让调用方**先确认安装器活了再退出自己**——否则被杀软拦掉时，
 * 应用已经关了、后端也停了，用户面前什么都没有。
 */
function runInstaller(file, installDir, onEvent) {
  const args = ['/S', '--force-run'];
  if (installDir) args.push('/D=' + installDir);
  const child = cp.spawn(file, args, {
    detached: true,
    stdio: 'ignore',
    // 见上面的实测：不加这句，含空格的路径会被 Node 加上引号
    windowsVerbatimArguments: true,
  });
  if (typeof onEvent === 'function') {
    child.on('spawn', () => onEvent(null));
    child.on('error', (e) => onEvent(e));
  }
  child.unref();
  return child;
}

/**
 * 挑出 temp 里该删的旧安装包。
 *
 * 为什么需要：清理原来只写在「点安装」那一刻。用户如果一直没点更新，
 * 每次下过又放弃的包就永久留在 %TEMP%\persona-desktop-update 里，
 * 每个 116 MB。实测这台机器上堆了三个（0.3.7 / 0.3.9 / 0.3.10），341 MB，
 * 而且系统自带的磁盘清理不一定认领这个子目录。
 *
 * 判据：比**当前运行的版本**旧的包一定没用了（装上也是降级），删；
 * 同版本或更新的保留——那是「稍后安装」留下的，或者刚下完还没装。
 * 半成品 .part 一律删，留着也没法用。
 */
const INSTALLER_RE = /^persona-desktop-(\d+\.\d+\.\d+)-setup\.exe(\.part)?$/;

function staleInstallers(names, currentVersion) {
  // 当前版本读不出来就一个都不删：删文件这种事，判据不成立时宁可不动。
  if (!parseVer(currentVersion)) return [];
  const out = [];
  for (const name of names) {
    const m = INSTALLER_RE.exec(name);
    if (!m) continue;
    if (m[2]) {
      out.push(name);          // .part 半成品
      continue;
    }
    const cmp = compareVer(m[1], currentVersion);
    if (cmp !== null && cmp < 0) out.push(name);
  }
  return out;
}

/**
 * 把网络错误翻成人话。
 *
 * 为什么要这一步：检查更新失败时，界面上只有按钮变成"更新失败"四个字，
 * 真正的原因（域名解析失败 / 403 / 超时）只进了 console 和 shell.log——
 * 用户既看不到，也不知道该干什么。放在这里而不是 main.js，是因为它是纯函数，
 * 可以脱离 Electron 单测。
 */
function friendlyNetError(err) {
  const raw = String((err && err.message) || err);
  if (/ENOTFOUND|EAI_AGAIN|getaddrinfo/.test(raw)) {
    return `连不上更新服务器：域名解析失败。多半是没联网，或者网络把这台机器拦了。\n原始信息：${raw}`;
  }
  if (/ECONNREFUSED|ECONNRESET|ETIMEDOUT|超时/.test(raw)) {
    return `连不上更新服务器：连接超时或被中途断开。\n原始信息：${raw}`;
  }
  if (/HTTP 403/.test(raw)) {
    return `更新服务器拒绝了这个请求（403）。通常是分享链接的凭证失效了，需要重新发布一次站点。\n原始信息：${raw}`;
  }
  if (/HTTP 5\d\d/.test(raw)) {
    return `更新服务器暂时出错。过一会儿再点一次就好。\n原始信息：${raw}`;
  }
  if (/不是合法 JSON|缺少/.test(raw)) {
    return `更新源的内容不对，客户端没法判断版本。\n原始信息：${raw}`;
  }
  return raw;
}

module.exports = { APP_ID, FEED_URL, ticket, parseVer, compareVer, check, download, verifyFile,
  runInstaller, withticket, friendlyNetError, staleInstallers };

