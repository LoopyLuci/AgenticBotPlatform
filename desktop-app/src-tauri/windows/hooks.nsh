; BotServer's own icon lives as a standalone external .ico file
; ($INSTDIR\icon.ico, bundled via tauri.conf.json's "resources") rather
; than only the one baked into ${MAINBINARYNAME}.exe's own PE resources.
; Windows shortcuts can point at an external icon file (CreateShortcut's
; icon-file parameter) instead of always extracting the target exe's own
; icon — so recreating the installer's default shortcuts with that path
; means a future icon change is just "overwrite icon.ico and re-pin the
; shortcut," never a rebuild+reinstall. This runs AFTER Tauri's own
; installer.nsi already created its default shortcuts (NSIS_HOOK_POSTINSTALL
; fires once every file is copied and the default shortcuts exist), so it
; deletes and recreates them rather than creating from scratch.
;
; The Desktop shortcut is optional (the installer's own UI lets the user
; skip it), so it's only recreated if the default installer actually made
; one — this hook must never create a shortcut the user opted out of.
;
; Caveat this hook alone can't close: for a normal interactive install,
; Tauri's own template creates the Desktop shortcut from the FINISH
; PAGE's "create desktop shortcut" checkbox, which runs AFTER this
; NSIS_HOOK_POSTINSTALL — so this fixes the Desktop shortcut only for
; silent/passive installs (where it's created earlier, in the same
; section as this hook). The real fix for the interactive-install case
; is fix_shortcut_icons() in lib.rs, which self-heals both shortcuts'
; icons at every app launch, independent of install-time ordering. This
; hook stays as a belt-and-suspenders fix for the cases it does cover.
!macro NSIS_HOOK_POSTINSTALL
  Delete "$SMPROGRAMS\${PRODUCTNAME}.lnk"
  CreateShortcut "$SMPROGRAMS\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe" "" "$INSTDIR\icon.ico" 0

  IfFileExists "$DESKTOP\${PRODUCTNAME}.lnk" recreate_desktop skip_desktop
  recreate_desktop:
    Delete "$DESKTOP\${PRODUCTNAME}.lnk"
    CreateShortcut "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe" "" "$INSTDIR\icon.ico" 0
  skip_desktop:
!macroend
