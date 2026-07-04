# Mobile Scan — on-device test checklist (post web-deploy, pre-APK)
Test at scanhound.turtleland.us in the phone browser first, then the APK.

> **Maintenance note:** haptics need `<uses-permission android:name="android.permission.VIBRATE" />`
> in `frontend/src-tauri/gen/android/app/src/main/AndroidManifest.xml`. That directory is
> git-ignored (generated), so the permission lives on-disk only — after any fresh clone or
> `npm run android:init`, re-add it next to the INTERNET permission or haptics silently no-op.
- [ ] Wall renders 2 cols portrait / 3 landscape; stacked group cards work (tap expands in place)
- [ ] Pull-to-refresh: arrow → armed haptic tick → spinner → list refreshes
- [ ] Swipe a tile right: green underlay, haptic at threshold, "Grabbed" toast, tile marked downloading
- [ ] Swipe a tile left: amber underlay, "Dismissed" toast, Undo restores it
- [ ] Vertical scroll never triggers a horizontal swipe (axis lock) and vice versa
- [ ] Long-press: action sheet opens, no accidental tap-through; Select entry enters selection mode
- [ ] Selection: toolbar swaps to bulk bar; Grab all sends N; Clear exits
- [ ] Tap a tile: detail sheet at half height; drag up = full; drag down/scrim/back = close
- [ ] Detail sheet Grab works; sibling releases switch the sheet
- [ ] Bottom toolbar: search expands + filters open the sheet (genre/language/dates/sort) + Deck opens
- [ ] Deck: swipe cards, close ✕ returns to the wall at the same scroll position
- [ ] Rotate: layout adapts, nothing clipped by notch/home bar (safe-area)
- [ ] Android back button: closes sheet/deck before navigating (KNOWN GAP if not — file follow-up)
- [ ] Desktop browser: literally unchanged (spot-check grid/list/detail/filters)
