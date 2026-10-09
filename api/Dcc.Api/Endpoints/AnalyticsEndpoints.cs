using Dcc.Api.Infrastructure;

namespace Dcc.Api.Endpoints;

/// <summary>Manager analytics. Money is reported three ways and labelled as such in the UI:
/// billed (charges denied), allowed (what the payer would realistically pay if overturned) and
/// expected (allowed x recovery probability, only while the action window is open).</summary>
public static class AnalyticsEndpoints
{
    // Whitelisted dimensions -> SQL column. User input never reaches the SQL text.
    private static readonly Dictionary<string, string> Dimensions = new()
    {
        ["payer"] = "payer_id", ["carc"] = "carc", ["root_cause"] = "root_cause", ["team"] = "owning_team",
        ["provider"] = "rendering_provider", ["coder"] = "coder_id", ["facility"] = "facility",
        ["action"] = "action_type", ["recoverability"] = "recoverability",
    };
    private static readonly HashSet<string> ClaimLevel = ["payer_id", "rendering_provider", "coder_id", "facility"];

    public static void Map(RouteGroupBuilder api)
    {
        var g = api.MapGroup("/analytics").RequireAuthorization("Manager");

        g.MapGet("/summary", async (Db db) => Results.Content(await db.JsonAsync("""
            SELECT json_build_object(
              'as_of', '2026-09-30',
              'claims_billed', (SELECT count(*) FROM dcc.claim),
              'billed_total', (SELECT sum(total_charge) FROM dcc.claim),
              'paid_total', (SELECT sum(payer_paid) FROM dcc.claim),
              'claims_adjudicated', (SELECT count(*) FROM dcc.claim WHERE claim_status <> 'NO_RESPONSE'),
              'claims_ever_denied', (SELECT count(DISTINCT claim_id) FROM dcc.denial),
              'claims_no_response', (SELECT count(*) FROM dcc.claim WHERE claim_status = 'NO_RESPONSE'),
              'open_denials', count(*) FILTER (WHERE is_open),
              'open_claims', count(DISTINCT claim_id) FILTER (WHERE is_open),
              'open_billed', coalesce(sum(denied_amount) FILTER (WHERE is_open), 0),
              'open_allowed', coalesce(sum(expected_allowed) FILTER (WHERE is_open), 0),
              'expected_recovery', coalesce(sum(expected_recovery) FILTER (WHERE is_open), 0),
              'due_14_days', count(*) FILTER (WHERE is_open AND recoverability = 'RECOVERABLE' AND days_left <= 14),
              'due_14_days_allowed', coalesce(sum(expected_allowed) FILTER (WHERE is_open AND recoverability = 'RECOVERABLE' AND days_left <= 14), 0),
              'recovered_allowed', coalesce(sum(expected_allowed) FILTER (WHERE NOT is_open), 0),
              'recovered_count', count(*) FILTER (WHERE NOT is_open),
              'preventable_open_allowed', coalesce(sum(expected_allowed) FILTER (WHERE is_open AND preventable), 0),
              'preventable_open_count', count(*) FILTER (WHERE is_open AND preventable),
              'needs_review', count(*) FILTER (WHERE is_open AND needs_review),
              'buckets', (SELECT json_agg(b ORDER BY b.sort) FROM (
                    SELECT recoverability, count(*) AS denials, sum(denied_amount) AS billed, sum(expected_allowed) AS allowed,
                           sum(expected_recovery) AS expected,
                           CASE recoverability WHEN 'RECOVERABLE' THEN 1 WHEN 'UNLIKELY' THEN 2 WHEN 'LOST' THEN 3 ELSE 4 END AS sort
                    FROM dcc.v_denial WHERE is_open GROUP BY recoverability) b),
              'exceptions_high', (SELECT count(*) FROM dcc.exception_item WHERE severity = 'HIGH'),
              'unapplied_cash', (SELECT coalesce(sum(unmatched_claim_paid), 0) FROM dcc.recon_payer),
              'possible_overpayments', (SELECT coalesce(sum(amount), 0) FROM dcc.exception_item WHERE kind = 'POSSIBLE_OVERPAYMENT'),
              'worklog_resolved_but_unpaid', (SELECT count(*) FROM dcc.exception_item WHERE kind = 'WORKLOG_RESOLVED_BUT_UNPAID'),
              'open_claims_not_in_worklog', (SELECT count(DISTINCT v.claim_id) FROM dcc.v_denial v WHERE v.is_open
                    AND NOT EXISTS (SELECT 1 FROM dcc.worklog_entry wl WHERE wl.claim_id = v.claim_id)),
              'last_run', (SELECT row_to_json(r) FROM (SELECT run_id, finished_at, ai_mode, output_fingerprint
                           FROM dcc.pipeline_run ORDER BY run_id DESC LIMIT 1) r)
            )::text FROM dcc.v_denial
            """), "application/json"));

        g.MapGet("/breakdown", async (Db db, string by) =>
        {
            if (!Dimensions.TryGetValue(by ?? "", out var col))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["by"] = [$"One of: {string.Join(", ", Dimensions.Keys)}"] });
            var rate = ClaimLevel.Contains(col)
                ? $", (SELECT count(*) FROM dcc.claim c WHERE c.{col} = x.key) AS claims_billed, " +
                  $"(SELECT count(DISTINCT d.claim_id) FROM dcc.v_denial d WHERE d.{col} = x.key) AS claims_denied"
                : "";
            var sql = $"""
                SELECT x.* {rate} FROM (
                  SELECT {col} AS key,
                         count(*) FILTER (WHERE is_open) AS open_denials,
                         coalesce(sum(denied_amount) FILTER (WHERE is_open), 0) AS open_billed,
                         coalesce(sum(expected_allowed) FILTER (WHERE is_open), 0) AS open_allowed,
                         coalesce(sum(expected_allowed) FILTER (WHERE is_open AND recoverability = 'RECOVERABLE'), 0) AS recoverable_allowed,
                         coalesce(sum(expected_allowed) FILTER (WHERE is_open AND recoverability = 'UNLIKELY'), 0) AS unlikely_allowed,
                         coalesce(sum(expected_allowed) FILTER (WHERE is_open AND recoverability = 'LOST'), 0) AS lost_allowed,
                         coalesce(sum(expected_recovery) FILTER (WHERE is_open), 0) AS expected_recovery,
                         coalesce(sum(expected_allowed) FILTER (WHERE NOT is_open), 0) AS recovered_allowed,
                         count(*) FILTER (WHERE is_open AND preventable) AS preventable_open,
                         count(*) AS denials_all_time
                  FROM dcc.v_denial GROUP BY {col}) x
                ORDER BY x.open_allowed DESC, x.key
                """;
            return Results.Content(await db.JsonArrayAsync(sql), "application/json");
        });

        g.MapGet("/trend", async (Db db) => Results.Content(await db.JsonAsync("""
            SELECT json_build_object(
              'by_denial_month', (SELECT json_agg(t ORDER BY t.month) FROM (
                  SELECT to_char(denial_date, 'YYYY-MM') AS month, count(*) AS denials,
                         sum(denied_amount) AS billed, sum(expected_allowed) AS allowed,
                         sum(expected_allowed) FILTER (WHERE is_open AND recoverability = 'RECOVERABLE') AS recoverable,
                         sum(expected_allowed) FILTER (WHERE is_open AND recoverability IN ('LOST', 'UNLIKELY')) AS lost_or_unlikely,
                         sum(expected_allowed) FILTER (WHERE NOT is_open) AS recovered
                  FROM dcc.v_denial GROUP BY 1) t),
              'by_dos_month', (SELECT json_agg(t ORDER BY t.month) FROM (
                  SELECT to_char(c.dos, 'YYYY-MM') AS month, count(*) AS claims,
                         count(*) FILTER (WHERE c.claim_status <> 'NO_RESPONSE') AS adjudicated,
                         count(*) FILTER (WHERE EXISTS (SELECT 1 FROM dcc.denial d WHERE d.claim_id = c.claim_id)) AS denied
                  FROM dcc.claim c GROUP BY 1) t)
            )::text
            """), "application/json"));

        g.MapGet("/prebill", async (Db db) => Results.Content(await db.JsonArrayAsync("""
            SELECT c.prebill_reviewed, count(*) AS claims,
                   count(*) FILTER (WHERE EXISTS (SELECT 1 FROM dcc.denial d WHERE d.claim_id = c.claim_id)) AS denied_claims,
                   count(*) FILTER (WHERE EXISTS (SELECT 1 FROM dcc.v_denial v WHERE v.claim_id = c.claim_id AND v.preventable)) AS preventable_denied_claims
            FROM dcc.claim c WHERE c.claim_status <> 'NO_RESPONSE' GROUP BY c.prebill_reviewed ORDER BY 1 DESC
            """), "application/json"));
    }
}
