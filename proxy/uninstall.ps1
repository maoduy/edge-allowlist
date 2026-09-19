# KidNest uninstaller - run as Administrator.
$ErrorActionPreference = "SilentlyContinue"
$Dir = "C:\Program Files\KidNest"
Unregister-ScheduledTask -TaskName "KidNest Watchdog" -Confirm:$false
Unregister-ScheduledTask -TaskName "KidNest" -Confirm:$false
Stop-Process -Name mitmdump -Force
certutil -delstore Root mitmproxy | Out-Null
foreach ($k in "HKLM:\SOFTWARE\Policies\Microsoft\Edge", "HKLM:\SOFTWARE\Policies\Google\Chrome") {
  Remove-ItemProperty -Path $k -Name ProxyMode, ProxyServer
}
Remove-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CurrentVersion\Internet Settings" -Name ProxySettingsPerUser
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings" -Name ProxyEnable -Value 0 -Type DWord
Remove-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Internet Explorer\Control Panel" -Name Proxy
Remove-Item -Path $Dir -Recurse -Force
Write-Host "KidNest removed. Browser lock policies (extensions/devtools/inprivate) were left in place; delete HKLM\SOFTWARE\Policies\Microsoft\Edge to clear them."
