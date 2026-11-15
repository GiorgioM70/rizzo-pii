#!/usr/bin/env bash
# ============================================================================
# Firma (Developer ID + hardened runtime + timestamp) di tutti i binari Mach-O
# del backend PyInstaller, PRIMA che Tauri lo copi nel bundle come risorsa.
#
# Uso:  bash scripts/firma_backend_macos.sh <cartella-backend> <identita> <entitlements>
#   <identita> "-" = firma ad-hoc (la usa la CI ocr-macos per provare lo script).
# Lo chiama build_macos.sh (passo 3).
#
# PERCHE' I LINK VANNO SCIOLTI: con un Python "framework" (python.org, Homebrew)
# PyInstaller mette la libreria in _internal/Python.framework/Versions/X/Python
# e crea _internal/Python come LINK SIMBOLICO. Firmata dentro il framework, la
# firma resta legata all'Info.plist del framework. Tauri, copiando le risorse,
# segue i link: _internal/Python diventa una copia del file FUORI dal framework,
# con una firma che non torna ("invalid Info.plist (plist or signature have been
# modified)"), e il backend non parte: "[PYI] Failed to load Python shared
# library ... code signature invalid". Lo stesso vale per i link interni al
# framework (Python.framework/Python, Versions/Current): Tauri li trasforma in
# copie. Qui tutti i link a un Mach-O e i link a cartelle del framework
# diventano copie vere PRIMA di firmare, cosi' ogni file viene firmato dov'e'
# e la firma resta valida dopo la copia di Tauri.
# ============================================================================
set -euo pipefail

DIR="$1"
IDENTITY="$2"
ENT="$3"

[ -d "$DIR" ] || { echo "ERRORE: cartella backend mancante: $DIR"; exit 1; }
[ -f "$ENT" ] || { echo "ERRORE: entitlements mancanti: $ENT"; exit 1; }

# 1) link -> copie vere (prima i link a cartelle, poi i link a file).
#    Un link a una cartella dentro un framework (Versions/Current, Resources,
#    Headers) diventerebbe comunque una copia nel bundle di Tauri.
while IFS= read -r -d '' l; do
  [ -L "$l" ] || continue
  if [ -d "$l" ]; then
    t="$l.tmpcopy"
    cp -RL "$l" "$t"
    rm -f "$l"
    mv "$t" "$l"
  fi
done < <(find "$DIR" -type l -print0)

n_link=0
while IFS= read -r -d '' l; do
  [ -L "$l" ] || continue
  case "$(file -bL "$l")" in
    *Mach-O*)
      cp "$l" "$l.tmpcopy"
      mv -f "$l.tmpcopy" "$l"
      n_link=$((n_link + 1))
      ;;
  esac
done < <(find "$DIR" -type l -print0)
echo ">> link a binari sostituiti da copie: $n_link"

# 2) firma, uno per uno. Entitlements solo sugli eseguibili non servono sulle
#    librerie ma non le danneggiano: si passano a tutti per semplicita'.
#    Le firme dentro i framework restano legate al loro Info.plist (che ora e'
#    una copia vera accanto a ogni copia del binario): si firma il file, non il
#    framework intero, perche' i framework duplicati da Tauri non sono bundle
#    regolari.
TS="--timestamp"
[ "$IDENTITY" = "-" ] && TS="--timestamp=none"
LIST="$(mktemp "${TMPDIR:-/tmp}/rizzo-macho.XXXXXX")"
trap 'rm -f "$LIST"' EXIT
while IFS= read -r -d '' f; do
  case "$(file -b "$f")" in *Mach-O*) printf '%s\0' "$f" >> "$LIST" ;; esac
done < <(find "$DIR" -type f -print0)
echo ">> binari da firmare: $(tr -cd '\0' < "$LIST" | wc -c | tr -d ' ')"
# I binari dei framework si firmano uno alla volta: firmarne due dello stesso
# framework in parallelo farebbe scrivere due processi nello stesso _CodeSignature.
SIGN=(codesign --force "$TS" --options runtime --entitlements "$ENT" --sign "$IDENTITY")
tr '\0' '\n' < "$LIST" | { grep -v '\.framework/' || true; } | tr '\n' '\0' \
  | xargs -0 -n 20 -P 8 "${SIGN[@]}"
tr '\0' '\n' < "$LIST" | { grep '\.framework/' || true; } | tr '\n' '\0' \
  | xargs -0 -n 1 "${SIGN[@]}"

# 3) controlli: nessun link rimasto, tutte le firme valide
if find "$DIR" -type l | grep -q .; then
  echo "ERRORE: link simbolici ancora presenti:"; find "$DIR" -type l; exit 1
fi
bad=0
while IFS= read -r -d '' f; do
  codesign --verify --strict "$f" 2>/dev/null || { echo "FIRMA NON VALIDA: $f"; codesign --verify --strict --verbose=4 "$f" || true; bad=1; }
done < "$LIST"
[ "$bad" -eq 0 ] || { echo "ERRORE: firme non valide nel backend"; exit 1; }
echo ">> firme del backend valide"
