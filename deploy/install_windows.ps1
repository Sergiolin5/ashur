# Ashur node installer for Windows (Ryzen 9, Legion Go).
# Run in PowerShell, as your normal user (not as administrator):
#   irm https://raw.githubusercontent.com/Sergiolin5/ashur/main/deploy/install_windows.ps1 | iex
# Running it again updates the code and keeps the node's identity.

$ErrorActionPreference = "Stop"
$Repo    = "https://github.com/Sergiolin5/ashur/archive/refs/heads/main.zip"
$Home_   = Join-Path $env:LOCALAPPDATA "Ashur"
$App     = Join-Path $Home_ "app"
$Venv    = Join-Path $Home_ "venv"
$Config  = Join-Path $Home_ "node.yaml"
$TaskName = "Ashur node"

function Say($msg) { Write-Host "[ashur] $msg" -ForegroundColor Cyan }

New-Item -ItemType Directory -Force -Path $Home_ | Out-Null

# 1. Python 3.12+  ($py is an array: the program, then its arguments)
function Find-Python {
    $candidates = @(@("py", "-3.12"), @("py", "-3"), @("python"),
                    @((Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe")))
    foreach ($c in $candidates) {
        $exe = $c[0]; $rest = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $ok = & $exe @rest -c "import sys; print(sys.version_info >= (3, 12))" 2>$null
            if ($ok -eq "True") { return ,$c }
        } catch {}
    }
    return $null
}
$py = Find-Python
if (-not $py) {
    Say "Instalando Python 3.12 (solo para tu usuario)..."
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements | Out-Null
    $py = Find-Python
    if (-not $py) { throw "No pude instalar Python. Instálalo desde python.org y vuelve a correr este comando." }
}
$pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)
Say "Python encontrado: $($py -join ' ')"

# 2. Code (replaced on every run; identity and logs live outside it)
Say "Descargando Ashur..."
$zip = Join-Path $env:TEMP "ashur-main.zip"
Invoke-WebRequest -Uri $Repo -OutFile $zip -UseBasicParsing
$tmp = Join-Path $env:TEMP "ashur-extract"
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive -Path $zip -DestinationPath $tmp
if (Test-Path $App) { Remove-Item -Recurse -Force $App }
Move-Item (Get-ChildItem $tmp | Select-Object -First 1).FullName $App
Remove-Item -Force $zip

# 3. Private Python environment
if (-not (Test-Path $Venv)) {
    Say "Creando entorno de Python..."
    & $pyExe @pyArgs -m venv $Venv
}
$vpy  = Join-Path $Venv "Scripts\python.exe"
$vpyw = Join-Path $Venv "Scripts\pythonw.exe"
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet -r (Join-Path $App "requirements-node.txt")

# 4. Config (asked only the first time)
if (-not (Test-Path $Config)) {
    do {
        $device = (Read-Host "Nombre de este equipo para Ashur (minúsculas, ej. ryzen9 o legion-go)").Trim()
    } until ($device -cmatch '^[a-z0-9][a-z0-9_-]*$')
    $server = (Read-Host "Nombre del servidor en Tailscale (ej. zimaos)").Trim()
    Write-Host "Política de recursos:"
    Write-Host "  1 = máquina principal compartida (Ryzen 9)"
    Write-Host "  2 = portátil con batería (Legion Go)"
    $choice = Read-Host "Elige 1 o 2"
    $policy = if ($choice -eq "2") { "portable" } else { "shared_main_machine" }
    $yaml = @"
device_id: $device
persona: $device
core_url: http://${server}:8711
modules: [inventory]
yield_policy: $policy
"@
    # UTF-8 without BOM (PowerShell 5.1 adds one with Set-Content -Encoding UTF8)
    [System.IO.File]::WriteAllText($Config, $yaml, (New-Object System.Text.UTF8Encoding $false))
    Say "Configuración guardada en $Config"
}

Push-Location $App
try {
    # 5. Enroll once (the key is typed, never saved)
    if (-not (Test-Path (Join-Path $Home_ "node_token"))) {
        Say "Registrando el nodo. Pega la enroll key de keys.json del servidor:"
        & $vpy -m node.agent --config $Config --enroll
        if ($LASTEXITCODE -ne 0) { throw "El registro falló. Revisa el mensaje de arriba: la clave, que Tailscale esté activo y que http://<servidor>:8711/health abra en este equipo." }
    }

    # 6. Self-test
    Say "Autoprueba:"
    & $vpy -m node.agent --config $Config --selftest
} finally { Pop-Location }

# 7. Start at logon, inside your session (needed to see idle time and fullscreen apps)
$action   = New-ScheduledTaskAction -Execute $vpyw -Argument "-m node.agent --config `"$Config`"" -WorkingDirectory $App
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Ashur node agent" -Force | Out-Null
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $TaskName

Say "Listo. El nodo está corriendo y arrancará solo al iniciar sesión."
Say "Registro: $(Join-Path $Home_ 'agent.log')"
