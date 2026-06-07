"""Incremental re-index: re-run ingest for every source in the AstraDB doc_registry.
Hash-based skip in ingest.py makes unchanged docs a no-op. Run by the CronJob."""
import subprocess, sys, os
import rag_common as rc
docs = rc.astra({"find": {"projection": {"source": 1, "title": 1}}}, rc.ASTRA_DOCS).get("data", {}).get("documents", [])
print(f"[reindex] {len(docs)} tracked sources")
for d in docs:
    src = d.get("source"); title = d.get("title") or ""
    if not src: continue
    print(f"[reindex] -> {src}")
    subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "ingest.py"), src, "--title", title])
print("[reindex] done")
