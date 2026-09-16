#!/usr/bin/env bash
# Download / bundle static ffmpeg binaries for packaging
#   macOS arm64: bundles homebrew ffmpeg + dylibs into portable layout
#   Windows x64: downloads from GyanD/codexffmpeg GitHub releases
#   Linux x64:   downloads from BtbN/FFmpeg-Builds GitHub releases
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FFMPEG_DIR="$SCRIPT_DIR/../ffmpeg"

download_macos_arm64() {
  # Use static ffmpeg binary from the npm ffmpeg-static package (arm64, no dylibs)
  local dest="$FFMPEG_DIR/macos-arm64/ffmpeg"
  if [ -f "$dest" ] && [ -x "$dest" ]; then
    echo "[ffmpeg] macOS arm64 already present ($(file "$dest" | cut -d: -f2-))"
    return
  fi
  echo "[ffmpeg] Extracting static ffmpeg from ffmpeg-static npm package..."
  cd "$SCRIPT_DIR/.."
  npm install ffmpeg-static --no-save 2>&1 | tail -1
  local NPM_BIN="$SCRIPT_DIR/../node_modules/ffmpeg-static/ffmpeg"
  if [ ! -f "$NPM_BIN" ]; then
    echo "[ffmpeg] ERROR: ffmpeg-static npm package did not provide ffmpeg binary"
    return 1
  fi
  mkdir -p "$FFMPEG_DIR/macos-arm64"
  cp "$NPM_BIN" "$dest"
  chmod +x "$dest"
  echo "[ffmpeg] macOS arm64 ready: $(file "$dest" | cut -d: -f2-)"
}
    local current="${queue[0]}"
    queue=("${queue[@]:1}")

    otool -L "$current" 2>/dev/null | tail -n +2 | awk '{print $1}' | while read -r lib; do
      [[ "$lib" == /System/* ]] && continue
      [[ "$lib" == /usr/lib/* ]] && continue
      [[ "$lib" == @* ]] && continue

      local basename
      basename=$(basename "$lib")
      grep -qxF "$basename" "$processed_file" 2>/dev/null && continue
      echo "$basename" >> "$processed_file"

      local src=""
      [ -f "$LIB_DIR/$basename" ] && src="$LIB_DIR/$basename"
      [ -z "$src" ] && [ -f "$HOMEBREW_LIB/$basename" ] && src="$HOMEBREW_LIB/$basename"

      if [ -n "$src" ] && [ ! -f "$OUTDIR/$basename" ]; then
        cp "$src" "$OUTDIR/$basename"
        chmod 755 "$OUTDIR/$basename"
        queue+=("$OUTDIR/$basename")
      fi
    done
  done

  # Fix all dylib references to @loader_path/
  for file in "$OUTDIR"/*; do
    [ ! -f "$file" ] && continue
    if [ "$file" != "$OUTDIR/ffmpeg" ]; then
      install_name_tool -id "@loader_path/$(basename "$file")" "$file" 2>/dev/null || true
    fi
    otool -L "$file" 2>/dev/null | tail -n +2 | awk '{print $1}' | while read -r lib; do
      [[ "$lib" == /System/* ]] && continue
      [[ "$lib" == /usr/lib/* ]] && continue
      [[ "$lib" == @loader_path/* ]] && continue
      install_name_tool -change "$lib" "@loader_path/$(basename "$lib")" "$file" 2>/dev/null || true
    done
  done

  # Ad-hoc sign everything so macOS allows execution
  for f in "$OUTDIR"/*; do
    [ -f "$f" ] && codesign --remove-signature "$f" 2>/dev/null || true
    [ -f "$f" ] && codesign -s - "$f" 2>/dev/null || true
  done

  rm -f "$processed_file"
  echo "[ffmpeg] macOS arm64 bundle ready: $(file "$dest" | cut -d: -f2-)"
}

download_win_x64() {
  local dest="$FFMPEG_DIR/win-x64/ffmpeg.exe"
  if [ -f "$dest" ]; then
    echo "[ffmpeg] Windows x64 already present"
    return
  fi
  echo "[ffmpeg] Downloading Windows x64 from GyanD/codexffmpeg ..."
  local tag
  tag=$(curl -sI -L --max-time 15 "https://github.com/GyanD/codexffmpeg/releases/latest" 2>/dev/null | grep -i "^location:" | tail -1 | sed 's|.*/tag/||' | tr -d '\r')
  if [ -z "$tag" ]; then
    echo "[ffmpeg] ERROR: could not determine latest Windows ffmpeg version"
    echo "[ffmpeg] Download manually from: https://github.com/GyanD/codexffmpeg/releases/latest"
    echo "[ffmpeg] Extract ffmpeg.exe to: $dest"
    return 1
  fi
  echo "[ffmpeg] Latest Windows version: $tag"
  local tmp_zip="/tmp/ffmpeg-win64.zip"
  curl -L --max-time 180 -o "$tmp_zip" "https://github.com/GyanD/codexffmpeg/releases/download/${tag}/ffmpeg-${tag}-essentials_build.zip"
  mkdir -p /tmp/ffmpeg_win_out
  unzip -o "$tmp_zip" "ffmpeg-${tag}-essentials_build/bin/ffmpeg.exe" -d /tmp/ffmpeg_win_out
  mkdir -p "$FFMPEG_DIR/win-x64"
  cp "/tmp/ffmpeg_win_out/ffmpeg-${tag}-essentials_build/bin/ffmpeg.exe" "$dest"
  rm -rf "$tmp_zip" /tmp/ffmpeg_win_out
  chmod +x "$dest"
  echo "[ffmpeg] Windows x64 ready: $(file "$dest")"
}

download_linux_x64() {
  local dest="$FFMPEG_DIR/linux-x64/ffmpeg"
  if [ -f "$dest" ] && [ -x "$dest" ]; then
    echo "[ffmpeg] Linux x64 already present"
    return
  fi
  echo "[ffmpeg] Downloading Linux x64 from BtbN/FFmpeg-Builds ..."
  local tmp_tar="/tmp/ffmpeg-linux64.tar.xz"
  curl -L --max-time 180 -o "$tmp_tar" "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
  mkdir -p "$FFMPEG_DIR/linux-x64"
  tar -xJf "$tmp_tar" --strip-components=2 -C "$FFMPEG_DIR/linux-x64" "ffmpeg-master-latest-linux64-gpl/bin/ffmpeg"
  rm -f "$tmp_tar"
  chmod +x "$dest"
  echo "[ffmpeg] Linux x64 ready: $(file "$dest")"
}

case "${1:-}" in
  mac|macos|macos-arm64) download_macos_arm64 ;;
  win|windows|win-x64)   download_win_x64 ;;
  linux|linux-x64)       download_linux_x64 ;;
  all)
    download_macos_arm64
    download_win_x64
    download_linux_x64
    ;;
  *)
    echo "Usage: $0 {mac|win|linux|all}"
    echo ""
    echo "Downloads / bundles static ffmpeg binary for the specified platform:"
    echo "  mac   - macOS arm64 (bundles homebrew ffmpeg + dylibs)"
    echo "  win   - Windows x64 (GyanD/codexffmpeg)"
    echo "  linux - Linux x64 (BtbN/FFmpeg-Builds)"
    echo "  all   - all three platforms"
    exit 1
    ;;
esac
