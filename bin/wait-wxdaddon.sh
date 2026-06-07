#!/bin/zsh
# Polls until watsonx.data wxdaddon RECONCILE/wxdStatus = Completed.
LOG=tmp/wxdaddon-watch.log
MAX=80   # ~80 * 90s = 2h
i=0
echo "$(date '+%H:%M:%S') wxdaddon watcher start" >> "$LOG"
while [ $i -lt $MAX ]; do
  i=$((i+1))
  st=$(oc get wxdaddon wxdaddon -n cpd -o jsonpath='{.status.wxdStatus}' 2>/dev/null)
  pr=$(oc get wxdaddon wxdaddon -n cpd -o jsonpath='{.status.progress}' 2>/dev/null)
  msg=$(oc get wxdaddon wxdaddon -n cpd -o jsonpath='{.status.progressMessage}' 2>/dev/null)
  echo "$(date '+%H:%M:%S') i=$i wxdStatus=$st progress=$pr msg=$msg" >> "$LOG"
  if [ "$st" = "Completed" ]; then echo "$(date '+%H:%M:%S') WXDADDON COMPLETED" >> "$LOG"; exit 0; fi
  sleep 90
done
echo "$(date '+%H:%M:%S') TIMEOUT" >> "$LOG"; exit 3
