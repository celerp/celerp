# Install and boot the packaged Windows app as a separate local account, then require
# a real db:ok from /health/ready and a clean force-quit.
#
#   -Account standard  a freshly created standard (non-admin) user: how most people run it.
#   -Account admin     the built-in Administrator. Its token is the full administrator
#                      token with no standard token linked to it, which is exactly what an
#                      administrator gets on a PC with UAC off, so the app has to boot in
#                      place with its servers started at a restricted token. The runner's
#                      own account cannot stand in for this: UAC is on there.
#
# Run from the electron folder after the build, as the runner's administrator account.
param([Parameter(Mandatory)][ValidateSet("standard", "admin")][string]$Account)
$ErrorActionPreference = "Stop"

function Fail([string]$why) { Write-Host "::error::boot smoke ($Account) FAILED on Windows - $why"; exit 1 }

$pw  = "Ci-" + [guid]::NewGuid().ToString("N").Substring(0,16) + "!Aa9"
$sec = ConvertTo-SecureString $pw -AsPlainText -Force
if ($Account -eq "standard") {
  $u = "celerpci"
  New-LocalUser -Name $u -Password $sec -AccountNeverExpires -PasswordNeverExpires | Out-Null
  Add-LocalGroupMember -Group "Users" -Member $u -ErrorAction SilentlyContinue
} else {
  $builtin = Get-LocalUser | Where-Object { $_.SID.Value -like "S-1-5-21-*-500" } | Select-Object -First 1
  if (-not $builtin) { Fail "no built-in Administrator account" }
  $u = $builtin.Name
  Set-LocalUser -Name $u -Password $sec -PasswordNeverExpires $true
  Enable-LocalUser -Name $u
  # Admin Approval Mode off for the built-in Administrator (the Windows default): it logs on
  # with the full token and no linked standard token.
  Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -Name FilterAdministratorToken -Value 0 -Type DWord
  # The app reads its own token, never a service.
  Stop-Service LanmanServer -Force
}
$cred = New-Object System.Management.Automation.PSCredential("$env:COMPUTERNAME\$u", $sec)

# seclogon backs Start-Process -Credential and is Stopped/Manual on the runner.
Set-Service seclogon -StartupType Manual
Start-Service seclogon

# Grant the account access to this process's window station + desktop (Electron's main
# process needs a desktop even before any window). The -Credential child inherits the
# caller's window station/desktop, so granting on the handles we hold is what lets the
# launched app attach to them.
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
public class WinStaAccess {
  [DllImport("user32.dll", SetLastError=true)] static extern IntPtr GetProcessWindowStation();
  [DllImport("user32.dll", SetLastError=true)] static extern IntPtr GetThreadDesktop(uint threadId);
  [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
  [DllImport("user32.dll", SetLastError=true)] static extern bool GetUserObjectSecurity(IntPtr h, ref int si, byte[] sd, int len, ref int needed);
  [DllImport("user32.dll", SetLastError=true)] static extern bool SetUserObjectSecurity(IntPtr h, ref int si, byte[] sd);
  const int DACL = 4;
  static byte[] Get(IntPtr h) {
    int si = DACL; int need = 0; GetUserObjectSecurity(h, ref si, null, 0, ref need);
    byte[] b = new byte[need];
    if (!GetUserObjectSecurity(h, ref si, b, need, ref need)) throw new Exception("Get failed " + Marshal.GetLastWin32Error());
    return b;
  }
  static void Set(IntPtr h, byte[] b) { int si = DACL; if (!SetUserObjectSecurity(h, ref si, b)) throw new Exception("Set failed " + Marshal.GetLastWin32Error()); }
  static void Add(IntPtr h, SecurityIdentifier sid, int mask, bool inheritOnlyGenericAll) {
    var sd = new RawSecurityDescriptor(Get(h), 0);
    var dacl = sd.DiscretionaryAcl == null ? new RawAcl(2, 1) : sd.DiscretionaryAcl;
    int i = dacl.Count;
    if (inheritOnlyGenericAll)
      dacl.InsertAce(i++, new CommonAce(AceFlags.ObjectInherit|AceFlags.ContainerInherit|AceFlags.InheritOnly, AceQualifier.AccessAllowed, unchecked((int)0x10000000), sid, false, null));
    dacl.InsertAce(i++, new CommonAce(AceFlags.None, AceQualifier.AccessAllowed, mask, sid, false, null));
    sd.DiscretionaryAcl = dacl;
    byte[] outb = new byte[sd.BinaryLength]; sd.GetBinaryForm(outb, 0); Set(h, outb);
  }
  public static void Grant(string account) {
    var sid = (SecurityIdentifier)(new NTAccount(account)).Translate(typeof(SecurityIdentifier));
    Add(GetProcessWindowStation(), sid, 0x37F, true);            // WINSTA_ALL_ACCESS + inherit-only GENERIC_ALL
    Add(GetThreadDesktop(GetCurrentThreadId()), sid, 0x1FF, false); // DESKTOP_ALL
  }
}
'@
[WinStaAccess]::Grant("$env:COMPUTERNAME\$u")

# Start-Process -Credential hands the child the CALLER's environment (the runner's
# %APPDATA%), so a warm-up logon first materialises the account's profile and the cmd
# wrappers point the profile variables at it, as a real logon does.
$pub = "C:\Users\Public"
Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "exit 0" -Credential $cred -LoadUserProfile -Wait -WorkingDirectory $pub
$sid = (New-Object System.Security.Principal.NTAccount("$env:COMPUTERNAME\$u")).Translate([System.Security.Principal.SecurityIdentifier]).Value
$profileDir = (Get-CimInstance Win32_UserProfile | Where-Object { $_.SID -eq $sid }).LocalPath
if (-not $profileDir) { Fail "no profile created for $u" }
$envSet = "set USERPROFILE=$profileDir`r`nset APPDATA=$profileDir\AppData\Roaming`r`nset LOCALAPPDATA=$profileDir\AppData\Local`r`nset TEMP=$profileDir\AppData\Local\Temp`r`nset TMP=$profileDir\AppData\Local\Temp`r`nif not exist `"%APPDATA%`" mkdir `"%APPDATA%`"`r`nif not exist `"%TEMP%`" mkdir `"%TEMP%`""

# Runs one command line as the account, with its profile, and returns what it printed.
function RunAs([string]$name, [string]$commandLine, [int]$timeoutMs) {
  $cmd = Join-Path $pub "celerp-$name.cmd"
  $out = Join-Path $pub "celerp-$name.out"
  Remove-Item $out -EA SilentlyContinue
  Set-Content -Encoding Ascii $cmd "@echo off`r`n$envSet`r`n$commandLine > `"$out`" 2>&1`r`necho exit %ERRORLEVEL% >> `"$out`""
  $p = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$cmd`"" -Credential $cred -LoadUserProfile -WorkingDirectory $pub -PassThru
  if (-not $p.WaitForExit($timeoutMs)) { Fail "$name did not finish in $($timeoutMs/1000)s" }
  if (Test-Path $out) { return (Get-Content $out) } else { return @() }
}

# Install the release installer as the account and launch the installed app exactly as a
# user would: default data directory, no --user-data-dir.
$setup = Join-Path $pub "Celerp-Setup.exe"
Copy-Item "dist/Celerp-Setup.exe" $setup -Force
Write-Host "smoke: installing $setup as $Account account $u (profile $profileDir)"
RunAs "install" "`"$setup`" /S /currentuser" 300000 | Write-Host
$exe = Get-ChildItem -Path "$profileDir\AppData\Local\Programs" -Recurse -Filter "Celerp.exe" -EA SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
if (-not $exe) { Fail "Celerp.exe not installed under $profileDir" }
$installDir = Split-Path $exe
$udd = "$profileDir\AppData\Roaming"

# The installed app's own token check must read the account the way the app will.
$py = Join-Path $installDir "resources\python-x64\python\python.exe"
$launcher = Join-Path $installDir "resources\app\celerp\win_restricted_launch.py"
$state = (RunAs "token" "`"$py`" -I `"$launcher`" --token-state" 60000 | Select-Object -First 1).Trim()
if ($state -ne $Account) { Fail "token state '$state' (want $Account)" }
if ($Account -eq "admin") {
  $child = (RunAs "child-token" "`"$py`" -I `"$launcher`" `"$py`" -I `"$launcher`" --token-state" 60000 | Select-Object -First 1).Trim()
  if ($child -ne "standard") { Fail "restricted launcher child token state '$child' (want standard)" }
}
Write-Host "token states OK for $Account"

# Every module in the lock is enabled; a bundled module that is missing or fails to load
# stops startup, so readiness also proves they all load.
$enabled = ((Get-Content "..\default_modules\first_party.lock.json" -Raw | ConvertFrom-Json).PSObject.Properties.Name | Sort-Object) -join ","
Write-Host "smoke: enabling $enabled"
$boot = Join-Path $pub "celerp-boot.out"
$run  = Join-Path $pub "celerp-run.cmd"
Remove-Item $boot -EA SilentlyContinue
Set-Content -Encoding Ascii $run "@echo off`r`n$envSet`r`nset ELECTRON_ENABLE_LOGGING=1`r`nset ENABLED_MODULES=$enabled`r`n`"$exe`" > `"$boot`" 2>&1"
Write-Host "smoke: launching installed $exe as $Account account $u (default data dir $udd, output $boot)"
Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$run`"" -Credential $cred -LoadUserProfile -WorkingDirectory $pub

$deadline = (Get-Date).AddSeconds(240)
$ok = $false; $port = $null; $tick = 0
while ((Get-Date) -lt $deadline) {
  if (-not $port) {
    $pf = Get-ChildItem -Path $udd -Recurse -Filter "api-port" -EA SilentlyContinue | Select-Object -First 1
    if ($pf) { $port = (Get-Content $pf.FullName -Raw).Trim(); Write-Host "smoke: app published API port $port ($($pf.FullName))" }
  }
  if ($port) {
    try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://127.0.0.1:$port/health/ready"; if ($r.Content -match '"db"\s*:\s*"ok"') { $ok = $true; break } } catch { }
  }
  if (($tick % 10) -eq 0) { Write-Host "smoke: t+$($tick*2)s Celerp procs=$(@(Get-Process Celerp -EA SilentlyContinue).Count) portFound=$([bool]$port)" }
  $tick++; Start-Sleep -Seconds 2
}
$ours = { @(Get-Process postgres, python -EA SilentlyContinue | Where-Object { $_.Path -like "$installDir\*" }) }
$before = (& $ours).Count
taskkill /IM Celerp.exe /F 2>$null | Out-Null
if (-not $ok) {
  Write-Host "===== Electron stdout/stderr ($boot) ====="
  if (Test-Path $boot) { Get-Content $boot -Tail 200 } else { Write-Host "(no captured output)" }
  Write-Host "===== boot-relevant files (logs / postgres state) ====="
  Get-ChildItem -Path $udd -Recurse -EA SilentlyContinue |
    Where-Object { -not $_.PSIsContainer -and $_.Name -match '(?i)\.log$|logfile|PG_VERSION|postmaster|\.pid$|migrat|deelevate' } |
    ForEach-Object { Write-Host "== $($_.FullName) =="; Get-Content $_.FullName -Tail 80 -EA SilentlyContinue }
  Fail "db:ok not reached (port=$port)"
}
Write-Host "boot smoke OK on Windows as $Account account (port $port)"

# The force-quit above must leave no API or database running (the app's job object ends them).
if ($before -eq 0) { Fail "force-quit check could not see the app's API and database processes" }
for ($i = 0; $i -lt 15; $i++) {
  $left = @(& $ours) + @(Get-NetTCPConnection -LocalPort $port -State Listen -EA SilentlyContinue)
  if ($left.Count -eq 0) { break }
  Start-Sleep -Seconds 2
}
if ($left.Count -ne 0) { $left | Format-Table | Out-String | Write-Host; Fail "force-quit left the API or the database running" }
Write-Host "force-quit OK on Windows as $Account account: the API and the database stopped ($before processes before)"
