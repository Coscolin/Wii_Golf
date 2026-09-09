<#
Registra (o quita) una tarea programada que arranca el servidor Wii Golf al
iniciar sesión en Windows, en segundo plano y con reinicio automático si se
cae. Así la web está siempre disponible en http://localhost:8000 (y desde la
tablet) sin tener que lanzar nada a mano: el equivalente a un "servicio".

    powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 -Install
    powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 -Uninstall
    powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 -Status

El registro del servidor queda en data\server.log.
#>
param([switch]$Install, [switch]$Uninstall, [switch]$Status)

$TaskName = "WiiGolfServer"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Script = Join-Path $Root "scripts\04_web.py"
$Log = Join-Path $Root "data\server.log"

if ($Install) {
    if (-not (Test-Path $Python)) { Write-Error "No existe $Python (crea el .venv primero)"; exit 1 }
    New-Item -ItemType Directory -Force (Join-Path $Root "data") | Out-Null
    $cmd = "`"$Python`" `"$Script`" >> `"$Log`" 2>&1"
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -Hidden -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Days 365) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Write-Output "Tarea '$TaskName' instalada y arrancada. Web: http://localhost:8000  (log: $Log)"
} elseif ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Tarea '$TaskName' eliminada."
} else {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($t) { Write-Output "Tarea '$TaskName': $($t.State)" } else { Write-Output "Tarea '$TaskName' no instalada." }
}
