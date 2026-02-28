# Automations — Claude Code Instructions

## Testing

### During regular development
Do NOT run tests after every edit. Work freely without stopping to run the suite.

### Before declaring work production-ready
When a task is complete and you're about to say "done" or summarize finished work touching `twitter/`, run the full suite including integration tests:

```bash
cd /projects/automations/twitter
uv run pytest tests/ -q
```

All tests must pass. Fix any failures before finishing.

**Use judgment about scope.** If the change is isolated (e.g. a standalone script not imported by anything else), skip the suite — don't test blindly just to follow the rule.
