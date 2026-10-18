; Copyright (c) 2026 Noah Severs
; SPDX-License-Identifier: BUSL-1.1
;
; Celerp additions to the Windows installer:
;   - the window title shows the version this file installs;
;   - run over an existing install, it says what will happen: an update (data is
;     kept), a reinstall of the same version, or, for an older file, that a newer
;     Celerp is already installed, and stops.
; The in-app updater runs the installer with --updated and is never prompted.

!include WordFunc.nsh

!macro customHeader
  Caption "${PRODUCT_NAME} ${VERSION} Setup"
!macroend

; "2.5.4-dev.3+gabc" -> "2.5.4": VersionCompare reads dotted numbers only.
!macro celerpCoreVersion VAR
  Push $R8
  Push $R9
  StrCpy $R9 0
  ${Do}
    StrCpy $R8 ${VAR} 1 $R9
    ${If} $R8 == ""
    ${OrIf} $R8 == "-"
    ${OrIf} $R8 == "+"
      ${ExitDo}
    ${EndIf}
    IntOp $R9 $R9 + 1
  ${Loop}
  StrCpy ${VAR} ${VAR} $R9
  Pop $R9
  Pop $R8
!macroend

!macro customInit
  ${IfNot} ${isUpdated}
  ${AndIfNot} ${UAC_IsInnerInstance}
    ReadRegStr $R0 HKCU "${UNINSTALL_REGISTRY_KEY}" "DisplayVersion"
    ${If} $R0 == ""
      ReadRegStr $R0 HKLM "${UNINSTALL_REGISTRY_KEY}" "DisplayVersion"
    ${EndIf}
    ${If} $R0 != ""
      StrCpy $R1 $R0
      !insertmacro celerpCoreVersion $R1
      StrCpy $R2 "${VERSION}"
      !insertmacro celerpCoreVersion $R2
      ${VersionCompare} $R1 $R2 $R3
      ; $R3: 0 same, 1 installed is newer, 2 installed is older
      ${If} $R3 == 1
        MessageBox MB_OK|MB_ICONEXCLAMATION "A newer Celerp ($R0) is already installed. This file is an older version (${VERSION}). Open Celerp from the Start menu; it keeps itself up to date." /SD IDOK
        SetErrorLevel 2
        Quit
      ${ElseIf} $R3 == 0
        MessageBox MB_YESNO|MB_ICONQUESTION "Celerp ${VERSION} is already installed. Reinstall?" /SD IDYES IDYES celerpReinstall
        SetErrorLevel 1
        Quit
        celerpReinstall:
      ${Else}
        MessageBox MB_OKCANCEL|MB_ICONINFORMATION "Celerp $R0 is installed. This will update it to ${VERSION}. Your data is kept." /SD IDOK IDOK celerpUpdate
        SetErrorLevel 1
        Quit
        celerpUpdate:
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

; ---------------------------------------------------------------------------------
; Replacing an install without losing it.
;
; Stock electron-builder uninstalls the installed version first and then copies the
; new one, so a copy that fails part way (a full disk, a stopped setup) leaves no
; working Celerp. Here the new files are copied in full into a folder next to the
; install folder, and only then swapped in by two renames: the installed folder
; moves aside, the new one takes its name, and the old copy is uninstalled and
; removed last. A failed copy deletes the new folder and the installed version is
; untouched. The two sibling names have the install folder's length, so no
; installed path gets longer (a Program Files install may sit at the 259 character
; limit). A marker file next to them records that a swap is under way, so the next
; setup can finish or undo one that was stopped, and never touches folders it did
; not make. An installed version is only ever replaced this way: when setup cannot
; write its marker, or finds a sibling folder or marker it did not make, it stops
; and leaves the installed version and what it found as they were.
;
; Before anything is changed, setup checks that every volume it writes to has room
; for what it writes there, and stops with a message saying so if one does not.
; ---------------------------------------------------------------------------------

!include FileFunc.nsh

!ifndef BUILD_UNINSTALLER
  ; swap: replace an installed Celerp; fresh: install into a missing or empty folder;
  ; direct: copy straight into a folder that holds something else (stock behaviour).
  Var celerpMode
  Var celerpStage
  Var celerpOld
  Var celerpMarker
  Var celerpMarkerText
  Var celerpParent
  Var celerpName
  Var celerpTry
  Var celerpFile
  Var celerpTempVol
  Var celerpTempFree
  Var celerpTempNeed
  Var celerpDirVol
  Var celerpDirFree
  Var celerpDirNeed
  Var celerpStoreVol
  Var celerpStoreFree
  Var celerpStoreNeed
  Var celerpPid
  Var celerpFind
  Var celerpBytes
  Var celerpKb
!endif

; A setup that was stopped (ended in Task Manager, signed out, power lost) cannot
; delete its temporary folder, which holds the unpacked app. Each setup marks its
; own folder with its process ID, and removes marked folders whose setup is no
; longer running. Folders without the mark belong to other installers and are left.
!macro celerpCleanStoppedSetups
  System::Call 'kernel32::GetCurrentProcessId() i .s'
  Pop $celerpPid
  FileOpen $celerpFile "$PLUGINSDIR\celerp-setup.pid" w
  FileWrite $celerpFile "$celerpPid"
  FileClose $celerpFile
  ${GetParent} "$PLUGINSDIR" $celerpParent
  FindFirst $celerpFind $celerpName "$celerpParent\ns*.tmp"
  ${DoWhile} $celerpName != ""
    ${If} "$celerpParent\$celerpName" != "$PLUGINSDIR"
    ${AndIf} ${FileExists} "$celerpParent\$celerpName\celerp-setup.pid"
      FileOpen $celerpFile "$celerpParent\$celerpName\celerp-setup.pid" r
      FileRead $celerpFile $celerpPid
      FileClose $celerpFile
      IntOp $celerpPid $celerpPid + 0
      ; PROCESS_QUERY_LIMITED_INFORMATION. Error 87 means no such process; any other
      ; failure (access denied) means it runs. A process that exited reports 259 no more.
      System::Call 'kernel32::OpenProcess(i 0x1000, i 0, i $celerpPid) p .s ?e'
      Pop $celerpTry
      Pop $celerpFile
      ${If} $celerpFile == 0
        ${If} $celerpTry == 87
          RMDir /r "$celerpParent\$celerpName"
        ${EndIf}
      ${Else}
        System::Call 'kernel32::GetExitCodeProcess(p $celerpFile, *i .s)'
        Pop $celerpTry
        System::Call 'kernel32::CloseHandle(p $celerpFile)'
        ${If} $celerpTry != 259
          RMDir /r "$celerpParent\$celerpName"
        ${EndIf}
      ${EndIf}
    ${EndIf}
    FindNext $celerpFind $celerpName
  ${Loop}
  FindClose $celerpFind
!macroend

; Picks how this install replaces what is in $INSTDIR, after finishing or undoing a
; swap an earlier setup started and did not end.
!macro celerpPrepareReplace
  StrCpy $celerpMode "direct"
  ; Setup starts in $INSTDIR, and a process cannot move or delete its working folder.
  ; It works from the folder that holds its temporary folder, so that it can still
  ; delete that temporary folder when it stops.
  SetOutPath $TEMP
  ${GetParent} "$INSTDIR" $celerpParent
  StrLen $celerpTry $celerpParent
  IntOp $celerpTry $celerpTry + 1
  StrCpy $celerpName "$INSTDIR" "" $celerpTry
  StrLen $celerpTry $celerpName
  StrCpy $celerpFile $celerpName 1
  ${If} $celerpParent != ""
  ${AndIf} $celerpTry >= 2
  ${AndIf} $celerpFile != "~"
    StrCpy $celerpName $celerpName "" 2
    StrCpy $celerpStage "$celerpParent\~N$celerpName"
    StrCpy $celerpOld "$celerpParent\~O$celerpName"
    StrCpy $celerpMarker "$celerpParent\~S$celerpName"
    StrCpy $celerpMarkerText "${PRODUCT_NAME} setup is replacing $INSTDIR"
    ; Only a marker this setup would write is setup's own (a folder cannot be read).
    ; It is UTF-16, as FileWrite would turn letters outside the code page into "?".
    StrCpy $celerpFile ""
    ClearErrors
    FileOpen $celerpFind "$celerpMarker" r
    ${IfNot} ${Errors}
      FileReadUTF16LE $celerpFind $celerpFile
      FileClose $celerpFind
    ${EndIf}
    ${If} $celerpFile == $celerpMarkerText
      ${If} ${FileExists} "$celerpOld\*.*"
        ; setup itself creates $INSTDIR empty when it starts
        ${DirState} "$INSTDIR" $celerpTry
        ${If} $celerpTry == 1
          RMDir /r "$celerpOld"
        ${Else}
          RMDir "$INSTDIR"
          Rename "$celerpOld" "$INSTDIR"
        ${EndIf}
      ${EndIf}
      RMDir /r "$celerpStage"
      ${IfNot} ${FileExists} "$celerpOld\*.*"
      ${AndIfNot} ${FileExists} "$celerpStage\*.*"
        Delete "$celerpMarker"
      ${EndIf}
    ${EndIf}
    ${If} ${FileExists} "$celerpMarker"
    ${OrIf} ${FileExists} "$celerpStage"
    ${OrIf} ${FileExists} "$celerpOld"
      DetailPrint "Left as found: $celerpMarker, $celerpStage, $celerpOld"
      ${If} ${FileExists} "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
        MessageBox MB_OK|MB_ICONEXCLAMATION "${PRODUCT_NAME} setup replaces the installed version through new folders next to $INSTDIR, and found $celerpStage, $celerpOld or $celerpMarker there, which it did not make. Move them somewhere else, then run setup again. The installed version is as it was." /SD IDOK
        SetErrorLevel 2
        Quit
      ${EndIf}
    ${ElseIf} ${FileExists} "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
      StrCpy $celerpMode "swap"
    ${Else}
      ${DirState} "$INSTDIR" $celerpTry
      ${If} $celerpTry != 1
        StrCpy $celerpMode "fresh"
      ${EndIf}
    ${EndIf}
  ${EndIf}
  ${If} $celerpMode == "direct"
  ${AndIf} ${FileExists} "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
    MessageBox MB_OK|MB_ICONEXCLAMATION "${PRODUCT_NAME} setup cannot replace the installed version in $INSTDIR safely. Install into another folder, or uninstall ${PRODUCT_NAME} first (your data is kept). The installed version is as it was." /SD IDOK
    SetErrorLevel 2
    Quit
  ${EndIf}
!macroend

; VOL gets the root of the volume that holds PATH and FREE its free MB for this
; account; both stay "" when Windows cannot say.
!macro celerpFreeSpace PATH VOL FREE
  StrCpy ${VOL} ""
  StrCpy ${FREE} ""
  System::Call 'kernel32::GetVolumePathNameW(w "${PATH}", w .s, i ${NSIS_MAX_STRLEN}) i .s'
  Pop $celerpTry
  Pop $celerpFile
  ${If} $celerpTry != 0
    System::Call 'kernel32::GetDiskFreeSpaceExW(w "$celerpFile", *l .s, p 0, p 0) i .s'
    Pop $celerpTry
    Pop ${FREE}
    ${If} $celerpTry != 0
      System::Int64Op ${FREE} / 1048576
      Pop ${FREE}
      StrCpy ${VOL} $celerpFile
    ${Else}
      StrCpy ${FREE} ""
    ${EndIf}
  ${EndIf}
!macroend

!macro celerpRequireSpace VOL FREE NEED
  ${If} ${VOL} != ""
  ${AndIf} ${NEED} > 0
    ; room for file system overhead, the uninstaller and the shortcuts
    IntOp ${NEED} ${NEED} + 64
    ${If} ${FREE} < ${NEED}
      MessageBox MB_OK|MB_ICONEXCLAMATION "There is not enough free space on ${VOL} to install ${PRODUCT_NAME}. Setup needs ${NEED} MB there and ${FREE} MB is free. Free up space, then run setup again. Nothing has been changed." /SD IDOK
      SetErrorLevel 2
      Quit
    ${EndIf}
  ${EndIf}
!macroend

; Setup writes the packed app and unpacks it in its temporary folder, copies the
; unpacked app to the install volume (the installed version stays until the copy is
; complete), and keeps a copy of this setup file under %LOCALAPPDATA%. The setup
; file's size bounds the packed app's.
!macro celerpCheckDiskSpace
  !ifdef APP_64_UNPACKED_SIZE
    IntOp $celerpDirNeed ${APP_64_UNPACKED_SIZE} / 1024
    IntOp $celerpDirNeed $celerpDirNeed + 1
    FileOpen $celerpFile "$EXEPATH" r
    FileSeek $celerpFile 0 END $celerpStoreNeed
    FileClose $celerpFile
    IntOp $celerpStoreNeed $celerpStoreNeed / 1048576
    IntOp $celerpStoreNeed $celerpStoreNeed + 1
    IntOp $celerpTempNeed $celerpStoreNeed + $celerpDirNeed

    ${If} $installMode == "all"
      SetShellVarContext current
    ${EndIf}
    ${If} ${FileExists} "$LOCALAPPDATA\${APP_INSTALLER_STORE_FILE}"
      FileOpen $celerpFile "$LOCALAPPDATA\${APP_INSTALLER_STORE_FILE}" r
      FileSeek $celerpFile 0 END $celerpTry
      FileClose $celerpFile
      IntOp $celerpTry $celerpTry / 1048576
      IntOp $celerpStoreNeed $celerpStoreNeed - $celerpTry
    ${EndIf}
    !insertmacro celerpFreeSpace "$LOCALAPPDATA" $celerpStoreVol $celerpStoreFree
    ${If} $installMode == "all"
      SetShellVarContext all
    ${EndIf}
    !insertmacro celerpFreeSpace "$PLUGINSDIR" $celerpTempVol $celerpTempFree
    !insertmacro celerpFreeSpace "$INSTDIR" $celerpDirVol $celerpDirFree

    ${If} $celerpDirVol == $celerpTempVol
      IntOp $celerpTempNeed $celerpTempNeed + $celerpDirNeed
      StrCpy $celerpDirNeed 0
    ${EndIf}
    ${If} $celerpStoreVol == $celerpTempVol
      IntOp $celerpTempNeed $celerpTempNeed + $celerpStoreNeed
      StrCpy $celerpStoreNeed 0
    ${ElseIf} $celerpStoreVol == $celerpDirVol
      IntOp $celerpDirNeed $celerpDirNeed + $celerpStoreNeed
      StrCpy $celerpStoreNeed 0
    ${EndIf}
    !insertmacro celerpRequireSpace $celerpTempVol $celerpTempFree $celerpTempNeed
    !insertmacro celerpRequireSpace $celerpDirVol $celerpDirFree $celerpDirNeed
    !insertmacro celerpRequireSpace $celerpStoreVol $celerpStoreFree $celerpStoreNeed
  !endif
!macroend

; Starts the copy: into the new sibling folder, or into $INSTDIR in direct mode. A
; replacement goes ahead only with its marker written and no sibling in its way; an
; install into an empty folder can do without them.
!macro celerpBeginCopy
  ${If} $celerpMode != "direct"
    CreateDirectory "$celerpParent"
    ClearErrors
    ${If} ${FileExists} "$celerpMarker"
    ${OrIf} ${FileExists} "$celerpStage"
    ${OrIf} ${FileExists} "$celerpOld"
      SetErrors
    ${Else}
      FileOpen $celerpFile "$celerpMarker" w
    ${EndIf}
    ${If} ${Errors}
      ${If} $celerpMode == "swap"
        MessageBox MB_OK|MB_ICONEXCLAMATION "${PRODUCT_NAME} setup could not start a new folder next to $INSTDIR to replace the installed version. Check that you can create files in $celerpParent, then run setup again. The installed version is as it was." /SD IDOK
        SetErrorLevel 2
        Quit
      ${EndIf}
      StrCpy $celerpMode "direct"
    ${Else}
      FileWriteUTF16LE $celerpFile "$celerpMarkerText"
      FileClose $celerpFile
    ${EndIf}
  ${EndIf}
  ${If} $celerpMode == "direct"
    SetOutPath $INSTDIR
  ${Else}
    SetOutPath $celerpStage
  ${EndIf}
!macroend

; A copy that did not finish: the new folder goes and the installed version stays.
!macro celerpAbandonCopy
  ${If} $celerpMode != "direct"
    ; a process cannot delete its own working folder
    SetOutPath $TEMP
    RMDir /r "$celerpStage"
    ${IfNot} ${FileExists} "$celerpOld\*.*"
      Delete "$celerpMarker"
    ${EndIf}
  ${EndIf}
!macroend

; The 7-Zip plugin reports no error when the disk fills while it unpacks, so the
; unpacked files are counted against the size electron-builder recorded for the app
; (the sum of its file sizes, in KB rounded up) before anything is copied. A short
; unpack stops setup instead of putting a partial app in place. package.json sets
; nsis.preCompressedFileExtensions to [] so that every file is in the archive.
!macro celerpCheckUnpacked
  !ifdef APP_64_UNPACKED_SIZE
    ${If} $packageArch == "64"
      ${GetSize} "$PLUGINSDIR\7z-out" "/S=0B" $celerpBytes $celerpFile $celerpFind
      System::Int64Op $celerpBytes + 1023
      Pop $celerpKb
      System::Int64Op $celerpKb / 1024
      Pop $celerpKb
      ${If} $celerpKb != ${APP_64_UNPACKED_SIZE}
        MessageBox MB_OK|MB_ICONEXCLAMATION "${PRODUCT_NAME} could not unpack all of its files, usually because the disk is full. Free up space, then run setup again. The installed version is as it was." /SD IDOK
        !insertmacro celerpAbandonCopy
        SetErrorLevel 2
        Quit
      ${EndIf}
    ${EndIf}
  !endif
!macroend

; Every new file is in place in the sibling folder: swap it in.
!macro celerpSwapIn
  ${If} $celerpMode != "direct"
    SetOutPath $TEMP
    ${If} $celerpMode == "swap"
      StrCpy $celerpTry 0
      ${Do}
        ClearErrors
        Rename "$INSTDIR" "$celerpOld"
        ${IfNot} ${Errors}
          ${ExitDo}
        ${EndIf}
        IntOp $celerpTry $celerpTry + 1
        ${If} $celerpTry < 10
          Sleep 1000
        ${ElseIf} ${Cmd} `MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "${PRODUCT_NAME} could not replace the files in $INSTDIR because one of them is in use. Close ${PRODUCT_NAME} and any window open on that folder, then click Retry. Cancel leaves the installed version as it is." /SD IDCANCEL IDCANCEL`
          !insertmacro celerpAbandonCopy
          SetErrorLevel 2
          Quit
        ${Else}
          StrCpy $celerpTry 0
        ${EndIf}
      ${Loop}
    ${Else}
      RMDir "$INSTDIR"
    ${EndIf}
    ClearErrors
    Rename "$celerpStage" "$INSTDIR"
    ${If} ${Errors}
      ${If} $celerpMode == "swap"
        Rename "$celerpOld" "$INSTDIR"
      ${EndIf}
      !insertmacro celerpAbandonCopy
      MessageBox MB_OK|MB_ICONEXCLAMATION "${PRODUCT_NAME} could not move its new files into $INSTDIR. The installed version is as it was." /SD IDOK
      SetErrorLevel 2
      Quit
    ${EndIf}
    SetOutPath $INSTDIR
    ; The packed and unpacked app are no longer needed, and the old uninstaller may
    ; need the room in the temporary folder to move the old files aside.
    RMDir /r "$PLUGINSDIR\7z-out"
    Delete "$PLUGINSDIR\app-$packageArch.${COMPRESSION_METHOD}"
  ${EndIf}
!macroend

; The version being replaced has moved aside: its uninstaller runs there. That
; uninstaller takes its folder from InstallLocation, not from the folder it is given,
; so InstallLocation names the moved folder while it runs; registryAddInstallInfo
; then sets it back to $INSTDIR.
!macro celerpOldVersionLocation
  ${If} $celerpMode == "swap"
  ${AndIf} $installationDir == $INSTDIR
    StrLen $celerpTry $installationDir
    StrCpy $celerpFile $uninstallerFileName $celerpTry
    ${If} $celerpFile == $installationDir
      StrCpy $celerpFile $uninstallerFileName "" $celerpTry
      StrCpy $uninstallerFileName "$celerpOld$celerpFile"
      StrCpy $installationDir $celerpOld
      ${If} $rootKey == "HKEY_CURRENT_USER"
        WriteRegStr HKEY_CURRENT_USER "${INSTALL_REGISTRY_KEY}" InstallLocation "$celerpOld"
      ${Else}
        WriteRegStr SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" InstallLocation "$celerpOld"
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

!macro celerpUninstallOldVersions
  !insertmacro uninstallOldVersion SHELL_CONTEXT
  !insertmacro handleUninstallResult SHELL_CONTEXT
  ${if} $installMode == "all"
    !insertmacro uninstallOldVersion HKEY_CURRENT_USER
    !insertmacro handleUninstallResult HKEY_CURRENT_USER
  ${endIf}
!macroend

; Once the new version is in place, an old uninstaller that fails is reported, not
; fatal: what it leaves in the old folder is removed with the folder.
!macro celerpUninstallCheck
  ${If} ${Errors}
    DetailPrint `Uninstall was not successful. Not able to launch uninstaller!`
  ${ElseIf} $R0 != 0
    ${If} $celerpMode == "direct"
      MessageBox MB_OK|MB_ICONEXCLAMATION "$(uninstallFailed): $R0"
      SetErrorLevel 2
      Quit
    ${EndIf}
    DetailPrint `Uninstall was not successful. Uninstaller error code: $R0.`
  ${EndIf}
!macroend

!macro customUnInstallCheck
  !insertmacro celerpUninstallCheck
!macroend

!macro customUnInstallCheckCurrentUser
  !insertmacro celerpUninstallCheck
!macroend

!macro celerpFinishReplace
  ${If} $celerpMode == "swap"
    RMDir /r "$celerpOld"
  ${EndIf}
  ${If} $celerpMode != "direct"
  ${AndIfNot} ${FileExists} "$celerpOld\*.*"
    Delete "$celerpMarker"
  ${EndIf}
!macroend
