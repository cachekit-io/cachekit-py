## Description

Brief description of the changes in this PR.

## Motivation

Why are these changes needed? What problem do they solve?

## Type of Change

- [ ] Bug fix (non-breaking)
- [ ] New feature (non-breaking)
- [ ] Breaking change
- [ ] Documentation update

---

## Documentation Validation Checklist

**For PRs that change public APIs or features:**

- [ ] If public API changed: Updated `docs/features/*.md` or created new feature doc
- [ ] Manually tested ALL code examples by copy-paste to Python REPL
- [ ] Clicked all links in documentation (internal and external)
- [ ] If competitive claims changed: Ran `pytest tests/competitive/ -v` and updated `docs/validation/VALIDATION_LOG.md`
- [ ] Code examples are copy-paste executable (tested in REPL)
- [ ] Feature documentation includes: TL;DR, Quickstart, Deep Dive, Troubleshooting sections
- [ ] Added/updated cross-links to related features in `docs/`

**For PRs that DON'T change public APIs:**
- [ ] No documentation changes required

---

## Testing

- [ ] Unit tests added/updated
- [ ] Integration tests added/updated
- [ ] Tests pass: `make test-critical`
- [ ] No test regressions

---

## Additional Notes

Any additional context or notes for reviewers?
