# Codex Safe Cleanup / Codex 安全清理

**v0.1.0-alpha · Windows 早期测试版 · 非 OpenAI 官方工具**

帮助检查 Codex 运行数据和已关联项目中的生成文件。先展示来源、用途、路径和大小，再由你确认具体文件是否不再需要，确认后移入 Windows 回收站。它不能可靠地替你判断整个项目已经废弃。

Conservative, preview-first cleanup for Codex on Windows. Exact files only, explicit consent, native Recycle Bin and recorded recovery. Early alpha, Windows-tested only; not affiliated with OpenAI.

## 适合做什么

- 检查当前用户 `.codex`、明确的 `CODEX_HOME`、Codex 本机索引和已登记的项目/任务输出路径。
- 列出可核实来源的应用清单缓存、Python 字节码、pytest/mypy缓存、Java/Gradle与C/C++编译目标、TypeScript增量缓存、部分项目自检日志，以及已登记的测试报告、旧交付包和依赖缓存。
- 全选待核对项（**Ctrl+Alt+A**）、逐文件预览、确认后回收、按记录恢复；窗口可自适应。

**已生成不等于已无用。** 当前自动识别规则有限。日志识别要求特定的生成代码与发布排除证据；旧技能包识别要求显式保留的当前包作比对；其他内容需要在 Codex 聊天中进一步核实。不会将任意 `tmp`、旧文件或重复文件自动判为垃圾，也不扫描整个磁盘。

## 环境与启动

- Windows；实际开发验证环境为 Windows x64、Python 3.12，本次同时验证 Python 3.14 的完整测试。其他系统/架构未验证。
- Python **3.12 或更新版本**；主程序只用标准库。回收站通过系统 PowerShell 和 Windows Shell 接口工作，不需要管理员权限。
- Git 可选；遇到 Git 项目却无法核实跟踪状态时，相关文件继续保护。

下载本仓库 ZIP 并解压，然后双击 `scripts/open-scan-panel.cmd`。启动器逐个验证 Codex 运行库、Python 启动器、PATH、本机安装目录和 Windows 注册表中的解释器，跳过过旧或无法运行的自动候选，并显示最终路径与版本。找不到时安装 Python，或将 `CODEX_CLEANUP_PYTHON` 设为 Python 3.12+ 的完整路径。

也可以在仓库根目录运行：

```powershell
.\scripts\open-scan-panel.cmd
```

如需让 Codex 作为 Skill 使用，把本文件夹复制为 `%USERPROFILE%\.agents\skills\codex-safe-cleanup`，或在允许运行本地脚本的 PowerShell 中执行 `scripts/install.ps1`。首次安装遇到已有目录会停止。升级用 `scripts/install.ps1 -Upgrade`，逐文件核对分发清单与用户修改，保留用户配置和恢复记录。切换失败会回退，中断后可用 `-Recover` 核对事务并恢复旧安装。启动 CMD 仅对本次 PowerShell 进程使用执行策略参数，组策略限制仍由系统执行。

## 启动诊断与手动项目登记

启动时显示 Python、状态目录、回收站桥接、Git 和索引能力，面板中也可展开查看。仅诊断可运行以下命令。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/open-scan-panel.ps1 --diagnose
```

手动登记只增加检查范围，未知文件仍保留。先用可用 Python 运行下列命令，再在面板“检查范围”中选择项目。

```powershell
python scripts/inventory.py --register-project "D:\开发项目\示例"
python scripts/inventory.py --project "D:\开发项目\示例" --deep
python scripts/inventory.py --register-project "E:\移动后的项目" --relocate-from "D:\原项目"
```

这里的 `python` 需要为 Python 3.12+，也可换为启动诊断显示的完整解释器路径。整盘、通用用户文件夹、系统和受保护内部目录拒绝登记。

## 使用与保护

1. 首次打开只显示说明，点击“快速检查 Codex”才扫描。
2. 核对建议中的用途、证据和影响；不能确认的保留。全选只是选择，不代表同意清理。
3. 点击“一键清理到回收站”，核对精确清单并勾选确认。同步目录内容另需确认云端/其他设备也会同步移除；默认不勾选。
4. 在“清理与恢复记录”中恢复；原位置已有同名文件时不覆盖。

配置、凭据、自定义指令、Skills、插件、任务历史、源码、明确保留的成果和 Git 已跟踪文件受保护。目录联接、符号链接、云端占位、硬链接及状态不明的内容拒绝处理。清理整个目录的旧入口和旧命令行清理模式已停用。

扫描以名称和大小为主，项目生成证据核对会限量读取脚本、字节码头或 ZIP 身份；不会执行项目代码。运行数据上限为2万文件/10秒，快速项目检查为20万文件/30秒，主动深度检查选定项目为100万文件/180秒；缺失、离线和跳过范围会标明。没有“所有项目都能完整识别”的保证。

在新电脑上不会携带作者的项目索引或候选清单。可从本机 Codex 项目路径字段发现部分范围；若需完整历史关联，请在 Codex 中使用本 Skill 刷新项目/聊天索引。不要把自己的索引或聊天资料提交到 GitHub。

一次授权最多2000个精确文件、总量64GiB、单文件16GiB。1MiB缓冲流式校验并显示进度，每个文件执行前复核，内部按100项分批。部分失败后保留未处理选择，可核对并继续原始清单。与状态目录不同卷的文件在已核实项目根的 `.codex-cleanup-staging` 中同卷暂存，恢复信息仍集中记录。源卷离线或无法建立暂存区时停止并保留记录。恢复状态保存在 `%LOCALAPPDATA%\CodexSafeCleanup`。Windows 回收站直接“还原”可能先回到工具暂存区，优先用面板恢复。

**回收站和隔离区仍占空间，也可能被用户或系统清空。** 本工具不清空回收站，不保证永远可恢复，也不承诺绝对不会误删。

## 关闭与隐私

点击“关闭面板”即可停止，空闲 30 分钟也会退出。没有实时后台监测、开机启动或定时自动清理；再次双击入口重新打开。旧版配置不能启用本面板自动清理。

主程序只监听 `127.0.0.1`，不上传数据，不连接分析服务。报告、项目索引和面板地址可能含私人路径或访问凭据，**不要直接分享日志、报告或含真实数据的截图**。公开发布仅包含代码、说明和合成测试。

## 开发与验证

本次逐项核实、测试命令与支持限制见 [审查与验证](references/审查与验证.md)。开发测试见 [CONTRIBUTING.md](CONTRIBUTING.md)，操作步骤见 [使用说明](references/使用说明.md)，来源与取舍见 [调研与边界](references/调研与边界.md)。所有移动测试仅使用新建可丢弃夹具。

MIT License。欢迎提供不含私人路径、聊天和凭据的复现步骤；规则宁可漏报，不靠扩大删除范围提高“清理量”。


面板提供文件名/路径搜索、项目/类型/风险筛选、大小排序和100项分页。候选大小、选择大小与成功回收量分别展示，实际释放空间未测量。源码和Skill方式继续提供，便携包评估结论见审查记录。
