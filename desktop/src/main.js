/**
 * 人格中枢 · 桌面版 —— 主进程
 *
 * 为什么要有这个项目：原来的窗口是借 Chrome/Edge 的 `--app` 模式开的，
 * 用户机器上没装浏览器就打不开。这里换成 Electron 自带 Chromium，
 * 装完就能用，不需要任何外部浏览器。
 *
 * 结构：本进程只做三件事——起后端、开窗口、退出时收干净。
 * 界面本身一行没重写，仍然是后端那套 HTML/JS（只是换了个渲染引擎）。
 *
 * 与旧版共存的三条隔离（用户明确要求"别影响现在这个程序的运行"）：
 *   1. 数据目录独立：后端通过 PERSONA_HUB_HOME 指到 %APPDATA%\人格中枢桌面版\data
 *   2. 端口独立：每次启动现挑一个空闲端口，不占用旧版默认的 8799
 *   3. 进程独立：后端以 --no-gui 启动，**不会**触发旧版的单实例守卫去复用别的实例
 */
'use strict';

const { app, BrowserWindow, shell, dialog, Tray, Menu, nativeImage, ipcMain } = require('electron');
const updater = require('./updater');
const bounds = require('./bounds');
const { spawn, execFileSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const net = require('node:net');
const http = require('node:http');

// 从另一个 Electron 应用（比如某些 IDE、终端封装）里启动时，环境里常带着
// ELECTRON_RUN_AS_NODE=1，这会让 electron.exe 退化成一个纯 Node 进程：
// require('electron') 返回的是路径字符串，app 是 undefined。
// 报错信息必须点明这一点，否则看到的是 "Cannot read properties of undefined"，
// 会去查代码而不是查环境——本机第一次跑就踩了这个坑。
if (!app || typeof app.requestSingleInstanceLock !== 'function') {
  console.error(
    '[致命] 当前进程不是 Electron 主进程。\n' +
      '原因通常是环境变量 ELECTRON_RUN_AS_NODE=1（由父进程注入）。\n' +
      '解法：启动前清掉它 —— set ELECTRON_RUN_AS_NODE= && npm start'
  );
  process.exit(1);
}

const APP_TITLE = '人格中枢 · 桌面版';
const PING_TIMEOUT_MS = 30_000;

/** 后端可执行文件位置：打包后在 resources/backend，开发时同理（先跑 fetch-backend） */
function backendPath() {
  const override = process.env.PERSONA_BACKEND_EXE;
  if (override && fs.existsSync(override)) return override;

  const candidates = [
    path.join(process.resourcesPath || '', 'backend', 'persona-hub.exe'),
    path.join(__dirname, '..', 'resources', 'backend', 'persona-hub.exe'),
  ];
  return candidates.find((p) => p && fs.existsSync(p)) || null;
}

/** 数据目录：放在 userData 下，与旧版项目目录完全分开 */
/**
 * 静态资源定位：打包后在 resources/ 根目录，开发时在 build/。
 *
 * 踩过的坑：图标原先按 __dirname/../build 找，开发时能跑，
 * 打包后那个路径在 asar 里根本不存在，托盘就变成空图标。
 * build/ 属于 buildResources，不会被打进 asar，必须显式写进 extraResources。
 */
function assetPath(name) {
  const packed = path.join(process.resourcesPath || '', name);
  if (process.resourcesPath && fs.existsSync(packed)) return packed;
  return path.join(__dirname, '..', 'build', name);
}

/**
 * 首次启动时，把旧版（persona-hub）的人格库复制过来。
 *
 * 为什么需要：桌面版用独立数据目录（%APPDATA%\人格中枢桌面版\data），
 * 这是"别影响旧程序"的代价——代价就是旧版里那些人格不会自动出现。
 * 用户最在意的资产恰恰是"已经调好的人格"，所以这里做一次性搬迁。
 *
 * 三条约束：
 *   1. 只搬"人格库"，不搬 vault/快照（那些是旧版的运行时状态，搬过来反而混乱）
 *   2. 只在本机目标为空时搬，绝不覆盖用户已在桌面版里新建的内容
 *   3. 源目录只读，不做任何修改
 */
function migratePersonasOnce() {
  const dst = path.join(dataHome(), 'personas');
  try {
    if (fs.existsSync(dst) && fs.readdirSync(dst).length > 0) return;
  } catch {
    return;
  }

  // 旧版数据目录：只从环境变量取。
  // 曾经这里硬编码了一个开发机路径（./
  // 后果是"清空桌面数据想重新开始"时，人格又被从源码目录悄悄拉回来一遍。
  // 交付给别人的包里也不该出现我这台机器的路径。
  const candidates = [process.env.PERSONA_LEGACY_DATA].filter(Boolean);

  const src = candidates
    .map((p) => path.join(p, 'personas'))
    .find((p) => {
      try {
        return fs.existsSync(p) && fs.readdirSync(p).length > 0;
      } catch {
        return false;
      }
    });
  if (!src) return;

  try {
    fs.mkdirSync(dst, { recursive: true });
    fs.cpSync(src, dst, { recursive: true });
    const n = fs.readdirSync(dst).length;
    console.log(`[迁移] 已从旧版复制 ${n} 个人格：${src} -> ${dst}`);
  } catch (err) {
    // 迁移失败不影响启动：空库也能用，只是要手动新建
    console.error('[迁移] 复制人格库失败（不影响启动）：', err && err.message);
  }
}

/**
 * 在线更新：检查 / 下载 / 交给安装包。
 *
 * 三条硬规矩：
 *   1. 下载完必须核对 sha256 + 大小，对不上就不运行。
 *   2. 不静默安装。检查可以自动，但"下载"和"装"都要用户点确认。
 *   3. 失败一定说清是哪一步：连不上源 / 源格式不对 / 下载中断 / 哈希不符。
 */
/**
 * 页面要在本机记住一点小事（比如"使用指引看过了"），但不能给它文件系统。
 *
 * 为什么不让页面自己用 localStorage：页面来自 http://127.0.0.1:<端口>，
 * 而端口是每次启动向系统要的随机空闲端口，**端口不同就是不同 origin**，
 * localStorage 各存各的。实测用户点了 8 次"跳过"仍然每次弹 —— leveldb 里
 * 躺着 8 个不同端口的 origin，每个都写了一份已读标记，谁也不认谁。
 *
 * 所以标记放这儿，按 userData 存，与端口无关。只收一个键值对，别的什么都不给。
 */
function flagsFile() {
  return path.join(app.getPath('userData'), 'ui-flags.json');
}

function readFlags() {
  try {
    const o = JSON.parse(fs.readFileSync(flagsFile(), 'utf8'));
    return o && typeof o === 'object' ? o : {};
  } catch {
    return {};
  }
}

function registerFlagIpc() {
  ipcMain.handle('ui:flag:get', (_e, key) => {
    if (typeof key !== 'string' || !key || key.length > 64) return null;
    const v = readFlags()[key];
    return v === undefined ? null : v;
  });

  ipcMain.handle('ui:flag:set', (_e, key, value) => {
    if (typeof key !== 'string' || !key || key.length > 64) return false;
    const all = readFlags();
    all[key] = typeof value === 'string' ? value.slice(0, 512) : String(value).slice(0, 512);
    try {
      fs.mkdirSync(path.dirname(flagsFile()), { recursive: true });
      fs.writeFileSync(flagsFile(), JSON.stringify(all));
      return true;
    } catch (err) {
      console.error('[界面] 写标记失败：', err && err.message);
      return false;
    }
  });
}

function registerUpdateIpc() {
  ipcMain.handle('update:check', async () => {
    try {
      const info = await updater.check(app.getVersion());
      console.log(
        `[更新] 检查：当前 ${info.current}，源上 ${info.latest}，` +
          (info.hasUpdate ? '有新版' : '已是最新')
      );
      return info;
    } catch (err) {
      const msg = updater.friendlyNetError(err);
      console.error('[更新] 检查失败：', msg);
      // 弹一次比只改按钮文字有用得多：用户至少知道是没网，还是源坏了
      dialog.showErrorBox('检查更新失败', `${msg}\n\n当前版本：v${app.getVersion()}`);
      throw err;   // 页面那边也要进 catch，把按钮状态收回去
    }
  });

  // 防止两次安装叠在一起：spawn 安装器后到进程退出之间有个时间窗，
  // 这期间再点一次会拉起第二个安装器，两个 NSIS 同时改同一个目录。
  let installing = false;

  ipcMain.handle('update:install', async () => {
    if (installing) return { ok: false, message: '上一次更新还在处理中' };
    installing = true;
    try {
      const info = await updater.check(app.getVersion());
      if (!info.hasUpdate) return { ok: false, message: '已经是最新版' };

      const dir = path.join(app.getPath('temp'), 'persona-desktop-update');
      fs.mkdirSync(dir, { recursive: true });
      const file = path.join(dir, `persona-desktop-${info.latest}-setup.exe`);

      // 清掉以前版本的包。不清的话 temp 里会随版本数线性堆积，
      // 每个 116 MB —— 用户不会去清，系统也未必及时清。
      try {
        for (const name of fs.readdirSync(dir)) {
          if (name !== path.basename(file) && /^persona-desktop-.*-setup\.exe(\.part)?$/.test(name)) {
            fs.unlinkSync(path.join(dir, name));
          }
        }
      } catch {
        /* 清不掉不影响本次更新 */
      }

      // 已经下过同一版本、且大小与 sha256 都对得上 → 不再下一次 116 MB。
      // 用户点过「稍后」、或上次下载完没装的情况都走这里。
      const already = updater.verifyFile(file, info.size, info.sha256);
      const reuseHint = already ? '\n\n（这个版本上次已经下载并校验过，这次不再重复下载。）' : '';

      const mb = (info.size / 1048576).toFixed(1);
      const notes = (info.notes || []).slice(0, 8).map((x) => `· ${x}`).join(String.fromCharCode(10));
      const ask = await dialog.showMessageBox(win, {
        type: 'question',
        buttons: [already ? '直接安装' : '下载并安装', '取消'],
        defaultId: 0,
        cancelId: 1,
        title: '发现新版本',
        message: `v${info.current} → v${info.latest}（${mb} MB）`,
        detail: `${notes}

下载完成后会核对 sha256，再退出应用并启动安装程序。数据目录不受影响。${reuseHint}`,
      });
      if (ask.response !== 0) return { ok: false, message: '已取消' };

      if (!already) {
        console.log(`[更新] 开始下载 ${info.url} -> ${file}`);
        // 进度节流：不上限的话 116 MB 会推几千次 IPC，每次都要改一次文字，
        // 下载期间界面反而发涩。120ms 一跳，肉眼比 60fps 还密。
        let lastSent = 0;
        await updater.download(info.url, file, info.size, info.sha256, (p) => {
          const now = Date.now();
          const finished = p.total && p.got >= p.total;
          if (now - lastSent < 120 && !finished) return;
          lastSent = now;
          if (win && !win.isDestroyed()) win.webContents.send('update:progress', p);
        });
        console.log('[更新] 下载完成，sha256 与大小均核对通过');
      } else {
        console.log('[更新] 命中已下载的包，跳过下载');
      }

      const again = await dialog.showMessageBox(win, {
        type: 'info',
        buttons: ['现在安装', '稍后'],
        defaultId: 0,
        cancelId: 1,
        title: '准备安装',
        message: `v${info.latest} 已下载并校验`,
        detail: `点「现在安装」会先退出程序，再静默覆盖安装到当前目录：\n${path.dirname(app.getPath('exe'))}\n\n装完会自动打开新版。如果这个位置需要管理员权限，系统会弹一次确认——那是覆盖安装本身，不会再装一份。\n\n如果不小心点了「否」或者没装成：重新打开程序、再点一次「更新」即可，下载过的东西不会重下一次。数据目录不受影响。`,
      });
      // 用户选「稍后」不是失败：包已经留在本机，下次点更新会直接用，
      // 不会再下一次 116 MB。调用方要靠 cancelled 区分，别报「更新失败」。
      if (again.response !== 0) {
        installing = false;
        return { ok: false, cancelled: true, message: '已下载，下次点更新可以直接安装' };
      }

      // 顺序：**先启动安装器、确认它真起来了，再停后端并退出自己**。
      // 反过来（先停后端再 spawn）的问题是：杀软拦掉安装包、或系统拒绝提权时，
      // spawn 会失败，而这时后端已经停了、应用马上退出——用户面前什么都不剩。
      const installDir = path.dirname(app.getPath('exe'));
      const spawned = await new Promise((resolve) => {
        let settled = false;
        // 兜底：极少数情况下 'spawn' 事件不来（比如被安全软件代跑），
        // 等 1.5 秒还没动静就当成已启动，不让用户干等。
        const timer = setTimeout(() => {
          if (!settled) {
            settled = true;
            resolve({ ok: true, timedOut: true });
          }
        }, 1500);
        updater.runInstaller(file, installDir, (err) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          resolve(err ? { ok: false, error: err } : { ok: true });
        });
      });

      if (!spawned.ok) {
        installing = false;
        const msg = String((spawned.error && spawned.error.message) || '未知原因');
        console.error('[更新] 安装程序没能启动：', msg);
        dialog.showErrorBox(
          '安装程序没能启动',
          `原因：${msg}\n\n安装包位置：\n${file}\n\n程序仍在运行旧版本（v${info.current}），可以直接继续用。\n想再试一次就重新点「更新」。`
        );
        return { ok: false, message: '安装程序没能启动：' + msg };
      }

      // 安装器活了再停后端：后端是独立进程，安装器不认识它，
      // 它继续占着 resources\backend\persona-hub.exe 会让覆盖只成功一半。
      stopBackend();
      setTimeout(() => {
        quitting = true;
        // app.exit 而不是 app.quit：quit 要等窗口 close、before-quit、will-quit
        // 几道异步钩子，任何一处被拖住，都会让安装器等一个"已经该死了"的进程。
        app.exit(0);
      }, 400);
      return { ok: true, message: `正在安装到 ${installDir}，装完会自动打开` };
    } catch (err) {
      installing = false;
      const msg = String((err && err.message) || err);
      console.error('[更新] 失败：', msg);
      dialog.showErrorBox('更新失败', msg);
      return { ok: false, message: msg };
    }
  });
}

function dataHome() {
  const dir = path.join(app.getPath('userData'), 'data');
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

/** 上次没退干净的记录（pid + 端口）放这里，下次启动先清理 */
function stateFile() {
  return path.join(app.getPath('userData'), 'backend.json');
}

/**
 * 这个 pid 现在装的还是不是我们的后端？
 *
 * Windows 会很快复用 pid：backend.json 里记的是上次运行时的号码，等这次启动
 * 时那个号码往往已经属于别人——可能是刚起来的安装器，也可能是资源管理器。
 * 认不出来就不杀（宁可有残留，不可误杀）。
 */
function pidLooksLikeBackend(pid) {
  if (!pid || pid === process.pid) return false;
  try {
    const out = execFileSync('tasklist', ['/FI', `PID eq ${pid}`, '/FO', 'CSV', '/NH'], {
      encoding: 'utf8',
      timeout: 5000,
    });
    return /persona-hub\.exe/i.test(out);
  } catch {
    return false;
  }
}

/**
 * 清理上一次残留的后端。
 *
 * 为什么要按**端口**找，而不是只按 pid：后端是 PyInstaller 单文件，
 * 它自己是"启动器 + 工作进程"两级结构——我们 spawn 到的是启动器，
 * 真正监听端口的是它的子进程。只杀启动器，工作进程会留下来继续占着端口，
 * 于是下次启动就出现两个后端各跑各的数据目录（实测踩到，两条残留）。
 *
 * 安全前提：先 ping，确认那个端口上跑的是**本程序**才动手。
 * 端口会被系统回收再分配给别人，盲杀等于杀掉无关进程。
 */
/**
 * 开机顺手清掉 temp 里过期的安装包。
 *
 * 清理原本只写在「点安装」那一刻，用户不点就一直堆着。
 * 实测这台机器上 %TEMP%\persona-desktop-update 里躺着三个旧包，341 MB。
 * 判据交给 updater.staleInstallers（纯函数，能单测）：只删比当前版本旧的，
 * 以及 .part 半成品；同版本或更新的保留，那是「稍后安装」留着的那份。
 */
function cleanUpdateTemp() {
  try {
    const dir = path.join(app.getPath('temp'), 'persona-desktop-update');
    if (!fs.existsSync(dir)) return;
    const names = fs.readdirSync(dir);
    const stale = updater.staleInstallers(names, app.getVersion());
    let freed = 0;
    for (const name of stale) {
      const p = path.join(dir, name);
      try {
        freed += fs.statSync(p).size;
        fs.unlinkSync(p);
      } catch {
        /* 被占用就下次再来 */
      }
    }
    if (stale.length) {
      console.log(`[清理] 删掉 ${stale.length} 个过期安装包，释放 ${(freed / 1048576).toFixed(0)} MB`);
    }
  } catch (err) {
    console.error('[清理] 安装包残留清理失败：', err && err.message);
  }
}

async function killStale() {
  let st = {};
  try {
    st = JSON.parse(fs.readFileSync(stateFile(), 'utf8'));
  } catch {
    return;
  }
  const victims = new Set();

  if (st.port) {
    const info = await ping(st.port);
    if (info && info.app && info.app.startsWith('人格中枢')) {
      const n = killListenerOnPort(st.port);
      if (n) console.log(`[清理] 结束了上次残留的后端监听进程 ${n} 个（端口 ${st.port}）`);
    }
  }
  if (st.pid) victims.add(st.pid);

  for (const p of victims) {
    // 这里原来是裸的 process.kill(p) —— 盲杀。0.3.7 用户点完更新后
    // "装完一打开就崩"，最可能就是这条：安装器进程的 pid 恰好被
    // 上次记录的后端 pid 撞上了。
    if (!pidLooksLikeBackend(p)) {
      console.log(`[清理] 跳过 pid ${p}：它已经不属于本程序（pid 被复用）`);
      continue;
    }
    try {
      process.kill(p);
      console.log(`[清理] 结束了上次残留的后端进程 ${p}`);
    } catch {
      /* 已经退出 */
    }
  }
  try {
    fs.unlinkSync(stateFile());
  } catch {
    /* 忽略 */
  }
}

/**
 * 杀掉在指定端口上监听的进程（同步）。
 *
 * 为什么必须按端口杀：后端是 PyInstaller 单文件，我们 spawn 到的是启动器，
 * 真正监听端口的是它的子进程。只杀启动器，子进程会留下来继续占端口
 * ——实测正常退出后仍残留一个 persona-hub.exe。
 */
function killListenerOnPort(port) {
  if (!port) return 0;
  let killed = 0;
  try {
    const out = execFileSync('netstat', ['-ano'], { encoding: 'utf8', timeout: 5000 });
    for (const line of out.split(/\r?\n/)) {
      if (!line.includes(`:${port}`) || !/LISTENING/i.test(line)) continue;
      const cols = line.trim().split(/\s+/);
      const pid = Number(cols[cols.length - 1]);
      if (!pid || pid === process.pid) continue;
      try {
        process.kill(pid);
        killed += 1;
      } catch {
        /* 已经退出 */
      }
    }
  } catch {
    /* netstat 不可用时只能退回到 pid */
  }
  return killed;
}

function writeState(port) {
  try {
    fs.writeFileSync(
      stateFile(),
      JSON.stringify({ pid: child ? child.pid : 0, port, at: Date.now() }),
      'utf8'
    );
  } catch {
    /* 记不下来不影响主流程 */
  }
}

/** 挑一个空闲端口：让系统分配，立刻释放，再交给后端 */
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function ping(port) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: '127.0.0.1', port, path: '/api/ping', timeout: 1200 },
      (res) => {
        let body = '';
        res.on('data', (c) => (body += c));
        res.on('end', () => {
          try {
            const j = JSON.parse(body);
            resolve(j && j.ok === true ? j.data : null);
          } catch {
            resolve(null);
          }
        });
      }
    );
    req.on('error', () => resolve(null));
    req.on('timeout', () => {
      req.destroy();
      resolve(null);
    });
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 后端起来后端口可能被顶走（后端遇到占用会往后找），所以要把邻近端口也扫一遍 */
async function waitForBackend(startPort) {
  const deadline = Date.now() + PING_TIMEOUT_MS;
  while (Date.now() < deadline) {
    for (let p = startPort; p < startPort + 20; p += 1) {
      const data = await ping(p);
      if (data && data.app && data.app.startsWith('人格中枢')) {
        return { port: p, info: data };
      }
    }
    if (child && child.exitCode !== null) {
      throw new Error(`后端已退出（退出码 ${child.exitCode}）`);
    }
    await sleep(150);
  }
  throw new Error(`后端在 ${PING_TIMEOUT_MS / 1000} 秒内没有就绪`);
}

let child = null;
let win = null;
let tray = null;
let quitting = false;
let lastPort = 0;

function startBackend(port) {
  const exe = backendPath();
  if (!exe) {
    throw new Error(
      '找不到后端程序 resources/backend/persona-hub.exe。\n' +
        '开发时先执行：npm run fetch-backend'
    );
  }
  const env = {
    ...process.env,
    PERSONA_HUB_HOME: dataHome(),
  };
  child = spawn(exe, ['--no-gui', '--port', String(port)], {
    env,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  child.stdout.on('data', (b) => process.stdout.write(`[backend] ${b}`));
  child.stderr.on('data', (b) => process.stderr.write(`[backend] ${b}`));
  child.on('exit', (code) => {
    console.log(`[backend] 退出，code=${code}`);
    child = null;
  });
  return child;
}

function stopBackend() {
  const port = lastPort;
  if (child) {
    try {
      child.kill();
    } catch {
      /* 忽略 */
    }
    child = null;
  }
  // 启动器被杀不代表工作进程会跟着死，所以再按端口收一次
  const n = killListenerOnPort(port);
  if (n) console.log(`[退出] 按端口清掉了 ${n} 个后端监听进程`);
  lastPort = 0;
  try {
    fs.unlinkSync(stateFile());
  } catch {
    /* 忽略 */
  }
}

function boundsFile() {
  return path.join(app.getPath('userData'), 'window.json');
}

function loadBounds() {
  try {
    const b = JSON.parse(fs.readFileSync(boundsFile(), 'utf8'));
    if (b && Number.isFinite(b.width) && Number.isFinite(b.height)) return fitToScreen(b);
  } catch {
    /* 第一次启动没有这个文件，正常 */
  }
  return fitToScreen({ width: 1280, height: 880 });
}

/**
 * 把记住的窗口位置拉回"当前真实存在的屏幕"。
 *
 * 判断逻辑在 `bounds.js`（纯函数，能单测）；这里只负责把屏幕信息喂进去。
 * 放这儿的原因：只有 app ready 之后才能 require('electron').screen。
 */
function fitToScreen(b) {
  try {
    const { screen } = require('electron');
    const displays = screen.getAllDisplays();
    const primary = screen.getPrimaryDisplay().workArea;
    const out = bounds.fit(b, displays, primary);
    if (Number.isFinite(b.x) && !Number.isFinite(out.x)) {
      console.log(`[窗口] 上次的位置 (${b.x},${b.y}) 已经不在任何屏幕上，改回居中`);
    }
    if (out.width !== b.width || out.height !== b.height) {
      console.log(`[窗口] 尺寸 ${b.width}×${b.height} 超出当前屏幕，调整为 ${out.width}×${out.height}`);
    }
    return out;
  } catch (err) {
    console.error('[窗口] 屏幕信息读不到：', err && err.message);
    return { width: b.width, height: b.height };
  }
}

function saveBounds() {
  if (!win || win.isDestroyed()) return;
  const b = win.getBounds();
  try {
    fs.writeFileSync(boundsFile(), JSON.stringify(b), 'utf8');
  } catch {
    /* 记不下来不影响使用 */
  }
}

/** 往页面里塞标题栏与外壳样式。
 *
 * 为什么用注入而不是改后端那套 HTML：那套界面旧版（浏览器壳）也在用，
 * 直接改会连带改变旧版的样子。注入只作用于本外壳的窗口。
 */
async function injectShell(wc) {
  const cssPath = path.join(__dirname, 'shell.css');
  try {
    await wc.insertCSS(fs.readFileSync(cssPath, 'utf8'));
  } catch (err) {
    console.error('[外壳] 样式注入失败：', err && err.message);
  }
  // 外壳自己的版本号：Electron 的 app.getVersion() 读的是 package.json 的 version。
  // 必须显示出来——只显示后端版本时，用户分不清手上这份外壳是哪一版。
  const shellVer = app.getVersion();
  const js = `
    (() => {
      if (document.getElementById('ph-titlebar')) return;
      const SHELL_VER = ${JSON.stringify(shellVer)};

      // 标题栏：应用名 + 右上角功能图标 + 版本号
      const bar = document.createElement('div');
      bar.id = 'ph-titlebar';
      bar.innerHTML =
        '<span class="tb-dot"></span>' +
        '<span class="tb-title">人格中枢</span>' +
        '<span class="tb-sub">桌面版</span>' +
        '<span class="tb-right" id="ph-tb-right">' +
        '<button class="tb-update" id="ph-update" title="检查更新">更新</button>' +
        '<span class="tb-ver" id="ph-tb-ver"></span></span>';
      document.body.prepend(bar);

      const rightBox = document.getElementById('ph-tb-right');
      const verEl = document.getElementById('ph-tb-ver');

      // 把原来的功能图标（帮助/升级/主题/退出）挪进标题栏
      const topRight = document.querySelector('.topbar-right');
      if (topRight && rightBox) rightBox.insertBefore(topRight, verEl);

      // 这两个按钮在本外壳里是坏的，撤掉比留着好：
      //
      //   #update-btn「在线升级」：它下载的是源码包（version.json 的 url），
      //     包里没有 exe，exe 模式下会走到「包里没有 exe，无法自动替换」这条路。
      //     Electron 版的更新是整包替换，不是换后端，点了只会让人以为升级了。
      //
      //   #quit-btn「安全退出（结束后台进程）」：它只杀后端，不管外壳，
      //     点完窗口还在、后端没了，界面立刻变成连不上。
      //     退出统一走标题栏的原生 ✕（现在是真退出）或托盘菜单「退出」。
      for (const id of ['update-btn', 'quit-btn']) {
        const el = document.getElementById(id);
        if (el) el.remove();
      }

      // 把原来的横向 tab 挪到左栏顶部，成为竖向导航。
      // 关键：页面**本来就有**左栏(.side)，导航要并进去，不能再造第二个侧栏。
      const tabs = document.querySelector('.tabs');
      const sideScroll = document.querySelector('.side .side-scroll');
      if (tabs && sideScroll) sideScroll.prepend(tabs);

      // 品牌条已无用（标题栏里有名字了），去掉，避免顶部多一条横条
      const topbar = document.querySelector('.topbar');
      if (topbar) topbar.remove();

      // 版本号从后端取，取不到就不显示——不编造
      // 更新按钮：一按查一次；有新版再按一次才下载安装。
      // 不自动装、不后台静默装——用户点的才算数。
      const upBtn = document.getElementById('ph-update');
      let pending = null;
      let busy = false;

      // 文字和 tooltip 必须一起变。
      // 只改文字的时候，用户 hover 看到的还是「检查更新」，和按钮上写的对不上；
      // 下载中想看点细节（总量/速度/还剩多久）也只能靠 tooltip——标题栏很窄，
      // 把这些塞进按钮会把版本号挤掉。
      const setLabel = (t, tip) => {
        if (!upBtn) return;
        upBtn.textContent = t;
        upBtn.title = tip || t;
      };
      const idle = () => {
        if (pending) setLabel('有新版 v' + pending.latest, '点一下开始下载并安装 v' + pending.latest);
        else setLabel('更新', '检查更新');
      };

      if (upBtn && window.phUpdate) {
        // 速率与剩余时间在页面侧算：主进程只报字节数，进度节流后的间隔不固定，
        // 用相邻两次的差值算速率会跳得厉害，所以固定用"开始下载那一刻"做基准。
        let t0 = 0;
        let g0 = 0;
        window.phUpdate.onProgress((p) => {
          if (!p || !p.total) return;
          const pct = Math.floor((p.got / p.total) * 100);
          const now = Date.now();
          if (!t0) { t0 = now; g0 = p.got; }
          const secs = (now - t0) / 1000;
          const rate = secs > 1.5 ? (p.got - g0) / secs : 0;
          let tip = '已下载 ' + (p.got / 1048576).toFixed(1) + ' / ' + (p.total / 1048576).toFixed(1) + ' MB';
          if (rate > 0) {
            const left = Math.ceil((p.total - p.got) / rate);
            tip += '，约 ' + (rate / 1048576).toFixed(1) + ' MB/s，剩 ' +
              (left > 60 ? Math.ceil(left / 60) + ' 分钟' : left + ' 秒');
          }
          setLabel('下载 ' + pct + '%', tip);
        });
        idle();
        upBtn.onclick = async () => {
          if (busy) return;
          busy = true;
          try {
            if (!pending) {
              setLabel('检查中…', '正在连接更新服务器');
              const info = await window.phUpdate.check();
              if (info.hasUpdate) {
                pending = info;
                upBtn.classList.add('has-update');
                setLabel('有新版 v' + info.latest, '点一下开始下载并安装 v' + info.latest);
              } else {
                // 源上退回了旧版本号时，上一次的高亮不能留着
                upBtn.classList.remove('has-update');
                setLabel('已是最新', '当前 v' + info.current + '，没有更新');
                setTimeout(idle, 2500);
              }
            } else {
              setLabel('准备安装…', '正在核对并准备 v' + pending.latest);
              t0 = 0;
              g0 = 0;
              const r = await window.phUpdate.install();
              if (r && r.cancelled) {
                // 用户点了「稍后」——包已经下载并校验好留在这台机器上，
                // 这不是失败。以前这里一律显示"更新失败"，用户会以为白下了。
                setLabel('已下载待装', '包已在本机，再点一次可以直接安装，不会重下');
                setTimeout(idle, 3000);
              } else if (!r || !r.ok) {
                setLabel('更新失败', '这次没成功：' + ((r && r.message) || '未知原因') + '。点一下重试');
                setTimeout(idle, 4000);
              }
              // 成功时主进程会退出应用，这里不用收尾
            }
          } catch (e) {
            setLabel('更新失败', '这次没成功：' + (e && e.message) + '。点一下重试');
            setTimeout(idle, 4000);
            console.error('[更新]', e && e.message);
          } finally {
            busy = false;
          }
        };
      }

      fetch('/api/state').then(r => r.json())
        .then(j => {
          const core = j && j.data && j.data.version;
          if (verEl) verEl.textContent = 'v' + SHELL_VER + (core ? ' · 内核 ' + core : '');
        })
        .catch(() => { if (verEl) verEl.textContent = 'v' + SHELL_VER; });
    })();
  `;
  try {
    await wc.executeJavaScript(js, true);
  } catch (err) {
    console.error('[外壳] 标题栏注入失败：', err && err.message);
  }
}

function createWindow(url) {
  const b = loadBounds();
  win = new BrowserWindow({
    width: b.width,
    height: b.height,
    x: b.x,
    y: b.y,
    minWidth: 980,
    minHeight: 640,
    title: APP_TITLE,
    backgroundColor: '#f6f7f9',
    show: false,
    icon: assetPath('icon.ico'),
    // 无边框 + Windows 原生窗口按钮：自己画标题栏，但不自己画最小化/关闭
    titleBarStyle: 'hidden',
    titleBarOverlay: {
      color: '#ffffff',
      symbolColor: '#1a1f26',
      height: 34,
    },
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      // preload 里要 require('electron')，sandbox 会把它挡掉，所以关掉 sandbox。
      // 安全性靠 contextIsolation + 只暴露三个窄接口（见 preload.js），不是靠 sandbox。
      sandbox: false,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  // 页面里的报错转进主进程日志。
  // 为什么要这一条：界面出问题时最常见的样子是"按钮点了没反应"，
  // 真实原因是 app.js 里某个绑定抛了 TypeError，而错误只在 DevTools 里能看到。
  // 不打开 DevTools 就等于没有证据。
  win.webContents.on('console-message', (_e, level, message, line, source) => {
    if (level >= 2) {
      console.error(`[页面] ${message}  (${String(source || '').replace(/^.*[\\/]/, '')}:${line})`);
    }
  });
  // 渲染进程崩掉时窗口还在、里面全白——用户嘴里的"崩了"多半就是这个形态，
  // 而且比进程退出更难自查（任务管理器里有进程、桌面上有窗口、就是没内容）。
  // 先自动重载一次（GPU 抖动、显卡驱动切换很常见），第二次还崩才认输。
  let recoveredCrash = false;
  win.webContents.on('render-process-gone', (_e, details) => {
    console.error('[页面] 渲染进程退出：', JSON.stringify(details));
    if (details && details.reason === 'clean-exit') return;
    if (!recoveredCrash) {
      recoveredCrash = true;
      console.log('[页面] 自动重新加载一次');
      setTimeout(() => {
        if (win && !win.isDestroyed()) win.loadURL(url);
      }, 600);
      return;
    }
    dialog.showErrorBox(
      '界面反复崩溃',
      `渲染进程连续退出（${(details && details.reason) || '未知'}）。\n` +
        `日志：${path.join(app.getPath('userData'), 'shell.log')}`
    );
  });

  // 后端刚起来的那一瞬间偶发连不上，页面会停在 ERR_CONNECTION_REFUSED。
  // 这种失败重试一次就好，不必让用户看到 Chromium 的错误页。
  let retriedLoad = false;
  win.webContents.on('did-fail-load', (_e, code, desc, failedUrl, isMainFrame) => {
    if (!isMainFrame) return;
    console.error(`[页面] 加载失败 code=${code} ${desc} ${failedUrl}`);
    if (retriedLoad) return;
    retriedLoad = true;
    setTimeout(() => {
      if (win && !win.isDestroyed()) win.loadURL(url);
    }, 900);
  });

  win.once('ready-to-show', () => win.show());
  win.loadURL(url);

  win.webContents.on('did-finish-load', async () => {
    await injectShell(win.webContents);
    // 端到端验证用：设了 PH_AUTOUPDATE_TEST=1 就自动跑一遍更新流程，跳过确认框。
    // 存在的理由：更新流程涉及下载 100+ MB 并重启应用，靠人手点无法在自动化里验证。
    if (process.env.PH_AUTOUPDATE_TEST === '1') {
      setTimeout(async () => {
        try {
          const info = await updater.check(app.getVersion());
          console.log(`[更新测试] 当前 ${info.current} 源上 ${info.latest} 有新版=${info.hasUpdate}`);
          if (!info.hasUpdate) return;
          const dir = path.join(app.getPath('temp'), 'persona-desktop-update');
          fs.mkdirSync(dir, { recursive: true });
          const file = path.join(dir, `persona-desktop-${info.latest}-setup.exe`);
          await updater.download(info.url, file, info.size, info.sha256, () => {});
          console.log('[更新测试] 下载并校验通过 ->', file);
          if (process.env.PH_AUTOUPDATE_INSTALL === '1') {
            // 与正式路径保持同一顺序：先确认安装器起来了，再停后端退出。
            // 测试路径如果和真实路径的时序不一样，它验出来的结论就不算数。
            const spawned = await new Promise((resolve) => {
              const timer = setTimeout(() => resolve({ ok: true }), 1500);
              updater.runInstaller(file, path.dirname(app.getPath('exe')), (err) => {
                clearTimeout(timer);
                resolve(err ? { ok: false, error: err } : { ok: true });
              });
            });
            if (!spawned.ok) {
              console.error('[更新测试] 安装器没能启动：', spawned.error && spawned.error.message);
              return;
            }
            stopBackend();
            setTimeout(() => {
              quitting = true;
              app.exit(0);
            }, 400);
          }
        } catch (err) {
          console.error('[更新测试] 失败：', err && err.message);
        }
      }, 2000);
    }
    // 调试用截图：设了 PH_SHOT=<png 路径> 就截一张然后退出。
    // 存在的理由：外壳的样子（标题栏/侧栏/圆角）没法靠看日志确认，
    // 只能真截一张看。生产环境不设这个变量就不会走。
    if (process.env.PH_SHOT) {
      setTimeout(async () => {
        try {
          // PH_SHOT_CLICK：截图前依次点击这些选择器（逗号分隔）。
          // 存在的理由：页面有多个面板，不做交互只能截到默认那一个；
          // 而"截不到的面板"就等于没人验证过它的样子。
          const sel = process.env.PH_SHOT_CLICK;
          if (sel) {
            const list = sel.split(',').map((x) => x.trim()).filter(Boolean);
            for (const one of list) {
              const ok = await win.webContents.executeJavaScript(
                `(() => { const el = document.querySelector(${JSON.stringify(one)});
                  if (!el) return false; el.click(); return true; })();`
              );
              console.log(`[调试] 点击 ${one} -> ${ok ? '成功' : '未找到'}`);
              await new Promise((r) => setTimeout(r, 1200));
            }
          }
          const img = await win.webContents.capturePage();
          fs.writeFileSync(process.env.PH_SHOT, img.toPNG());
          console.log('[调试] 已截图 ->', process.env.PH_SHOT);
        } catch (err) {
          console.error('[调试] 截图失败：', err && err.message);
        }
        quitting = true;
        app.quit();
      }, 2500);
    }
  });

  // 外链交给系统浏览器，不在应用里开新窗口（应用里没有地址栏，用户回不来）
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    if (/^https?:/i.test(target)) shell.openExternal(target);
    return { action: 'deny' };
  });

  // 点右上角 ✕ = 真退出（外壳 + 后端一起结束）。
  // 旧行为是"隐藏到托盘、后端继续跑"，用户必须再找托盘菜单点一次「退出」才干净：
  // 观感上就是"这软件关不掉"，而且后台一直在，下次双击又遇到端口占用。
  // 现在不拦 close：窗口关掉 → window-all-closed → 停后端 → 退出进程。
  win.on('close', () => {
    saveBounds();
    quitting = true;
  });

  let saveTimer = null;
  const debouncedSave = () => {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveBounds, 400);
  };
  win.on('resize', debouncedSave);
  win.on('move', debouncedSave);

  win.on('closed', () => {
    win = null;
  });
}

function createTray() {
  const iconPath = assetPath('icon.png');
  let img = nativeImage.createFromPath(iconPath);
  if (img.isEmpty()) {
    // 图标缺失不该导致托盘建不起来，用空图标兜底并留痕
    console.error('[托盘] 找不到图标：', iconPath);
    img = nativeImage.createEmpty();
  }
  console.log(`[托盘] 图标 ${iconPath} 读取结果=${img.isEmpty() ? '空(异常)' : '正常'} 尺寸=${JSON.stringify(img.getSize())}`);
  tray = new Tray(img.resize({ width: 16, height: 16 }));
  tray.setToolTip(APP_TITLE);
  const menu = Menu.buildFromTemplate([
    {
      label: '显示主界面',
      click: () => {
        if (!win) return;
        win.show();
        win.focus();
      },
    },
    {
      label: '打开数据目录',
      click: async () => {
        // openPath 失败时只返回一段错误字符串，不抛异常也不弹窗。
        // 以前没人接这个返回值，路径被移走或权限不足时点下去毫无反应，
        // 看起来像菜单坏了。
        const dir = dataHome();
        try {
          const err = await shell.openPath(dir);
          if (err) {
            dialog.showErrorBox('打不开数据目录', `${dir}\n\n${err}`);
          }
        } catch (e) {
          dialog.showErrorBox('打不开数据目录', `${dir}\n\n${(e && e.message) || e}`);
        }
      },
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        quitting = true;
        app.quit();
      },
    },
  ]);
  tray.setContextMenu(menu);
  tray.on('double-click', () => {
    if (!win) return;
    win.show();
    win.focus();
  });
}

// 截图 / 自动化用：把 userData 指到别处，绝不碰用户真实数据目录。
// 为什么需要单独一个变量：截图要跑真实外壳，但默认会往 %APPDATA%\人格中枢桌面版
// 写 backend.json、window.json 和 data/ —— 那是用户正在用的数据，验界面不该动它。
if (process.env.PH_USER_DATA) {
  try {
    fs.mkdirSync(process.env.PH_USER_DATA, { recursive: true });
    app.setPath('userData', process.env.PH_USER_DATA);
    console.log('[外壳] userData 已切到', process.env.PH_USER_DATA);
  } catch (err) {
    console.error('[外壳] 切换 userData 失败：', err && err.message);
  }
}

/**
 * 主进程日志落盘。
 *
 * 为什么必须有：Windows 上 Electron 是 GUI 子系统程序，**双击启动时没有 stdout**，
 * console.log 的输出直接进黑洞。于是"更新完打不开""启动失败"这类问题在用户机器上
 * 一点证据都留不下，只能靠猜——0.3.7 更新后崩溃就卡在这里：hub.log 只记后端，
 * 外壳那一半全程静默。
 *
 * 落盘位置跟后端日志放一起（userData/shell.log），排查时一次要两个文件。
 */
const LOG_KEEP = 512 * 1024;

function fmtLog(v) {
  if (typeof v === 'string') return v;
  if (v instanceof Error) return v.stack || v.message;
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

function attachFileLog() {
  let stream = null;
  let file = '';
  try {
    const dir = app.getPath('userData');
    // ready 之前 userData 目录可能还不存在，先建——否则 createWriteStream
    // 会异步报 ENOENT，日志静默丢失，等于没装。
    fs.mkdirSync(dir, { recursive: true });
    file = path.join(dir, 'shell.log');
    try {
      if (fs.statSync(file).size > LOG_KEEP) fs.renameSync(file, `${file}.1`);
    } catch {
      /* 第一次启动没有这个文件，正常 */
    }
    stream = fs.createWriteStream(file, { flags: 'a' });
    // 磁盘满 / 被占用都不能反过来把应用弄崩
    stream.on('error', () => {
      stream = null;
    });
  } catch {
    return;
  }
  const stamp = () => new Date().toISOString().slice(11, 23);
  for (const level of ['log', 'info', 'warn', 'error']) {
    const orig = console[level].bind(console);
    console[level] = (...args) => {
      orig(...args);
      if (!stream) return;
      try {
        stream.write(`[${stamp()}] ${args.map(fmtLog).join(' ')}\n`);
      } catch {
        /* 忽略 */
      }
    };
  }
  console.log(`[外壳] 版本 ${app.getVersion()}  日志 ${file}`);
}

attachFileLog();

// 兜底：任何没被接住的异常都要留痕并让用户看见，而不是静默变成"打不开"。
process.on('uncaughtException', (err) => {
  console.error('[外壳] 未捕获异常：', err && (err.stack || err.message));
  try {
    dialog.showErrorBox('人格中枢出错', String((err && err.message) || err));
  } catch {
    /* 弹不出来也只能留日志 */
  }
  // 出错之后进程状态不可信，退出比挂在那里装活人强（挂着的表现就是
  // "双击有进程、没有窗口"，用户只能去任务管理器收尸）。
  app.exit(1);
});
process.on('unhandledRejection', (reason) => {
  console.error('[外壳] 未处理的 Promise 拒绝：', reason && (reason.stack || reason.message));
});

// 单实例：第二次启动只把已有窗口提到前台，不再起第二个服务。
// （旧版外壳做不到这一点，双击几次就有几个窗口——这里顺手解决）
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  // 用 exit 而不是 quit：quit 要等 ready 和几道异步钩子，在 ready 之前调用
  // 有概率挂住——进程留着、窗口没有，用户看到的就是"双击没反应"。
  console.log('[外壳] 已有一个实例在跑，本次直接退出');
  app.exit(0);
} else {
  app.on('second-instance', () => {
    if (win) {
      if (!win.isVisible()) win.show();
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(async () => {
    await killStale();
    cleanUpdateTemp();
    migratePersonasOnce();
    registerUpdateIpc();
    registerFlagIpc();
    try {
      const port = await freePort();
      startBackend(port);
      const { port: realPort, info } = await waitForBackend(port);
      lastPort = realPort;
      writeState(realPort);   // 必须写**真实**端口：后端遇到占用会往后找
      console.log(`[启动] 后端 ${info.version} 就绪于 ${realPort}`);
      // 截图**默认不跳过**首次引导。
      // 以前是反过来（设了 PH_SHOT 就自动 ?noguide=1），后果是「首次引导到底弹没弹」
      // 这件事永远截不到，只能靠读代码猜。要截主界面就显式设 PH_SHOT_NOGUIDE=1。
      const q = process.env.PH_SHOT_NOGUIDE ? '?noguide=1' : '';
      createWindow(`http://127.0.0.1:${realPort}/${q}`);
      createTray();
    } catch (err) {
      // 失败了得让人知道去哪儿看。只说"启动失败"，用户唯一的办法是重装。
      console.error('[外壳] 启动失败：', err && (err.stack || err.message));
      const shellLog = path.join(app.getPath('userData'), 'shell.log');
      const coreLog = path.join(dataHome(), 'hub.log');
      const raw = String((err && err.message) || err);
      // 最常见的一条原因是杀软把后端子程序隔离了：报错只会是 ENOENT 或"退出码 1"，
      // 看着像程序坏了。把原因直接写进对话框，比让用户去猜日志快得多。
      const missing = /ENOENT|退出码\s*[1-9]|找不到后端/.test(raw);
      const hint = missing
        ? `\n\n常见原因：杀毒软件把 resources\\backend\\persona-hub.exe 隔离了。\n`
          + `在杀软的隔离区把它恢复并加入信任，再重新打开。`
        : '';
      dialog.showErrorBox(
        '启动失败',
        `${raw}${hint}\n\n`
          + `诊断日志（把这两个发出来就能定位）：\n· ${shellLog}\n· ${coreLog}`
      );
      stopBackend();
      app.quit();
    }
  });

  // 窗口关完就退出，三个平台一致。
  // 原来在 Windows 上什么都不做（因为"关窗口只是隐藏"），于是进程留在托盘里，
  // 用户以为关了、实际上后端还在写文件——这正是"必须点退出键"的来源。
  app.on('window-all-closed', () => {
    stopBackend();
    app.quit();
  });

  app.on('before-quit', () => {
    quitting = true;
  });
  app.on('will-quit', () => {
    stopBackend();
    if (tray) {
      tray.destroy();
      tray = null;
    }
  });
  process.on('exit', stopBackend);
}
