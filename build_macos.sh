#!/usr/bin/env bash
# ============================================================================
# Build dell'app desktop Rizzo PII per macOS (.app + .dmg), CPU/offline.
#
# Speculare a build_linux.sh: stesso build_sidecar.spec, sidecar 'pii-backend'
# senza estensione (il Rust in lib.rs sceglie il nome col cfg!(windows)), e
# bundle 'app'/'dmg' invece di deb/appimage. La conf Tauri resta su nsis per
# Windows: qui si sovrascrive da CLI con --bundles.
#
# Uso:
#   bash build_macos.sh            # .app + .dmg
#   bash build_macos.sh app        # solo il .app (piu' veloce, per testare)
#
# Prerequisiti (una volta):
#   - Xcode Command Line Tools:  xcode-select --install   (anche swiftc, per l'OCR)
#   - Rust (https://rustup.rs) e Node.js 18+ (con npm)
# Il modello e' gitignorato: dev'essere presente in $MODEL_DIR.
#
# NOTA firma: con una "Developer ID Application" nel portachiavi lo script firma
# tutti i binari del backend e il .dmg (vedi passo 3); senza, la build e' ad-hoc:
# in locale si apre, su un altro Mac serve "tasto destro > Apri" oppure
#   xattr -dr com.apple.quarantine "Rizzo PII.app"
# OCR: il passo 1b compila l'eseguibile Swift dell'OCR (framework Vision).
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

MODEL_DIR="models/rizzo-pii-0.3B-v1.5.0"     # deve combaciare con build_sidecar.spec
VENV="${VENV:-.venv}"                      # override: VENV=build_env_macos bash build_macos.sh
BUNDLES="${*:-app dmg}"

# ---- 0) controlli ----------------------------------------------------------
[ "$(uname -s)" = "Darwin" ] || { echo "ERRORE: questo script gira solo su macOS"; exit 1; }
[ -d "$MODEL_DIR" ] || { echo "ERRORE: modello mancante: $MODEL_DIR"; exit 1; }
command -v cargo >/dev/null || { echo "ERRORE: Rust/cargo non trovato (https://rustup.rs)"; exit 1; }
command -v npm   >/dev/null || { echo "ERRORE: npm non trovato (installa Node.js 18+)"; exit 1; }
command -v swiftc >/dev/null || { echo "ERRORE: swiftc non trovato (xcode-select --install)"; exit 1; }

# ---- 1) venv con le dipendenze (su macOS torch e' gia' CPU/MPS, niente indice extra) ----
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip
  "$VENV/bin/pip" install "torch==2.13.0" "transformers==4.57.3" "tokenizers==0.22.2" "safetensors==0.8.0" "flask==3.1.3" "pymupdf==1.28.2" "huggingface_hub==0.36.2" "pyinstaller==6.22.3"
fi
"$VENV/bin/python" -c "import PyInstaller" 2>/dev/null \
  || "$VENV/bin/pip" install "pyinstaller==6.22.3"

# ---- 1b) OCR delle scansioni: eseguibile Swift col framework Vision ---------
# Nessuna dipendenza Python: Vision e' nel sistema. Va in build/ocr/ e il spec lo
# copia nel sidecar (_internal/ocr/rizzo-ocr). Architettura della macchina che
# compila (arm64 su Apple Silicon), come il resto del sidecar.
mkdir -p build/ocr
swiftc -O -target "$(uname -m)-apple-macos11" \
  -o build/ocr/rizzo-ocr src/app/ocr_macos/main.swift
build/ocr/rizzo-ocr --version

# ---- 2) sidecar PyInstaller -> tauri/src-tauri/backend/pii-backend/pii-backend ----
rm -rf tauri/src-tauri/backend
"$VENV/bin/pyinstaller" build_sidecar.spec --noconfirm \
  --distpath tauri/src-tauri/backend --workpath build/sidecar_work_macos
[ -f tauri/src-tauri/backend/pii-backend/pii-backend ] \
  || { echo "ERRORE: sidecar macOS non prodotto"; exit 1; }
[ -x tauri/src-tauri/backend/pii-backend/_internal/ocr/rizzo-ocr ] \
  || { echo "ERRORE: OCR non incluso nel sidecar"; exit 1; }

# ---- 3) firma + notarizzazione (opzionali, attivate dall'ambiente) ---------
# FIRMA: se nel portachiavi c'e' una "Developer ID Application", Tauri la usa e
# applica hardened runtime + entitlements.plist. Senza, la build resta ad-hoc
# (gira in locale, ma su un altro Mac serve "tasto destro > Apri").
if [ -z "${APPLE_SIGNING_IDENTITY:-}" ]; then
  APPLE_SIGNING_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
    | sed -n 's/.*"\(Developer ID Application: .*\)".*/\1/p' | head -1)"
fi
if [ -n "$APPLE_SIGNING_IDENTITY" ]; then
  export APPLE_SIGNING_IDENTITY
  echo ">> firma con: $APPLE_SIGNING_IDENTITY"
  # Il backend entra nel bundle come "resources": Tauri firma l'eseguibile
  # principale, non i file Mach-O dentro Resources (librerie di torch, estensioni
  # Python, l'OCR). La notarizzazione li rifiuta se non hanno firma Developer ID,
  # hardened runtime e timestamp: si firmano qui, uno per uno, prima del bundle.
  # Entitlements solo sugli eseguibili (sulle librerie non contano).
  ENT="$ROOT/tauri/src-tauri/entitlements.plist"
  echo ">> firmo i binari del backend (qualche minuto)"
  # scioglie i link simbolici (Tauri li copierebbe come file, con firme non piu'
  # valide: "_internal/Python ... invalid Info.plist") e firma ogni Mach-O.
  # Dettagli in testa allo script.
  bash scripts/firma_backend_macos.sh tauri/src-tauri/backend "$APPLE_SIGNING_IDENTITY" "$ENT"
  [ ! -L tauri/src-tauri/backend/pii-backend/_internal/Python ] \
    || { echo "ERRORE: _internal/Python e' ancora un link simbolico"; exit 1; }
  codesign --verify --strict tauri/src-tauri/backend/pii-backend/pii-backend
  codesign --verify --strict tauri/src-tauri/backend/pii-backend/_internal/ocr/rizzo-ocr
  if [ -e tauri/src-tauri/backend/pii-backend/_internal/Python ]; then
    codesign --verify --strict tauri/src-tauri/backend/pii-backend/_internal/Python
  fi
else
  echo ">> ATTENZIONE: nessuna 'Developer ID Application' nel portachiavi -> firma ad-hoc"
fi

# NOTARIZZAZIONE: serve un profilo di credenziali Apple. Creane uno una volta con
#   xcrun notarytool store-credentials rizzo-pii --apple-id <id> --team-id <team> --password <app-specific-password>
# poi esporta le variabili qui sotto (Tauri notarizza e fa lo staple da solo).
# Con l'app-specific password:  APPLE_ID, APPLE_PASSWORD, APPLE_TEAM_ID
# Con la API key App Store Connect:  APPLE_API_KEY, APPLE_API_ISSUER, APPLE_API_KEY_PATH
if [ -n "${APPLE_ID:-}" ] || [ -n "${APPLE_API_KEY:-}" ]; then
  echo ">> notarizzazione attiva"
else
  echo ">> notarizzazione SALTATA (nessuna credenziale Apple nell'ambiente)"
fi

# ---- 3b) smonta i volumi DMG rimasti appesi -------------------------------
# bundle_dmg.sh fa 'hdiutil attach': se una build precedente e' stata interrotta
# il volume resta montato come /Volumes/dmg.XXXXXX e il bundle fallisce con un
# laconico "failed to run bundle_dmg.sh".
for v in /Volumes/dmg.*; do
  [ -d "$v" ] || continue
  echo ">> smonto volume DMG residuo: $v"
  hdiutil detach "$v" -force >/dev/null 2>&1 || true
done

# ---- 4) build Tauri: la conf di default e' nsis, qui si sovrascrive --------
cd tauri
npm install
npx tauri build --bundles $BUNDLES

# il .dmg si firma anch'esso (notarytool lo accetta anche senza, Gatekeeper preferisce firmato)
if [ -n "$APPLE_SIGNING_IDENTITY" ]; then
  for d in src-tauri/target/release/bundle/dmg/*.dmg; do
    [ -f "$d" ] && codesign --force --timestamp --sign "$APPLE_SIGNING_IDENTITY" "$d"
  done
fi

# controllo sull'app FINITA (anche quella dentro il dmg: con il solo bundle dmg
# Tauri cancella il .app a fine giro). Dettagli in scripts/verifica_app_macos.sh.
if [ -n "$APPLE_SIGNING_IDENTITY" ]; then
  DA_CONTROLLARE=()
  [ -d "src-tauri/target/release/bundle/macos/Rizzo PII.app" ] \
    && DA_CONTROLLARE+=("src-tauri/target/release/bundle/macos/Rizzo PII.app")
  for d in src-tauri/target/release/bundle/dmg/*.dmg; do
    [ -f "$d" ] && DA_CONTROLLARE+=("$d")
  done
  # (bash 3.2 di macOS: un array vuoto con set -u da' "unbound variable")
  [ "${#DA_CONTROLLARE[@]}" -gt 0 ] || { echo "ERRORE: ne' .app ne' .dmg da controllare"; exit 1; }
  bash ../scripts/verifica_app_macos.sh "${DA_CONTROLLARE[@]}"
fi

echo
echo "FATTO. Artefatti macOS in:"
echo "  tauri/src-tauri/target/release/bundle/macos/Rizzo PII.app"
echo "  tauri/src-tauri/target/release/bundle/dmg/*.dmg"
