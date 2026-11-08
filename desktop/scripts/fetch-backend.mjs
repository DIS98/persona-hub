/**
 * 把后端程序取到 resources/backend/ 下。
 *
 * 为什么不在这个项目里复制一份后端源码：后端（core/ + hub.py）已经有 5000 多行并带 337 项测试，
 * 复制一份立刻就会出现"两份会漂移的真源"。这里只取**已构建的产物**，
 * 后端怎么改仍然只有一个地方。
 *
 * 取不到就直接失败，不静默跳过——没有后端的壳只是个空白窗口。
 */
import { copyFileSync, existsSync, mkdirSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const dest = join(root, 'resources', 'backend', 'persona-hub.exe');

const candidates = [
  process.env.PERSONA_BACKEND_SRC,
  resolve(root, '..', 'dist', 'persona-hub.exe'),
  resolve(root, '..', 'persona-hub.exe'),
  // 2026-10-31 合并目录之前的位置（桌面版原来在 persona-hub 的兄弟目录）。
  // 留着是为了让"从旧布局切换过来"的人不至于第一步就失败，不是主路径。
  resolve(root, '..', '..', 'persona-hub', 'dist', 'persona-hub.exe'),
  resolve(root, '..', '..', 'persona-hub', 'persona-hub.exe'),
].filter(Boolean);

const src = candidates.find((p) => existsSync(p));
if (!src) {
  console.error('[fetch-backend] 找不到后端程序，试过这些位置：');
  for (const p of candidates) console.error('  - ' + p);
  console.error('先在人背后端项目里构建：pyinstaller .build/persona-hub.spec');
  process.exit(1);
}

mkdirSync(dirname(dest), { recursive: true });
copyFileSync(src, dest);
const size = statSync(dest).size;
console.log(`[fetch-backend] ${src}`);
console.log(`            -> ${dest}  (${(size / 1024 / 1024).toFixed(2)} MB)`);
