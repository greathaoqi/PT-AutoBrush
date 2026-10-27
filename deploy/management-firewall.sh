#!/usr/bin/env bash
set -euo pipefail
for command in iptables ip6tables; do
    if command -v "$command" >/dev/null; then
        "$command" -C INPUT ! -i lo -p tcp -m multiport --dports 12300,12301 -j REJECT --reject-with tcp-reset 2>/dev/null || \
            "$command" -I INPUT 1 ! -i lo -p tcp -m multiport --dports 12300,12301 -j REJECT --reject-with tcp-reset
    fi
done
