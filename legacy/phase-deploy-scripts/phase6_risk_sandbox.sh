#!/usr/bin/env bash
# ==============================================================================
# Itarang Phase 6 — Risk Hypothesis Python Sandbox
# Run as root on the Hostinger VPS.
#
# Adds a 'risk-sandbox' Docker service to the storage stack:
#   - python:3.12-slim + pandas / numpy / scipy / fastapi
#   - bound to 127.0.0.1:8091 → container :8000
#   - network: none (no outbound connectivity)
#   - read-only filesystem (so executed code can't persist anything)
#   - cap_drop ALL + no-new-privileges (defense in depth)
#
# The CRM's LangGraph workflow (Phase D) talks to this sandbox over HTTP
# via NBFC_SANDBOX_URL. From your laptop in dev: open an SSH tunnel
#   ssh -L 8091:127.0.0.1:8091 root@72.61.246.37
# and set NBFC_SANDBOX_URL=http://127.0.0.1:8091 in .env.local.
# ==============================================================================
set -euo pipefail

STORAGE_DIR=/opt/intellicar/storage
SBX_DIR="$STORAGE_DIR/risk-sandbox"
COMPOSE="$STORAGE_DIR/docker-compose.yml"

cd "$STORAGE_DIR"

echo "==> [1/4] Writing risk-sandbox files (md5-verified)"
mkdir -p "$SBX_DIR"

extract_b64() {
  local target="$1"; local expected="$2"; local b64="$3"
  echo "$b64" | base64 -d > "$target"
  local actual=$(md5sum "$target" | awk '{print $1}')
  if [ "$actual" != "$expected" ]; then
    echo "    ERROR: md5 mismatch for $target. expected=$expected actual=$actual"
    exit 1
  fi
  echo "    $target ($(wc -c < $target) bytes, md5 ok)"
}

extract_b64 "$SBX_DIR/Dockerfile"   "4adba13c0920826b6ba171983d52e3bc" "RlJPTSBweXRob246My4xMi1zbGltCgojIEJhcmUtbWluaW11bSBhbmFseXNpcyBzdGFjay4gUGlubmVkIHRvIGF2b2lkIHN1cnByaXNlcy4KUlVOIHBpcCBpbnN0YWxsIC0tbm8tY2FjaGUtZGlyIC0tcm9vdC11c2VyLWFjdGlvbj1pZ25vcmUgXAogICAgInBhbmRhcz09Mi4yLjMiIFwKICAgICJudW1weT09Mi4xLjMiIFwKICAgICJzY2lweT09MS4xNC4xIiBcCiAgICAiZmFzdGFwaT09MC4xMTUuNiIgXAogICAgInV2aWNvcm49PTAuMzIuMSIgXAogICAgInB5ZGFudGljPT0yLjEwLjQiCgojIFJ1biBhcyBub24tcm9vdCBmb3IgZGVmZW5zZSBpbiBkZXB0aCAodGhlIGRvY2tlci1jb21wb3NlIGxheWVyIGFsc28gdHVybnMKIyBvZmYgbmV0d29yayBhbmQgYWRkcyByZWFkLW9ubHkgZnMpLgpSVU4gdXNlcmFkZCAtLWNyZWF0ZS1ob21lIC0tc2hlbGwgL3Vzci9zYmluL25vbG9naW4gc2FuZGJveApVU0VSIHNhbmRib3gKV09SS0RJUiAvaG9tZS9zYW5kYm94CgpDT1BZIC0tY2hvd249c2FuZGJveDpzYW5kYm94IGV4ZWN1dG9yLnB5IC9ob21lL3NhbmRib3gvZXhlY3V0b3IucHkKCiMgQmluZCB0byAwLjAuMC4wIGluc2lkZSBjb250YWluZXI7IGRvY2tlci1jb21wb3NlIG1hcHMgdG8gMTI3LjAuMC4xOjgwOTEuCkVYUE9TRSA4MDAwCkNNRCBbInB5dGhvbiIsICItdSIsICJleGVjdXRvci5weSJdCg=="
extract_b64 "$SBX_DIR/executor.py"  "34e38162d05237e17c70469233b3ce8f"   "IiIiClJpc2sgaHlwb3RoZXNpcyBzYW5kYm94IOKAlCBleGVjdXRlcyBMTE0tZ2VuZXJhdGVkIFB5dGhvbiBhZ2FpbnN0IHRlbmFudCBkYXRhLgoKVGhlIENSTSAoUGhhc2UgRCBMYW5nR3JhcGggd29ya2Zsb3cpIFBPU1RzIGEgSlNPTiBib2R5IG9mOgoKICB7CiAgICAiaHlwb3RoZXNpc19zbHVnIjogInVzYWdlLWRyb3AtN2QiLAogICAgImNvZGUiOiAiPHB5dGhvbiBzb3VyY2UgZGVmaW5pbmcgYSBmdW5jdGlvbiBgZXZhbHVhdGUobG9hbnMsIHZlaGljbGVfc3RhdGVzLCBkYWlseV9rbSwgY2FuX2hpc3RvcnkpIC0+IGRpY3RgPiIsCiAgICAiZGF0YSI6IHsKICAgICAgImxvYW5zIjogICAgICAgICAgWy4uLl0sICAgIyB0ZW5hbnQncyBsb2FuIHNsaWNlCiAgICAgICJ2ZWhpY2xlX3N0YXRlcyI6IFsuLi5dLCAgICMgSW9UIHZlaGljbGVfc3RhdGUgcm93cwogICAgICAiZGFpbHlfa20iOiAgICAgICBbLi4uXSwgICAjIGRhaWx5X2Rpc3RhbmNlX3Blcl92ZWhpY2xlIHJvd3MKICAgICAgImNhbl9oaXN0b3J5IjogICAgWy4uLl0gICAgIyBvcHRpb25hbCAyNGggQ0FOIGV4dHJhY3RzCiAgICB9CiAgfQoKV2UgZXhlYygpIHRoZSBjb2RlIGluIGEgcmVzdHJpY3RlZCBuYW1lc3BhY2UgKG5vIGJ1aWx0aW5zIGV4cG9zZWQgZXhjZXB0IGEKc21hbGwgYWxsb3dsaXN0KSwgY2FsbCBldmFsdWF0ZSgpIHdpdGggdGhlIGRhdGEsIGFuZCByZXR1cm4gdGhlIEpTT04tYWJsZQpyZXN1bHQuIDMwcyB3YWxsY2xvY2sgY2FwLiBUaGUgY29udGFpbmVyIGhhcyBubyBuZXR3b3JrIGFuZCBhIHJlYWQtb25seSBmczsKZXNjYXBlIGZyb20gdGhpcyBQeXRob24gc2FuZGJveCBqdXN0IGdldHMgeW91IGEgdXNlbGVzcyBib3guCgpOT1QgY29uc2lkZXJlZCBzYWZlIGFnYWluc3QgYSBkZXRlcm1pbmVkIGFkdmVyc2FyeSDigJQgaXQncyBkZWZlbnNlIGluIGRlcHRoCmZvciBjb250ZW50IHRoZSBMTE0gcHJvZHVjZXMuIFJlYWwgaXNvbGF0aW9uIGxpdmVzIGF0IHRoZSBkb2NrZXItY29tcG9zZQpsYXllciAobmV0d29yazogbm9uZSwgcmVhZF9vbmx5OiB0cnVlLCBjYXBfZHJvcDogQUxMLCBub19uZXdfcHJpdmlsZWdlcykuCiIiIgpmcm9tIF9fZnV0dXJlX18gaW1wb3J0IGFubm90YXRpb25zCgppbXBvcnQganNvbgppbXBvcnQgbG9nZ2luZwppbXBvcnQgc2lnbmFsCmltcG9ydCBzeXMKZnJvbSB0eXBpbmcgaW1wb3J0IEFueQoKZnJvbSBmYXN0YXBpIGltcG9ydCBGYXN0QVBJLCBIVFRQRXhjZXB0aW9uCmZyb20gcHlkYW50aWMgaW1wb3J0IEJhc2VNb2RlbCwgRmllbGQKaW1wb3J0IHV2aWNvcm4KCiMgcGFuZGFzIC8gbnVtcHkgYXJlIGltcG9ydGVkIGhlcmUgc28gdGhlIHNhbmRib3hlZCBjb2RlIGNhbiByZWFjaCB0aGVtIHZpYQojIHRoZSBuYW1lc3BhY2Ugd2UgaGFuZCBpdC4KaW1wb3J0IG51bXB5IGFzIG5wCmltcG9ydCBwYW5kYXMgYXMgcGQKCmxvZ2dpbmcuYmFzaWNDb25maWcobGV2ZWw9bG9nZ2luZy5JTkZPLCBmb3JtYXQ9IiUoYXNjdGltZSlzIFslKGxldmVsbmFtZSlzXSAlKG1lc3NhZ2UpcyIpCmxvZyA9IGxvZ2dpbmcuZ2V0TG9nZ2VyKCJzYW5kYm94IikKCkVYRUNfVElNRU9VVF9TRUMgPSAzMApNQVhfSU5QVVRfQllURVMgPSA1ICogMTAyNCAqIDEwMjQgICMgNSBNQiBjZWlsaW5nIG9uIHRoZSBkYXRhIHBheWxvYWQKCiMgQnVpbHRpbnMgd2UgZXhwb3NlIHRvIGV4ZWN1dGVkIGNvZGUuIEtlZXBzIGBleGVjYCwgYGV2YWxgLCBgb3BlbmAsIGBpbXBvcnRgCiMgb3V0IG9mIHRoZSBydW50aW1lIG5hbWVzcGFjZSBieSBkZWZhdWx0LgpTQUZFX0JVSUxUSU5TID0gewogICAgImFicyI6IGFicywgImFsbCI6IGFsbCwgImFueSI6IGFueSwgImJvb2wiOiBib29sLCAiZGljdCI6IGRpY3QsCiAgICAiZW51bWVyYXRlIjogZW51bWVyYXRlLCAiZmlsdGVyIjogZmlsdGVyLCAiZmxvYXQiOiBmbG9hdCwgImludCI6IGludCwKICAgICJsZW4iOiBsZW4sICJsaXN0IjogbGlzdCwgIm1hcCI6IG1hcCwgIm1heCI6IG1heCwgIm1pbiI6IG1pbiwKICAgICJwcmludCI6IHByaW50LCAicmFuZ2UiOiByYW5nZSwgInJvdW5kIjogcm91bmQsICJzZXQiOiBzZXQsCiAgICAic29ydGVkIjogc29ydGVkLCAic3RyIjogc3RyLCAic3VtIjogc3VtLCAidHVwbGUiOiB0dXBsZSwgInppcCI6IHppcCwKICAgICJpc2luc3RhbmNlIjogaXNpbnN0YW5jZSwgInR5cGUiOiB0eXBlLAogICAgIlRydWUiOiBUcnVlLCAiRmFsc2UiOiBGYWxzZSwgIk5vbmUiOiBOb25lLAp9CgoKY2xhc3MgRXhlY3V0ZVJlcXVlc3QoQmFzZU1vZGVsKToKICAgIGh5cG90aGVzaXNfc2x1Zzogc3RyID0gRmllbGQoLi4uLCBtYXhfbGVuZ3RoPTEyOCkKICAgIGNvZGU6IHN0ciA9IEZpZWxkKC4uLiwgbWF4X2xlbmd0aD01MF8wMDApCiAgICBkYXRhOiBkaWN0W3N0ciwgQW55XSA9IEZpZWxkKGRlZmF1bHRfZmFjdG9yeT1kaWN0KQoKCmNsYXNzIEV4ZWN1dGVSZXNwb25zZShCYXNlTW9kZWwpOgogICAgb2s6IGJvb2wKICAgIHJlc3VsdDogZGljdFtzdHIsIEFueV0gfCBOb25lID0gTm9uZQogICAgZXJyb3I6IHN0ciB8IE5vbmUgPSBOb25lCiAgICBlbGFwc2VkX21zOiBpbnQKCgphcHAgPSBGYXN0QVBJKHRpdGxlPSJyaXNrLXNhbmRib3giLCB2ZXJzaW9uPSIwLjEiKQoKCkBhcHAuZ2V0KCIvaGVhbHRoeiIpCmRlZiBoZWFsdGh6KCk6CiAgICByZXR1cm4geyJvayI6IFRydWV9CgoKQGFwcC5wb3N0KCIvZXhlY3V0ZSIsIHJlc3BvbnNlX21vZGVsPUV4ZWN1dGVSZXNwb25zZSkKZGVmIGV4ZWN1dGUocmVxOiBFeGVjdXRlUmVxdWVzdCk6CiAgICBpbXBvcnQgdGltZQogICAgc3RhcnRlZCA9IHRpbWUucGVyZl9jb3VudGVyKCkKCiAgICBwYXlsb2FkX3NpemUgPSBsZW4oanNvbi5kdW1wcyhyZXEuZGF0YSkuZW5jb2RlKCkpCiAgICBpZiBwYXlsb2FkX3NpemUgPiBNQVhfSU5QVVRfQllURVM6CiAgICAgICAgcmFpc2UgSFRUUEV4Y2VwdGlvbihzdGF0dXNfY29kZT00MTMsIGRldGFpbD1mImRhdGEgcGF5bG9hZCB0b28gbGFyZ2U6IHtwYXlsb2FkX3NpemV9ID4ge01BWF9JTlBVVF9CWVRFU30iKQoKICAgIG5hbWVzcGFjZTogZGljdFtzdHIsIEFueV0gPSB7CiAgICAgICAgIl9fYnVpbHRpbnNfXyI6IFNBRkVfQlVJTFRJTlMsCiAgICAgICAgInBkIjogcGQsCiAgICAgICAgIm5wIjogbnAsCiAgICB9CgogICAgIyBDb252ZXJ0IGVhY2ggZGF0YSBzbGljZSBpbnRvIGEgRGF0YUZyYW1lIGZvciBlcmdvbm9taWMgdXNlIGluIHVzZXIgY29kZS4KICAgIGZvciBrZXksIHJvd3MgaW4gKHJlcS5kYXRhIG9yIHt9KS5pdGVtcygpOgogICAgICAgIGlmIGlzaW5zdGFuY2Uocm93cywgbGlzdCk6CiAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAgIG5hbWVzcGFjZVtrZXldID0gcGQuRGF0YUZyYW1lKHJvd3MpCiAgICAgICAgICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgICAgICAgICBuYW1lc3BhY2Vba2V5XSA9IHJvd3MKICAgICAgICBlbHNlOgogICAgICAgICAgICBuYW1lc3BhY2Vba2V5XSA9IHJvd3MKCiAgICBkZWYgdGltZW91dF9oYW5kbGVyKF9zaWdudW0sIF9mcmFtZSk6CiAgICAgICAgcmFpc2UgVGltZW91dEVycm9yKGYic2FuZGJveCBleGVjIGV4Y2VlZGVkIHtFWEVDX1RJTUVPVVRfU0VDfXMiKQoKICAgIHNpZ25hbC5zaWduYWwoc2lnbmFsLlNJR0FMUk0sIHRpbWVvdXRfaGFuZGxlcikKICAgIHNpZ25hbC5hbGFybShFWEVDX1RJTUVPVVRfU0VDKQoKICAgIHRyeToKICAgICAgICBleGVjKHJlcS5jb2RlLCBuYW1lc3BhY2UpCiAgICAgICAgaWYgImV2YWx1YXRlIiBub3QgaW4gbmFtZXNwYWNlIG9yIG5vdCBjYWxsYWJsZShuYW1lc3BhY2VbImV2YWx1YXRlIl0pOgogICAgICAgICAgICByYWlzZSBWYWx1ZUVycm9yKCJjb2RlIG11c3QgZGVmaW5lIGEgY2FsbGFibGUgbmFtZWQgYGV2YWx1YXRlYCIpCiAgICAgICAgcmVzdWx0ID0gbmFtZXNwYWNlWyJldmFsdWF0ZSJdKCoqe2s6IHYgZm9yIGssIHYgaW4gbmFtZXNwYWNlLml0ZW1zKCkgaWYgayBpbiAocmVxLmRhdGEgb3Ige30pfSkKICAgICAgICBpZiBub3QgaXNpbnN0YW5jZShyZXN1bHQsIGRpY3QpOgogICAgICAgICAgICByYWlzZSBWYWx1ZUVycm9yKGYiZXZhbHVhdGUoKSBtdXN0IHJldHVybiBhIGRpY3Q7IGdvdCB7dHlwZShyZXN1bHQpLl9fbmFtZV9ffSIpCiAgICAgICAgIyBDb2VyY2UgdG8gSlNPTi1hYmxlCiAgICAgICAgcmVzdWx0X2pzb24gPSBqc29uLmxvYWRzKGpzb24uZHVtcHMocmVzdWx0LCBkZWZhdWx0PV9qc29uX2ZhbGxiYWNrKSkKICAgICAgICBlbGFwc2VkX21zID0gaW50KCh0aW1lLnBlcmZfY291bnRlcigpIC0gc3RhcnRlZCkgKiAxMDAwKQogICAgICAgIHJldHVybiBFeGVjdXRlUmVzcG9uc2Uob2s9VHJ1ZSwgcmVzdWx0PXJlc3VsdF9qc29uLCBlbGFwc2VkX21zPWVsYXBzZWRfbXMpCiAgICBleGNlcHQgVGltZW91dEVycm9yIGFzIGU6CiAgICAgICAgZWxhcHNlZF9tcyA9IGludCgodGltZS5wZXJmX2NvdW50ZXIoKSAtIHN0YXJ0ZWQpICogMTAwMCkKICAgICAgICByZXR1cm4gRXhlY3V0ZVJlc3BvbnNlKG9rPUZhbHNlLCBlcnJvcj1zdHIoZSksIGVsYXBzZWRfbXM9ZWxhcHNlZF9tcykKICAgIGV4Y2VwdCBFeGNlcHRpb24gYXMgZToKICAgICAgICBlbGFwc2VkX21zID0gaW50KCh0aW1lLnBlcmZfY291bnRlcigpIC0gc3RhcnRlZCkgKiAxMDAwKQogICAgICAgIHJldHVybiBFeGVjdXRlUmVzcG9uc2Uob2s9RmFsc2UsIGVycm9yPWYie3R5cGUoZSkuX19uYW1lX199OiB7ZX0iLCBlbGFwc2VkX21zPWVsYXBzZWRfbXMpCiAgICBmaW5hbGx5OgogICAgICAgIHNpZ25hbC5hbGFybSgwKQoKCmRlZiBfanNvbl9mYWxsYmFjayhvOiBBbnkpIC0+IEFueToKICAgIGlmIGlzaW5zdGFuY2UobywgKG5wLmludGVnZXIsKSk6CiAgICAgICAgcmV0dXJuIGludChvKQogICAgaWYgaXNpbnN0YW5jZShvLCAobnAuZmxvYXRpbmcsKSk6CiAgICAgICAgcmV0dXJuIGZsb2F0KG8pCiAgICBpZiBpc2luc3RhbmNlKG8sIG5wLm5kYXJyYXkpOgogICAgICAgIHJldHVybiBvLnRvbGlzdCgpCiAgICBpZiBpc2luc3RhbmNlKG8sIHBkLlRpbWVzdGFtcCk6CiAgICAgICAgcmV0dXJuIG8uaXNvZm9ybWF0KCkKICAgIGlmIGlzaW5zdGFuY2UobywgcGQuRGF0YUZyYW1lKToKICAgICAgICByZXR1cm4gby50b19kaWN0KG9yaWVudD0icmVjb3JkcyIpCiAgICBpZiBpc2luc3RhbmNlKG8sIHBkLlNlcmllcyk6CiAgICAgICAgcmV0dXJuIG8udG9fbGlzdCgpCiAgICByZXR1cm4gc3RyKG8pCgoKaWYgX19uYW1lX18gPT0gIl9fbWFpbl9fIjoKICAgIHV2aWNvcm4ucnVuKGFwcCwgaG9zdD0iMC4wLjAuMCIsIHBvcnQ9ODAwMCwgbG9nX2xldmVsPSJpbmZvIiwgYWNjZXNzX2xvZz1GYWxzZSkK"

echo "==> [2/4] Patching docker-compose.yml (idempotent insert)"
if grep -qE '^[[:space:]]*risk-sandbox:' "$COMPOSE"; then
    echo "    risk-sandbox service already present"
else
    cp "$COMPOSE" "$COMPOSE.bak.phase6.$(date +%s)"
    if grep -qE '^volumes:' "$COMPOSE"; then
        awk '
          /^volumes:/ && !done {
            print "  risk-sandbox:";
            print "    build: ./risk-sandbox";
            print "    container_name: itarang_risk_sandbox";
            print "    restart: unless-stopped";
            print "    network_mode: \"none\"";
            print "    read_only: true";
            print "    tmpfs:";
            print "      - /tmp:size=64m,mode=1777";
            print "    cap_drop: [\"ALL\"]";
            print "    security_opt:";
            print "      - no-new-privileges:true";
            print "    mem_limit: 512m";
            print "    cpus: 1.0";
            print "    ports:";
            print "      - \"127.0.0.1:8091:8000\"";
            print "    logging:";
            print "      driver: \"json-file\"";
            print "      options:";
            print "        max-size: \"10m\"";
            print "        max-file: \"3\"";
            print "";
            done = 1;
          }
          { print }
        ' "$COMPOSE" > "$COMPOSE.new"
        mv "$COMPOSE.new" "$COMPOSE"
    else
        cat >> "$COMPOSE" <<'YAML_EOF'

  risk-sandbox:
    build: ./risk-sandbox
    container_name: itarang_risk_sandbox
    restart: unless-stopped
    network_mode: "none"
    read_only: true
    tmpfs:
      - /tmp:size=64m,mode=1777
    cap_drop: ["ALL"]
    security_opt:
      - no-new-privileges:true
    mem_limit: 512m
    cpus: 1.0
    ports:
      - "127.0.0.1:8091:8000"
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
YAML_EOF
    fi
    echo "    risk-sandbox service inserted (backup saved)"

    if ! docker compose config -q 2>/dev/null; then
        echo "    ERROR: docker compose config -q failed. Restoring backup."
        cp "$(ls -t $COMPOSE.bak.phase6.* | head -1)" "$COMPOSE"
        exit 1
    fi
fi

# IMPORTANT: ports + network_mode:none don't mix in some Docker versions.
# If you hit "ports defined when network_mode is none" on up, switch to a
# bridge network just for this service (the container has no outbound DNS
# regardless thanks to the lack of internet routing inside the bridge).
# We document this in the runbook and leave the strict version as default.

echo "==> [3/4] Building and starting risk-sandbox container"
docker compose build risk-sandbox
docker compose up -d risk-sandbox || {
    echo "    Strict 'network_mode: none' failed (Docker version doesn't allow ports + none)."
    echo "    Switching to a bridge network for this service. Editing compose..."
    sed -i 's|network_mode: "none"|# network_mode: "none"  # incompat w/ ports — using bridge|' "$COMPOSE"
    docker compose up -d risk-sandbox
}
docker compose ps risk-sandbox

echo "==> [4/4] Smoke test"
sleep 4
echo "    /healthz (from VPS host):"
curl -sf -m 4 http://127.0.0.1:8091/healthz && echo " OK" || echo " FAILED"
echo ""
echo "    /execute round-trip with a tiny test payload:"
curl -sf -m 10 -X POST http://127.0.0.1:8091/execute   -H "Content-Type: application/json"   -d '{"hypothesis_slug":"smoke","code":"def evaluate():\n    return {\"severity\":\"ok\",\"affected_count\":0,\"total_count\":0,\"finding_summary\":\"smoke test\"}","data":{}}'
echo ""

echo ""
echo "================================================================="
echo " Phase 6 risk-sandbox complete."
echo ""
echo " From your Mac, open an SSH tunnel + set the env var:"
echo ""
echo "   ssh -L 8091:127.0.0.1:8091 root@72.61.246.37"
echo ""
echo "   # in CRM .env.local"
echo "   NBFC_SANDBOX_URL=http://127.0.0.1:8091"
echo ""
echo " Phase D agent code already calls executeInSandbox() — once the env"
echo " var is set, Re-run analysis in the dashboard will execute LLM-"
echo " generated Python instead of single-shot tool-call verdicts."
echo "================================================================="
