# Source:  navigo-bau (private), tests/test_api_routes_audit.py (729 lines), taken
#          2026-10-15 at commit dd2721a7: lines 430-480 (_audit_precedes_commit) and
#          503-552 (the per-handler test).
# Author:  the gate was created by Laszlo on 2026-05-09; the lines shown date from
#          2026-05 to 2026-09, by Laszlo, Marcel and one unattended agent run.
# Changed: nothing inside the lines shown. Not shown: the exemption list, the AST
#          helpers (_is_audit_construction, _is_commit_call, ...), the route collector,
#          and the test that rejects exemptions naming handlers that do not exist.
#
# Why it is here: "every API mutation writes an audit row" is enforced by CI, not by
# review. It is a syntactic check, not path analysis: a handler must build an audit row
# or be exempt with a stated reason, and where the handler commits itself, the first
# audit row must come before the first commit.

def _audit_precedes_commit(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    """True if the audit row is constructed BEFORE the transaction is committed.

    Rule 15 requires the `AuditLog(...)` to be `db.add`-ed before `db.commit()`,
    so the audit row lands in the SAME transaction as the mutation. A
    commit-then-audit ordering is silent data loss: a crash (or a second commit
    failing) after the data commit but before the audit commit leaves a mutation
    with no trail.

    Check (linear, no branch analysis per the task spec): the FIRST audit
    construction in the function body must appear before the FIRST commit call.
    Walking in source-position order catches the bad case (a commit at the top
    of the handler, audit added afterwards) without false-positiving on the real
    routes, which all construct the audit row immediately before their commit.

    Since the 2026-09-01 write_entity_audit migration, most handlers have NO
    in-body commit at all (the helper's `commit=True` default adds+commits
    internally, core/audit.py) — this check short-circuits True for them,
    which is correct FOR THAT DEFAULT: the add-before-commit ordering is
    guaranteed inside the helper itself, so green here means "ordering
    enforced by the helper", not "unchecked". **That reasoning does not
    extend to `write_entity_audit(..., commit=False)`** (2026-09-05 low-batch
    item 10) — there the helper adds but does NOT commit, so "no commit in
    this function body" means the audit row is never committed by this
    function at all, not that a helper committed it elsewhere. A deferred
    call is therefore held to its own, stricter rule below: a real `.commit()`
    must actually follow it in the SAME body.

    Only meaningful when the function both audits and commits — callers guard on
    `_function_calls_audit_class` first, and a function with no commit at all
    (audit row added via the `commit=True` default, commit deferred to a
    caller) trivially satisfies ordering.
    """
    audit_positions = [_node_position(n) for n in ast.walk(fn) if _is_audit_construction(n)]
    commit_positions = [_node_position(n) for n in ast.walk(fn) if _is_commit_call(n)]
    deferred_positions = [_node_position(n) for n in ast.walk(fn) if _is_deferred_audit_call(n)]

    if deferred_positions and (
        not commit_positions or max(deferred_positions) >= max(commit_positions)
    ):
        # A commit=False call with nothing committing it afterward in this
        # body — the exact gap the "no commit → vacuously fine" branch below
        # would otherwise paper over.
        return False

    if not audit_positions or not commit_positions:
        # No audit, or no commit in this function body — ordering is vacuously
        # fine (the no-audit case is handled by the separate presence check;
        # the deferred-commit case was already ruled on above).
        return True
    return min(audit_positions) < min(commit_positions)


_HANDLERS = _collect_route_handlers()


@pytest.mark.parametrize(
    "handler",
    _HANDLERS,
    ids=lambda h: f"{h[0].name}:{h[1].name}",
)
def test_route_handler_writes_audit_log(handler: tuple[Path, ast.AsyncFunctionDef]) -> None:
    """Per rule 15: every API mutation writes an audit row (or is exempted)."""
    py_file, fn = handler
    key = f"{py_file.name}:{fn.name}"
    if key in _AUDIT_EXEMPTIONS:
        # Exempted — skip the audit-call check.
        return
    assert _function_calls_audit_class(fn), (
        f"{py_file.name}:{fn.name} is a write endpoint (POST/PATCH/DELETE/PUT) "
        f"but does not call AuditLog(...) or PlatformAuditLog(...) anywhere in "
        f"its body.\n\n"
        f"Per CLAUDE.md rule 15, every API mutation must write an audit row "
        f"before commit. Pattern (from api/routes/admin.py):\n\n"
        f"    db.add(\n"
        f"        AuditLog(\n"
        f"            tenant_id=tenant_id,\n"
        f"            user_id=user_id,\n"
        f"            action='<resource>.<verb>',\n"
        f"            resource_type='<table>',\n"
        f"            resource_id=str(<row_id>),\n"
        f"            details={{...meaningful payload...}},\n"
        f"        )\n"
        f"    )\n"
        f"    await db.commit()\n\n"
        f"If this endpoint genuinely shouldn't audit (e.g., audited inside a "
        f"dispatched skill, audited at the agent-loop level, or read-only "
        f"despite the HTTP method), add it to _AUDIT_EXEMPTIONS in this test "
        f"with a one-line reason. Use key `{key}`."
    )

    assert _audit_precedes_commit(fn), (
        f"{py_file.name}:{fn.name} constructs its AuditLog(...) AFTER calling "
        f"db.commit() — the audit row does not land in the same transaction as "
        f"the mutation.\n\n"
        f"Per CLAUDE.md rule 15, the audit row MUST be db.add-ed BEFORE the "
        f"commit. A commit-then-audit ordering is silent data loss: a crash "
        f"after the data commit but before the audit commit leaves a mutation "
        f"with no trail. Move the AuditLog(...) construction above the "
        f"`await db.commit()` so both land atomically:\n\n"
        f"    db.add(AuditLog(...))   # <- before\n"
        f"    await db.commit()       # <- after\n"
    )
