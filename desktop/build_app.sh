#!/usr/bin/env bash
# Build the Voitta RAG menu-bar .app via Briefcase.
#
# The Briefcase manifest is desktop/pyproject.toml — intentionally separate
# from the repo-root pyproject.toml so Briefcase doesn't try to install the
# server's heavy [project].dependencies into the bundle (see desktop/pyproject
# header). Briefcase writes build/ and dist/ next to its manifest (desktop/),
# so this script operates from there. The two source packages are symlinked
# into desktop/src/ → ../../src/ so Briefcase's same-tree `sources` resolve.
#
#   ./desktop/build_app.sh              # DEFAULT: bump + signed + NOTARISED + stapled DMG
#   ./desktop/build_app.sh --no-package # just the .app (no DMG) — fast iteration
#   ./desktop/build_app.sh --no-notarize# signed DMG but skip the Apple submission (faster)
#   ./desktop/build_app.sh --adhoc      # ad-hoc sign, no DMG signing/notarisation
#   ./desktop/build_app.sh --no-bump    # don't bump the version this build
#   ./desktop/build_app.sh --clean      # nuke build/ dist/ wheels/ src symlinks
#   ./desktop/build_app.sh --sign "ID"  # sign with an explicit identity
#
# BY DEFAULT every build produces a *launchable, distributable* DMG: bumps the
# patch version, builds the .app, signs it with the Developer ID identity below,
# NOTARISES it with Apple, and staples the ticket. Notarisation is the default
# because a Developer-ID + hardened-runtime app that ISN'T notarised is rejected
# by Gatekeeper ("Apple cannot check it for malicious software") and will not
# launch from a DMG at all. It submits to Apple and waits ~2–15 min; for fast
# local iteration use --no-package (run the .app straight from the build tree).
#
# The version bump is cheap: the first-run installer keys "already installed"
# off the pinned dependency set, not the app version, so a bump alone does NOT
# trigger a dependency reinstall (see installer._fingerprint).
#
# Default signing identity:
#   "Developer ID Application: roman semeine (KU3WTX9RXB)"
# Notarisation is performed by briefcase (it signs + notarises + staples in one
# `package` step); it uses its own stored notary credentials, already set up on
# this machine. Re-store them with `briefcase` if it ever prompts.
#
# Output:
#   desktop/build/voitta-rag-desktop/macos/app/Voitta RAG.app
#   desktop/dist/Voitta RAG-<VERSION>.dmg

set -euo pipefail
DESK="$(cd "$(dirname "$0")" && pwd)"     # desktop/ (manifest dir)
ROOT="$(cd "$DESK/.." && pwd)"            # repo root
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
REAL_SHIM="$ROOT/src/voitta_rag_desktop"
RES="$REAL_SHIM/resources"
MANIFEST="$DESK/pyproject.toml"

DEFAULT_SIGN_IDENTITY="Developer ID Application: roman semeine (KU3WTX9RXB)"

# Defaults: bump + package (DMG) + Developer ID sign + notarise. A DMG is not
# launchable without notarisation, so it is ON by default (see header).
CLEAN=0; PACKAGE=1; SIGN_IDENTITY=""; NOTARIZE=1; BUMP=1; ADHOC=0

while [ $# -gt 0 ]; do
  case "$1" in
    --clean)       CLEAN=1; shift ;;
    --package)     PACKAGE=1; shift ;;
    --no-package)  PACKAGE=0; shift ;;
    --bump)        BUMP=1; shift ;;
    --no-bump)     BUMP=0; shift ;;
    --adhoc)       ADHOC=1; NOTARIZE=0; shift ;;
    --notarize)    NOTARIZE=1; shift ;;
    --no-notarize) NOTARIZE=0; shift ;;
    --release)     BUMP=1; PACKAGE=1; NOTARIZE=1; shift ;;  # alias for the default
    --sign)
      [ $# -ge 2 ] && [ -n "$2" ] || { echo "--sign needs an identity" >&2; exit 2; }
      SIGN_IDENTITY="$2"; shift 2 ;;
    -h|--help)     sed -n '2,33p' "$0"; exit 0 ;;
    *) echo "[build_app] unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Default to the Developer ID identity unless ad-hoc was requested or an
# explicit --sign identity was given. (Empty SIGN_IDENTITY → ad-hoc DMG.)
if [ "$ADHOC" -eq 0 ] && [ -z "$SIGN_IDENTITY" ]; then
  SIGN_IDENTITY="$DEFAULT_SIGN_IDENTITY"
fi

# Notarisation requires a real Developer ID signature.
if [ "$NOTARIZE" -eq 1 ] && [ -z "$SIGN_IDENTITY" ]; then
  echo "[build_app] cannot notarise an ad-hoc build — use --no-notarize with --adhoc" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# 0. Sanity
# ---------------------------------------------------------------------------
[ -d "$VENV" ] || { echo "[build_app] .venv not found — create it first" >&2; exit 1; }
"$PY" -c "import briefcase" 2>/dev/null || {
  echo "[build_app] briefcase missing — run: uv pip install --python $PY 'briefcase>=0.4.1' build" >&2
  exit 1
}
command -v uv >/dev/null || { echo "[build_app] uv required (for the pinned lock)" >&2; exit 1; }

cd "$DESK"   # briefcase reads ./pyproject.toml and writes ./build ./dist here

# ---------------------------------------------------------------------------
# 1. Optional clean
# ---------------------------------------------------------------------------
if [ "$CLEAN" -eq 1 ]; then
  echo "[build_app] --clean: removing build/ dist/ wheels/ src/"
  rm -rf build dist wheels src
fi
rm -rf dist

# ---------------------------------------------------------------------------
# 2. Symlink the two source packages into desktop/src/ for Briefcase
# ---------------------------------------------------------------------------
mkdir -p src
ln -sfn "$ROOT/src/voitta_rag_desktop"    src/voitta_rag_desktop
ln -sfn "$ROOT/src/voitta_rag_enterprise" src/voitta_rag_enterprise

# ---------------------------------------------------------------------------
# 3. Bump patch version in the manifest
# ---------------------------------------------------------------------------
if [ "$BUMP" -eq 1 ]; then
  CUR=$("$PY" - "$MANIFEST" <<'PYEOF'
import tomllib, sys
print(tomllib.load(open(sys.argv[1],"rb"))["tool"]["briefcase"]["version"])
PYEOF
)
  NEW=$("$PY" - "$CUR" <<'PYEOF'
import sys
p=sys.argv[1].split("."); p[-1]=str(int(p[-1])+1); print(".".join(p))
PYEOF
)
  sed -i '' "s/^version = \"$CUR\"/version = \"$NEW\"/" "$MANIFEST"
  echo "[build_app] version bump: $CUR → $NEW"
fi

VERSION=$("$PY" - "$MANIFEST" <<'PYEOF'
import tomllib, sys
print(tomllib.load(open(sys.argv[1],"rb"))["tool"]["briefcase"]["version"])
PYEOF
)
echo "[build_app] building version $VERSION"
echo "__version__ = \"$VERSION\"" > "$REAL_SHIM/_version.py"

# Built-in Google Drive OAuth client (Desktop-app type) — baked from the
# build machine's env so end users get the zero-setup "Sign in (no setup)"
# Drive tab. Without the env vars the build still succeeds; the tab is
# simply not offered. Regenerated every build so a stale bake can't leak
# from a previous run.
cat > "$REAL_SHIM/_gd_oauth.py" <<PYEOF
"""Built-in Google Drive OAuth client — generated by build_app.sh."""

CLIENT_ID = "${VOITTA_GD_BUILTIN_CLIENT_ID:-}"
CLIENT_SECRET = "${VOITTA_GD_BUILTIN_CLIENT_SECRET:-}"
PYEOF
if [[ -n "${VOITTA_GD_BUILTIN_CLIENT_ID:-}" && -n "${VOITTA_GD_BUILTIN_CLIENT_SECRET:-}" ]]; then
  echo "[build_app] built-in GD OAuth client baked (${VOITTA_GD_BUILTIN_CLIENT_ID:0:12}…)"
else
  echo "[build_app] WARNING: no built-in GD OAuth client baked (set VOITTA_GD_BUILTIN_CLIENT_ID/SECRET to enable the zero-setup Drive tab)"
fi

# ---------------------------------------------------------------------------
# 4. Pinned requirements lock — compiled from the ROOT [project].dependencies
#    (the full server stack), bundled for the first-run installer.
# ---------------------------------------------------------------------------
echo "[build_app] compiling pinned requirements lock via uv…"
mkdir -p "$RES"
PYVER=$("$PY" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')
uv pip compile "$ROOT/pyproject.toml" \
  --python-version "$PYVER" \
  --output-file "$RES/requirements-lock.txt" \
  --quiet
echo "[build_app] lock: $(grep -vc '^#' "$RES/requirements-lock.txt") pinned packages"

# ---------------------------------------------------------------------------
# 5. Pre-build the rumps wheel (sdist-only on PyPI; manifest references it)
# ---------------------------------------------------------------------------
WHEELS="$DESK/wheels"
mkdir -p "$WHEELS"
if ! ls "$WHEELS"/rumps-*.whl >/dev/null 2>&1; then
  echo "[build_app] building rumps wheel…"
  SD="$WHEELS/sdist"; mkdir -p "$SD"
  "$VENV/bin/pip" download --no-binary :all: --no-deps -d "$SD" "rumps==0.4.0" -q
  SRC="$SD/src_$$"; mkdir -p "$SRC"
  tar -xzf "$SD"/rumps-*.tar.gz -C "$SRC" --strip-components=1
  "$PY" -m build --wheel --outdir "$WHEELS" "$SRC" -q
  rm -rf "$SRC"
fi

# ---------------------------------------------------------------------------
# 5b. Pre-build the pylatexenc wheel into the bundled wheelhouse.
#     pylatexenc is sdist-only on PyPI, and the first-run installer runs pip
#     in-process inside the frozen .app, where pip's build-isolation can't
#     spawn an interpreter (sys.executable is the Briefcase stub) — so a
#     runtime sdist->wheel build dies with a misleading missing-output.json
#     OSError (mineru pulls pylatexenc transitively). Build the wheel here,
#     where isolation works, and ship it under resources/wheels so first-run
#     pip installs it via --find-links instead of building. The spec is read
#     from the generated lock so the shipped wheel can never drift from the
#     version we pin.
# ---------------------------------------------------------------------------
RES_WHEELS="$RES/wheels"
rm -rf "$RES_WHEELS"; mkdir -p "$RES_WHEELS"
PYLATEX_SPEC=$(grep -E '^pylatexenc==' "$RES/requirements-lock.txt" | head -1)
[ -n "$PYLATEX_SPEC" ] || { echo "[build_app] ERROR: pylatexenc not pinned in lock — unexpected" >&2; exit 1; }
echo "[build_app] building $PYLATEX_SPEC wheel…"
SD="$RES_WHEELS/sdist"; mkdir -p "$SD"
"$VENV/bin/pip" download --no-binary :all: --no-deps -d "$SD" "$PYLATEX_SPEC" -q
SRC="$SD/src_$$"; mkdir -p "$SRC"
tar -xzf "$SD"/pylatexenc-*.tar.gz -C "$SRC" --strip-components=1
"$PY" -m build --wheel --outdir "$RES_WHEELS" "$SRC" -q
rm -rf "$SD"

# Guardrail: any package that is sdist-only on PyPI must be pre-wheeled above,
# or the frozen-app first-run installer will try (and fail) to build it. We
# can't cheaply probe all ~769 pins against PyPI on every build, so the known
# sdist-only deps are tracked explicitly here. If you add another sdist-only
# dependency, pre-build its wheel above and add a line below.
_require_wheel() {  # $1 = wheelhouse dir, $2 = distribution name
  ls "$1"/"$2"-*.whl >/dev/null 2>&1 || {
    echo "[build_app] ERROR: sdist-only package '$2' has no pre-built wheel in" \
         "$1 — the first-run installer would fail to build it inside the .app" >&2
    exit 1
  }
}
_require_wheel "$WHEELS" rumps
_require_wheel "$RES_WHEELS" pylatexenc

# ---------------------------------------------------------------------------
# 6. Stage the SPA into the bundle resources
# ---------------------------------------------------------------------------
echo "[build_app] staging static/ → resources/static"
rm -rf "$RES/static"
cp -r "$ROOT/static" "$RES/static"

# ---------------------------------------------------------------------------
# 7. Briefcase create / update / build
# ---------------------------------------------------------------------------
BRIEFCASE="$VENV/bin/briefcase"
echo "[build_app] briefcase $([ -d "$DESK/build" ] && echo update || echo create)…"
if [ -d "$DESK/build" ]; then
  "$BRIEFCASE" update macOS app
else
  "$BRIEFCASE" create macOS app
fi
echo "[build_app] briefcase build…"
"$BRIEFCASE" build macOS app
echo "[build_app] .app built: desktop/build/voitta-rag-desktop/macos/app/Voitta RAG.app"

# Stamp the bundle's version into Info.plist. `briefcase update` (the path taken
# when build/ already exists) does NOT re-stamp the plist from the bumped
# manifest, so Finder "Get Info" / the DMG would otherwise show a stale version.
# Keep it consistent with the runtime __version__.
APP_BUNDLE="$DESK/build/voitta-rag-desktop/macos/app/Voitta RAG.app"
APP_PLIST="$APP_BUNDLE/Contents/Info.plist"
if [ -f "$APP_PLIST" ]; then
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP_PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$APP_PLIST"
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$APP_PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$APP_PLIST"
  # `briefcase update` (the build/ exists path) does NOT re-copy the app icon,
  # so refresh it from source — otherwise an icon change never reaches the .app.
  SRC_ICNS="$RES/voitta.icns"
  APP_ICNS="$APP_BUNDLE/Contents/Resources/voitta-rag-desktop.icns"
  if [ -f "$SRC_ICNS" ] && [ -f "$APP_ICNS" ]; then
    cp "$SRC_ICNS" "$APP_ICNS"
    echo "[build_app] refreshed app icon"
  fi
  # Editing Info.plist invalidates the ad-hoc signature briefcase applied during
  # build; an invalid signature gets the app SIGKILL'd by Gatekeeper. Re-sign
  # ad-hoc so the local build runs. (A later `--package --identity` re-signs
  # with the real Developer ID, overriding this.)
  codesign --force --deep --sign - "$APP_BUNDLE" 2>/dev/null \
    && echo "[build_app] stamped Info.plist version → $VERSION (re-signed ad-hoc)" \
    || echo "[build_app] WARN: stamped version but ad-hoc re-sign failed" >&2
fi

# ---------------------------------------------------------------------------
# 8. Package (DMG) + sign + notarize
#
# Briefcase does signing, notarisation AND stapling in the single `package`
# step when given a Developer ID `--identity`. We therefore let briefcase own
# the whole flow and just toggle `--no-notarize`: notarisation submits to Apple
# and waits 2–15 min, so it stays opt-in (--notarize/--release) to keep the
# default build fast and offline. (briefcase uses its own stored notary
# credentials; the legacy `voitta-notary` profile is no longer needed here.)
# ---------------------------------------------------------------------------
if [ "$PACKAGE" -eq 0 ]; then
  echo "[build_app] done (--no-package: built the .app only, no DMG)"
  exit 0
fi

PKG_ARGS=()
if [ -n "$SIGN_IDENTITY" ]; then
  PKG_ARGS+=(--identity "$SIGN_IDENTITY")
  SIGN_DESC="Developer ID: $SIGN_IDENTITY"
else
  PKG_ARGS+=(--adhoc-sign)
  SIGN_DESC="ad-hoc"
fi
if [ "$NOTARIZE" -eq 1 ]; then
  echo "[build_app] briefcase package (DMG, $SIGN_DESC, notarising — 2–15 min)…"
else
  PKG_ARGS+=(--no-notarize)
  echo "[build_app] briefcase package (DMG, $SIGN_DESC, no notarisation)…"
fi
"$BRIEFCASE" package macOS app "${PKG_ARGS[@]}"

DMG=$(ls dist/*.dmg 2>/dev/null | head -1)
[ -n "$DMG" ] || { echo "[build_app] DMG not found under dist/" >&2; exit 1; }

if [ "$NOTARIZE" -eq 1 ]; then
  echo "[build_app] verifying…"
  spctl -a -vv --type install "$DMG" || true
  echo "[build_app] notarised + stapled DMG: $DMG"
elif [ -n "$SIGN_IDENTITY" ]; then
  echo "[build_app] done (signed DMG; not notarised — add --notarize/--release to submit to Apple): $DMG"
else
  echo "[build_app] done (ad-hoc DMG; not signed/notarised for distribution): $DMG"
fi
