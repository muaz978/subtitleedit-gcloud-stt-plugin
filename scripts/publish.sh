#!/usr/bin/env bash
#
# Builds one plugin zip per platform.
#
# Three things here are load bearing and were established by testing, not by preference:
#
#   1. --self-contained, so the user needs no .NET runtime. The manifest deliberately does
#      NOT use "runtime": "dotnet", because Subtitle Edit launches that via the shared
#      dotnet host, which is the dependency being avoided.
#   2. NO trimming. The default full trim silently breaks credential loading at runtime,
#      and even a partial trim is refused here: Google.Apis, Google.Api.Gax.Grpc,
#      Newtonsoft.Json and Avalonia.DesignerSupport all declare trim warnings of their own
#      (IL2104), meaning the vendors do not consider these assemblies trim safe. A trimmed
#      build of that stack fails at runtime rather than at build time, which is the worst
#      possible place to find out. Single file compression recovers much of the size.
#   3. Ad-hoc signing on macOS. Without a valid signature the kernel kills the process on
#      Apple Silicon with no dialog, and the user sees only Subtitle Edit reporting
#      "exited with code 137".
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="$ROOT/src/SubtitleEdit.GoogleCloudStt/SubtitleEdit.GoogleCloudStt.csproj"
OUT="$ROOT/dist"
NAME="SubtitleEditGoogleCloudStt"
PLUGIN_FOLDER="Google Cloud Speech-to-Text"

RIDS=("${@:-}")
if [ -z "${RIDS[0]:-}" ]; then
  RIDS=(win-x64 win-arm64 linux-x64 linux-arm64 osx-x64 osx-arm64)
fi

rm -rf "$OUT"
mkdir -p "$OUT"

for RID in "${RIDS[@]}"; do
  echo "==> publishing $RID"
  STAGE="$OUT/stage/$RID/$PLUGIN_FOLDER"
  mkdir -p "$STAGE"

  dotnet publish "$PROJECT" \
    -c Release \
    -r "$RID" \
    --self-contained true \
    -p:PublishSingleFile=true \
    -p:IncludeNativeLibrariesForSelfExtract=true \
    -p:EnableCompressionInSingleFile=true \
    -p:PublishTrimmed=false \
    -p:InvariantGlobalization=true \
    -p:DebugType=none \
    -o "$OUT/build/$RID" \
    --nologo -v quiet

  EXE="$NAME"
  if [[ "$RID" == win-* ]]; then
    EXE="$NAME.exe"
  fi

  cp "$OUT/build/$RID/$EXE" "$STAGE/$EXE"
  cp "$ROOT/plugin.json" "$STAGE/plugin.json"

  if [[ "$RID" == osx-* ]]; then
    # The SDK ad-hoc signs osx apphosts already, but anything that rewrites the binary
    # after publish invalidates it. Re-signing explicitly is cheap insurance.
    if command -v codesign >/dev/null 2>&1; then
      codesign --force --sign - --timestamp=none "$STAGE/$EXE"
      codesign --verify --verbose "$STAGE/$EXE"
    else
      echo "    WARNING: codesign not available; this build will be killed on Apple Silicon" >&2
    fi
  fi

  chmod +x "$STAGE/$EXE"

  # Prove the packaged binary actually starts and can reach its credential loader before
  # it is zipped. A build that only "compiles" is not evidence of anything.
  if [[ "$RID" == "$(dotnet --info | awk -F: '/^ RID:/ {gsub(/ /,"",$2); print $2}')" ]]; then
    if ! "$STAGE/$EXE" --selftest; then
      echo "    SELF TEST FAILED for $RID" >&2
      exit 1
    fi
    echo "    self test passed"
  fi

  ZIP="$OUT/GoogleCloudSpeechToText-$RID.zip"
  ( cd "$OUT/stage/$RID" && zip -q -r -X "$ZIP" "$PLUGIN_FOLDER" )
  echo "    $(basename "$ZIP")  $(du -h "$ZIP" | cut -f1)"
done

rm -rf "$OUT/stage" "$OUT/build"
echo
echo "Done. Zips are in $OUT"
