#!/usr/bin/env bash
# ============================================================================
# Controlla le firme dell'app FINITA: ogni Mach-O dentro Contents/Resources e
# l'app intera (codesign --deep --strict). Accetta un .app o un .dmg: il dmg
# viene montato in sola lettura in una cartella temporanea e si controllano le
# app che contiene. Serve perche' Tauri copia il backend nelle risorse e una
# firma valida prima della copia puo' non esserlo dopo; con il solo bundle dmg,
# inoltre, Tauri cancella il .app a fine giro.
#
# Uso:  bash scripts/verifica_app_macos.sh <app-o-dmg> [<app-o-dmg> ...]
# Lo chiama build_macos.sh; la CI firma-macos lo prova su un .app e su un .dmg.
# ============================================================================
set -euo pipefail

verifica_app() {
  local app="$1" bad=0 f
  echo ">> controllo le firme dentro $app"
  [ -d "$app/Contents/Resources" ] || { echo "ERRORE: $app/Contents/Resources mancante"; return 1; }
  while IFS= read -r -d '' f; do
    case "$(file -b "$f")" in
      *Mach-O*) codesign --verify --strict "$f" 2>/dev/null \
                  || { echo "FIRMA NON VALIDA: $f"; bad=1; } ;;
    esac
  done < <(find "$app/Contents/Resources" -type f -print0)
  codesign --verify --deep --strict "$app" || bad=1
  return "$bad"
}

ok=1
controllate=0
for x in "$@"; do
  case "$x" in
    *.dmg)
      MNT="$(mktemp -d "${TMPDIR:-/tmp}/rizzo-dmg.XXXXXX")"
      hdiutil attach -nobrowse -readonly -noautoopen -mountpoint "$MNT" "$x" >/dev/null
      for a in "$MNT"/*.app; do
        [ -d "$a" ] || continue
        verifica_app "$a" || ok=0
        controllate=$((controllate + 1))
      done
      hdiutil detach "$MNT" -force >/dev/null 2>&1 || true
      rmdir "$MNT" 2>/dev/null || true
      ;;
    *)
      verifica_app "$x" || ok=0
      controllate=$((controllate + 1))
      ;;
  esac
done
[ "$controllate" -gt 0 ] || { echo "ERRORE: nessuna app trovata da controllare"; exit 1; }
[ "$ok" -eq 1 ] || { echo "ERRORE: firme non valide nell'app finita"; exit 1; }
echo ">> firme dell'app valide ($controllate controllate)"
