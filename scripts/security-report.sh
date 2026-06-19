#!/usr/bin/env bash
# Generate a comprehensive security report (cargo-audit / cargo-deny / geiger).
# Extracted verbatim from the Makefile `security-report` target. The `setup-logs`
# prerequisite (which creates logs/ subdirs referenced in the report text) stays
# on the Makefile target.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

echo "${BLUE}Generating security report...${RESET}"
mkdir -p reports/security
REPORT_FILE="$(pwd)/reports/security/report_$TIMESTAMP.md"
echo "# Security Report" > "$REPORT_FILE"
echo "Generated: $(date -u +"%Y-%m-%d %H:%M:%S UTC")" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"
echo "## Summary" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"
echo "### Vulnerability Scan (cargo-audit)" >> "$REPORT_FILE"
(cd rust && cargo audit --json 2>/dev/null | jq -r '.vulnerabilities.count // 0' | xargs -I {} echo "- Vulnerabilities found: {}") >> "$REPORT_FILE" || echo "- cargo-audit not run" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"
echo "### License Compliance (cargo-deny)" >> "$REPORT_FILE"
(cd rust && cargo deny check licenses --format json 2>/dev/null | jq -r '.advisories | length' | xargs -I {} echo "- License issues: {}") >> "$REPORT_FILE" || echo "- cargo-deny not run" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"
echo "### Unsafe Code Analysis (cargo-geiger)" >> "$REPORT_FILE"
if [ -f rust/geiger-report.json ]; then
	TOTAL=$(jq '[.packages[].package.functions.safe + .packages[].package.functions.unsafe] | add' rust/geiger-report.json)
	UNSAFE=$(jq '[.packages[].package.functions.unsafe] | add' rust/geiger-report.json)
	RATIO=$(echo "scale=2; $UNSAFE / $TOTAL * 100" | bc)
	echo "- Unsafe ratio: $RATIO% ($UNSAFE / $TOTAL functions)" >> "$REPORT_FILE"
else
	echo "- Geiger report not available" >> "$REPORT_FILE"
fi
echo "" >> "$REPORT_FILE"
echo "## Details" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"
echo "See individual tool outputs in logs/ directory for detailed findings." >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"
echo "Report saved to: $REPORT_FILE"
cat "$REPORT_FILE"
echo "${GREEN}✓ Security report generated${RESET}"
