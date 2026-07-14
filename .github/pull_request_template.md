## Summary

Describe the user-visible outcome and why the change is needed.

## Related issue

Closes #

## Validation

- [ ] `ruff check src tests examples`
- [ ] `ruff format --check src tests examples`
- [ ] `pytest -q`
- [ ] Documentation updated when behavior changed

Real-device tests are optional and must not be run solely to complete this
template. If you ran one, describe the bounded test and safe final state
without including private device data.

## Safety and privacy

- [ ] The PR title follows Conventional Commits.
- [ ] No real/private device ID, serial, IP/MAC address, SSID, username,
      password, token, hash, full parameter output, or private media is included.
- [ ] New or changed writes have catalog policy, safety gates, and tests.
- [ ] Timeout and retry behavior is safe for any state-changing operation.
- [ ] Public files contain only product code, tests, examples, documentation,
      and runtime safety metadata.
