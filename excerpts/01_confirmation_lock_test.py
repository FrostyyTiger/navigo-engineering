# Source:  navigo-bau (private), taken 2026-10-15 at commit dd2721a7:
#            ai/agent.py, line 976 and lines 996-1002 (start of _resolve_requires_confirmation)
#            tests/test_confirmation_locked.py, lines 23-60 of 88
# Author:  Marcell (agent-written under his spec), first commit 2026-06-12.
# Changed: nothing inside the lines shown. Omissions are marked "not shown": the
#          resolver's docstring and the rest of the function, the test module's
#          docstring and imports, the test that pins one real send tool, and the
#          workflow-engine twin test.
#
# Why it is here: a security review found that a per-tenant setting could switch off
# the approval gate on an outward send. For those tools the opt-out itself is the
# vulnerability, so the fix is a class property that wins before any configuration is
# read, and the exact bypass the review found is now a regression test.

# --- ai/agent.py ----------------------------------------------------------------

def _resolve_requires_confirmation(instance: BaseTool, config: dict) -> bool:
    # ... (docstring, lines 977-995, not shown)
    if getattr(instance, "confirmation_locked", False):
        if config.get("requires_confirmation") is False:
            logger.warning(
                "ignoring requires_confirmation=false for %s — confirmation_locked tool",
                instance.name,
            )
        return True
    # ... (rest of the function not shown)


# --- tests/test_confirmation_locked.py --------------------------------------------

class _LockedTool(BaseTool):
    side_effect = "external-effecting"
    requires_confirmation = True
    confirmation_locked = True

    async def execute(self, tenant_id, input, config) -> ToolResult:  # pragma: no cover
        return ToolResult(success=True)


class _UnlockedWriteTool(BaseTool):
    side_effect = "internal-write"
    requires_confirmation = True

    async def execute(self, tenant_id, input, config) -> ToolResult:  # pragma: no cover
        return ToolResult(success=True)


def test_locked_tool_ignores_tenant_opt_out():
    """The exact bypass the review found: config flip must NOT ungate."""
    assert _resolve_requires_confirmation(_LockedTool(), {"requires_confirmation": False}) is True


def test_locked_tool_gated_with_empty_config():
    assert _resolve_requires_confirmation(_LockedTool(), {}) is True


def test_unlocked_tool_keeps_the_documented_opt_out():
    """The per-tenant opt-out stays supported for ordinary write tools."""
    assert (
        _resolve_requires_confirmation(_UnlockedWriteTool(), {"requires_confirmation": False})
        is False
    )
    assert _resolve_requires_confirmation(_UnlockedWriteTool(), {}) is True


def test_base_tool_default_is_unlocked():
    """Additive default: existing tools are unaffected."""
    assert BaseTool.confirmation_locked is False
