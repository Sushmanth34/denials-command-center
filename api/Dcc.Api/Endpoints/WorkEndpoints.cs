using System.Security.Claims;
using Dcc.Api.Infrastructure;
using Npgsql;

namespace Dcc.Api.Endpoints;

public sealed record StatusUpdate(string Status, int Version, string? Reason);
public sealed record NoteCreate(string Text);
public sealed record AssignRequest(string Assignee, int Version, string? Reason);
public sealed record ReviewRequest(string Decision, string? RootCause, string? OwningTeam, bool? Preventable, string? Comment);

public static class WorkEndpoints
{
    public static readonly string[] Statuses = ["Open", "In Progress", "Pending Payer", "Resolved", "Written Off"];

    // Mirrors pipeline/dcc/rules.py (ROOT_CAUSES, TEAMS); a reviewer can only choose from the same taxonomy.
    public static readonly string[] RootCauses =
    [
        "Payer error", "Authorization", "Eligibility", "Credentialing", "Medical necessity", "Coding - diagnosis",
        "Coding - modifier", "Coding - frequency", "Billing - timely filing", "Billing - duplicate", "Other",
    ];
    public static readonly string[] Teams =
    [
        "Denials (appeal)", "Front desk / Authorization", "Front desk / Eligibility", "Credentialing",
        "Coding / Clinical", "Coding", "Billing",
    ];

    private const string WorklistSql = """
        SELECT w.claim_id, w.kind, w.status, w.assignee, w.priority_score, w.priority_rank, w.priority_reason,
               w.expected_recovery, w.at_stake, w.deadline, w.days_left, w.version, w.updated_at,
               c.payer_id, c.dos, c.rendering_provider, c.facility, c.claim_status, c.total_charge,
               c.patient_last || ', ' || left(c.patient_first, 1) || '.' AS patient,
               top.carc, top.root_cause, top.owning_team, top.action_type, top.next_action, top.recoverability,
               top.confidence_label,
               coalesce((SELECT bool_or(v.needs_review) FROM dcc.v_denial v WHERE v.claim_id = w.claim_id AND v.is_open), false) AS needs_review,
               (SELECT count(*) FROM dcc.v_denial v WHERE v.claim_id = w.claim_id AND v.is_open) AS open_denials,
               (SELECT count(*) FROM dcc.work_note n WHERE n.claim_id = w.claim_id) AS note_count
        FROM dcc.work_item w
        JOIN dcc.claim c USING (claim_id)
        LEFT JOIN LATERAL (
            SELECT v.carc, v.root_cause, v.owning_team, v.action_type, v.next_action, v.recoverability, v.confidence_label
            FROM dcc.v_denial v WHERE v.claim_id = w.claim_id AND v.is_open
            ORDER BY v.expected_recovery DESC, v.denial_id LIMIT 1) top ON true
        WHERE w.system_state = 'ACTIVE'
          AND (@assignee::text IS NULL OR w.assignee = @assignee)
          AND (@status::text IS NULL OR w.status = @status)
          AND (@include_closed OR w.status NOT IN ('Resolved', 'Written Off'))
        ORDER BY (w.status IN ('Resolved', 'Written Off')), w.priority_score DESC, w.days_left NULLS LAST, w.claim_id
        """;

    public static void Map(RouteGroupBuilder api)
    {
        api.MapGet("/worklist", async (ClaimsPrincipal p, Db db, string? assignee, string? status, bool? includeClosed) =>
        {
            var user = CurrentUser.From(p);
            // Specialists always get their own queue, whatever they ask for.
            var who = user.IsManager ? (string.IsNullOrWhiteSpace(assignee) ? null : assignee) : user.Username;
            var json = await db.JsonArrayAsync(WorklistSql, ("assignee", who),
                ("status", string.IsNullOrWhiteSpace(status) ? null : status), ("include_closed", includeClosed ?? false));
            return Results.Content(json, "application/json");
        });

        api.MapPatch("/work-items/{claimId}", async (string claimId, StatusUpdate body, ClaimsPrincipal p, Db db) =>
        {
            var user = CurrentUser.From(p);
            if (!Statuses.Contains(body.Status))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["status"] = [$"Must be one of: {string.Join(", ", Statuses)}"] });
            if (body.Status == "Written Off" && !user.IsManager)
                return Results.Problem("Only a manager can write off a denial.", statusCode: 403);

            return await db.InTransactionAsync<IResult>(async (conn, tx) =>
            {
                var item = await LockItem(conn, tx, claimId);
                if (item is null) return Results.NotFound();
                if (!user.CanWork(item.Value.Assignee)) return Results.Forbid();
                if (item.Value.Version != body.Version)
                    return Results.Conflict(new { error = "This item was changed by someone else. Reload and try again.", current_version = item.Value.Version });
                if (item.Value.Status == body.Status) return Results.Ok(new { claim_id = claimId, status = body.Status, version = item.Value.Version });

                await using (var cmd = new NpgsqlCommand(
                    "UPDATE dcc.work_item SET status=@s, version=version+1, updated_at=now() WHERE claim_id=@id", conn, tx))
                {
                    cmd.Parameters.AddWithValue("s", body.Status);
                    cmd.Parameters.AddWithValue("id", claimId);
                    await cmd.ExecuteNonQueryAsync();
                }
                await Audit.WriteAsync(conn, tx, user.Username, "STATUS_CHANGED", "work_item", claimId,
                    new { status = item.Value.Status }, new { status = body.Status }, Trim(body.Reason, 500));
                return Results.Ok(new { claim_id = claimId, status = body.Status, version = item.Value.Version + 1 });
            });
        });

        api.MapPost("/work-items/{claimId}/notes", async (string claimId, NoteCreate body, ClaimsPrincipal p, Db db) =>
        {
            var user = CurrentUser.From(p);
            var text = body.Text?.Trim() ?? "";
            if (text.Length is 0 or > 2000)
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["text"] = ["Note must be 1-2000 characters."] });

            return await db.InTransactionAsync<IResult>(async (conn, tx) =>
            {
                var item = await LockItem(conn, tx, claimId);
                if (item is null) return Results.NotFound();
                if (!user.CanWork(item.Value.Assignee)) return Results.Forbid();
                long noteId;
                await using (var cmd = new NpgsqlCommand(
                    "INSERT INTO dcc.work_note(claim_id, author, text) VALUES (@id, @a, @t) RETURNING note_id", conn, tx))
                {
                    cmd.Parameters.AddWithValue("id", claimId);
                    cmd.Parameters.AddWithValue("a", user.Username);
                    cmd.Parameters.AddWithValue("t", text);
                    noteId = (long)(await cmd.ExecuteScalarAsync())!;
                }
                await Audit.WriteAsync(conn, tx, user.Username, "NOTE_ADDED", "work_item", claimId, null,
                    new { note_id = noteId, text });
                return Results.Created($"/api/work-items/{claimId}/notes/{noteId}", new { note_id = noteId });
            });
        });

        api.MapPost("/work-items/{claimId}/assign", async (string claimId, AssignRequest body, ClaimsPrincipal p, Db db) =>
        {
            var user = CurrentUser.From(p);
            return await db.InTransactionAsync<IResult>(async (conn, tx) =>
            {
                await using (var check = new NpgsqlCommand(
                    "SELECT 1 FROM dcc.app_user WHERE username=@u AND role='Specialist' AND active", conn, tx))
                {
                    check.Parameters.AddWithValue("u", body.Assignee ?? "");
                    if (await check.ExecuteScalarAsync() is null)
                        return Results.ValidationProblem(new Dictionary<string, string[]> { ["assignee"] = ["Unknown or inactive specialist."] });
                }
                var item = await LockItem(conn, tx, claimId);
                if (item is null) return Results.NotFound();
                if (item.Value.Version != body.Version)
                    return Results.Conflict(new { error = "This item was changed by someone else. Reload and try again.", current_version = item.Value.Version });
                if (item.Value.Assignee == body.Assignee) return Results.Ok(new { claim_id = claimId, assignee = body.Assignee, version = item.Value.Version });
                await using (var cmd = new NpgsqlCommand(
                    "UPDATE dcc.work_item SET assignee=@a, version=version+1, updated_at=now() WHERE claim_id=@id", conn, tx))
                {
                    cmd.Parameters.AddWithValue("a", body.Assignee!);
                    cmd.Parameters.AddWithValue("id", claimId);
                    await cmd.ExecuteNonQueryAsync();
                }
                await Audit.WriteAsync(conn, tx, user.Username, "REASSIGNED", "work_item", claimId,
                    new { assignee = item.Value.Assignee }, new { assignee = body.Assignee }, Trim(body.Reason, 500));
                return Results.Ok(new { claim_id = claimId, assignee = body.Assignee, version = item.Value.Version + 1 });
            });
        }).RequireAuthorization("Manager");

        api.MapGet("/review-queue", async (ClaimsPrincipal p, Db db) =>
        {
            var user = CurrentUser.From(p);
            var json = await db.JsonArrayAsync("""
                SELECT v.denial_id, v.claim_id, v.carc, v.payer_id, v.denied_amount, v.expected_allowed, v.root_cause,
                       v.owning_team, v.preventable, v.action_type, v.confidence, v.confidence_label, v.engine,
                       v.review_reasons, v.days_left, w.assignee
                FROM dcc.v_denial v JOIN dcc.work_item w USING (claim_id)
                WHERE v.is_open AND v.needs_review AND (@me::text IS NULL OR w.assignee = @me)
                ORDER BY v.confidence, v.expected_allowed DESC
                """, ("me", user.IsManager ? null : user.Username));
            return Results.Content(json, "application/json");
        });

        api.MapPost("/analyses/{denialId}/review", async (string denialId, ReviewRequest body, ClaimsPrincipal p, Db db) =>
        {
            var user = CurrentUser.From(p);
            if (body.Decision is not ("Approved" or "Overridden"))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["decision"] = ["Approved or Overridden"] });
            if (body.Decision == "Overridden" && (body.RootCause is null || !RootCauses.Contains(body.RootCause)
                                                  || body.OwningTeam is null || !Teams.Contains(body.OwningTeam) || body.Preventable is null))
                return Results.ValidationProblem(new Dictionary<string, string[]> { ["override"] = ["root_cause, owning_team and preventable are required and must use the standard taxonomy."] });

            return await db.InTransactionAsync<IResult>(async (conn, tx) =>
            {
                string? claimId = null, assignee = null, rc = null, team = null;
                bool prev = false;
                await using (var cmd = new NpgsqlCommand("""
                    SELECT a.root_cause, a.owning_team, a.preventable, d.claim_id, w.assignee
                    FROM dcc.denial_analysis a JOIN dcc.denial d USING (denial_id)
                    LEFT JOIN dcc.work_item w ON w.claim_id = d.claim_id
                    WHERE a.denial_id = @d
                    """, conn, tx))
                {
                    cmd.Parameters.AddWithValue("d", denialId);
                    await using var r = await cmd.ExecuteReaderAsync();
                    if (!await r.ReadAsync()) return Results.NotFound();
                    rc = r.GetString(0); team = r.GetString(1); prev = r.GetBoolean(2);
                    claimId = r.GetString(3); assignee = r.IsDBNull(4) ? null : r.GetString(4);
                }
                if (!user.CanWork(assignee)) return Results.Forbid();
                var approved = body.Decision == "Approved";
                await using (var up = new NpgsqlCommand("""
                    INSERT INTO dcc.analysis_review(denial_id, reviewer, decision, root_cause, owning_team, preventable, comment)
                    VALUES (@d, @u, @dec, @rc, @t, @p, @c)
                    ON CONFLICT (denial_id) DO UPDATE SET reviewer=@u, decision=@dec, root_cause=@rc, owning_team=@t,
                        preventable=@p, comment=@c, reviewed_at=now()
                    """, conn, tx))
                {
                    up.Parameters.AddWithValue("d", denialId);
                    up.Parameters.AddWithValue("u", user.Username);
                    up.Parameters.AddWithValue("dec", body.Decision);
                    up.Parameters.AddWithValue("rc", approved ? DBNull.Value : body.RootCause!);
                    up.Parameters.AddWithValue("t", approved ? DBNull.Value : body.OwningTeam!);
                    up.Parameters.AddWithValue("p", approved ? DBNull.Value : body.Preventable!.Value);
                    up.Parameters.AddWithValue("c", (object?)Trim(body.Comment, 1000) ?? DBNull.Value);
                    await up.ExecuteNonQueryAsync();
                }
                await Audit.WriteAsync(conn, tx, user.Username, approved ? "ANALYSIS_APPROVED" : "ANALYSIS_OVERRIDDEN",
                    "denial_analysis", denialId,
                    new { root_cause = rc, owning_team = team, preventable = prev },
                    approved ? new { root_cause = (string?)rc, owning_team = (string?)team, preventable = prev }
                             : new { root_cause = body.RootCause, owning_team = body.OwningTeam, preventable = body.Preventable!.Value },
                    Trim(body.Comment, 1000));
                return Results.Ok(new { denial_id = denialId, decision = body.Decision, claim_id = claimId });
            });
        });
    }

    private static string? Trim(string? s, int max) =>
        string.IsNullOrWhiteSpace(s) ? null : (s.Trim().Length > max ? s.Trim()[..max] : s.Trim());

    private static async Task<(string Status, string? Assignee, int Version)?> LockItem(
        NpgsqlConnection conn, NpgsqlTransaction tx, string claimId)
    {
        await using var cmd = new NpgsqlCommand(
            "SELECT status, assignee, version FROM dcc.work_item WHERE claim_id=@id FOR UPDATE", conn, tx);
        cmd.Parameters.AddWithValue("id", claimId);
        await using var r = await cmd.ExecuteReaderAsync();
        if (!await r.ReadAsync()) return null;
        return (r.GetString(0), r.IsDBNull(1) ? null : r.GetString(1), r.GetInt32(2));
    }
}
