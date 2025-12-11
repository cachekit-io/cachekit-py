## Description

Brief description of the changes in this PR.

## Motivation

Why are these changes needed? What problem do they solve?

## Type of Change

- [ ] Bug fix (non-breaking)
- [ ] New feature (non-breaking)
- [ ] Breaking change
- [ ] Performance improvement
- [ ] Documentation update
- [ ] Refactoring (no behavior change)
- [ ] CI/CD or tooling change

---

## Security Checklist

**For ALL PRs, verify:**

- [ ] No secrets, credentials, or API keys in code or comments
- [ ] No hardcoded sensitive data (use env vars or config)
- [ ] User input is validated/sanitized where applicable
- [ ] Error messages don't leak sensitive information

**For PRs touching security-critical paths** (`/rust/`, `/src/cachekit/serializers/`, `/src/cachekit/reliability/`, workflows):

- [ ] Changes reviewed by security team (@cachekit-io/security)
- [ ] No new `unsafe` blocks without justification
- [ ] Cryptographic code uses audited libraries (no custom crypto)
- [ ] FFI boundaries maintain memory safety guarantees

**For PRs adding/updating dependencies:**

- [ ] Dependency is from trusted source with active maintenance
- [ ] No known CVEs (`pip-audit` / `cargo-audit` clean)
- [ ] License is compatible (MIT, Apache-2.0, BSD)
- [ ] Justified: not adding unnecessary attack surface

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
- [ ] For performance changes: Benchmark results attached

---

## Backward Compatibility

- [ ] API is backward compatible OR breaking change is documented
- [ ] No removal of public APIs without deprecation period
- [ ] Migration path documented for breaking changes

---

## Additional Notes

Any additional context or notes for reviewers?
