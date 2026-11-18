<#
  autostart.ps1 -Enable    start the clipper automatically when you log on to Windows
  autostart.ps1 -Disable   remove the automatic start
Runs as your user (no admin, no stored password). Restarts on failure.
#>
param([switch]$Enable, [switch]$Disable)
$TaskName = "GTA Clipper"
if ($Disable) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Autostart removed."
    exit 0
}
$start = Join-Path $PSScriptRoot "start.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$start`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = "PT1M"   # let Windows finish logging on and the network come up
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 9 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Autostart enabled: '$TaskName' runs 1 minute after you log on."
Start-ScheduledTask -TaskName $TaskName
Write-Host "Started now. Dashboard: http://127.0.0.1:8080"
