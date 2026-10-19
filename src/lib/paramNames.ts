/**
 * A SQL++ parameter's name without its `$` / `@`, as a regular expression source: what the backend accepts
 * (`domain/execution.py` `PARAM_NAME`). The parameters panel's slots and the workspace's draft keys are both built from
 * it, so it lives here, in neither of them.
 */
export const PARAM_NAME = "[A-Za-z_][A-Za-z0-9_]*";
