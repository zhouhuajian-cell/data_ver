<#
  把当前分支的最新代码同步到 GitHub 公开仓库（Data-Ver）。

  规则：
    - 单文件超过 -MaxFileMB 的二进制（字体、PDF、安装包等）自动排除，不推到 GitHub；
    - GitHub 上只保留「最新代码快照」一个提交，完整提交历史仍在内部 GitLab；
    - 每次运行都是 force push（覆盖 GitHub 的 main）。

  用法：
    powershell -ExecutionPolicy Bypass -File tools\push_github.ps1
    powershell -ExecutionPolicy Bypass -File tools\push_github.ps1 -MaxFileMB 2 -Remote github -Branch main
#>
param(
    [string]$Remote = "github",
    [string]$Branch = "main",
    [int]$MaxFileMB = 1,
    [string]$Message = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $limit = [long]$MaxFileMB * 1MB
    $big = @()
    foreach ($line in (git ls-tree -r -l HEAD)) {
        $m = [regex]::Match($line, '^\S+\s+\S+\s+\S+\s+(\S+)\s+(.+)$')
        if ($m.Success -and [long]$m.Groups[1].Value -gt $limit) {
            $big += $m.Groups[2].Value
        }
    }

    # 用临时索引从 HEAD 的树里摘掉大文件，再写一个新树（不动工作区）
    $tmpIndex = Join-Path $env:TEMP ("dataver-index-" + [guid]::NewGuid().ToString('N'))
    $env:GIT_INDEX_FILE = $tmpIndex
    try {
        git read-tree HEAD | Out-Null
        foreach ($path in $big) {
            git rm --cached --quiet --ignore-unmatch -- $path
        }
        $tree = (git write-tree).Trim()
    } finally {
        Remove-Item Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue
        Remove-Item $tmpIndex -Force -ErrorAction SilentlyContinue
    }

    $headShort = (git rev-parse --short HEAD).Trim()
    $msg = if ($Message) { $Message } else { "sync: $headShort " + (Get-Date -Format "yyyy-MM-dd HH:mm") }
    $commit = (git -c user.name="zhouhuajian" -c user.email="zhouhuajian@maxieyetech.com" commit-tree $tree -m $msg).Trim()

    git push --force $Remote "$($commit):refs/heads/$Branch" | Out-Host
    "snapshot $commit -> $Remote/$Branch"
    if ($big.Count) {
        "excluded (> $MaxFileMB MB):"
        $big | ForEach-Object { "  - $_" }
    }
} finally {
    Pop-Location
}
