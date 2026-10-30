# Install and boot the packaged Windows app as a separate local account, then require
# a real db:ok from /health/ready and a clean force-quit. Without -Previous or -Demo, the
# first launch is a new install that must set up a company with every business type, restart
# exactly when a type needs modules that are not running, and run every module each type
# needs (smoke_records.py first-run).
#
#   -Account standard  a freshly created standard (non-admin) user: how most people run it.
#   -Account admin     the built-in Administrator. Its token is the full administrator
#                      token with no standard token linked to it, which is exactly what an
#                      administrator gets on a PC with UAC off, so the app has to boot in
#                      place with its servers started at a restricted token. The runner's
#                      own account cannot stand in for this: UAC is on there.
#
# For a standard account:
#   -UserName      the account to create. A name with spaces and non-ASCII letters puts
#                  them in the profile path, so in the data folder and the install folder.
#   -Install machine  the runner's administrator installs for all users (/allusers) into
#                  a Program Files folder with spaces whose deepest file is 259 characters
#                  long, the most a path may hold without long-path support; the account
#                  then runs that install. Default: the account installs for itself.
#   -Previous      a released installer: the account installs it, it opens fresh data and
#                  keeps records in it, then this build installs over it and must serve the
#                  same records (scripts/smoke_records.py).
#   -PreviousFails with -Previous: the released copy cannot set up its database under this
#                  account (it must not reach db:ok); this build installs over what it left
#                  and must start clean.
#   -Installer     a built Celerp-Setup.exe to test instead of dist/Celerp-Setup.exe. Without
#                  the unpacked build next to it the install folder is not compared file by
#                  file; the build job's own smokes do that.
#   -NoShortNames  Windows stops making short (8.3) names before the account is created,
#                  so its profile folder has none; the smoke checks that it has none.
#                  With a name Postgres cannot read in the ANSI code page (Thai, Chinese)
#                  the database must then be set up in the account's private folder
#                  under %ProgramData% (Celerp-<account SID>).
#   -OverRunning   once the app is ready, the account runs this build's installer again
#                  with the app open (its processes and API checked alive first); it must
#                  end exactly those processes, leave a whole install, and boot again.
#   -Demo          once the app is ready, moves the sample company in, with the account's
#                  name as a customer, the invoice number prefix and an attachment name;
#                  the attachment, the invoice PDF, the CSV exports and a company backup
#                  restored as a new company must all carry it (smoke_records.py demo).
#                  Then keeps records and an attachment named in Thai, Arabic and Chinese,
#                  downloads a full backup, changes the data, restores the backup through
#                  System Recovery and restarts; the app must publish its new API port and
#                  serve exactly the backed-up records and attachment (backup-restore,
#                  restored). Last, a force-quit and a fresh launch must serve the same
#                  records again (carried).
#
# After every install the installed folder must hold exactly the build's files, byte for
# byte, plus the uninstaller (scripts/check_win_bundle.py installed). Install and start times go to the job summary; they are reported, never
# compared against a limit.
#
# Run from the electron folder after the build, as the runner's administrator account.
param(
  [Parameter(Mandatory)][ValidateSet("standard", "admin")][string]$Account,
  [string]$UserName = "celerpci",
  [ValidateSet("user", "machine")][string]$Install = "user",
  [string]$Previous = "",
  [switch]$PreviousFails,
  [string]$Installer = "dist/Celerp-Setup.exe",
  [switch]$NoShortNames,
  [switch]$OverRunning,
  [switch]$Demo
)
$ErrorActionPreference = "Stop"

$smoke = "$Account account, $Install install"
function Fail([string]$why) { Write-Host "::error::boot smoke ($smoke) FAILED on Windows - $why"; exit 1 }
if ($Account -eq "admin" -and ($Install -ne "user" -or $Previous -or $OverRunning -or $NoShortNames -or $Demo -or $PSBoundParameters.ContainsKey("UserName"))) {
  Fail "the administrator smoke runs the built-in Administrator on a fresh install for itself; -UserName, -Install machine, -Previous, -NoShortNames, -OverRunning and -Demo are for a standard account"
}
if ($Previous -and $Install -eq "machine") { Fail "-Previous upgrades an install for the account itself; it does not combine with -Install machine" }
if ($PreviousFails -and -not $Previous) { Fail "-PreviousFails needs -Previous" }
if ($Previous -and -not (Test-Path $Previous)) { Fail "no previous installer at $Previous" }
if (-not (Test-Path $Installer)) { Fail "no installer at $Installer" }

function Timing([string]$what, [Diagnostics.Stopwatch]$watch) {
  $line = "- Windows $smoke ($u): $what {0:N1} s" -f $watch.Elapsed.TotalSeconds
  Write-Host "timing: $line"
  if ($env:GITHUB_STEP_SUMMARY) { Add-Content -Encoding utf8 $env:GITHUB_STEP_SUMMARY $line }
}

$pw  = "Ci-" + [guid]::NewGuid().ToString("N").Substring(0,16) + "!Aa9"
$sec = ConvertTo-SecureString $pw -AsPlainText -Force
if ($NoShortNames) {
  fsutil 8dot3name set 1 | Write-Host
  if ($LASTEXITCODE -ne 0) { Fail "fsutil could not stop short name creation" }
}
if ($Account -eq "standard") {
  $u = $UserName
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
# wrappers set the account's user name and point the profile variables at it, as a
# real logon does. The installer finds a running app by user name, so a runner
# USERNAME would hide the account's app from it.
$pub = "C:\Users\Public"
Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "exit 0" -Credential $cred -LoadUserProfile -Wait -WorkingDirectory $pub
$sid = (New-Object System.Security.Principal.NTAccount("$env:COMPUTERNAME\$u")).Translate([System.Security.Principal.SecurityIdentifier]).Value
$profileDir = (Get-CimInstance Win32_UserProfile | Where-Object { $_.SID -eq $sid }).LocalPath
if (-not $profileDir) { Fail "no profile created for $u" }
if ($NoShortNames) {
  Add-Type -Namespace Win32 -Name Paths -MemberDefinition '[DllImport("kernel32.dll", CharSet=CharSet.Unicode)] public static extern uint GetShortPathNameW(string longPath, System.Text.StringBuilder shortPath, uint size);'
  $short = New-Object System.Text.StringBuilder 1024
  if ([Win32.Paths]::GetShortPathNameW($profileDir, $short, 1024) -eq 0) { Fail "could not read the short form of $profileDir" }
  if ($short.ToString() -ne $profileDir) { Fail "the profile $profileDir still has a short name ($short)" }
  Write-Host "smoke: the profile $profileDir has no short name"
}
# The wrappers are UTF-8 and switch cmd to UTF-8 first, so a profile path with non-ASCII
# letters reaches the app as it is.
$envSet = "set USERNAME=$u`r`nset USERDOMAIN=$env:COMPUTERNAME`r`nset USERPROFILE=$profileDir`r`nset APPDATA=$profileDir\AppData\Roaming`r`nset LOCALAPPDATA=$profileDir\AppData\Local`r`nset TEMP=$profileDir\AppData\Local\Temp`r`nset TMP=$profileDir\AppData\Local\Temp`r`nif not exist `"%APPDATA%`" mkdir `"%APPDATA%`"`r`nif not exist `"%TEMP%`" mkdir `"%TEMP%`""
function Wrapper([string]$path, [string]$body) {
  Set-Content -Encoding utf8NoBOM $path "@echo off`r`nchcp 65001 >nul`r`n$envSet`r`n$body"
}

# Runs one command line as the account, with its profile; returns what it printed and
# its exit code.
function RunAs([string]$name, [string]$commandLine, [int]$timeoutMs) {
  $cmd = Join-Path $pub "celerp-$name.cmd"
  $out = Join-Path $pub "celerp-$name.out"
  Remove-Item $out -EA SilentlyContinue
  Wrapper $cmd "$commandLine > `"$out`" 2>&1`r`necho exit %ERRORLEVEL% >> `"$out`""
  $p = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$cmd`"" -Credential $cred -LoadUserProfile -WorkingDirectory $pub -PassThru
  if (-not $p.WaitForExit($timeoutMs)) { Fail "$name did not finish in $($timeoutMs/1000)s" }
  $lines = if (Test-Path $out) { @(Get-Content -Encoding utf8 $out) } else { @() }
  $last = $lines | Where-Object { $_ -match '^exit (-?\d+)\s*$' } | Select-Object -Last 1
  if (-not $last) { Fail "$name printed no exit code" }
  [pscustomobject]@{ Lines = @($lines | Where-Object { $_ -ne $last }); Code = [int]($last -replace '^exit (-?\d+)\s*$', '$1') }
}

$build = Join-Path (Split-Path (Resolve-Path $Installer)) "win-unpacked"
if (-not (Test-Path $build)) { $build = $null }
if ($Install -eq "machine" -and -not $build) { Fail "-Install machine sizes its folder from the unpacked build next to $Installer" }
$setup = Join-Path $pub "Celerp-Setup.exe"
Copy-Item $Installer $setup -Force
$udd = "$profileDir\AppData\Roaming"
if ($Install -eq "machine") {
  $deepest = (Get-ChildItem $build -Recurse -File | ForEach-Object { $_.FullName.Length - $build.Length - 1 } | Measure-Object -Maximum).Maximum
  $base = "$env:ProgramFiles\Celerp CI long path with spaces"
  $pad = 259 - $deepest - 1 - $base.Length - 1
  if ($pad -lt 1) { Fail "the deepest build file is $deepest characters, too long for a Program Files folder within 259" }
  $machineDir = "$base\" + ("d" * $pad)
  Write-Host "smoke: per-machine folder $machineDir ($($machineDir.Length) characters, deepest file $($machineDir.Length + 1 + $deepest)); LongPathsEnabled=$((Get-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem).LongPathsEnabled)"
}

# Runs one installer the way this smoke installs, times it, and returns the install folder.
function InstallSetup([string]$installer, [string]$what) {
  Write-Host "smoke: $what ($installer) for $smoke, account $u (profile $profileDir)"
  $watch = [Diagnostics.Stopwatch]::StartNew()
  if ($Install -eq "machine") {
    # /D must come last and unquoted.
    $code = (Start-Process -FilePath $installer -ArgumentList "/S /allusers /D=$machineDir" -Wait -PassThru).ExitCode
  } else {
    $r = RunAs "install" "`"$installer`" /S /currentuser" 600000
    $r.Lines | Write-Host
    $code = $r.Code
  }
  $watch.Stop()
  if ($code -ne 0) { Fail "$what exited $code" }
  Timing $what $watch
  $root = if ($Install -eq "machine") { $machineDir } else { "$profileDir\AppData\Local\Programs" }
  $found = Get-ChildItem -Path $root -Recurse -Filter "Celerp.exe" -EA SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
  if (-not $found) { Fail "Celerp.exe not installed under $root after $what" }
  Split-Path $found
}

# The installed folder holds exactly the build's files, byte for byte, plus the
# uninstaller: nothing left from an earlier copy, nothing missing, nothing part written.
function TreeMatches([string]$dir, [string]$what) {
  if (-not $build) { Write-Host "smoke: no unpacked build next to $Installer, so the folder after $what is not compared file by file"; return }
  python scripts\check_win_bundle.py installed $build $dir
  if ($LASTEXITCODE -ne 0) { Fail "after $what the install folder differs from the build" }
}

# Every module in the lock is enabled, except on a new install's first launch; a bundled
# module that is missing or fails to load stops startup, so readiness also proves they all load.
$enabled = ((Get-Content "..\default_modules\first_party.lock.json" -Raw | ConvertFrom-Json).PSObject.Properties.Name | Sort-Object) -join ","
Write-Host "smoke: enabling $enabled"

# What the app printed ($boot, its stdout and stderr) and every log in the account's data
# folder, the server logs from before a restart (*.log.prev) and the database state among them.
function AppOutput([string]$boot) {
  Write-Host "===== Electron stdout/stderr ($boot) ====="
  if (Test-Path $boot) { Get-Content $boot -Tail 200 } else { Write-Host "(no captured output)" }
  Write-Host "===== boot-relevant files (logs / postgres state) ====="
  Get-ChildItem -Path $udd -Recurse -EA SilentlyContinue |
    Where-Object { -not $_.PSIsContainer -and $_.FullName -notmatch '(?i)\\celerp-data\\modules\\' -and
                   $_.Name -match '(?i)\.log(\.prev)?$|logfile|PG_VERSION|postmaster|\.pid$|migrat|deelevate' } |
    ForEach-Object { Write-Host "== $($_.FullName) =="; Get-Content $_.FullName -Tail 80 -EA SilentlyContinue }
}

# Launches the installed app exactly as a user would (default data directory, no
# --user-data-dir) and waits until it is ready: it has published its API port (it does
# once it has started and handles a restart) and the API answers db:ok. Returns the
# port. -NewInstall names no modules, so only the ones a new install starts are running.
function Launch([string]$exe, [string]$what, [switch]$MustFail, [switch]$NewInstall) {
  Get-ChildItem -Path $udd -Recurse -Filter "api-port" -EA SilentlyContinue | Remove-Item -Force
  $boot = Join-Path $pub "celerp-boot.out"
  $run  = Join-Path $pub "celerp-run.cmd"
  Remove-Item $boot -EA SilentlyContinue
  $modules = if ($NewInstall) { "" } else { "set ENABLED_MODULES=$enabled`r`n" }
  Wrapper $run "set ELECTRON_ENABLE_LOGGING=1`r`n$modules`"$exe`" > `"$boot`" 2>&1"
  Write-Host "smoke: $what, launching $exe as $u (default data dir $udd, output $boot)"
  $watch = [Diagnostics.Stopwatch]::StartNew()
  Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$run`"" -Credential $cred -LoadUserProfile -WorkingDirectory $pub
  $deadline = (Get-Date).AddSeconds(240)
  $port = $null; $tick = 0
  while ((Get-Date) -lt $deadline) {
    if (-not $port) {
      $pf = Get-ChildItem -Path $udd -Recurse -Filter "api-port" -EA SilentlyContinue | Select-Object -First 1
      if ($pf) { $port = (Get-Content $pf.FullName -Raw).Trim(); Write-Host "smoke: app published API port $port ($($pf.FullName))" }
    }
    if ($port) {
      try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://127.0.0.1:$port/health/ready"; if ($r.Content -match '"db"\s*:\s*"ok"') { if ($MustFail) { Fail "$what reached db:ok on port $port, so this account can keep data in it: upgrade with data instead" }; $watch.Stop(); Timing "$what, launch to db:ok" $watch; return $port } } catch { }
    }
    if (($tick % 10) -eq 0) { Write-Host "smoke: t+$($tick*2)s Celerp procs=$(@(Get-Process Celerp -EA SilentlyContinue).Count) portFound=$([bool]$port)" }
    $tick++; Start-Sleep -Seconds 2
  }
  taskkill /IM Celerp.exe /F 2>$null | Out-Null
  if ($MustFail) {
    $dir = Split-Path $exe
    for ($i = 0; $i -lt 15 -and @(Ours $dir).Count; $i++) { Start-Sleep -Seconds 2 }
    if (@(Ours $dir).Count) { Fail "$what`: closing it left its processes running" }
    Write-Host "smoke: $what did not reach db:ok in 240 s, as expected for this account; its output:"
    if (Test-Path $boot) { Get-Content $boot -Tail 40 }
    return
  }
  AppOutput $boot
  Fail "$what`: db:ok not reached (port=$port)"
}

# The app's processes: run from the install folder, or for the database of an account
# whose profile path has letters outside ASCII, through the account's private folder.
$private = Join-Path $env:ProgramData "Celerp-$sid"
function Ours([string]$dir) { @(Get-Process Celerp, postgres, python -EA SilentlyContinue | Where-Object { $_.Path -like "$dir\*" -or $_.Path -like "$private\*" }) }

# The app is open: its window, API and database processes run from the install folder (or
# the private folder) and the API answers with db:ok. Returns each process's id, name, path and start time as
# read now, so a later check can tell that exactly these ended: a process object looks up its path and start
# time by id only when they are first asked for, and by then Windows may have given the id to another process.
function RunningApp([string]$dir, [string]$port, [string]$what) {
  $procs = @(Ours $dir)
  foreach ($name in "Celerp", "python", "postgres") {
    if (-not ($procs | Where-Object { $_.Name -eq $name })) { Fail "$what`: no $name process is running from $dir or $private" }
  }
  $ready = try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "http://127.0.0.1:$port/health/ready").Content } catch { "" }
  if ($ready -notmatch '"db"\s*:\s*"ok"') { Fail "$what`: the API on port $port does not answer db:ok" }
  Write-Host "smoke: $what, app running: $(($procs | ForEach-Object { "$($_.Name) $($_.Id)" }) -join ', ')"
  $procs | Select-Object Id, Name, Path, StartTime
}

# Force-quits the app: no API or database may stay running (the app's job object ends them).
function ForceQuit([string]$dir, [string]$port, [string]$what) {
  $before = @(Ours $dir | Where-Object { $_.Name -ne "Celerp" }).Count
  taskkill /IM Celerp.exe /F 2>$null | Out-Null
  if ($before -eq 0) { Fail "$what`: force-quit check could not see the app's API and database processes" }
  for ($i = 0; $i -lt 15; $i++) {
    $left = @(Ours $dir) + @(Get-NetTCPConnection -LocalPort $port -State Listen -EA SilentlyContinue)
    if ($left.Count -eq 0) { break }
    Start-Sleep -Seconds 2
  }
  if ($left.Count -ne 0) { $left | Format-Table | Out-String | Write-Host; Fail "$what`: force-quit left the API or the database running" }
  Write-Host "force-quit OK on Windows ($smoke, $what): the API and the database stopped ($before processes before)"
}

# Business records through the running copy's API, from the repository's scripts folder.
function Records([string]$command, [string]$port, [string]$out, [string]$folder) {
  python ..\scripts\smoke_records.py $command "http://127.0.0.1:$port" @(if ($folder) { $folder }) > $out
  if ($LASTEXITCODE -ne 0) { Get-Content $out -EA SilentlyContinue | Write-Host; Fail "smoke_records.py $command against port $port exited $LASTEXITCODE" }
}

if ($PreviousFails) {
  $installDir = InstallSetup $Previous "previous release install"
  Launch (Join-Path $installDir "Celerp.exe") "previous release" -MustFail
} elseif ($Previous) {
  $installDir = InstallSetup $Previous "previous release install"
  $port = Launch (Join-Path $installDir "Celerp.exe") "previous release"
  Records seed $port (Join-Path $pub "celerp-previous.seed")
  Records snapshot $port (Join-Path $pub "celerp-previous.snapshot")
  Write-Host "smoke: previous release $(Get-Content (Join-Path $pub 'celerp-previous.seed'))"
  ForceQuit $installDir $port "previous release"
}

$installDir = InstallSetup $setup "install"
TreeMatches $installDir "install"
$exe = Join-Path $installDir "Celerp.exe"

# The installed app's own token check must read the account the way the app will.
$py = Join-Path $installDir "resources\python-x64\python\python.exe"
$launcher = Join-Path $installDir "resources\app\celerp\win_restricted_launch.py"
$state = ((RunAs "token" "`"$py`" -I `"$launcher`" --token-state" 60000).Lines | Select-Object -First 1).Trim()
if ($state -ne $Account) { Fail "token state '$state' (want $Account)" }
if ($Account -eq "admin") {
  $child = ((RunAs "child-token" "`"$py`" -I `"$launcher`" `"$py`" -I `"$launcher`" --token-state" 60000).Lines | Select-Object -First 1).Trim()
  if ($child -ne "standard") { Fail "restricted launcher child token state '$child' (want standard)" }
}
Write-Host "token states OK for $Account"

# A new install as a user meets it: every business type must set up a company, restart
# exactly when it needs modules that are not running, and run them (smoke_records.py first-run).
if (-not $Previous -and -not $Demo) {
  $port = Launch $exe "new install" -NewInstall
  $pf = Get-ChildItem -Path $udd -Recurse -Filter "api-port" -EA SilentlyContinue | Select-Object -First 1
  $firstRun = Join-Path $pub "celerp-first-run.out"
  python ..\scripts\smoke_records.py first-run $pf.FullName ..\default_modules\celerp-verticals\celerp_verticals\presets > $firstRun
  $firstRunCode = $LASTEXITCODE
  Get-Content $firstRun -EA SilentlyContinue | Write-Host
  if ($firstRunCode -ne 0) { AppOutput (Join-Path $pub "celerp-boot.out"); Fail "setting up a new install: smoke_records.py first-run exited $firstRunCode" }
  ForceQuit $installDir (Get-Content $pf.FullName -Raw).Trim() "new install"
}

$port = Launch $exe "first launch"
Write-Host "boot smoke OK on Windows ($smoke, account $u, port $port)"

# A new database under a profile path with letters outside ASCII is set up in the
# account's private folder, which only the account, SYSTEM and administrators can open.
if ((-not $Previous -or $PreviousFails) -and $profileDir -notmatch '^[\x20-\x7e]+$') {
  if (-not (Test-Path (Join-Path $private "postgres\PG_VERSION"))) { Fail "no database in the private folder $private" }
  $acl = Get-Acl $private
  $others = @($acl.Access | Where-Object { $_.IsInherited -or $_.IdentityReference.Value -notin @("$env:COMPUTERNAME\$u", "NT AUTHORITY\SYSTEM", "BUILTIN\Administrators") })
  if ($others.Count) { $acl.Access | Format-Table | Out-String | Write-Host; Fail "the private folder $private is open to others" }
  Write-Host "smoke: the database is in $private, open only to $u, SYSTEM and administrators"
}

if ($Previous -and -not $PreviousFails) {
  Records snapshot $port (Join-Path $pub "celerp-candidate.snapshot")
  python ..\scripts\smoke_records.py carried (Join-Path $pub "celerp-previous.snapshot") (Join-Path $pub "celerp-candidate.snapshot")
  if ($LASTEXITCODE -ne 0) { Fail "the upgrade from the previous release did not keep its records" }
}

if ($Demo) {
  $work = Join-Path $env:RUNNER_TEMP "celerp-backup-restore"
  $pf = Get-ChildItem -Path $udd -Recurse -Filter "api-port" -EA SilentlyContinue | Select-Object -First 1
  # The sample turns on modules, so Celerp restarts onto a new port during it.
  $demoOut = Join-Path $pub "celerp-demo.out"
  python ..\scripts\smoke_records.py demo $pf.FullName (Join-Path $env:RUNNER_TEMP "celerp-demo") ..\celerp\importers\sample\sample.manager $u > $demoOut
  $demoCode = $LASTEXITCODE
  Get-Content $demoOut -EA SilentlyContinue | Write-Host
  if ($demoCode -ne 0) { Fail "smoke_records.py demo exited $demoCode" }
  $port = (Get-Content $pf.FullName -Raw).Trim()
  Records backup-restore $port (Join-Path $pub "celerp-recovery.out") $work
  Get-Content (Join-Path $pub "celerp-recovery.out") | Write-Host
  # The restart moves the API to a new port: the app must publish it, and serve there.
  $watch = [Diagnostics.Stopwatch]::StartNew()
  $deadline = (Get-Date).AddSeconds(240)
  $restarted = $null
  while (-not $restarted -and (Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $now = (Get-Content $pf.FullName -Raw -EA SilentlyContinue)
    if ($now -and $now.Trim() -ne $port) {
      try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://127.0.0.1:$($now.Trim())/health/ready"; if ($r.Content -match '"db"\s*:\s*"ok"') { $restarted = $now.Trim() } } catch { }
    }
  }
  if (-not $restarted) { Fail "after the restore and restart, $($pf.FullName) still names port $port, or the new port does not answer db:ok" }
  $watch.Stop(); Timing "restart after the restore, to db:ok" $watch
  $port = $restarted
  Records restored $port (Join-Path $pub "celerp-recovery.check") $work
  Get-Content (Join-Path $pub "celerp-recovery.check") | Write-Host

  # Closing and opening it again keeps everything.
  Records snapshot $port (Join-Path $pub "celerp-open.snapshot")
  ForceQuit $installDir $port "before reopening"
  $port = Launch $exe "reopen"
  Records snapshot $port (Join-Path $pub "celerp-reopened.snapshot")
  python ..\scripts\smoke_records.py carried (Join-Path $pub "celerp-open.snapshot") (Join-Path $pub "celerp-reopened.snapshot")
  if ($LASTEXITCODE -ne 0) { Fail "reopening the app did not serve the same records" }
}

if ($OverRunning) {
  $before = RunningApp $installDir $port "before installing over the running app"
  $installDir = InstallSetup $setup "install over the running app ($($before.Count) app processes)"
  # Windows reuses PIDs, and a process the smoke cannot read reports no StartTime or Path, so a survivor is
  # the same PID with the same readable start time and the same executable as recorded before the install.
  $left = @($before | Where-Object {
    $p = Get-Process -Id $_.Id -EA SilentlyContinue
    $p -and $null -ne $p.StartTime -and $p.StartTime -eq $_.StartTime -and $p.Path -and $p.Path -eq $_.Path
  })
  if ($left.Count) { $left | Format-Table Id, Name, Path | Out-String | Write-Host; Fail "the installer left the app running" }
  TreeMatches $installDir "install over the running app"
  $port = Launch $exe "launch after installing over the running app"
}

ForceQuit $installDir $port "last launch"
