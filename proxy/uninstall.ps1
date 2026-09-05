# KidProxy uninstaller - run as Administrator.
$ErrorActionPreference = "SilentlyContinue"
$Dir = "C:\Program Files\KidProxy"
Unregister-ScheduledTask -TaskName "KidProxy Watchdog" -Confirm:$false
Unregister-ScheduledTask -TaskName "KidProxy" -Confirm:$false
Stop-Process -Name mitmdump -Force
certutil -delstore Root mitmproxy | Out-Null
foreach ($k in "HKLM:\SOFTWARE\Policies\Microsoft\Edge", "HKLM:\SOFTWARE\Policies\Google\Chrome") {
  Remove-ItemProperty -Path $k -Name ProxyMode, ProxyServer
}
Remove-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CurrentVersion\Internet Settings" -Name ProxySettingsPerUser
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings" -Name ProxyEnable -Value 0 -Type DWord
Remove-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Internet Explorer\Control Panel" -Name Proxy
Remove-Item -Path $Dir -Recurse -Force
Write-Host "KidProxy removed. Browser lock policies (extensions/devtools/inprivate) were left in place; delete HKLM\SOFTWARE\Policies\Microsoft\Edge to clear them."
