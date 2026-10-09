using Dcc.Api.Infrastructure;

namespace Dcc.Api.Endpoints;

public static class AdminEndpoints
{
    public static void Map(RouteGroupBuilder api)
    {
        var mgr = api.MapGroup("").RequireAuthorization("Manager");

        mgr.MapGet("/users", async (Db db) => Results.Content(await db.JsonArrayAsync("""
            SELECT u.username, u.display_name, u.role,
                   count(w.claim_id) FILTER (WHERE w.status NOT IN ('Resolved', 'Written Off') AND w.system_state = 'ACTIVE') AS open_items,
                   coalesce(sum(w.expected_recovery) FILTER (WHERE w.status NOT IN ('Resolved', 'Written Off') AND w.system_state = 'ACTIVE'), 0) AS open_expected,
                   count(w.claim_id) FILTER (WHERE w.days_left <= 14 AND w.status NOT IN ('Resolved', 'Written Off') AND w.system_state = 'ACTIVE') AS due_14_days
            FROM dcc.app_user u LEFT JOIN dcc.work_item w ON w.assignee = u.username
            WHERE u.active GROUP BY u.username, u.display_name, u.role ORDER BY u.role DESC, u.username
            """), "application/json"));

        mgr.MapGet("/exceptions", async (Db db, string? kind, string? severity) => Results.Content(await db.JsonArrayAsync("""
            SELECT exception_id, kind, severity, source, reference, claim_id, amount, message, details
            FROM dcc.exception_item
            WHERE (@k::text IS NULL OR kind = @k) AND (@s::text IS NULL OR severity = @s)
            ORDER BY CASE severity WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, kind, reference
            """, ("k", string.IsNullOrWhiteSpace(kind) ? null : kind), ("s", string.IsNullOrWhiteSpace(severity) ? null : severity)),
            "application/json"));

        mgr.MapGet("/reconciliation", async (Db db) => Results.Content(await db.JsonAsync("""
            SELECT json_build_object(
              'payers', (SELECT json_agg(r ORDER BY r.payer_id) FROM dcc.recon_payer r),
              'payments', (SELECT json_agg(p ORDER BY p.payment_date, p.trn) FROM dcc.remit_payment p),
              'files', (SELECT json_agg(f ORDER BY f.file_name) FROM dcc.source_file f),
              'claims', (SELECT json_build_object('total', count(*), 'by_status', json_object_agg(s, n))
                         FROM (SELECT claim_status AS s, count(*) AS n FROM dcc.claim GROUP BY 1) x),
              'exceptions', (SELECT json_object_agg(kind, n) FROM (SELECT kind, count(*) AS n FROM dcc.exception_item GROUP BY 1) e),
              'runs', (SELECT json_agg(r ORDER BY r.run_id DESC) FROM (SELECT run_id, started_at, finished_at, input_fingerprint,
                       output_fingerprint, ai_mode FROM dcc.pipeline_run ORDER BY run_id DESC LIMIT 10) r)
            )::text
            """), "application/json"));

        mgr.MapGet("/audit", async (Db db, string? actor, int? limit) => Results.Content(await db.JsonArrayAsync("""
            SELECT audit_id, at, actor, action, entity_type, entity_id, before, after, reason
            FROM dcc.audit_log WHERE (@a::text IS NULL OR actor = @a)
            ORDER BY audit_id DESC LIMIT @l
            """, ("a", string.IsNullOrWhiteSpace(actor) ? null : actor), ("l", Math.Clamp(limit ?? 200, 1, 1000))),
            "application/json"));

        // Prevention rules are useful to everyone (coders, front desk), so not manager-only.
        api.MapGet("/prevention/rules", async (Db db) => Results.Content(await db.JsonArrayAsync("""
            SELECT rule_id, name, denials_caught, denied_amount, expected_allowed, claims_flagged, false_positives,
                   definition
            FROM dcc.prevention_rule ORDER BY expected_allowed DESC, rule_id
            """), "application/json"));

        api.MapGet("/prevention/rules.json", async (Db db) =>
        {
            var json = await db.JsonAsync("""
                SELECT jsonb_pretty(jsonb_build_object(
                  'schema_version', '1.0',
                  'generated_at', now(),
                  'data_as_of', '2026-09-30',
                  'source', 'Denials Command Center - backtested on Jan-Aug 2026 claims',
                  'condition_language', 'See README: all/any/not, field ops (eq, in, contains, any_startswith, is_empty, not_empty, gte, gt), lines_any, other_claims_exist, reference_contains',
                  'rules', (SELECT jsonb_agg(definition ORDER BY expected_allowed DESC, rule_id) FROM dcc.prevention_rule)
                ))
                """);
            return Results.File(System.Text.Encoding.UTF8.GetBytes(json), "application/json", "prebill_rules.json");
        });
    }
}
