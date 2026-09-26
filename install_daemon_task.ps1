# Repair the existing task without changing account credentials or Bot settings.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$launcher = Join-Path $root 'run_daemon.ps1'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw 'Launcher not found' }
$existing = Get-ScheduledTask -TaskName 'FinanceNarrativeDaemon' -ErrorAction Stop
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`"" -WorkingDirectory $root
$login = New-ScheduledTaskTrigger -AtLogOn -User $existing.Principal.UserId
$retry = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Set-ScheduledTask -TaskName $existing.TaskName -Action $action -Trigger @($login, $retry) -Settings $settings | Out-Null
# This legacy task points at an obsolete copy, and must not launch a second bot.
$legacy = Get-ScheduledTask -TaskName 'FinanceNarrativeBot' -ErrorAction SilentlyContinue
if ($legacy -and ($legacy.Actions.WorkingDirectory -contains 'C:\Users\bcb08\Downloads\finance-narrative-app')) {
    try {
        Disable-ScheduledTask -TaskName $legacy.TaskName | Out-Null
    } catch {
        Write-Warning 'Legacy task could not be disabled; administrator action is required. Continuing current-task recovery.'
    }
}
Start-ScheduledTask -TaskName $existing.TaskName
Get-ScheduledTask -TaskName $existing.TaskName | Select-Object TaskName, State
