#!/bin/bash
# restrict_network.sh — apply the release pipeline's pf network restriction.
#
# The frozen reference and recreated app stay in a stable OFFLINE state during
# preparation, recreation and eval. The immutable test suite is an input; this
# script never runs an authoring/test-generation stage.
#
# Idempotent: rebuilds the rule file and re-applies each call. Source it (so the
# fail-closed `exit 1` propagates to the caller) OR run it standalone.
#
# Allows (per-uid): BASE_URL upstream + MODEL_API_ENDPOINTS + VLM endpoint + DNS
#   + SSH (control channel). Blocks everything else for the isolation account and
#   harness/GUI account. Loopback is `set skip on lo0`. Torn down only in cleanup.
#
# NET_PHASE (default "full") selects how tight the allow-list is:
#   full        — trusted harness preparation and eval. Allows configured model
#                 endpoints and the VLM endpoint for the harness user.
#   recreation  — recreation LOCKDOWN. Uses a GLOBAL default-deny
#                 (`block out all`, Windows-style blockoutbound) INSTEAD of the
#                 per-uid block. per-uid pf only catches sockets OWNED by
#                 the two run accounts, so any process owned by root or a service uid
#                 slips through: system daemons (softwareupdate, trustd/OCSP,
#                 iCloud) and BACKGROUND NSURLSession downloads (handled by the
#                 nsurlsessiond daemon, uid _nsurlsessiond) are NOT owned by the
#                 blocked users. (In-process user networking — curl, Safari's
#                 network process, an nscurl LocalDataTask — IS uid 501/502 and is
#                 already blocked by per-uid; the residual gap is specifically the
#                 root/service-uid class.) Verified: a direct socket as root
#                 reaches the net under per-uid but TIMES OUT under this global
#                 rule. Per the spec ("禁网配置下……只保留 model proxy port"), the
#                 ONLY egress kept open (for ALL uids) is the model proxy channel
#                 (BASE_URL host:port), SSH (control channel), and DNS. Claude reaches
#                 it through the loopback credential proxy; Codex reaches LiteLLM directly.
#                 VLM is not needed until eval (which re-applies NET_PHASE=full).
#
# Env: BASE_URL, MODEL_API_ENDPOINTS, VLM_BASE_URL, DEVAGENT_USER, WORK_DIR,
#   NET_PHASE (default full), BLOCK_HARNESS_NET (default true),
#   ALLOW_NO_NETWORK_ISOLATION (default false).

NET_PHASE="${NET_PHASE:-full}"
if [ "$NET_PHASE" = "recreation" ]; then
    echo "[net] Restricting network access (recreation LOCKDOWN — only model proxy port + DNS + SSH)..."
else
    echo "[net] Restricting network access (trusted harness/eval, reference stays offline)..."
fi

MODEL_API_ENDPOINTS="${MODEL_API_ENDPOINTS:-}"

# Fold the VLM endpoint host into the trusted-harness allow-list used by eval.
VLM_HOST=""
if [ -n "${VLM_BASE_URL:-}" ]; then
    VLM_HOST=$(echo "$VLM_BASE_URL" | sed -E 's|https?://||; s|/.*||; s|:.*||')
fi
ALL_ENDPOINTS="$MODEL_API_ENDPOINTS"
if [ -n "$VLM_HOST" ] && ! echo " $ALL_ENDPOINTS " | grep -q " $VLM_HOST "; then
    ALL_ENDPOINTS="$ALL_ENDPOINTS $VLM_HOST"
fi
if [ -n "$VLM_HOST" ]; then
    VLM_IPS=$(dig +short "$VLM_HOST" A 2>/dev/null | grep -E '^[0-9]+\.' || true)
    if [ -z "$VLM_IPS" ] && echo "$VLM_HOST" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
        VLM_IPS="$VLM_HOST"
    fi
    if [ -z "$VLM_IPS" ]; then
        echo "ERROR: VLM endpoint is not resolvable for the eval allow-list: $VLM_HOST" >&2
        exit 1
    fi
fi

DEVAGENT_UID=$(id -u "${DEVAGENT_USER:-devagent}" 2>/dev/null || echo "")
if [ -z "$DEVAGENT_UID" ]; then
    echo "  WARNING: Could not resolve ${DEVAGENT_USER:-devagent} UID, falling back to generic block"
fi

PF_RULES_FILE="/tmp/pipeline_pf_rules.conf"
echo "set skip on lo0" > "$PF_RULES_FILE"

if [ "$NET_PHASE" = "recreation" ]; then
    # ── recreation: GLOBAL default-deny (Windows-style blockoutbound) ──
    # Per-uid pf only blocks sockets owned by the run accounts, so any process owned
    # by root or a service uid slips through: system daemons (softwareupdate,
    # trustd/OCSP, iCloud) and BACKGROUND NSURLSession downloads (nsurlsessiond,
    # uid _nsurlsessiond). A global `block out all` (all uids, incl. root/daemons)
    # closes that hole, matching Windows' blockoutbound. `set skip on lo0` keeps
    # loopback fully open, so
    # devagent->127.0.0.1 credential proxy, the cua-driver and MCP are unaffected.
    # The ONLY egress kept open (for ALL uids):
    #   1. model endpoint channel (BASE_URL host:port) used by the trusted credential
    #      proxy dials — the recreation agent reaches the model only via this;
    #   2. SSH (sshd is root; the global block would otherwise kill its reply
    #      socket and lock us / the monitor out of the sandbox);
    #   3. DNS (resolve BASE_URL when it is a hostname).
    DNS_RESOLVERS=$(scutil --dns 2>/dev/null | grep 'nameserver\[' | awk '{print $3}' | sort -u || true)
    if [ -z "$DNS_RESOLVERS" ]; then
        DNS_RESOLVERS=$(grep '^nameserver' /etc/resolv.conf 2>/dev/null | awk '{print $2}' || true)
    fi
    {
        # Control SSH: STATELESS quick passes. This lockdown is applied MID-CONNECTION
        # onto a controller SSH that has already been established for the whole run. A
        # `keep state` pass (or a broad `pass in all keep state`) creates a FRESH state
        # entry for that in-flight connection whose TCP seq window starts "now" and does
        # NOT match the connection's real in-flight sequence numbers -> the sshd reply
        # packets fail the state's seq check and get dropped -> the control channel dies
        # -> the controller's ServerAlive (30s x 3 = 90s) times out -> pipeline exit 255.
        # `no state` = pf does not create/seq-check a state for these -> the established
        # control channel survives the ruleset swap. Only source-port-22 (root sshd) and
        # inbound-to-22 are opened; devagent (uid 502) cannot bind :22, so this is not an
        # egress path a cheating recreation agent can abuse. (The old outbound
        # `to any port 22` rule was unnecessary — the target never dials out on 22 — and
        # a small exfil seam, so it is removed.)
        echo "pass in quick proto tcp to any port 22 no state"
        echo "pass out quick proto tcp from any port 22 no state"
        # Airtight default-deny for EVERYTHING else, ALL uids (browser/Safari HTTPS,
        # nsurlsessiond background downloads, system daemons, devagent, root). This is the
        # anti-cheat core the per-uid block could not achieve (non-devagent uids slipped
        # through). Kept exactly as before.
        echo "block out all"
        # Inbound return for the agent's OWN outbound (model proxy / DNS) via state. The
        # control SSH already matched the quick no-state rule above, so this broad
        # keep-state pass never touches (and cannot break) the control channel.
        echo "pass in all keep state"
    } >> "$PF_RULES_FILE"
    # Deployment-owned model endpoint selected for the active agent.
    PROXY_OPEN=""
    if [ -n "${BASE_URL:-}" ]; then
        PROXY_HOST=$(echo "$BASE_URL" | sed -E 's|https?://||; s|/.*||; s|:.*||')
        PROXY_PORT=$(echo "$BASE_URL" | sed -E 's|https?://||; s|/.*||' | grep -oE ':[0-9]+' | tr -d ':' || true)
        PROXY_IPS=$(dig +short "$PROXY_HOST" A 2>/dev/null | grep -E '^[0-9]+\.' || true)
        if [ -z "$PROXY_IPS" ] && echo "$PROXY_HOST" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
            PROXY_IPS="$PROXY_HOST"
        fi
        for ip in $PROXY_IPS; do
            echo "pass out quick proto tcp to $ip port {443, 80} keep state" >> "$PF_RULES_FILE"
            [ -n "$PROXY_PORT" ] && echo "pass out quick proto tcp to $ip port $PROXY_PORT keep state" >> "$PF_RULES_FILE"
            PROXY_OPEN="yes"
        done
    fi
    if [ -n "$PROXY_OPEN" ]; then
        echo "  Model proxy channel open: $PROXY_HOST${PROXY_PORT:+:$PROXY_PORT} ($PROXY_IPS)"
    else
        echo "ERROR: BASE_URL is not resolvable for the recreation allow-list: '${BASE_URL:-}'" >&2
        exit 1
    fi
    # DNS (needed to resolve BASE_URL if it is a hostname)
    if [ -n "$DNS_RESOLVERS" ]; then
        for resolver in $DNS_RESOLVERS; do
            echo "pass out quick proto {tcp, udp} to $resolver port 53 keep state" >> "$PF_RULES_FILE"
        done
        echo "  DNS allowed: $DNS_RESOLVERS"
    else
        echo "pass out quick proto {tcp, udp} to any port 53 keep state" >> "$PF_RULES_FILE"
    fi
    echo "  GLOBAL default-deny (blockoutbound) — only model proxy + SSH + DNS open (all uids)"

elif [ -n "$DEVAGENT_UID" ]; then
    # ── devagent (recreation agent) ──
    if [ -n "${BASE_URL:-}" ]; then
        PROXY_HOST=$(echo "$BASE_URL" | sed -E 's|https?://||; s|/.*||; s|:.*||')
        PROXY_PORT=$(echo "$BASE_URL" | sed -E 's|https?://||; s|/.*||' | grep -oE ':[0-9]+' | tr -d ':' || true)
        if [ -n "$PROXY_HOST" ] && [ -n "$PROXY_PORT" ]; then
            echo "pass out quick proto tcp to $PROXY_HOST port $PROXY_PORT user $DEVAGENT_UID keep state" >> "$PF_RULES_FILE"
            echo "  Allowed proxy: $PROXY_HOST:$PROXY_PORT (devagent)"
        fi
    fi
    for endpoint in $ALL_ENDPOINTS; do
        RESOLVED_IPS=$(dig +short "$endpoint" A 2>/dev/null | grep -E '^[0-9]+\.' || true)
        if [ -n "$RESOLVED_IPS" ]; then
            for ip in $RESOLVED_IPS; do
                echo "pass out quick proto tcp to $ip port {443, 80} user $DEVAGENT_UID keep state" >> "$PF_RULES_FILE"
            done
            echo "  Allowed: $endpoint ($RESOLVED_IPS)"
        else
            echo "  WARNING: Could not resolve $endpoint, skipping"
        fi
    done
    DNS_RESOLVERS=$(scutil --dns 2>/dev/null | grep 'nameserver\[' | awk '{print $3}' | sort -u || true)
    if [ -z "$DNS_RESOLVERS" ]; then
        DNS_RESOLVERS=$(grep '^nameserver' /etc/resolv.conf 2>/dev/null | awk '{print $2}' || true)
    fi
    if [ -n "$DNS_RESOLVERS" ]; then
        for resolver in $DNS_RESOLVERS; do
            echo "pass out quick proto {tcp, udp} to $resolver port 53 user $DEVAGENT_UID keep state" >> "$PF_RULES_FILE"
        done
        echo "  DNS restricted to: $DNS_RESOLVERS"
    else
        echo "  WARNING: Could not determine DNS resolvers, allowing all DNS for devagent"
        echo "pass out quick proto {tcp, udp} to any port 53 user $DEVAGENT_UID keep state" >> "$PF_RULES_FILE"
    fi
    echo "block out quick proto tcp from any to any user $DEVAGENT_UID" >> "$PF_RULES_FILE"
    echo "block out quick proto udp from any to any user $DEVAGENT_UID" >> "$PF_RULES_FILE"
    echo "  Per-user blocking: devagent (uid=$DEVAGENT_UID)"

    # ── harness/GUI account — the reference app and cua-driver run as
    #    this user, so blocking it is what actually keeps the reference OFFLINE and
    #    closes the privilege-borrowing gap. SSH survives (its socket is
    #    root's sshd, not this uid — verified on the sandbox Mac). BLOCK_HARNESS_NET
    #    =false opts out. Never block uid 0.
    HARNESS_UID=$(id -u "$(whoami)" 2>/dev/null || echo "")
    if [ "${BLOCK_HARNESS_NET:-true}" = "true" ] && [ -n "$HARNESS_UID" ] && [ "$HARNESS_UID" != "$DEVAGENT_UID" ] && [ "$HARNESS_UID" != "0" ]; then
        echo "pass out quick proto tcp from any to any port 22 user $HARNESS_UID keep state" >> "$PF_RULES_FILE"
        # Trusted harness traffic to the deployment platform pod proxy: allow its host on 443/80
        # and its explicit port (covers IP / nonstandard port / not-in-endpoints).
        if [ -n "${PROXY_HOST:-}" ]; then
            HARNESS_PROXY_IPS=$(dig +short "$PROXY_HOST" A 2>/dev/null | grep -E '^[0-9]+\.' || true)
            if [ -z "$HARNESS_PROXY_IPS" ] && echo "$PROXY_HOST" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
                HARNESS_PROXY_IPS="$PROXY_HOST"
            fi
            for ip in $HARNESS_PROXY_IPS; do
                echo "pass out quick proto tcp to $ip port {443, 80} user $HARNESS_UID keep state" >> "$PF_RULES_FILE"
                if [ -n "${PROXY_PORT:-}" ]; then
                    echo "pass out quick proto tcp to $ip port $PROXY_PORT user $HARNESS_UID keep state" >> "$PF_RULES_FILE"
                fi
            done
        fi
        # Model + VLM endpoints used by the trusted harness and eval judge.
        for endpoint in $ALL_ENDPOINTS; do
            HARNESS_IPS=$(dig +short "$endpoint" A 2>/dev/null | grep -E '^[0-9]+\.' || true)
            for ip in $HARNESS_IPS; do
                echo "pass out quick proto tcp to $ip port {443, 80} user $HARNESS_UID keep state" >> "$PF_RULES_FILE"
            done
        done
        if [ -n "${DNS_RESOLVERS:-}" ]; then
            for resolver in $DNS_RESOLVERS; do
                echo "pass out quick proto {tcp, udp} to $resolver port 53 user $HARNESS_UID keep state" >> "$PF_RULES_FILE"
            done
        else
            echo "pass out quick proto {tcp, udp} to any port 53 user $HARNESS_UID keep state" >> "$PF_RULES_FILE"
        fi
        echo "block out quick proto tcp from any to any user $HARNESS_UID" >> "$PF_RULES_FILE"
        echo "block out quick proto udp from any to any user $HARNESS_UID" >> "$PF_RULES_FILE"
        echo "  Per-user blocking: harness/$(whoami) (uid=$HARNESS_UID) — upstream/model/VLM/DNS/SSH allowed"
    fi
else
    # Fallback: global block with SSH passthrough (no devagent — debug/single-user)
    cat >> "$PF_RULES_FILE" <<'PF_FALLBACK'
pass in all keep state
block out all
pass out quick proto tcp from any port 22 keep state
PF_FALLBACK
    for endpoint in $ALL_ENDPOINTS; do
        RESOLVED_IPS=$(dig +short "$endpoint" A 2>/dev/null | grep -E '^[0-9]+\.' || true)
        if [ -n "$RESOLVED_IPS" ]; then
            for ip in $RESOLVED_IPS; do
                echo "pass out quick proto tcp to $ip port {443, 80} keep state" >> "$PF_RULES_FILE"
            done
            echo "  Allowed: $endpoint ($RESOLVED_IPS)"
        fi
    done
    DNS_RESOLVERS=$(scutil --dns 2>/dev/null | grep 'nameserver\[' | awk '{print $3}' | sort -u || true)
    if [ -z "$DNS_RESOLVERS" ]; then
        DNS_RESOLVERS=$(grep '^nameserver' /etc/resolv.conf 2>/dev/null | awk '{print $2}' || true)
    fi
    if [ -n "$DNS_RESOLVERS" ]; then
        for resolver in $DNS_RESOLVERS; do
            echo "pass out quick proto {tcp, udp} to $resolver port 53 keep state" >> "$PF_RULES_FILE"
        done
    fi
    echo "  WARNING: Using global block (devagent UID unavailable)"
fi

# Apply firewall rules (requires root). Idempotent across preparation,
# recreation and eval. `pfctl -ef` fails with exit 1 ("pf already enabled")
# on subsequent applications even though the ruleset loads fine. Load the
# ruleset with `-f` (returns 0 even when pf is already
# enabled, and still surfaces real syntax errors), enable pf separately (tolerating
# "already enabled"), then VERIFY pf is actually active. One retry rides out a
# transient failure.
_apply_pf() {
    sudo pfctl -f "$PF_RULES_FILE" 2>/dev/null || return 1   # load ruleset (real errors surface here)
    sudo pfctl -e 2>/dev/null || true                        # enable; "already enabled" is not a failure
    sudo pfctl -s info 2>/dev/null | grep -q "Status: Enabled" || return 1  # confirm pf is actually on
    return 0
}
if _apply_pf || { sleep 1; _apply_pf; }; then
    echo "  Firewall rules applied"
    [ -n "${WORK_DIR:-}" ] && echo "NETWORK_RESTRICTED=true" > "$WORK_DIR/.network_state"
else
    echo "  WARNING: Failed to apply firewall rules (may need sudo)"
    [ -n "${WORK_DIR:-}" ] && echo "NETWORK_RESTRICTED=false" > "$WORK_DIR/.network_state"
    if [ "${ALLOW_NO_NETWORK_ISOLATION:-false}" != "true" ] && [ -n "$DEVAGENT_UID" ]; then
        echo "  ERROR: pfctl failed but devagent isolation is required — refusing to continue"
        echo "  (set ALLOW_NO_NETWORK_ISOLATION=true to override for debug/single-user runs)"
        exit 1
    fi
    echo "  Continuing WITHOUT network restriction (isolation not required / overridden)"
fi
