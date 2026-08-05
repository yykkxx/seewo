# build_cpp.ps1 - 编译 C++ 版 SeewoGuardCpp (单文件, /MT 静态, 无依赖)
# 用法: powershell -ExecutionPolicy Bypass -File build_cpp.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { throw "未找到 vswhere.exe" }
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw "未找到 VS Build Tools (需安装 C++ 工作负载)" }

$vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"
$outDir = Join-Path $here "..\dist"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$out = Join-Path $outDir "SeewoGuardCpp.exe"

$cmds = @(
    "call `"$vcvars`" >nul 2>&1",
    "rc.exe /nologo /fo icon.res icon.rc",
    "cl.exe /nologo /O2 /MT /EHsc /std:c++17 /utf-8 /DUNICODE /DUNICODE main.cpp icon.res /Fe:`"$out`" /link /SUBSYSTEM:WINDOWS user32.lib shell32.lib ole32.lib oleaut32.lib advapi32.lib"
)
cmd /c ($cmds -join " && ")
if ($LASTEXITCODE -ne 0) { throw "编译失败 (exit=$LASTEXITCODE)" }
Remove-Item icon.res -ErrorAction SilentlyContinue
Write-Host "打包完成: $out"
