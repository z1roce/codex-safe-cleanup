param([string]$Destination = (Join-Path $env:USERPROFILE '.agents\skills\codex-safe-cleanup'))
$ErrorActionPreference = 'Stop'
$cleanupSource = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$cleanupTarget = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $cleanupTarget) { throw "目标已有技能，保留不覆盖：$cleanupTarget" }
$cleanupFiles = @('SKILL.md','agents\openai.yaml','assets\panel.html','references\使用说明.md','references\调研与边界.md',
 'scripts\project_inventory.py','scripts\test_project_inventory.py','scripts\file_cleanup.py','scripts\test_file_cleanup.py','scripts\install.ps1','scripts\inventory.py','scripts\quick_scan.py','scripts\safe_cleanup.py','scripts\reviewed_cleanup.py',
 'scripts\scan_panel.py','scripts\windows_recycle.py','scripts\RecycleBridge.cs','scripts\run-cleanup.cmd',
 'scripts\run-cleanup.ps1','scripts\open-scan-panel.cmd','scripts\test_inventory.py','scripts\test_safe_cleanup.py',
 'scripts\test_reviewed_cleanup.py','scripts\test_scan_panel.py','scripts\test_panel_ui.cjs')
foreach ($cleanupRelative in $cleanupFiles) {
 $cleanupFrom = Join-Path $cleanupSource $cleanupRelative
 if (!(Test-Path -LiteralPath $cleanupFrom -PathType Leaf)) { throw "安装文件缺失：$cleanupRelative" }
}
foreach ($cleanupRelative in $cleanupFiles) {
 $cleanupTo = Join-Path $cleanupTarget $cleanupRelative
 New-Item -ItemType Directory -Path (Split-Path -Parent $cleanupTo) -Force | Out-Null
 Copy-Item -LiteralPath (Join-Path $cleanupSource $cleanupRelative) -Destination $cleanupTo
}
Write-Output "已安装：$cleanupTarget"
Write-Output '仅 Codex 专用检查；没有启用清理或创建后台任务。双击 scripts/open-scan-panel.cmd 打开。'
