# Contributing to Switchyard

Thank you for contributing to Switchyard!

## Development Guidelines

1. **Apigee X Compatibility**:
   - All JavaScript callouts in `src/ai-functions/*.js` must execute inside Apigee's Rhino JS sandbox (`context.getVariable` / `context.setVariable`) as well as the Node-based local emulator.
   - Never use Node-only globals (`Buffer`, `require`, `process`) in production callouts.
   - Keep tool schemas pristine: never corrupt string arrays such as `required`, `enum`, or `type`.

2. **Testing**:
   - Always run `./deploy.sh --build-only` to ensure that all XML proxy bundles compile and validate.
   - Always run `/usr/bin/python3 tests/verify_e2e.py` to verify the offline test suite passes (all protocol translations, security checks, quota enforcement, and routing tests).

3. **Submitting Changes**:
   - Ensure clean commits with descriptive commit messages.
   - Do not include proprietary customer names, internal project IDs, or real access keys in code or documentation.
