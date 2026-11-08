#!/usr/bin/env node
/**
 * 窗口位置恢复的判据。
 *
 * 修的是什么：位置是上次退出时存的，但那块屏幕未必还在（拔掉外接屏、换分辨率、
 * 4K 切回内置屏）。旧实现只检查"宽高是不是数字"，于是窗口被恢复到看不见的地方，
 * 用户看到的是"程序开着、任务栏有、屏幕上没有"。
 *
 * 这里的判据全部是数字进出，不启动 Electron。
 */

const path = require('node:path');
const bounds = require(path.join(__dirname, '..', 'desktop', 'src', 'bounds.js'));

let PASS = 0;
let FAIL = 0;
const FAILS = [];

function check(name, ok, detail = '') {
  if (ok) {
    PASS += 1;
    console.log('  \u2713 ' + name);
  } else {
    FAIL += 1;
    FAILS.push(`${name}  ->  ${String(detail).slice(0, 160)}`);
    console.log('  \u2717 ' + name + '  ->  ' + String(detail).slice(0, 160));
  }
}

// 常见组合：单屏 1920×1080（任务栏占 40）
const solo = [{ workArea: { x: 0, y: 0, width: 1920, height: 1040 } }];
const soloArea = solo[0].workArea;

// 双屏：主屏 1920，副屏在右边 x=1920 起
const dual = [
  { workArea: { x: 0, y: 0, width: 1920, height: 1040 } },
  { workArea: { x: 1920, y: 0, width: 2560, height: 1400 } },
];
const dualArea = dual[0].workArea;

// 笔记本小屏
const small = [{ workArea: { x: 0, y: 0, width: 1366, height: 728 } }];
const smallArea = small[0].workArea;

console.log('='.repeat(56));
console.log('窗口位置恢复');
console.log('='.repeat(56));

console.log('\n[A] 该保留的必须原样保留');
{
  const r = bounds.fit({ x: 100, y: 80, width: 1280, height: 880 }, solo, soloArea);
  check('屏幕内的位置原样返回',
    r.x === 100 && r.y === 80 && r.width === 1280 && r.height === 880, JSON.stringify(r));
}
{
  const r = bounds.fit({ x: 0, y: 0, width: 1000, height: 700 }, solo, soloArea);
  check('贴左上角也算可见（不能因为 x=0 就当无效）',
    r.x === 0 && r.y === 0, JSON.stringify(r));
}
{
  const r = bounds.fit({ x: 2100, y: 100, width: 1200, height: 800 }, dual, dualArea);
  check('落在第二块屏上的位置要保留（双屏用户不该被强制居中）',
    r.x === 2100, JSON.stringify(r));
}
{
  const r = bounds.fit(undefined, solo, soloArea);
  check('没有记录时给默认尺寸', Number.isFinite(r.width) && Number.isFinite(r.height), JSON.stringify(r));
}

console.log('\n[B] 该拉回来的必须拉回来');
{
  // 副屏拔掉后，上次的坐标还停在 x=2100
  const r = bounds.fit({ x: 2100, y: 100, width: 1200, height: 800 }, solo, soloArea);
  check('副屏拔掉 → 丢掉坐标（交给 Electron 居中）',
    !Number.isFinite(r.x) && !Number.isFinite(r.y), JSON.stringify(r));
  check('副屏拔掉 → 尺寸仍然有效', r.width > 0 && r.height > 0, JSON.stringify(r));
}
{
  const r = bounds.fit({ x: -5000, y: -5000, width: 1200, height: 800 }, solo, soloArea);
  check('坐标离谱（负数远界）→ 丢掉坐标', !Number.isFinite(r.x), JSON.stringify(r));
}
{
  const r = bounds.fit({ x: 100, y: 80, width: 2600, height: 1600 }, solo, soloArea);
  check('窗口比屏幕大 → 尺寸夹到工作区内',
    r.width <= soloArea.width && r.height <= soloArea.height, JSON.stringify(r));
}
{
  const r = bounds.fit({ x: 40, y: 40, width: 1280, height: 880 }, small, smallArea);
  check('1366×768 上 880 高的窗口 → 高度降到工作区（否则底部按钮点不到）',
    r.height <= smallArea.height, JSON.stringify(r));
  check('小屏上宽度也不超界', r.width <= smallArea.width, JSON.stringify(r));
}
{
  const r = bounds.fit({ x: NaN, y: NaN, width: 1200, height: 800 }, solo, soloArea);
  check('坐标是 NaN → 丢掉坐标', !Number.isFinite(r.x), JSON.stringify(r));
}
{
  const r = bounds.fit({ x: 100, y: 100, width: 1200, height: 800 }, solo, null);
  check('读不到屏幕信息 → 原样返回（不猜、不崩）',
    r.width === 1200 && r.height === 800, JSON.stringify(r));
}
{
  // 只露出很小一角：既不该算"可见"，也不该把窗口判成无效
  const r = bounds.fit({ x: 1900, y: 1000, width: 1200, height: 800 }, solo, soloArea);
  check('只露出 20×40 的一角 → 不算可见', !Number.isFinite(r.x), JSON.stringify(r));
}

console.log('\n' + '='.repeat(56));
console.log(`通过 ${PASS} 项，失败 ${FAIL} 项`);
if (FAILS.length) {
  console.log('\n失败项：');
  FAILS.forEach((f) => console.log('  - ' + f));
  process.exit(1);
}
