/**
 * 预加载脚本：只做一件事——把更新能力递给页面，别的一概不暴露。
 *
 * 页面来自 http://127.0.0.1:<随机端口>，是别人（后端）在写 HTML，
 * 所以绝不能把 node / fs 直接给过去。这里只放三个窄接口。
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('phUpdate', {
  /** 查一次有没有新版。返回 { current, latest, hasUpdate, notes, size, ... } */
  check: () => ipcRenderer.invoke('update:check'),

  /** 下载并安装。返回 { ok, message } */
  install: () => ipcRenderer.invoke('update:install'),

  /** 只看进度：{ got, total } */
  onProgress: (cb) => {
    ipcRenderer.on('update:progress', (_e, p) => cb(p));
  },
});

/**
 * 记住一点界面状态，跨启动有效。
 *
 * 页面自己也有 localStorage，但它的 origin 带随机端口（每次启动都变），
 * 存了等于没存——"使用指引每次打开都弹"就是这么来的。这里按 userData 存，
 * 与端口无关。只开放存取字符串标记，不给文件路径、不给读写权限。
 */
contextBridge.exposeInMainWorld('phUI', {
  getFlag: (key) => ipcRenderer.invoke('ui:flag:get', key),
  setFlag: (key, value) => ipcRenderer.invoke('ui:flag:set', key, value),
});
