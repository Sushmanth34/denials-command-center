FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY api/Dcc.Api/Dcc.Api.csproj api/Dcc.Api/
RUN dotnet restore api/Dcc.Api/Dcc.Api.csproj
COPY api/ api/
RUN dotnet publish api/Dcc.Api/Dcc.Api.csproj -c Release -o /out --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /out .
RUN mkdir -p /keys && chown app /keys
USER app
ENV ASPNETCORE_URLS=http://+:8080 DATA_PROTECTION_DIR=/keys
EXPOSE 8080
ENTRYPOINT ["dotnet", "Dcc.Api.dll"]
