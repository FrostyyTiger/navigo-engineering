# Source:  navigo-bau (private), taken 2026-10-15 at commit dd2721a7:
#            core/db/session.py, lines 83-86 and 89-114 (the after_begin listener)
#            tests/isolation/test_rls_set_local.py, lines 86-128 (one of its tests)
# Author:  Laszlo (agent-written under his plan), 2026-06-02. The RLS backbone it plugs
#          into was Marcell's.
# Changed: nothing inside the lines shown. Test helpers and imports not shown.
#
# Why it is here: tenant context used to be a session-level SET on the pooled
# connection. A commit in the middle of a request dropped it, which crashed chats
# that resumed after a confirmation; under transaction pooling the same flaw would
# let one tenant's context reach the next tenant's transaction. The fix binds the
# context to the transaction and re-applies it at every begin. The test is the
# regression for the crash: it commits mid-session and checks the context is back.

# --- core/db/session.py -------------------------------------------------------

# Both RLS-enforced engines (app + readonly) carry the GUC context; the admin
# (superuser, RLS-bypass) engine must NEVER. The `after_begin` listener filters on
# membership here — preserving A1's invariant "GUCs only on RLS-enforced engines."
_RLS_ENFORCED_SYNC_ENGINES = frozenset({_APP_SYNC_ENGINE, _READONLY_SYNC_ENGINE})


@event.listens_for(_SyncSession, "after_begin")
def _apply_rls_context_on_begin(session, transaction, connection) -> None:
    """Re-apply the ambient RLS context (the contextvar) at the start of EVERY
    transaction on an RLS-enforced engine, via `set_config(..., is_local=true)`.

    This is what makes RLS context transaction-scoped: it survives commits and
    pooled-connection reuse by construction (Postgres discards the local value at
    txn end; we re-apply it here on the next begin). Runs synchronously inside
    SQLAlchemy's async greenlet — the contextvar set in the async task is visible
    here, and `connection.execute(...)` works without `await`.

    - Filtered to the RLS-enforced engines (app + readonly): the admin engine
      bypasses RLS and must never carry GUCs.
    - No context set → no GUCs applied → RLS-protected tables fail closed
      (the desired property for public routes / context-free units).
    """
    if connection.engine not in _RLS_ENFORCED_SYNC_ENGINES:
        return
    ctx = current_rls_context()
    if ctx is None:
        return
    for name, value in rls_guc_assignments(ctx):
        connection.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": name, "value": value},
        )


# --- tests/isolation/test_rls_set_local.py -------------------------------------

@pytest.mark.asyncio
async def test_context_survives_across_commits():
    """The original-bug regression: within one tenant_session, commit mid-unit
    (as the agent loop does per tool call) and keep querying — context must NOT
    be lost on the post-commit transaction.

    With session-level `SET` this trivially held; with the transaction-local
    `set_config(...,true)` model it ONLY holds because the `after_begin` listener
    re-applies the contextvar on every new transaction. That is exactly what this
    pins.
    """
    tenant_a = _create_tenant_sync("Tenant A", f"a-{uuid.uuid4().hex[:8]}")
    _seed_knowledge_sync(tenant_a, "first")

    async with tenant_session(tenant_a) as db:
        # txn #1
        rows = (await db.execute(text("SELECT content FROM tenant_knowledge"))).fetchall()
        assert len(rows) == 1

        # Prove the GUC is present in txn #1.
        guc = (await db.execute(text(f"SELECT current_setting('{GUC_TENANT}', true)"))).scalar()
        assert guc == str(tenant_a)

        # Write + commit — ends txn #1 (set_config local is discarded here).
        await db.execute(
            text(
                "INSERT INTO tenant_knowledge (id, tenant_id, entry_type, content) "
                "VALUES (:id, :tid, 'insight', 'second')"
            ),
            {"id": uuid.uuid4(), "tid": tenant_a},
        )
        await db.commit()

        # txn #2 (autobegun by the next statement) — after_begin must re-arm.
        guc2 = (await db.execute(text(f"SELECT current_setting('{GUC_TENANT}', true)"))).scalar()
        assert guc2 == str(tenant_a), (
            "RLS context lost after commit — the after_begin listener did not "
            "re-apply the contextvar on the post-commit transaction. This is the "
            "2026-06-02 confirmation-resume crash class."
        )
        rows2 = (await db.execute(text("SELECT content FROM tenant_knowledge"))).fetchall()
        assert len(rows2) == 2

