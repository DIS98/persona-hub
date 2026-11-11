<div align="center">

# 人格中枢 <sub>Persona Hub</sub>

**一处编辑，多端注入，随时逐字节还原。**

![license](https://img.shields.io/badge/license-MIT-blue?style=flat-square) ![source](https://img.shields.io/badge/source-public-informational?style=flat-square) ![platform](https://img.shields.io/badge/platform-local-first-success?style=flat-square)

`Windows · Python 3.10+（源码运行）`

[下载安装包](https://github.com/DIS98/persona-hub/releases) · [项目主页](https://dis98.github.io/persona-hub/) · [使用指南](docs/guide.md) · [架构说明](docs/architecture.md)

</div>

## 目录

1. [软件介绍](#intro)
2. [功能](#features)
3. [快速开始](#start)
4. [使用方法](#usage)
5. [开发逻辑](#arch)
6. [下载与版本](#downloads)
7. [目录结构](#layout)
8. [许可与第三方组件](#license)
9. [常见问题](#faq)

<a id="intro"></a>
## 软件介绍

人格中枢处理的是「把一份人格提示词写进多个 AI 客户端」这件小事里最容易出问题的三处：写之前有没有原文、写之后有没有校验、不想要了能不能完整撤回。

它只走客户端公开的扩展点与普通文本文件——人格文件、托管提示词目录、记忆层、自定义指令——不改宿主程序文件、不注入进程、不依赖网络完成任务。同类工具常只记路径不存内容，这里改成写前快照、内容寻址备份、写后逐字节回读。

编辑、差异预演、注入、检测、还原集中在同一界面；所有写操作走一个「全或无」事务，任何一步回读不一致就用刚拍的快照整笔回滚。

> 项目定位、能力边界与版本状态以本页和 [项目主页](https://dis98.github.io/persona-hub/) 为准。

<a id="features"></a>
## 功能

- **写前快照** — 提交第一步拍现状快照，记录原文件是否存在、大小、权限与原文；原本不存在的文件还原时删除，而不是留一个空壳。
- **逐字节还原** — 快照按 SHA-256 回读比对；停用第三方注入时用二进制存档，避免换行符被文本模式改写。
- **差异预演** — 提交前生成 unified diff 与增删行数、字节数统计，预览阶段不写任何文件。
- **幂等与共存** — 带 id 的区块就地替换，重复注入不叠加；多个默认并存于同一文件，显式选「替换」才只留一份，冲突时给出告警。
- **注入检测** — 本工具区块、第三方特征签名、结构异常三类扫描并按人格归组；认不出的注入明确标为「未知」，不猜测。
- **生效自检** — 从会话记录读回这一轮真正被读取的内容，给出生效 / 沿用 / 过期 / 部分 / 缺失 / 未注入的判定。
- **读取窗口预算** — 按目标声明的读取上限计算剩余额度，注入位置超窗或挤掉使用者自己的规则时给出分级告警。
- **模板体系** — frontmatter + 正文、继承链、变量渲染、导入任意 Markdown、内置六套模板，界面可一键插入六节骨架。
- **自定义目标** — 侧栏手填名称与路径，与自动检测并列显示；移除只删配置，不动已写入的文件。
- **本地优先** — 断网满功能、纯标准库 Python 可审计、零设备标识采集、不上传任何内容。

<a id="start"></a>
## 快速开始

### 1. 选一种运行方式

推荐直接下载桌面安装包；也可以下载单文件可执行程序，或用 Python 源码运行。

```text
Releases → desktop-0.3.18 → persona-desktop-0.3.18-e7112894-setup.exe
Releases → core-1.14.15    → persona-hub-1.14.15-7a3d0bb5.zip
```

### 2. 启动

桌面安装包走向导式安装，不需要管理员权限，卸载时保留数据。源码运行只需 Python 3.10+，无第三方依赖。

```text
python hub.py                # 默认图形界面
python hub.py --no-gui       # 只起本地服务
python hub.py --port 8900    # 指定起始端口（默认 8799）
```

### 3. 完成第一次注入

在「人格与注入」页选中一个人格 → 右侧抽屉编辑 → 填变量 → 点「预览差异」确认 → 选「并存」或「替换」→ 写入。

```text
① 侧栏选人格
② 悬停铅笔 / 「改这个人格」打开抽屉
③ 预览差异（只读，不写文件）
④ 写入方式与变量确认后提交
⑤ 在「注入管理」页看检测结果
```

### 4. 写出自己的人格模板

模板是带 frontmatter 的 Markdown，支持 extends 继承与 {{变量}} 占位；界面「插入骨架」可一键生成六节结构。

```text
---
name: 我的助手
extends: base-direct
tags: [general]
---

【身份】
{{角色描述}}

【完成判据】
…
```

<a id="usage"></a>
## 使用方法

### 三个页签

- 人格与注入：侧栏选人格、抽屉编辑、变量填写、差异预演、写入方式、Ctrl+S 保存。
- 注入管理：目标客户端与自定义目标、生效自检、格式遵从、记忆层、宿主模板插槽、重新扫描、开关与批量移除。
- 备份与还原：快照列表、还原到此、清理旧快照、操作日志。

### 人格模板写法

- 文件放在 personas/ 下，以 --- 开头的 frontmatter + 正文；字段含 id、name（必填）、desc、extends、tags、version、author、size_limit。
- extends 支持链式继承，父正文在前、子正文在后；循环继承会被拒绝。
- {{变量}} 由界面变量栏填写，未填值会被单独报出，不会静默变成空串。
- 六节骨架：身份 / 完成判据 / 方法与硬约束 / 输出规格 / 不做的事 / 不确定时。

### 停用、移除与还原

- 本工具注入停用后只留一行说明，正文不写进文件；再次启用时 id 不变。
- 第三方注入停用后整块移入存档，原位留标记，可再启用；移除按字符偏移精确切除。
- 删除人格前会先拍一份快照；任何一次写入都能从备份页回到写入前的状态。

### 升级与生效

- 顶栏升级入口：检查 → 下载 → 校验 → 暂存 → 下次启动应用；桌面与内核使用两份独立清单。
- 升级包只接受 HTTPS，下载后校验 SHA-256 与体积上限，解压时拦截路径穿越，应用时跳过数据目录。
- 改动身份类文件后，需要完全退出宿主客户端（含托盘）再打开才会生效。

<a id="arch"></a>
## 开发逻辑

```mermaid
flowchart LR
  N1["单页前端"] --> N2["本地 HTTP API"] --> N3["业务层"] --> N4["适配器"] --> N5["事务与备份库"] --> N6["目标文本文件"]
```

### 模块划分

| 位置 | 职责 |
| --- | --- |
| `hub.py` | 入口：启动服务、拉起窗口、单实例守卫、退出判活、更新引导 |
| `core/server.py` | 标准库 HTTP 服务，仅绑回环地址；统一响应信封与协议层校验 |
| `core/adapters/` | 目标发现与四种注入策略；注册表与自定义目标 |
| `core/persona.py` | 模板解析、继承、变量渲染、校验与导入 |
| `core/txn.py` | stage → preview → commit；按路径锁、原子写、整笔回滚 |
| `core/vault.py` | 内容寻址快照、还原、回读校验与垃圾回收 |
| `core/detect.py · core/switch.py` | 注入检测归组；停用、启用与移除 |
| `core/verify.py · core/audit.py` | 生效自检与输出格式遵从度统计 |
| `core/update.py` | 在线升级：检查、下载、校验、暂存、延迟应用 |
| `web/ · desktop/` | 原生单页界面；Electron 外壳只引入已构建的后端二进制 |

### 关键设计决策

- **快照先于写入** — 提交的第一步是拍摄现状快照，顺序不可颠倒；否则回滚没有依据。
- **全或无事务** — 逐文件原子写完成后统一回读，任一不一致就用刚拍的快照把已写的全部还原；写操作加进程内锁，防并发覆盖。
- **只走官方扩展点** — 不修改宿主程序文件与依赖包，不注入进程，把宿主升级带来的冲突面降到最小。
- **判定靠证据** — 目标是否可用看目标文件或标志物是否存在，不靠父目录猜测；注入状态靠完整区块标记解析，不靠子串包含。
- **正文防伪造** — 写入前把正文里伪造的区块标记替换为全角字符，防止内容伪装成控制结构。
- **前后端分离** — 桌面外壳不放后端源码副本，通过本机 HTTP API 与页面心跳通信；界面样式只在本窗口内注入。
- **协议层收敛** — 仅允许回环 Host、限定请求体大小与内容类型，补充 nosniff 与同源读取策略。

<a id="downloads"></a>
## 下载与版本

所有二进制都放在 Releases，不进源码仓库。当前版本：**内核 1.14.15 / 桌面 0.3.18**。

| 安装包 | 体积 | 说明 | Release |
| --- | --- | --- | --- |
| `persona-desktop-0.3.18-e7112894-setup.exe` | 116,579,104 B | 桌面版 NSIS 安装包，向导式、当前用户安装 | [desktop-0.3.18](https://github.com/DIS98/persona-hub/releases/tag/desktop-0.3.18) |
| `persona-hub-1.14.15-7a3d0bb5.zip` | 565,763 B | 内核源码包，可用 Python 3.10+ 直接运行 | [core-1.14.15](https://github.com/DIS98/persona-hub/releases/tag/core-1.14.15) |

下载后请核对文件体积；Release 说明里附有校验方式。

<a id="layout"></a>
## 目录结构

```text
persona-hub/
├── hub.py                 入口：服务、窗口、单实例、退出判活
├── core/                  后端核心
│   ├── server.py          HTTP API 与静态资源
│   ├── adapters/          目标发现与注入策略
│   ├── persona.py         模板解析、继承、变量、校验
│   ├── txn.py             写入事务与回滚
│   ├── vault.py           内容寻址备份与还原
│   ├── detect.py          注入检测
│   ├── switch.py          停用 / 启用 / 移除
│   ├── verify*.py         生效自检
│   ├── audit.py           格式遵从度统计
│   └── update.py          在线升级
├── web/                   原生 HTML / CSS / JS 单页界面
├── personas/              内置只读人格模板
├── desktop/               Electron 外壳与打包配置
├── tests/ · tools/        测试用例与辅助工具
└── docs/                  使用指南、架构说明、第三方组件声明
```

<a id="license"></a>
## 许可与第三方组件

本项目自研代码采用 **MIT License**（见 [LICENSE](LICENSE)），Copyright (c) 2026 DIS98。

随安装包分发的第三方组件各自保留其许可，完整署名见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)：

| 组件 | 许可 | 说明 |
| --- | --- | --- |
| Python 标准库 | PSF License | 后端运行时，无第三方包依赖 |
| Electron | MIT | 桌面外壳 |
| electron-builder | MIT | NSIS 安装包构建 |
| PyInstaller | GPL with bootloader exception | 单文件可执行程序打包 |

<a id="faq"></a>
## 常见问题

<details><summary>会修改宿主程序文件吗？</summary>

不会。只写客户端公开的扩展点与普通文本文件，不改二进制、不注入进程。

</details>

<details><summary>断网能用吗？</summary>

能。核心功能不依赖网络；只有主动检查更新时才会发起请求。

</details>

<details><summary>支持哪些客户端？</summary>

内置常见文本型通道的适配器，并允许在侧栏自定义目标；适配器注册表可扩展。

</details>

<details><summary>写入失败会留下半截内容吗？</summary>

不会。任一文件回读不一致，整笔事务会用快照还原到写入前。

</details>

---

本项目只发布与产品本身有关的内容：源码、文档、安装包。任何与开发机环境、内部分发通道或个人配置有关的内容均不包含在仓库中。
