$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
python "$PSScriptRoot\ap18_visualizer.py" --lan --open
