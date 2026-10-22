"""Fail unless a pytest junit report ran every test of one module, none skipped.

A module that skips itself when its service is unreachable stays green with zero
coverage. CI reads the report afterwards, so a service that never started, died
mid-run, or was addressed wrong turns the job red instead.

    python .github/scripts/assert-junit-ran.py junit.xml tests.integration.test_memcached_integration

The second argument is the module's dotted path, as junit's classname carries it.
"""

import sys
import xml.etree.ElementTree as ET


def main(report: str, module: str) -> int:
    # The report is this job's own pytest output, not untrusted input.
    cases = [c for c in ET.parse(report).iter("testcase") if f"{c.get('classname', '')}.".startswith(f"{module}.")]  # noqa: S314
    skipped = [f"{c.get('classname')}::{c.get('name')}" for c in cases if c.find("skipped") is not None]
    if not cases:
        print(f"::error::{report} has no tests from {module}: the module was not collected")
        return 1
    if skipped:
        reasons = {s.get("message", "") for c in cases for s in c.findall("skipped")}
        print(f"::error::{len(skipped)} of {len(cases)} tests in {module} skipped: {'; '.join(sorted(reasons))}")
        print("\n".join(skipped))
        return 1
    print(f"{len(cases)} tests in {module} ran, none skipped")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(f"usage: {sys.argv[0]} <junit.xml> <module dotted path>")
    sys.exit(main(sys.argv[1], sys.argv[2]))
