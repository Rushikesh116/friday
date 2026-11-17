#!/bin/zsh
# Build Friday.app and install it, so Friday opens like any Mac app (Dock, Launchpad, Spotlight).
#
#   ./make-app.sh             build and install to /Applications
#   ./make-app.sh --dock      ...and keep Friday in the Dock
#   ./make-app.sh --no-install   only build (into ./build/Friday.app)
#
# Run it again after moving the friday folder or updating Friday's code layout; the app
# itself is a small launcher that runs Friday from this folder.
set -euo pipefail

ROOT=${0:A:h}
cd "$ROOT"
BUNDLE_ID="local.friday.assistant"
ADD_TO_DOCK=0
INSTALL=1
for arg in "$@"; do
  case $arg in
    --dock) ADD_TO_DOCK=1 ;;
    --no-install) INSTALL=0 ;;
    *) echo "Unknown option: $arg"; exit 2 ;;
  esac
done

# ---- checks -------------------------------------------------------------------------
[[ -x .venv/bin/python && -f friday/__main__.py ]] || { echo "Run ./setup.sh first (no .venv here)."; exit 1; }
PYHOME=$(sed -n 's/^home = //p' .venv/pyvenv.cfg)
PYVER=$(sed -n 's/^version_info = \([0-9]*\.[0-9]*\).*/\1/p' .venv/pyvenv.cfg)
PYCONFIG="$PYHOME/python$PYVER-config"
[[ -x $PYCONFIG ]] || { echo "Can't find $PYCONFIG (the Python that .venv was made with)."; exit 1; }
command -v clang >/dev/null || { echo "Needs Apple's command line tools: xcode-select --install"; exit 1; }

BUILD="$ROOT/build"
APP="$BUILD/Friday.app"
mkdir -p "$BUILD"
[[ -d $APP ]] && rm -rf -- "$APP"   # previous build output only
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ---- launcher -----------------------------------------------------------------------
echo "• Compiling the launcher"
clang -O2 -Wall $("$PYCONFIG" --includes) macos/launcher.c -o "$APP/Contents/MacOS/Friday" \
  $("$PYCONFIG" --ldflags --embed) -framework CoreFoundation

# ---- icon ---------------------------------------------------------------------------
echo "• Drawing the icon"
.venv/bin/python macos/make_icon.py "$BUILD/icon-1024.png"
ICONSET="$BUILD/Friday.iconset"
[[ -d $ICONSET ]] && rm -rf -- "$ICONSET"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$BUILD/icon-1024.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$BUILD/icon-1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/Friday.icns"

# ---- Info.plist ---------------------------------------------------------------------
echo "• Writing Info.plist"
ROOT_XML=$(print -r -- "$ROOT" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Friday</string>
  <key>CFBundleDisplayName</key><string>Friday</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>Friday</string>
  <key>CFBundleIconFile</key><string>Friday</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>$(date +%Y%m%d%H%M)</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>LSMultipleInstancesProhibited</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSSupportsAutomaticGraphicsSwitching</key><true/>
  <key>NSHumanReadableCopyright</key><string>Your personal assistant, powered by Claude.</string>
  <key>NSMicrophoneUsageDescription</key><string>Friday listens for her name and your requests. Your speech is turned into text right here on your Mac.</string>
  <key>NSAppleEventsUsageDescription</key><string>Friday uses apps like Mail and Calendar when you ask her to.</string>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key><true/>
    <key>NSAllowsArbitraryLoadsInWebContent</key><true/>
  </dict>
  <key>FridayProjectPath</key><string>$ROOT_XML</string>
</dict>
</plist>
PLIST
plutil -lint "$APP/Contents/Info.plist" >/dev/null

# ---- sign (ad hoc, so macOS can remember the permissions you grant) --------------------
echo "• Signing"
codesign --force --sign - "$APP" 2>/dev/null
codesign --verify "$APP"

if (( ! INSTALL )); then
  echo "Built $APP"
  exit 0
fi

# ---- install ------------------------------------------------------------------------
DEST=/Applications
[[ -w $DEST ]] || { DEST="$HOME/Applications"; mkdir -p "$DEST"; }
TARGET="$DEST/Friday.app"
if [[ -e $TARGET ]]; then
  existing=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$TARGET/Contents/Info.plist" 2>/dev/null || true)
  if [[ $existing != "$BUNDLE_ID" ]]; then
    echo "There's already a different app at $TARGET (bundle id '$existing'). Not replacing it."
    exit 1
  fi
fi
pid=$(pgrep -f "/Friday.app/Contents/MacOS/Friday" || true)
if [[ -n $pid ]]; then
  echo "• Quitting the running Friday so it can be updated"
  kill -TERM ${=pid} 2>/dev/null || true
  for _ in {1..40}; do pgrep -f "/Friday.app/Contents/MacOS/Friday" >/dev/null || break; sleep 0.5; done
  if pgrep -f "/Friday.app/Contents/MacOS/Friday" >/dev/null; then
    echo "Friday is still running. Quit it (⌘Q) and run this again."
    exit 1
  fi
fi
echo "• Installing to $TARGET"
[[ -d $TARGET ]] && rm -rf -- "$TARGET"   # our own previous install (checked above)
ditto "$APP" "$TARGET"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$TARGET"

# ---- Dock ---------------------------------------------------------------------------
if (( ADD_TO_DOCK )); then
  if defaults read com.apple.dock persistent-apps 2>/dev/null | grep -q "Friday.app"; then
    echo "• Friday is already in the Dock"
  else
    echo "• Adding Friday to the Dock (backup of your Dock layout: build/dock-backup.plist)"
    defaults export com.apple.dock "$BUILD/dock-backup.plist"
    defaults write com.apple.dock persistent-apps -array-add \
      "<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>$TARGET/</string><key>_CFURLStringType</key><integer>0</integer></dict></dict></dict>"
    killall Dock
  fi
fi

echo "Done. Open Friday from the Dock, Launchpad or Spotlight (⌘Space, \"Friday\")."
