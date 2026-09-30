#!/usr/bin/env bash
# name: sysinfo
# description: This machine at a glance on Linux/macOS: OS, kernel, CPU, memory, disks, addresses, uptime
# params: (none)
# safety: read
set -euo pipefail
echo "Host:    $(hostname)"
if [ -f /etc/os-release ]; then . /etc/os-release; echo "OS:      ${PRETTY_NAME:-unknown}"; else echo "OS:      $(uname -s) $(sw_vers -productVersion 2>/dev/null || true)"; fi
echo "Kernel:  $(uname -r)"
echo "Uptime:  $(uptime | sed 's/.*up \([^,]*\),.*/\1/')"
if command -v lscpu >/dev/null; then echo "CPU:     $(lscpu | awk -F: '/Model name/ {gsub(/^ +/,"",$2); print $2; exit}') ($(nproc) threads)"; else echo "CPU:     $(sysctl -n machdep.cpu.brand_string 2>/dev/null)"; fi
if command -v free >/dev/null; then free -h | awk 'NR==2 {print "Memory:  " $3 " used of " $2}'; fi
echo "Disks:"; df -h -x tmpfs -x devtmpfs 2>/dev/null | awk 'NR>1 {print "  " $6 "  " $4 " free of " $2}' || df -h
echo "IPv4:"; (ip -4 -o addr show 2>/dev/null | awk '{print "  " $2 ": " $4}') || ifconfig | awk '/inet / {print "  " $2}'
