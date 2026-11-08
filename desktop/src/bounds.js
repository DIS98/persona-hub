/**
 * 窗口位置的可用性判断（纯函数，不碰 Electron，方便单测）。
 *
 * 为什么单独成文件：这段逻辑的输入是"上次记住的坐标"，输出是"这次该用哪个坐标"，
 * 全是数字，完全可以脱离 Electron 验证。留在 main.js 里就只能靠人眼看窗口。
 *
 * 要解决的问题：位置是上次退出时存的，但那块屏幕未必还在——拔掉外接显示器、
 * 改分辨率、4K 切回内置屏，都会让坐标落到可视区之外。表现是"程序开了、
 * 任务栏里有、屏幕上什么都没有"，用户第一反应是软件坏了。
 */

const MIN_W = 960;
const MIN_H = 600;

/** 两块矩形够不够"看得见"：允许跨屏，只要露出足够多让人能抓住 */
function enoughOverlap(x, y, w, h, a) {
  const ox = Math.min(x + w, a.x + a.width) - Math.max(x, a.x);
  const oy = Math.min(y + h, a.y + a.height) - Math.max(y, a.y);
  return ox >= 160 && oy >= 120;
}

/**
 * @param {{width:number,height:number,x?:number,y?:number}} saved 上次记住的边界
 * @param {Array<{workArea:{x:number,y:number,width:number,height:number}}>} displays 所有屏幕
 * @param {{x:number,y:number,width:number,height:number}} primary 主屏工作区
 * @returns {{width:number,height:number,x?:number,y?:number}}
 */
function fit(saved, displays, primary) {
  if (!saved || !Number.isFinite(saved.width) || !Number.isFinite(saved.height)) {
    return { width: MIN_W, height: MIN_H };
  }
  if (!primary || !Number.isFinite(primary.width)) {
    return { width: saved.width, height: saved.height };
  }

  // 先夹进工作区：默认的 1280×880 在 1366×768 的笔记本上会顶出屏幕底部，
  // 底部那排按钮永久点不到。
  const width = Math.max(Math.min(MIN_W, primary.width), Math.min(saved.width, primary.width));
  const height = Math.max(Math.min(MIN_H, primary.height), Math.min(saved.height, primary.height));

  const list = Array.isArray(displays) && displays.length ? displays : [{ workArea: primary }];
  if (Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
    const seen = list.some((d) => d && d.workArea && enoughOverlap(saved.x, saved.y, width, height, d.workArea));
    if (seen) return { x: saved.x, y: saved.y, width, height };
  }
  // 不带坐标 → 由 Electron 居中
  return { width, height };
}

module.exports = { fit, MIN_W, MIN_H };
