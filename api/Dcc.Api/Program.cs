using System.Threading.RateLimiting;
using Dcc.Api.Endpoints;
using Dcc.Api.Infrastructure;
using Microsoft.AspNetCore.Authentication.BearerToken;
using Microsoft.AspNetCore.DataProtection;
using Npgsql;

var builder = WebApplication.CreateBuilder(args);

var connectionString = builder.Configuration["DATABASE_URL"] is { Length: > 0 } url
    ? ToNpgsql(url)
    : builder.Configuration.GetConnectionString("Default")
      ?? "Host=localhost;Database=dcc;Username=dcc;Password=dcc";

builder.Services.AddSingleton(_ => new NpgsqlDataSourceBuilder(connectionString).Build());
builder.Services.AddSingleton<Db>();

// Opaque, data-protected bearer tokens (built into ASP.NET Core 8). Keys persisted so restarts keep sessions.
var keysDir = builder.Configuration["DATA_PROTECTION_DIR"];
var dp = builder.Services.AddDataProtection().SetApplicationName("dcc-api");
if (!string.IsNullOrWhiteSpace(keysDir)) dp.PersistKeysToFileSystem(new DirectoryInfo(keysDir));

builder.Services.AddAuthentication(BearerTokenDefaults.AuthenticationScheme)
    .AddBearerToken(o => o.BearerTokenExpiration = TimeSpan.FromHours(8));
builder.Services.AddAuthorizationBuilder()
    .SetFallbackPolicy(new Microsoft.AspNetCore.Authorization.AuthorizationPolicyBuilder().RequireAuthenticatedUser().Build())
    .AddPolicy("Manager", p => p.RequireRole(Roles.Manager));

builder.Services.AddRateLimiter(o =>
{
    o.RejectionStatusCode = StatusCodes.Status429TooManyRequests;
    o.AddPolicy("login", ctx => RateLimitPartition.GetFixedWindowLimiter(
        ctx.Connection.RemoteIpAddress?.ToString() ?? "unknown",
        _ => new FixedWindowRateLimiterOptions { PermitLimit = 10, Window = TimeSpan.FromMinutes(1) }));
});

builder.Services.ConfigureHttpJsonOptions(o =>
    o.SerializerOptions.PropertyNamingPolicy = System.Text.Json.JsonNamingPolicy.SnakeCaseLower);

var corsOrigins = (builder.Configuration["CORS_ORIGINS"] ?? "").Split(',', StringSplitOptions.RemoveEmptyEntries);
builder.Services.AddCors(o => o.AddDefaultPolicy(p => p.WithOrigins(corsOrigins).AllowAnyHeader().AllowAnyMethod()));
builder.Services.AddProblemDetails();

var app = builder.Build();

app.UseExceptionHandler();
app.Use(async (ctx, next) =>
{
    // PHI-bearing API: never cache, never frame, never sniff.
    ctx.Response.Headers["Cache-Control"] = "no-store";
    ctx.Response.Headers["X-Content-Type-Options"] = "nosniff";
    ctx.Response.Headers["X-Frame-Options"] = "DENY";
    ctx.Response.Headers["Referrer-Policy"] = "no-referrer";
    await next();
});
app.UseCors();
app.UseRateLimiter();
app.UseAuthentication();
app.UseAuthorization();

app.MapGet("/health", async (Db db) =>
{
    var ok = await db.JsonAsync("SELECT to_json(count(*))::text FROM dcc.pipeline_run");
    return Results.Ok(new { status = "ok", pipeline_runs = int.Parse(ok) });
}).AllowAnonymous();

var api = app.MapGroup("/api");
AuthEndpoints.Map(api);
WorkEndpoints.Map(api);
ClaimEndpoints.Map(api);
AnalyticsEndpoints.Map(api);
AdminEndpoints.Map(api);

app.Run();

// postgresql://user:pass@host:port/db -> Npgsql keyword format
static string ToNpgsql(string url)
{
    var uri = new Uri(url);
    var userInfo = uri.UserInfo.Split(':', 2);
    var b = new NpgsqlConnectionStringBuilder
    {
        Host = uri.Host,
        Port = uri.Port > 0 ? uri.Port : 5432,
        Database = uri.AbsolutePath.TrimStart('/'),
        Username = Uri.UnescapeDataString(userInfo[0]),
        Password = userInfo.Length > 1 ? Uri.UnescapeDataString(userInfo[1]) : null,
    };
    return b.ConnectionString;
}

public partial class Program;
