#!/bin/sh
# A dated snapshot of the synced data every night: unchanged files are hard links to the previous
# night's (rides never change, so 30 nights cost little more than one copy). Keeps 30 snapshots;
# the sync history (every version a sync replaced or deleted) is kept forever: nothing you record
# is ever thrown away.
set -eu
DATA=/var/lib/assioma/data
DEST=/var/lib/assioma/backups
HIST=/var/lib/assioma/history
mkdir -p "$DEST"
TODAY=$(date -u +%Y-%m-%d)
PREV=$(ls -1d "$DEST"/20* 2>/dev/null | grep -v "/$TODAY\$" | tail -n 1 || true)
if [ -n "$PREV" ]; then
  rsync -a --delete --exclude 'rides/.cache/' --link-dest="$PREV" "$DATA"/ "$DEST/$TODAY"/
else
  rsync -a --delete --exclude 'rides/.cache/' "$DATA"/ "$DEST/$TODAY"/
fi
ls -1d "$DEST"/20* | head -n -30 | xargs -r rm -rf
chown -R assioma:assioma "$DEST"
echo "backup $TODAY: $(du -sh "$DEST/$TODAY" | cut -f1), $(ls -1d "$DEST"/20* | wc -l) snapshots"
