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
