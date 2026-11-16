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
# e crea _internal/Python come LINK SIMBOLICO. Tauri, copiando le risorse, segue
# i link: _internal/Python diventa una copia FUORI dal framework con una firma
# legata all'Info.plist del framework, quindi non valida ("invalid Info.plist"),
# e il backend non parte ("[PYI] Failed to load Python shared library ... code
# signature invalid"). Anche il framework stesso, con i suoi link interni
# (Versions/Current, Python), dopo la copia diventa un pacchetto ambiguo che
# codesign rifiuta ("bundle format is ambiguous").
# Soluzione: _internal/Python diventa un file vero (e' quello che il
# bootloader carica) e Python.framework si toglie, ma solo se nessun binario lo
# richiama per percorso (controllo con otool -L). Gli altri link a binari
# diventano copie. Poi ogni Mach-O si firma dov'e', e la firma resta valida
# dopo la copia di Tauri.
# ============================================================================
set -euo pipefail

DIR="$1"
IDENTITY="$2"
ENT="$3"

[ -d "$DIR" ] || { echo "ERRORE: cartella backend mancante: $DIR"; exit 1; }
[ -f "$ENT" ] || { echo "ERRORE: entitlements mancanti: $ENT"; exit 1; }

# 1a) libreria Python fuori dal framework
INTERNAL="$(find "$DIR" -maxdepth 3 -type d -name _internal | head -1)"
if [ -n "$INTERNAL" ] && [ -L "$INTERNAL/Python" ] && [ -d "$INTERNAL/Python.framework" ]; then
  cp "$INTERNAL/Python" "$INTERNAL/Python.tmpcopy"     # cp segue il link
  rm -f "$INTERNAL/Python"
  mv "$INTERNAL/Python.tmpcopy" "$INTERNAL/Python"
  # qualcuno carica la libreria dal percorso del framework?
  usa_fw=""
  while IFS= read -r -d '' f; do
    # la libreria stessa ha come nome interno il percorso nel framework: non conta
    case "$f" in "$INTERNAL/Python.framework/"*|"$INTERNAL/Python") continue ;; esac
    case "$(file -b "$f")" in
      *Mach-O*) otool -L "$f" 2>/dev/null | tail -n +2 | grep -q 'Python\.framework/' \
                  && usa_fw="$usa_fw $f" ;;
    esac
  done < <(find "$DIR" -type f -print0)
  if [ -n "$usa_fw" ]; then
    echo "ERRORE: questi binari richiamano Python.framework per percorso:$usa_fw"
    echo "        il framework non si puo' togliere: serve un'altra soluzione"
    exit 1
  fi
  rm -rf "$INTERNAL/Python.framework"
  echo ">> _internal/Python ora e' un file; Python.framework tolto"
fi

# 1b) altri framework con link interni: Tauri li romperebbe allo stesso modo
if find "$DIR" -type d -name '*.framework' | grep -q .; then
  echo "ERRORE: framework nel backend (Tauri ne copia i link come file):"
  find "$DIR" -type d -name '*.framework'
  exit 1
fi

# 1c) link restanti -> copie vere (prima le cartelle, poi i file)
while IFS= read -r -d '' l; do
  [ -L "$l" ] && [ -d "$l" ] || continue
  cp -RL "$l" "$l.tmpcopy"
  rm -f "$l"
  mv "$l.tmpcopy" "$l"
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
echo ">> altri link a binari sostituiti da copie: $n_link"

# 2) firma, uno per uno (entitlements a tutti: sulle librerie non contano)
TS="--timestamp"
[ "$IDENTITY" = "-" ] && TS="--timestamp=none"
LIST="$(mktemp "${TMPDIR:-/tmp}/rizzo-macho.XXXXXX")"
trap 'rm -f "$LIST"' EXIT
while IFS= read -r -d '' f; do
  case "$(file -b "$f")" in *Mach-O*) printf '%s\0' "$f" >> "$LIST" ;; esac
done < <(find "$DIR" -type f -print0)
echo ">> binari da firmare: $(tr -cd '\0' < "$LIST" | wc -c | tr -d ' ')"
xargs -0 -n 20 -P 8 codesign --force "$TS" --options runtime \
  --entitlements "$ENT" --sign "$IDENTITY" < "$LIST"

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
