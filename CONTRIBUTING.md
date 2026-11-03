# 开发和验证

运行环境：Windows、Python 3.12+、Git（用于验证跟踪文件保护）。不需要管理员权限。主程序无第三方 Python 依赖。

在独立、非同步目录准备测试根；测试会在其中新建各自的临时子目录，不允许指向真实项目来试删。

```powershell
$env:CLEANUP_TEST_ROOT = Join-Path $env:TEMP 'CodexCleanupTests'
New-Item -ItemType Directory -Path $env:CLEANUP_TEST_ROOT -Force | Out-Null
$env:PYTHONUTF8 = '1'
Set-Location scripts
py -3 -B -m unittest discover -p 'test_*.py' -v
```

完整测试会将**新建测试文件**移入真实 Windows 回收站并恢复，也会测试历史记录兼容性。失败时先检查夹具与恢复记录，勿清空回收站或直接删掉失败夹具。旧内部引擎保留用于恢复和测试；公开 CLI 不提供其清理模式。

可选浏览器测试要求 Node.js、Google Chrome 和 Playwright。已有 Codex 开发运行库时会复用；否则可在外部开发目录安装官方 npm 包 `playwright`，把 `CLEANUP_PLAYWRIGHT_MODULE` 设为该模块目录，必要时用 `CLEANUP_NODE` 指定 Node 可执行文件，再设 `CLEANUP_BROWSER_TEST=1` 运行 `test_scan_panel`。这些是开发依赖，不是使用面板的要求。

GitHub Actions 只跑指定的只读回归测试（其中仍会创建合成夹具），不调用回收站；不能把 CI 通过说成远程回收站已验证。本地完整回归与 UI 测试结果应分别报告。

`test_safe_cleanup` 会将自己的夹具登记到独立的合成 CODEX_HOME，测试根可位于普通临时目录，继续经过真实范围保护。

跨卷测试默认跳过。明确设置 `CLEANUP_CROSS_VOLUME_ROOT` 为另一个本地卷上专门创建的可丢弃目录后，单独运行 `test_file_cleanup.FileCleanupTests.test_real_multi_volume_batch_recycle_restart_restore`。它断言两个测试目录的设备号不同，使用真实回收站并恢复，勿指向真实项目。

`CLEANUP_PYTHON_VERSIONS` 可指定其他已安装解释器的完整路径，Windows 上以分号分隔，用于真实编译不同版本的缓存。`CLEANUP_JAVAC` 可指定本机 javac，验证标准输出布局内的真实 class。缺少 javac 时跳过该可选测试，仍运行合成格式测试。测试不安装运行时或第三方 Python 包。

安装测试仅在测试根中生成安装副本，覆盖旧安装识别、用户修改、配置与恢复文件保留、LICENSE、切换失败回退和事务恢复。中断测试在独立子进程中使用 `os._exit`，验证暂存与原生回收两个边界后由新进程恢复。磁盘离线和权限不足使用故障注入，物理拔盘及系统 ACL 测试结果需要单独报告。

增加识别规则时必须同时给出生成来源证据与保护场景测试。来源关联不能证明无用；不按名称、时间或相同内容直接授权删除。禁止扩大到整盘扫描、永久删除或后台自动清理。

提交前检查 `git diff --cached`：不提交项目/任务索引、产物登记、运行状态、隔离记录、真实扫描报告、`.env`、缓存或作者的旧 ZIP 包。`.gitignore` 只能减少意外，不能代替检查。

