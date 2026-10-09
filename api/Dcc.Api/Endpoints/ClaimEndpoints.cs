using System.Security.Claims;
using Dcc.Api.Infrastructure;

namespace Dcc.Api.Endpoints;

public static class ClaimEndpoints
{
    private const string ClaimDetailSql = """
        SELECT json_build_object(
          'claim', (SELECT row_to_json(c) FROM dcc.claim c WHERE c.claim_id = @id),
          'payer', (SELECT row_to_json(py) FROM dcc.payer py JOIN dcc.claim c USING (payer_id) WHERE c.claim_id = @id),
          'lines', (SELECT coalesce(json_agg(l ORDER BY l.line_no), '[]') FROM dcc.claim_line l WHERE l.claim_id = @id),
          'events', (SELECT coalesce(json_agg(e ORDER BY e.seq), '[]') FROM dcc.claim_event e WHERE e.claim_id = @id),
          'denials', (SELECT coalesce(json_agg(x ORDER BY x.denial_date DESC, x.line_no), '[]') FROM (
                SELECT v.*, a.appeal_draft, a.rationale, a.evidence, a.rules_result, a.ai_model, a.prompt_version,
                       r.comment AS review_comment, r.reviewed_at
                FROM dcc.v_denial v JOIN dcc.denial_analysis a USING (denial_id)
                LEFT JOIN dcc.analysis_review r USING (denial_id)
                WHERE v.claim_id = @id) x),
          'work_item', (SELECT row_to_json(w) FROM dcc.work_item w WHERE w.claim_id = @id),
          'notes', (SELECT coalesce(json_agg(n ORDER BY n.created_at, n.note_id), '[]') FROM dcc.work_note n WHERE n.claim_id = @id),
          'audit', (SELECT coalesce(json_agg(a ORDER BY a.at, a.audit_id), '[]') FROM dcc.audit_log a
                    WHERE (a.entity_type = 'work_item' AND a.entity_id = @id)
                       OR (a.entity_type = 'denial_analysis' AND a.entity_id LIKE @id || ':%')),
          'remits', (SELECT coalesce(json_agg(x ORDER BY x.payment_date, x.remit_claim_key), '[]') FROM (
                SELECT rc.*, p.source_file,
                       (SELECT json_agg(rl ORDER BY rl.svc_seq) FROM dcc.remit_line rl WHERE rl.remit_claim_key = rc.remit_claim_key) AS lines
                FROM dcc.remit_claim rc JOIN dcc.remit_payment p USING (trn) WHERE rc.claim_id = @id) x),
          'worklog', (SELECT coalesce(json_agg(wl ORDER BY wl.row_no), '[]') FROM dcc.worklog_entry wl WHERE wl.claim_id = @id),
          'exceptions', (SELECT coalesce(json_agg(ex), '[]') FROM dcc.exception_item ex WHERE ex.claim_id = @id),
          'policies', (SELECT coalesce(json_agg(ps ORDER BY ps.section_id), '[]') FROM dcc.policy_section ps
                       WHERE ps.section_id IN (SELECT unnest(a.policy_refs) FROM dcc.denial_analysis a
                                               JOIN dcc.denial d USING (denial_id) WHERE d.claim_id = @id))
        )::text
        """;

    public static void Map(RouteGroupBuilder api)
    {
        api.MapGet("/claims/{claimId}", async (string claimId, ClaimsPrincipal p, Db db) =>
        {
            var user = CurrentUser.From(p);
            if (!user.IsManager)
            {
                // Least privilege for PHI: a specialist can open claims in their own queue only.
                var owner = await db.JsonAsync("SELECT to_json(assignee)::text FROM dcc.work_item WHERE claim_id=@id", ("id", claimId));
                if (owner != $"\"{user.Username}\"") return Results.Forbid();
            }
            var json = await db.JsonAsync(ClaimDetailSql, ("id", claimId));
            return json.Contains("\"claim\" : null") ? Results.NotFound() : Results.Content(json, "application/json");
        });

        api.MapGet("/claims", async (Db db, string q) =>
        {
            // Manager search by claim id / patient last name / member id.
            var term = (q ?? "").Trim();
            if (term.Length < 3) return Results.Content("[]", "application/json");
            var json = await db.JsonArrayAsync("""
                SELECT claim_id, payer_id, dos, rendering_provider, claim_status, total_charge, payer_paid, denied_open,
                       patient_last || ', ' || left(patient_first, 1) || '.' AS patient
                FROM dcc.claim
                WHERE claim_id ILIKE '%' || @q || '%' OR patient_last ILIKE @q || '%' OR member_id = @q
                ORDER BY claim_id LIMIT 50
                """, ("q", term));
            return Results.Content(json, "application/json");
        }).RequireAuthorization("Manager");
    }
}
