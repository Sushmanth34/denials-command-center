using System.Text.Json;
using Npgsql;

namespace Dcc.Api.Infrastructure;

/// <summary>
/// Thin data-access helpers. Read endpoints let PostgreSQL build the JSON (json_agg / row_to_json) so the API
/// streams exactly what the database computed - no hand-written DTO mapping that could drift from the schema.
/// All SQL is parameterised; values never get concatenated into SQL text.
/// </summary>
public sealed class Db(NpgsqlDataSource dataSource)
{
    public NpgsqlDataSource DataSource { get; } = dataSource;

    /// <summary>Runs a query that returns a single json value and returns it as a raw JSON string.</summary>
    public async Task<string> JsonAsync(string sql, params (string Name, object? Value)[] args)
    {
        await using var cmd = DataSource.CreateCommand(sql);
        foreach (var (name, value) in args)
            cmd.Parameters.AddWithValue(name, value ?? DBNull.Value);
        var result = await cmd.ExecuteScalarAsync();
        return result is null or DBNull ? "null" : (string)result;
    }

    /// <summary>Wraps a SELECT so it returns a JSON array of rows.</summary>
    public Task<string> JsonArrayAsync(string selectSql, params (string Name, object? Value)[] args) =>
        JsonAsync($"SELECT coalesce(json_agg(t), '[]'::json)::text FROM ({selectSql}) t", args);

    /// <summary>Wraps a SELECT so it returns the first row as a JSON object (or null).</summary>
    public Task<string> JsonObjectAsync(string selectSql, params (string Name, object? Value)[] args) =>
        JsonAsync($"SELECT row_to_json(t)::text FROM ({selectSql}) t LIMIT 1", args);

    public async Task<T> InTransactionAsync<T>(Func<NpgsqlConnection, NpgsqlTransaction, Task<T>> work)
    {
        await using var conn = await DataSource.OpenConnectionAsync();
        await using var tx = await conn.BeginTransactionAsync();
        try
        {
            var result = await work(conn, tx);
            await tx.CommitAsync();
            return result;
        }
        catch
        {
            await tx.RollbackAsync();
            throw;
        }
    }
}

public static class Audit
{
    /// <summary>Appends an audit row in the caller's transaction, so the change and its audit commit together.</summary>
    public static async Task WriteAsync(NpgsqlConnection conn, NpgsqlTransaction tx, string actor, string action,
        string entityType, string entityId, object? before, object? after, string? reason = null)
    {
        await using var cmd = new NpgsqlCommand(
            "INSERT INTO dcc.audit_log(actor, action, entity_type, entity_id, before, after, reason) " +
            "VALUES (@actor, @action, @etype, @eid, @before::jsonb, @after::jsonb, @reason)", conn, tx);
        cmd.Parameters.AddWithValue("actor", actor);
        cmd.Parameters.AddWithValue("action", action);
        cmd.Parameters.AddWithValue("etype", entityType);
        cmd.Parameters.AddWithValue("eid", entityId);
        cmd.Parameters.AddWithValue("before", before is null ? DBNull.Value : JsonSerializer.Serialize(before));
        cmd.Parameters.AddWithValue("after", after is null ? DBNull.Value : JsonSerializer.Serialize(after));
        cmd.Parameters.AddWithValue("reason", (object?)reason ?? DBNull.Value);
        await cmd.ExecuteNonQueryAsync();
    }
}
