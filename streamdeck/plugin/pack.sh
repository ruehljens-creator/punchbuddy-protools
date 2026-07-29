#!/bin/sh
# Packt den .sdPlugin-Ordner zu einer installierbaren .streamDeckPlugin-Datei.
# (Eine .streamDeckPlugin ist ein ZIP, dessen oberster Eintrag der
#  <UUID>.sdPlugin-Ordner ist.)
set -e
cd "$(dirname "$0")"

PLUGIN_DIR="com.punchbuddy.control.sdPlugin"
OUT="com.punchbuddy.control.streamDeckPlugin"

if [ ! -f "$PLUGIN_DIR/bin/plugin.js" ]; then
    echo "FEHLER: $PLUGIN_DIR/bin/plugin.js fehlt – zuerst 'npm run build' ausführen." >&2
    exit 1
fi

rm -f "$OUT"
# .DS_Store ausschliessen, deterministisch zippen.
zip -r -X "$OUT" "$PLUGIN_DIR" -x '*.DS_Store' >/dev/null
echo "Erzeugt: $OUT"
