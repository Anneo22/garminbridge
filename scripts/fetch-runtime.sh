#!/usr/bin/env bash
# Recreate the ignored, arm64-only runtimes used by the app bundle.
#
# CPython bootstraps with stock macOS tools. gphoto2 deliberately comes from exact,
# hashed Homebrew Cellar inputs: building requires Homebrew, but running the shipped
# app does not. Pulling pinned bottles from GHCR would remove that build dependency,
# but an authenticated registry client is not worth adding for this one-developer build.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$ROOT/runtime.lock"
REQUIREMENTS="$ROOT/engine/requirements.txt"
VENDOR="$ROOT/vendor"
PYTHON_DIR="$VENDOR/python"
WHEELS_DIR="$VENDOR/wheels"
SITE_PACKAGES_DIR="$VENDOR/site-packages"
GPHOTO_DIR="$VENDOR/gphoto2"
HOMEBREW_CELLAR="/opt/homebrew/Cellar"
WORK_DIR=""
REGENERATE=0

case "${1:-}" in
  --regenerate-lock) REGENERATE=1 ;;
  "") ;;
  *) echo "usage: ${0##*/} [--regenerate-lock]" >&2; exit 2 ;;
esac

die() {
  echo "fetch-runtime: ERROR: $*" >&2
  exit 1
}

lock_value() {
  awk -v key="$1" '$1 == key { print $2; found = 1; exit } END { if (!found) exit 1 }' "$LOCK"
}

python_lock_digest() {
  awk '$1 == "python_version" || $1 == "python_url" || $1 == "python_sha256" ||
       $1 == "engine_requirements_sha256" || $1 == "wheel" { print }' "$LOCK" |
    shasum -a 256 | awk '{print $1}'
}

gphoto_lock_digest() {
  awk '$1 == "formula" || $1 == "gphoto2_file" { print }' "$LOCK" |
    shasum -a 256 | awk '{print $1}'
}

formula_version() {
  awk -v formula="$1" '$1 == "formula" && $2 == formula { print $3; found = 1; exit }
    END { if (!found) exit 1 }' "$LOCK"
}

cleanup() {
  if [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then
    /bin/rm -rf "$WORK_DIR"
  fi
}
trap cleanup EXIT INT TERM

[ -f "$LOCK" ] || die "missing lockfile: $LOCK"
[ "$(uname -m)" = "arm64" ] || die "this runtime lock supports Apple Silicon (arm64) only"

PYTHON_VERSION="$(lock_value python_version)"
PYTHON_URL="$(lock_value python_url)"
PYTHON_SHA256="$(lock_value python_sha256)"
REQUIREMENTS_SHA256="$(lock_value engine_requirements_sha256)"
LOCK_SHA256="$(python_lock_digest)"
GPHOTO_SHA256="$(gphoto_lock_digest)"
PYTHON_MARKER="$PYTHON_DIR/.garminbridge-runtime-sha256"
WHEELS_MARKER="$WHEELS_DIR/runtime-lock.sha256"

[ -f "$REQUIREMENTS" ] || die "missing engine requirements: $REQUIREMENTS"
actual_requirements_sha256="$(shasum -a 256 "$REQUIREMENTS" | awk '{print $1}')"
# --regenerate-lock is the one mode allowed past this gate: it exists precisely to
# resolve the mismatch the gate reports, and it rewrites the lock to match.
[ "$REGENERATE" = "1" ] || [ "$actual_requirements_sha256" = "$REQUIREMENTS_SHA256" ] || \
  die "engine/requirements.txt changed without regenerating runtime.lock (fix: $0 --regenerate-lock)"

verify_python() {
  [ -x "$PYTHON_DIR/bin/python3" ] || return 1
  [ -f "$PYTHON_MARKER" ] || return 1
  [ "$(sed -n '1p' "$PYTHON_MARKER")" = "$PYTHON_SHA256" ] || return 1
  PYTHONDONTWRITEBYTECODE=1 "$PYTHON_DIR/bin/python3" -c \
    'import platform, sys; raise SystemExit(0 if (platform.machine() == "arm64" and platform.python_version() == sys.argv[1]) else 1)' \
    "$PYTHON_VERSION" >/dev/null 2>&1
}

verify_wheel_files() {
  [ -d "$WHEELS_DIR" ] || return 1

  expected="$(awk '$1 == "wheel" { count++ } END { print count + 0 }' "$LOCK")"
  actual="$(find "$WHEELS_DIR" -maxdepth 1 -type f -name '*.whl' | wc -l | tr -d ' ')"
  [ "$actual" = "$expected" ] || return 1

  while read -r _ hash filename; do
    [ -f "$WHEELS_DIR/$filename" ] || return 1
    actual_hash="$(shasum -a 256 "$WHEELS_DIR/$filename" | awk '{print $1}')"
    [ "$actual_hash" = "$hash" ] || return 1
  done < <(awk '$1 == "wheel" { print }' "$LOCK")
}

verify_wheels() {
  [ -f "$WHEELS_MARKER" ] || return 1
  [ "$(sed -n '1p' "$WHEELS_MARKER")" = "$LOCK_SHA256" ] || return 1
  verify_wheel_files
}

verify_site_packages() {
  local root="${1:-$SITE_PACKAGES_DIR}"
  local marker="$root/runtime-lock.sha256"
  local native_count

  [ -d "$root" ] || return 1
  [ -f "$marker" ] || return 1
  [ "$(sed -n '1p' "$marker")" = "$LOCK_SHA256" ] || return 1
  [ -z "$(find "$root" -type l -print -quit)" ] || return 1
  native_count="$(find "$root" -type f -name '*.so' | wc -l | tr -d ' ')"
  [ "$native_count" = "6" ] || return 1

  /usr/bin/env -i \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="$root" \
    "$PYTHON_DIR/bin/python3" -c '
import importlib
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
for name in (
    "fit_tool",
    "fitdecode",
    "garminconnect",
):
    importlib.import_module(name)

for name in (
    "bitstruct.c",
    "_cffi_backend",
    "ada92cb5d92a588d1b93__mypyc",
    "charset_normalizer.md",
    "charset_normalizer.cd",
    "curl_cffi._wrapper",
):
    module = importlib.import_module(name)
    Path(module.__file__).resolve().relative_to(root)
' "$root" >/dev/null 2>&1
}

collapse_site_package_symlinks() {
  local root="$1"

  "$PYTHON_DIR/bin/python3" - "$root" <<'PY'
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1])
for link in [path for path in root.rglob("*") if path.is_symlink()]:
    target = link.resolve(strict=True)
    if not target.is_file():
        raise SystemExit(f"site-packages symlink does not resolve to one file: {link}")
    replacement = link.with_name(f".{link.name}.garminbridge-copy")
    shutil.copy2(target, replacement)
    link.unlink()
    replacement.replace(link)
PY
}

build_site_packages() {
  local stage="$WORK_DIR/site-packages"
  local wheel_files=()

  verify_wheels || die "wheelhouse hash verification failed before site-packages install"
  mkdir -p "$stage"
  while read -r _ _ filename; do
    wheel_files+=("$WHEELS_DIR/$filename")
  done < <(awk '$1 == "wheel" { print }' "$LOCK")

  echo "==> Installing locked wheels into site-packages"
  PIP_NO_CACHE_DIR=1 PIP_NO_INDEX=1 PYTHONDONTWRITEBYTECODE=1 \
    "$PYTHON_DIR/bin/python3" -m pip --isolated install \
      --disable-pip-version-check \
      --no-index \
      --no-deps \
      --no-compile \
      --target "$stage" \
      "${wheel_files[@]}" \
      </dev/null

  collapse_site_package_symlinks "$stage"
  printf '%s\n' "$LOCK_SHA256" > "$stage/runtime-lock.sha256"
  verify_site_packages "$stage" || die "installed site-packages failed verification"
  /bin/rm -rf "$SITE_PACKAGES_DIR"
  mv "$stage" "$SITE_PACKAGES_DIR"
}

prune_python() {
  local root="$1"
  local stdlib="$root/lib/python3.14"

  # Tauri dereferences resource symlinks. Keeping python -> python3.14 aliases
  # would copy the 18 MB executable three times into the .app, so retain one
  # real executable at the path the shell uses.
  if [ -L "$root/bin/python3" ] && [ -f "$root/bin/python3.14" ]; then
    /bin/rm -f "$root/bin/python" "$root/bin/python3" "$root/bin/python3-config"
    mv "$root/bin/python3.14" "$root/bin/python3"
  fi

  /bin/rm -rf \
    "$root/include" \
    "$root/share" \
    "$root/lib/tcl9.0" \
    "$root/lib/tk9.0" \
    "$root/lib/tcl9" \
    "$root/lib/itcl4.3.5" \
    "$root/lib/thread3.0.4" \
    "$root/lib/pkgconfig" \
    "$stdlib/idlelib" \
    "$stdlib/tkinter" \
    "$stdlib/pydoc_data" \
    "$stdlib/config-3.14-darwin"
  /bin/rm -f \
    "$root/bin/python" \
    "$root/bin/python3-config" \
    "$root/bin/python3.14" \
    "$root/bin/python3.14-config" \
    "$root/bin/idle3" \
    "$root/bin/idle3.14" \
    "$root/bin/pydoc3" \
    "$root/bin/pydoc3.14" \
    "$root/bin/pip" \
    "$root/bin/pip3" \
    "$root/bin/pip3.14" \
    "$root/lib/libpython3.14.dylib"
  find "$root/lib" -maxdepth 1 -type f \
    \( -name 'libtcl*.dylib' -o -name 'libtk*.dylib' \) -delete
  find "$root" -type d -name __pycache__ -prune -exec /bin/rm -rf {} +
}

verify_gphoto_sources() {
  [ -d "$HOMEBREW_CELLAR" ] || \
    die "Homebrew Cellar is required to build gphoto2: $HOMEBREW_CELLAR"
  [ "$(awk '$1 == "formula" { count++ } END { print count + 0 }' "$LOCK")" = "9" ] || \
    die "runtime.lock must contain exactly 9 gphoto2 formula pins"
  [ "$(awk '$1 == "gphoto2_file" { count++ } END { print count + 0 }' "$LOCK")" = "13" ] || \
    die "runtime.lock must contain exactly 13 gphoto2 files"

  while read -r _ formula version; do
    [ -d "$HOMEBREW_CELLAR/$formula/$version" ] || \
      die "locked Homebrew formula is missing: $formula $version"
  done < <(awk '$1 == "formula" { print }' "$LOCK")

  while read -r _ expected_hash formula relative_source destination; do
    version="$(formula_version "$formula")" || \
      die "gphoto2 source refers to unpinned formula: $formula"
    source="$HOMEBREW_CELLAR/$formula/$version/$relative_source"
    [ -f "$source" ] || die "locked gphoto2 source is missing: $source"
    actual_hash="$(shasum -a 256 "$source" | awk '{print $1}')"
    [ "$actual_hash" = "$expected_hash" ] || \
      die "Homebrew source drifted for $destination: expected $expected_hash, got $actual_hash"
  done < <(awk '$1 == "gphoto2_file" { print }' "$LOCK")
}

bundled_library_name() {
  case "$(basename "$1")" in
    libjpeg.8.dylib|libjpeg.8.3.2.dylib) echo "libjpeg.8.3.2.dylib" ;;
    libreadline.8.dylib|libreadline.8.3.dylib) echo "libreadline.8.3.dylib" ;;
    *) basename "$1" ;;
  esac
}

rewrite_macho() {
  local file="$1"
  local role="$2"
  local library dependency replacement rpath

  if [ "$role" = "lib" ]; then
    install_name_tool -id "@loader_path/$(basename "$file")" "$file"
  fi

  while read -r dependency; do
    case "$dependency" in
      /opt/homebrew/*|/usr/local/*)
        library="$(bundled_library_name "$dependency")"
        [ -f "$(dirname "$(dirname "$file")")/lib/$library" ] || \
          die "unbundled dependency in $file: $dependency"
        case "$role" in
          bin) replacement="@executable_path/../lib/$library" ;;
          lib) replacement="@loader_path/$library" ;;
          driver) replacement="@loader_path/../lib/$library" ;;
          *) die "unknown Mach-O role: $role" ;;
        esac
        install_name_tool -change "$dependency" "$replacement" "$file"
        ;;
    esac
  done < <(otool -L "$file" | sed -n '2,$s/^[[:space:]]*\([^[:space:]]*\).*/\1/p')

  while read -r rpath; do
    case "$rpath" in
      /opt/homebrew/*|/usr/local/*) install_name_tool -delete_rpath "$rpath" "$file" ;;
    esac
  done < <(otool -l "$file" | awk '
    $1 == "cmd" { in_rpath = ($2 == "LC_RPATH"); next }
    in_rpath && $1 == "path" { print $2; in_rpath = 0 }
  ')

  codesign -f -s - "$file" >/dev/null 2>&1
}

verify_gphoto_payload() {
  local root="${1:-$GPHOTO_DIR}"
  local marker="$root/.garminbridge-runtime-sha256"
  local file list_output

  [ -f "$marker" ] || return 1
  [ "$(sed -n '1p' "$marker")" = "$GPHOTO_SHA256" ] || return 1

  while read -r _ _ _ _ destination; do
    file="$root/$destination"
    [ -f "$file" ] && [ ! -L "$file" ] || return 1
    if (otool -L "$file"; otool -l "$file") | grep -Eq '/opt/homebrew|/usr/local'; then
      return 1
    fi
    codesign -v "$file" >/dev/null 2>&1 || return 1
  done < <(awk '$1 == "gphoto2_file" { print }' "$LOCK")

  /usr/bin/env -i \
    PATH="$root/bin:/usr/bin:/bin" \
    CAMLIBS="$root/camlibs" \
    IOLIBS="$root/iolibs" \
    "$root/bin/gphoto2" --version >/dev/null 2>&1 || return 1
  list_output="$(/usr/bin/env -i \
    PATH="$root/bin:/usr/bin:/bin" \
    CAMLIBS="$root/camlibs" \
    IOLIBS="$root/iolibs" \
    "$root/bin/gphoto2" --list-cameras 2>&1)" || return 1
  grep -Eiq 'PTP|MTP' <<<"$list_output" || return 1
  /usr/bin/env -i \
    PATH="$root/bin:/usr/bin:/bin" \
    CAMLIBS="$root/camlibs" \
    IOLIBS="$root/iolibs" \
    "$root/bin/gphoto2" --auto-detect >/dev/null 2>&1 || return 1
}

build_gphoto_payload() {
  local stage="$WORK_DIR/gphoto2"
  local formula relative_source destination version source target role

  mkdir -p "$stage/bin" "$stage/lib" "$stage/camlibs" "$stage/iolibs"
  while read -r _ _ formula relative_source destination; do
    version="$(formula_version "$formula")"
    source="$HOMEBREW_CELLAR/$formula/$version/$relative_source"
    target="$stage/$destination"
    /bin/cp "$source" "$target"
    chmod u+w "$target"
  done < <(awk '$1 == "gphoto2_file" { print }' "$LOCK")

  while read -r _ _ _ _ destination; do
    case "$destination" in
      bin/*) role="bin" ;;
      lib/*) role="lib" ;;
      camlibs/*|iolibs/*) role="driver" ;;
      *) die "unsupported gphoto2 destination: $destination" ;;
    esac
    rewrite_macho "$stage/$destination" "$role"
  done < <(awk '$1 == "gphoto2_file" { print }' "$LOCK")

  printf '%s\n' "$GPHOTO_SHA256" > "$stage/.garminbridge-runtime-sha256"
  verify_gphoto_payload "$stage" || die "relocated gphoto2 payload failed verification"
  /bin/rm -rf "$GPHOTO_DIR"
  mv "$stage" "$GPHOTO_DIR"
}

# Rewrite the wheel half of the lock from engine/requirements.txt. The interpreter
# and gphoto2 halves are deliberately untouched: gphoto2 carries its own digest, and
# a new CPython is a decision, not a side effect of adding a dependency. Resolution
# runs under the bundled interpreter so the recorded wheels carry the same cp*/arm64
# tags the app will ship, which a host Python cannot be trusted to reproduce.
regenerate_lock() {
  local stage="$WORK_DIR/relock" new_req_sha new_lock
  mkdir -p "$stage"

  echo "==> Re-resolving $REQUIREMENTS with the bundled Python"
  PYTHONDONTWRITEBYTECODE=1 "$PYTHON_DIR/bin/python3" -m pip download \
    --disable-pip-version-check \
    --only-binary=:all: \
    --requirement "$REQUIREMENTS" \
    --dest "$stage" \
    </dev/null >/dev/null || die "could not resolve $REQUIREMENTS"

  [ -n "$(find "$stage" -maxdepth 1 -type f -name '*.whl' -print -quit)" ] || \
    die "resolution produced no wheels"

  new_req_sha="$(shasum -a 256 "$REQUIREMENTS" | awk '{print $1}')"
  new_lock="$WORK_DIR/runtime.lock.new"

  # Replace the engine_requirements_sha256 value and the whole wheel block in place,
  # keeping every other line and the original ordering.
  {
    awk -v sha="$new_req_sha" '
      $1 == "engine_requirements_sha256" { print $1, sha; next }
      $1 == "wheel" { if (!seen++) print "@@GARMINBRIDGE_WHEELS@@"; next }
      { print }
    ' "$LOCK"
  } > "$new_lock.tmpl"

  find "$stage" -maxdepth 1 -type f -name '*.whl' -print0 |
    sort -z |
    while IFS= read -r -d '' whl; do
      printf 'wheel %s %s\n' \
        "$(shasum -a 256 "$whl" | awk '{print $1}')" "$(basename "$whl")"
    done > "$new_lock.wheels"

  awk -v wheelfile="$new_lock.wheels" '
    $0 == "@@GARMINBRIDGE_WHEELS@@" { while ((getline line < wheelfile) > 0) print line; next }
    { print }
  ' "$new_lock.tmpl" > "$new_lock"

  grep -q '^wheel ' "$new_lock" || die "regenerated lock contains no wheel lines"
  mv "$new_lock" "$LOCK"

  echo "==> Rewrote $LOCK: $(awk '$1 == "wheel" { n++ } END { print n + 0 }' "$LOCK") wheels"
  echo "    Re-run without --regenerate-lock to rebuild vendor/ from it."
}

mkdir -p "$VENDOR"
WORK_DIR="$(mktemp -d "$VENDOR/.runtime-fetch.XXXXXX")"
verify_gphoto_sources

if verify_python; then
  echo "==> CPython $PYTHON_VERSION already present"
  prune_python "$PYTHON_DIR"
else
  archive="$WORK_DIR/python.tar.gz"
  echo "==> Downloading CPython $PYTHON_VERSION"
  curl --fail --location --retry 3 --output "$archive" "$PYTHON_URL"
  actual_sha256="$(shasum -a 256 "$archive" | awk '{print $1}')"
  [ "$actual_sha256" = "$PYTHON_SHA256" ] || \
    die "CPython checksum mismatch: expected $PYTHON_SHA256, got $actual_sha256"

  extract_dir="$WORK_DIR/extract"
  mkdir -p "$extract_dir"
  tar -xzf "$archive" -C "$extract_dir"
  [ -x "$extract_dir/python/bin/python3" ] || die "archive did not contain python/bin/python3"
  prune_python "$extract_dir/python"
  printf '%s\n' "$PYTHON_SHA256" > "$extract_dir/python/.garminbridge-runtime-sha256"
  PYTHONDONTWRITEBYTECODE=1 "$extract_dir/python/bin/python3" -c \
    'import platform, sys; assert platform.machine() == "arm64"; assert platform.python_version() == sys.argv[1]' \
    "$PYTHON_VERSION"

  /bin/rm -rf "$PYTHON_DIR"
  mv "$extract_dir/python" "$PYTHON_DIR"
fi

if [ "$REGENERATE" = "1" ]; then
  regenerate_lock
  exit 0
fi

# gphoto2 has its own lock identity. Re-key a previously verified 5a wheelhouse
# without downloading it again now that runtime.lock contains a second payload.
if ! verify_wheels && verify_wheel_files; then
  printf '%s\n' "$LOCK_SHA256" > "$WHEELS_MARKER"
fi

if verify_wheels; then
  echo "==> Locked wheelhouse already present"
else
  requirements="$WORK_DIR/wheels.requirements.txt"
  wheel_stage="$WORK_DIR/wheels"
  mkdir -p "$wheel_stage"
  awk '$1 == "wheel" {
    split($3, parts, "-");
    printf "%s==%s --hash=sha256:%s\n", parts[1], parts[2], $2
  }' "$LOCK" > "$requirements"

  echo "==> Downloading locked wheelhouse with bundled Python"
  PYTHONDONTWRITEBYTECODE=1 "$PYTHON_DIR/bin/python3" -m pip download \
    --disable-pip-version-check \
    --only-binary=:all: \
    --require-hashes \
    --requirement "$requirements" \
    --dest "$wheel_stage" \
    </dev/null

  printf '%s\n' "$LOCK_SHA256" > "$wheel_stage/runtime-lock.sha256"
  old_wheels="$WHEELS_DIR"
  WHEELS_DIR="$wheel_stage"
  WHEELS_MARKER="$WHEELS_DIR/runtime-lock.sha256"
  verify_wheels || die "downloaded wheelhouse does not match $LOCK"
  WHEELS_DIR="$old_wheels"
  WHEELS_MARKER="$WHEELS_DIR/runtime-lock.sha256"

  /bin/rm -rf "$WHEELS_DIR"
  mv "$wheel_stage" "$WHEELS_DIR"
fi

if verify_site_packages; then
  echo "==> Locked site-packages already present"
else
  build_site_packages
fi

if verify_gphoto_payload; then
  echo "==> Locked gphoto2 runtime already present"
else
  echo "==> Copying and relocating locked gphoto2 runtime"
  build_gphoto_payload
fi

verify_python || die "final interpreter verification failed"
verify_wheels || die "final wheelhouse verification failed"
verify_site_packages || die "final site-packages verification failed"
verify_gphoto_payload || die "final gphoto2 verification failed"
echo "==> Runtime ready: $(du -sh "$VENDOR" | awk '{print $1}') in $VENDOR"
