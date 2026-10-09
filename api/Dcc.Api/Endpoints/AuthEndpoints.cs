using System.Security.Claims;
using Dcc.Api.Infrastructure;
using Microsoft.AspNetCore.Authentication.BearerToken;
using Npgsql;

namespace Dcc.Api.Endpoints;

public sealed record LoginRequest(string Username, string Password);

public static class AuthEndpoints
{
    public static void Map(RouteGroupBuilder api)
    {
        api.MapPost("/auth/login", async (LoginRequest body, Db db, ILoggerFactory lf) =>
        {
            var log = lf.CreateLogger("auth");
            var username = (body.Username ?? "").Trim().ToLowerInvariant();
            string? hash = null, role = null, display = null;
            await using (var cmd = db.DataSource.CreateCommand(
                "SELECT password_hash, role, display_name FROM dcc.app_user WHERE username=@u AND active"))
            {
                cmd.Parameters.AddWithValue("u", username);
                await using var r = await cmd.ExecuteReaderAsync();
                if (await r.ReadAsync())
                {
                    hash = r.GetString(0); role = r.GetString(1); display = r.GetString(2);
                }
            }
            if (hash is null || !PasswordHasher.Verify(body.Password ?? "", hash))
            {
                log.LogWarning("Failed login for {User}", username.Length > 40 ? username[..40] : username);
                return Results.Problem("Invalid username or password.", statusCode: 401);
            }
            var identity = new ClaimsIdentity(
                [new Claim(ClaimTypes.Name, username), new Claim(ClaimTypes.Role, role!), new Claim("display_name", display!)],
                BearerTokenDefaults.AuthenticationScheme);
            // Issues {tokenType, accessToken, expiresIn, refreshToken}
            return Results.SignIn(new ClaimsPrincipal(identity), authenticationScheme: BearerTokenDefaults.AuthenticationScheme);
        }).AllowAnonymous().RequireRateLimiting("login");

        api.MapGet("/auth/me", (ClaimsPrincipal p) => Results.Ok(new
        {
            username = p.FindFirstValue(ClaimTypes.Name),
            role = p.FindFirstValue(ClaimTypes.Role),
            display_name = p.FindFirstValue("display_name"),
        }));
    }
}
