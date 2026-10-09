using System.Security.Claims;

namespace Dcc.Api.Infrastructure;

public static class Roles
{
    public const string Manager = "Manager";
    public const string Specialist = "Specialist";
}

public sealed record CurrentUser(string Username, string Role)
{
    public bool IsManager => Role == Roles.Manager;

    public static CurrentUser From(ClaimsPrincipal principal) =>
        new(principal.FindFirstValue(ClaimTypes.Name) ?? throw new UnauthorizedAccessException(),
            principal.FindFirstValue(ClaimTypes.Role) ?? throw new UnauthorizedAccessException());

    /// <summary>Specialists may only act on items assigned to them; managers on everything.</summary>
    public bool CanWork(string? assignee) => IsManager || string.Equals(assignee, Username, StringComparison.Ordinal);
}
