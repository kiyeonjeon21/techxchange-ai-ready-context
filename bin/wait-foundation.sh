#!/bin/zsh
# Polls until CPD foundation (zenservice + ibmcpd) is Completed, or deployer fails.
# Exits 0 when foundation ready; 2 if deployer pod gone/failed; 3 on timeout.
LOG=tmp/foundation-watch.log
MAX=100   # ~100 * 180s ≈ 5h
i=0
echo "$(date '+%H:%M:%S') watcher start" >> "$LOG"
while [ $i -lt $MAX ]; do
  i=$((i+1))
  # deployer pod health
  pod=$(oc get pods -n cloud-pak-deployer -o name 2>/dev/null | head -1)
  phase=$(oc get pods -n cloud-pak-deployer -o jsonpath='{.items[0].status.phase}' 2>/dev/null)
  ibmcpd=$(oc get ibmcpd -n cpd -o jsonpath='{.items[0].status.controlPlaneStatus}' 2>/dev/null)
  zen=$(oc get zenservice -n cpd -o jsonpath='{.items[0].status.zenStatus}' 2>/dev/null)
  # field-name-agnostic fallback: does the full status blob report Completed?
  icp_full=$(oc get ibmcpd -n cpd -o jsonpath='{.items[0].status}' 2>/dev/null)
  zen_full=$(oc get zenservice -n cpd -o jsonpath='{.items[0].status}' 2>/dev/null)
  ready=0
  if echo "$icp_full" | grep -q Completed && echo "$zen_full" | grep -q Completed; then ready=1; fi
  echo "$(date '+%H:%M:%S') i=$i deployerPhase=$phase ibmcpd=$ibmcpd zen=$zen ready=$ready" >> "$LOG"
  if [ "$ready" = "1" ]; then
    echo "$(date '+%H:%M:%S') FOUNDATION READY" >> "$LOG"; exit 0
  fi
  if [ "$phase" = "Failed" ] || [ "$phase" = "Succeeded" ]; then
    echo "$(date '+%H:%M:%S') deployer phase=$phase but foundation not ready" >> "$LOG"; exit 2
  fi
  sleep 180
done
echo "$(date '+%H:%M:%S') TIMEOUT" >> "$LOG"; exit 3
