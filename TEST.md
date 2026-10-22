# Mock Microsoft Graph server

A small HTTP server that answers the subset of [Microsoft Graph v1.0](https://learn.microsoft.com/graph/api/overview) you need to build and test a document-permissions-aware app (sites, drives, files, permissions, users, groups, delta queries) without a Microsoft 365 tenant.

You point the official `Microsoft.Graph` SDK at it with a different base URL. The paths, JSON property names, `@odata.*` annotations and paging behaviour match Graph, so the same client code works against the real service later. Access control is real: a user only sees the files they can read, through direct grants, group membership, nested groups and folder inheritance.

It is built and maintained by the course mentor. Your team consumes it as a Docker image.

> **Unofficial project.** This is an independent mock for development and testing. It is not affiliated with, endorsed by or supported by Microsoft. "Microsoft", "Microsoft Graph", "Microsoft 365" and "SharePoint" are trademarks of the Microsoft group of companies.

## Contents

- [What it covers](#what-it-covers)
- [What it does not cover](#what-it-does-not-cover)
- [Running it](#running-it)
- [Trying the API by hand](#trying-the-api-by-hand)
- [Using the Graph SDK](#using-the-graph-sdk)
- [Authentication](#authentication)
- [Sample data and visibility](#sample-data-and-visibility)
- [Paging](#paging)
- [Change tracking](#change-tracking)
- [Admin API](#admin-api)
- [Writing a fixture](#writing-a-fixture)
- [Known differences from Microsoft Graph](#known-differences-from-microsoft-graph)
- [Development](#development)
- [License](#license)

## What it covers

All paths are under `/v1.0`. Every `GET` below is supported. Nothing else under `/v1.0` is implemented: other methods return `405`, unknown paths return `404`.

**Directory**

| Path | Notes |
|---|---|
| `GET /users` | Not filtered by visibility. `$filter`, `$orderby` and `$count` as described in [Query options](#query-options) |
| `GET /users/{id or upn}` | |
| `GET /users/{id}/memberOf`, `GET /users/{id}/transitiveMemberOf` | Returns `directoryObject`s with `@odata.type` `#microsoft.graph.group` |
| `GET /me`, `GET /me/memberOf`, `GET /me/transitiveMemberOf` | User tokens only. With `Bearer app` they return `400` |
| `GET /groups`, `GET /groups/{id}` | Not filtered by visibility. Same query options as `/users` |
| `GET /groups/{id}/members`, `GET /groups/{id}/transitiveMembers` | Nested groups are supported |
| `GET /groups/delta` | Group membership changes, with `members@delta` (see [Change tracking](#change-tracking)) |

**Sites and drives**

| Path | Notes |
|---|---|
| `GET /sites` | The sites in the fixture (the implicit root site is not listed here) |
| `GET /sites?search=*`, `GET /sites?search=<keyword>` | `*` returns every site including the root; a keyword matches `name` or `displayName`, case-insensitively. `$search=` is accepted too (the SDK sends that spelling) |
| `GET /sites/getAllSites` | All sites, paged. Also the `getAllSites()` form the SDK uses |
| `GET /sites/root` | The tenant root site, with `siteCollection.hostname` |
| `GET /sites/{siteId}` | `siteId` is the wire id or the fixture id. The bare hostname also addresses the root site |
| `GET /sites/{hostname}:/{server-relative-path}` | For example `/sites/contoso-mock.sharepoint.com:/sites/kb`; also `:/drives` and `:/drive` after the path |
| `GET /sites/{siteId}/drives`, `GET /sites/{siteId}/drive` | Also under `/sites/root` and the path form |
| `GET /drives/{driveId}` | |

**Batching**

| Path | Notes |
|---|---|
| `POST /$batch` | Up to 20 `GET` sub-requests, run as the calling user. See [JSON batching](#json-batching). The only `POST` that Graph paths accept |

**Files**

| Path | Notes |
|---|---|
| `GET /drives/{driveId}/root` | Visible to every caller. `$expand=children` is supported here and on item and path GETs |
| `GET /drives/{driveId}/root/children` | Only children the caller can read |
| `GET /drives/{driveId}/items/{itemId}` | `404 itemNotFound` if the caller cannot read it |
| `GET /drives/{driveId}/items/{itemId}/children` | |
| `GET /drives/{driveId}/items/{itemId}/content` | `302` to `/_download/{token}`; the SDK follows it |
| `GET /drives/{driveId}/items/{itemId}/permissions` | Includes `inheritedFrom` for inherited grants. Grants to SharePoint site groups come back as `grantedToV2.siteGroup` (see [Site groups](#site-groups)) |
| `GET /drives/{driveId}/root:/path/to/file.md:` | Path addressing, also `:/children`, `:/content`, `:/permissions` |

**Change tracking**

| Path | Notes |
|---|---|
| `GET /drives/{driveId}/root/delta` | Also `/items/{itemId}/delta` (folder scope), the `delta()` form and `?token=` |
| `GET /groups/delta` | Also the `delta()` form |

**Non-Graph**: `/_admin/*` (see [Admin API](#admin-api)) and `/_download/{token}`.

## What it does not cover

- No real OAuth, Entra ID or token validation. Any `Bearer` value is parsed as described in [Authentication](#authentication).
- No writes through Graph. `PUT`, `PATCH` and `POST` on Graph paths return `405`. All mutations go through `/_admin`.
- No Teams, bot or chat APIs.
- No search (`/search/query`).
- No sharing links, sensitivity labels, file versions or thumbnails.
- Query options are limited to what [Query options](#query-options) lists. Anything else (`$filter` on other collections, `$expand=permissions`, `$orderby` on items, ...) returns `400` with a message naming it.
- Batches contain `GET` sub-requests only.

## Running it

The image is published to GitHub Container Registry as `ghcr.io/sergey-shulyak/mock-graph-server`. Tags: `latest` (default branch), `vX.Y.Z`, `vX.Y` (version tags) and `sha-<commit>`. Platforms: `linux/amd64` and `linux/arm64`.

**Docker**

```bash
docker run --rm -p 8080:8080 ghcr.io/sergey-shulyak/mock-graph-server:latest

curl -H "Authorization: Bearer app" http://localhost:8080/v1.0/users
```

**Compose**: `compose.yaml` in this repository has a service you can paste into your own file:

```bash
docker compose up mock-graph
```

**From source** (the repo pins .NET with mise):

```bash
mise install
mise exec -- dotnet run --project src/MockGraph.Server
```

With `dotnet run` the server listens on `http://localhost:5066` (from `Properties/launchSettings.json`). In the container it listens on port 8080.

**Which fixture is served.** The Docker image serves the rich Contoso company fixture, `fixtures/company.yaml` (16 users, 15 groups, 4 sites, 6 drives, 79 documents; see [Rich company fixture](#rich-company-fixture)). The small `fixtures/sample.yaml` ships in the image too. To use it:

```bash
docker run --rm -p 8080:8080 -e MOCKGRAPH__FIXTUREPATH=fixtures/sample.yaml ghcr.io/sergey-shulyak/mock-graph-server:latest
```

The server's own built-in default is `fixtures/sample.yaml`; the Dockerfile overrides it with `MOCKGRAPH__FIXTUREPATH=fixtures/company.yaml`. So `dotnet run` and the unit tests keep using the sample, and only the image defaults to the company data.

**Custom fixture.** The server reads `MockGraph:FixturePath` (environment variable `MOCKGRAPH__FIXTUREPATH`). A relative path is searched for under the content root and then each parent directory. In the image the content root is `/app`, so `fixtures/company.yaml` resolves to `/app/fixtures/company.yaml`. To use your own file:

```bash
docker run --rm -p 8080:8080 \
  -e MOCKGRAPH__FIXTUREPATH=/app/my-fixtures/team.yaml \
  -v "$PWD/my-fixtures:/app/my-fixtures:ro" \
  ghcr.io/sergey-shulyak/mock-graph-server:latest
```

If the fixture is missing or invalid (unknown principal, duplicate id, bad role, missing source file), the server fails at startup and prints the reason.

## Trying the API by hand

The repository has two request collections with the same ~58 requests, in learning order: auth, directory, sites, files, visibility, change tracking, `$batch` and the Admin API. Each request says what it shows and the status it should return.

- **VS Code, Visual Studio or Rider/IntelliJ:** open `mock-graph.http` and click *Send Request* above a request. VS Code needs the REST Client extension. The variables at the top set `@baseUrl` (default `http://localhost:8080`) and `@token`.
- **Bruno:** open the `bruno/` folder in Bruno and select the `local` environment. Each request has an assertion on its expected status.

To act as another user, change `token` (`@token` in the `.http` file, `token` in the `local` environment) to `user:alice`, `user:erin` and so on. `app` sees everything. Admin requests change the server state; the last one resets it to the fixture.

## Using the Graph SDK

Add the package (the server uses the same version, 6.8.0):

```bash
dotnet add package Microsoft.Graph --version 6.8.0
```

Then point the client at the mock. This is the whole setup. `StaticTokenProvider` is the small class below it, which returns a fixed token:

```csharp
using Microsoft.Graph;
using Microsoft.Kiota.Abstractions.Authentication;
using Microsoft.Kiota.Http.HttpClientLibrary;

var auth = new BaseBearerTokenAuthenticationProvider(new StaticTokenProvider("user:alice"));
var adapter = new HttpClientRequestAdapter(auth) { BaseUrl = "http://localhost:8080/v1.0" };
var graph = new GraphServiceClient(adapter);

var me = await graph.Me.GetAsync();   // 401 for an unknown user; 200 for user:alice

internal sealed class StaticTokenProvider(string token) : IAccessTokenProvider
{
    public AllowedHostsValidator AllowedHostsValidator { get; } = new();

    public Task<string> GetAuthorizationTokenAsync(
        Uri uri, Dictionary<string, object>? additionalAuthenticationContext = null, CancellationToken cancellationToken = default) =>
        Task.FromResult(token);
}
```

Inside the Compose network, use `http://mock-graph:8080/v1.0` instead of `localhost`.

**Month 3 (real Microsoft 365):** change `BaseUrl` to `https://graph.microsoft.com/v1.0` and replace `StaticTokenProvider` with a provider that returns a real access token. Nothing else in the Graph calls should change. If it does, that is a mismatch the mock should have caught, so report it.

## Authentication

Send `Authorization: Bearer <token>` on every `/v1.0` request. `/_admin/*` and `/_download/*` need no header.

| Token | Context |
|---|---|
| `app` | App-only. Sees everything, like an application permission. `/me` returns `400` |
| `user:<id>` | Runs as that user. `<id>` is the fixture id (`alice`), the UPN (`alice@contoso-mock.onmicrosoft.com`) or the user's GUID |
| anything else | `401` `InvalidAuthenticationToken` |

A missing header, a non-Bearer scheme or an unknown `user:` also return `401`. Error bodies follow Graph:

```json
{ "error": { "code": "itemNotFound", "message": "...", "innerError": { "request-id": "...", "date": "...", "client-request-id": "..." } } }
```

Every response has a `request-id` header.

## Sample data and visibility

This section describes the small `fixtures/sample.yaml`. Its data is contained unchanged in the rich company fixture (see [below](#rich-company-fixture)); the visibility table below stays true there for these five users and these paths.

`fixtures/sample.yaml` is the data behind the team's RBAC test matrix. It has five users, six groups and one drive (`kb-docs`). `all-staff` contains the group `engineering` plus `carol` and `dave`. `engineering` contains the groups `eng-backend` and `eng-frontend`, which is a nested-group case.

Users:

| Id | Name | Groups |
|---|---|---|
| `alice` | Alice Backend | `eng-backend` (in `engineering`, `all-staff`) |
| `bob` | Bob Frontend | `eng-frontend` (in `engineering`, `all-staff`) |
| `carol` | Carol HR | `hr`, `all-staff` |
| `dave` | Dave Support | `support`, `all-staff` |
| `erin` | Erin Contractor | none; one document granted directly |

Files a user can open (folders are visible where a readable file is below them; see the note below the table):

| User | Can read |
|---|---|
| `alice` | `/General/*`, `/Engineering/*` (including `/Engineering/Backend/*`), `/Shared/contractor-onboarding.md` |
| `bob` | `/General/*`, `/Engineering/local-dev-setup.md`, `/Shared/contractor-onboarding.md`. Not `/Engineering/Backend/*` |
| `carol` | `/General/*`, `/HR/*`. Not `/Engineering/*` |
| `dave` | `/General/*`, `/Support/*` |
| `erin` | `/Shared/contractor-onboarding.md` only |
| `app` | Everything |

Notes:

- Unreadable items are left out of lists and return `404 itemNotFound` on get, content and permissions.
- A readable file does not make its parent folders listable. `erin` can open the onboarding file by id, but `/Shared` is not in her tree.
- The drive root and site and drive metadata are visible to any valid token.
- `/users` and `/groups` are not filtered.

### Rich company fixture

`fixtures/company.yaml` is the default of the Docker image: the fictional software company Contoso with 16 users (managers, QA, DevOps, finance, sales, legal, IT, a new hire, an intern, two contractors and an offboarded employee), 15 groups (nested four levels deep, one group with two parents, one empty group), 4 sites and 6 drives (one site has two), site groups on every site, and 79 realistic documents (62 `.md`/`.txt`, 7 `.docx`, 6 `.pdf`, 2 `.xlsx`, 1 `.pptx`, 1 `.png`) with checkable facts for building question and answer sets. It is a superset of the sample: the five sample users, six groups, site `kb`, drive `kb-docs` and every sample item keep their ids, paths, content and permissions.

- [`fixtures/company.notes.md`](fixtures/company.notes.md): org chart, drive layout, a per-user access summary, the deliberate test material (conflicting documents, near-duplicates, prompt-injection text, restricted-versus-general topics, deep nesting, absent topics, long and short files, odd file names) and admin-API scenarios with their expected effect.
- [`fixtures/company.visibility.yaml`](fixtures/company.visibility.yaml): the RBAC test oracle, the exact files and folders each user can read. It is generated by `tools/fixture-visibility/compute_visibility.py` and checked against the server by an integration test.
- With several drives, admin calls need a `driveId`, for example `hr-docs`.

## Paging

Every collection is paged. The default page size is **5**, so client paging bugs show up early.

- `$top=n` sets the page size, from 1 to `MaxPageSize` (default 999). Values outside the range return `400`.
- `@odata.nextLink` is an absolute URL. It keeps `$top` and `$select` and carries an opaque `$skiptoken`. Follow it until there is none.
- A `$skiptoken` from before a reset is rejected with `400`.

Change the default with `MOCKGRAPH__DEFAULTPAGESIZE` (for example `-e MOCKGRAPH__DEFAULTPAGESIZE=2`). Change the maximum with `MOCKGRAPH__MAXPAGESIZE`.

`$select` is accepted on every endpoint. The response keeps the selected properties, `id` and all `@` annotations, and drops the rest.

Unsupported query options return `400`. They are not ignored. Supported ones beyond paging and `$select` are in [Query options](#query-options).

Iterating with the SDK's `PageIterator` works, as in the test suite (`tests/MockGraph.Tests/DirectoryTests.cs`).

## Query options

| Option | Where | Behaviour |
|---|---|---|
| `$filter` | `/users`, `/groups` | `eq` and `startsWith(prop,'x')` on `mail`, `userPrincipalName`, `displayName`, `id` (users) and `displayName`, `mail`, `id` (groups), joined with `and` / `or` and parentheses. Strings compare case-insensitively. `id` matches the wire id only. Anything else (`ne`, `not`, `endsWith`, other properties) is `400 BadRequest` naming what is unsupported. Our groups have no mailbox, so `mail eq` on groups never matches |
| `$count=true` | `/users`, `/groups` | With `ConsistencyLevel: eventual` the response has `@odata.count` (the total after `$filter`, not the page size). Without the header it is silently ignored, as in Graph |
| `$orderby` | `/users`, `/groups` | `displayName` (and `userPrincipalName` on users), optional `asc`/`desc`. Combined with `$filter` it needs `ConsistencyLevel: eventual` plus `$count=true`, otherwise `400 Request_UnsupportedQuery` |
| `search` | `/sites` | See [What it covers](#what-it-covers) |
| `$expand=children` | driveItem `GET`s (root, item id, path) | Children the caller can read, inline. No other `$expand` value is accepted. `$expand=permissions` is `400`: Graph documents that a driveItem's permissions cannot be expanded; call `/permissions` |

`$filter`, `$count` and `$orderby` survive in `@odata.nextLink`. Send the `ConsistencyLevel` header again when you follow it.

```bash
curl -H "Authorization: Bearer app" -H "ConsistencyLevel: eventual" \
  "http://localhost:8080/v1.0/users?\$filter=startsWith(displayName,'a')&\$count=true"
```

## JSON batching

`POST /v1.0/$batch` takes `{"requests":[{"id","method","url","headers"?,"dependsOn"?}]}` and returns `{"responses":[{"id","status","headers","body"}]}`. The `url` is relative to `/v1.0`.

- Up to 20 requests; ids must be unique. A malformed batch is `400`; otherwise the batch itself is `200` and each response carries its own status.
- Each sub-request goes through the normal pipeline as the batch's caller, so visibility, errors, paging and the `ConsistencyLevel` or `Prefer` headers you pass per request all apply. A `302` from `/content` is reported as a `302` with its `Location` header.
- Only `GET`. `POST`, `PUT`, `PATCH` and `DELETE` sub-requests get a `405` response and are not run; unknown methods get `400`.
- `dependsOn` is honoured: dependencies run first, and a request whose dependency failed gets `424`.
- Throttling (below) is applied per sub-request.

The SDK's `BatchRequestContentCollection` works:

```csharp
var batch = new BatchRequestContentCollection(graph);
var users = await batch.AddBatchRequestStepAsync(graph.Users.ToGetRequestInformation());
var hr = await batch.AddBatchRequestStepAsync(graph.Groups["hr"].ToGetRequestInformation());
var responses = await graph.Batch.PostAsync(batch);
var page = await responses.GetResponseByIdAsync<UserCollectionResponse>(users);
```

## Site groups

On a real SharePoint tenant most library permissions are not granted to Entra groups but to SharePoint **site groups** (Owners, Members, Visitors, or custom). Graph reports those in `grantedToV2` as `siteGroup` instead of `user` or `group`:

```json
{ "id": "...", "roles": ["read"],
  "grantedToV2": { "siteGroup": { "id": "3", "displayName": "Knowledge Base Members", "loginName": "Knowledge Base Members" } } }
```

The `id` is a number that is only unique inside the site. The sample fixture has one site group, `kb-members` ("Knowledge Base Members", numbered `3`), whose member is `group:all-staff`, and grants it `read` on `/General` in addition to the direct `all-staff` grant. So the permissions listing for anything under `/General` now has two entries, and code that only understands `user` and `group` will break on this mock, as it would on a real tenant.

**You cannot list the members of a site group through Graph v1.0.** The permission only names the group. The `sharePointGroup` members API in v1.0 is for SharePoint Embedded containers (`/storage/fileStorage/containers/{id}/sharePointGroups/{id}/members`), and `/groups/{id}/members` only knows Entra groups. Usual strategies: call the SharePoint REST API (`/_api/web/sitegroups/getbyid({id})/users`, which needs a token for the SharePoint host, not Graph) to expand the group; keep your own mapping from site-group ids to the Entra groups you know are inside them (on Microsoft 365 group-connected sites the Members and Owners site groups normally contain the connected group, which Graph can expand; verify this in your tenant); or treat unresolved site groups as "unknown" and fail closed. The mock does **not** invent a Graph endpoint for this. Membership is visible only in `GET /_admin/state`, and tests change it through the admin API.

## Change tracking

### Drive delta

Drive delta returns the first full enumeration, then only what changed. It works like Graph:

1. Call `GET /drives/{driveId}/root/delta` and follow `@odata.nextLink` until you get an `@odata.deltaLink`.
2. Store the `deltaLink`. Later, call it again to get changes since then.
3. A call with `?token=latest` returns no items and a fresh `deltaLink`. Use it to start from "now".

Deleted items come back as `{ "id": ..., "deleted": { "state": "deleted" }, "parentReference": ... }`, with no name.

Permission changes show up as items in the page. Send `Prefer: deltashowsharingchanges` to also get `"@microsoft.graph.sharedChanged": "True"` on those items. Without the header the item is still reported, just without the annotation.

Here is a sync that runs in the middle of an admin change:

```csharp
// graph is the GraphServiceClient from "Using the Graph SDK", created with "app" or a user token.
var site = (await graph.Sites.GetAsync())!.Value!.Single(s => s.Name == "kb");   // the sample has only this site; the company fixture has four
var driveId = (await graph.Sites[site.Id].Drives.GetAsync())!.Value![0].Id!;

async Task<(List<DriveItem> Items, string DeltaLink)> SyncAsync(string? link, bool sharing = false)
{
    var items = new List<DriveItem>();
    var builder = graph.Drives[driveId].Items["root"].Delta;
    if (link is not null)
    {
        builder = builder.WithUrl(link);
    }

    while (true)
    {
        var page = await builder.GetAsDeltaGetResponseAsync(r =>
        {
            if (sharing)
            {
                r.Headers.Add("Prefer", "deltashowsharingchanges");
            }
        });
        items.AddRange(page!.Value!);
        if (page.OdataDeltaLink is { } done)
        {
            return (items, done);
        }

        builder = graph.Drives[driveId].Items["root"].Delta.WithUrl(page.OdataNextLink!);
    }
}

// 1. Full enumeration. Keep the deltaLink.
var (_, deltaLink) = await SyncAsync(null);

// 2. Change something through the admin API, e.g. revoke erin:
//    curl -X DELETE http://localhost:8080/_admin/permissions -H 'Content-Type: application/json' \
//         -d '{"itemPath":"/Shared/contractor-onboarding.md","principal":"user:erin"}'

// 3. Incremental sync from the saved deltaLink.
var (changes, nextLink) = await SyncAsync(deltaLink, sharing: true);
foreach (var item in changes)
{
    var shared = item.AdditionalData.ContainsKey("@microsoft.graph.sharedChanged");
    Console.WriteLine($"{item.Id} deleted={item.Deleted is not null} sharedChanged={shared}");
}
// Save nextLink for the next sync.
```

Visibility is part of the diff. If a user loses access to an item, the next user-context delta reports it as `deleted`. If a user gains access, it is reported as an upsert.

### Groups delta

Group membership changes are not in drive delta, so use `GET /groups/delta`. It is plain JSON in the sample below because the SDK call shape is not part of the mock's contract.

Each group carries `members@delta`. Typed members are `#microsoft.graph.user` or `#microsoft.graph.group`. A removed member has `"@removed": { "reason": "deleted" }`.

```csharp
using System.Net.Http.Headers;
using System.Text.Json.Nodes;

using var http = new HttpClient { BaseAddress = new Uri("http://localhost:8080/") };
http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", "app");

var url = "/v1.0/groups/delta";
string deltaLink;
while (true)
{
    var page = (await http.GetFromJsonAsync<JsonObject>(url))!;
    foreach (var group in page["value"]!.AsArray())
    {
        foreach (var member in group!["members@delta"]?.AsArray() ?? [])
        {
            var removed = member!["@removed"] is not null;
            Console.WriteLine($"{group["id"]} {member["id"]} removed={removed}");
        }
    }

    if (page["@odata.deltaLink"] is { } link)
    {
        deltaLink = link.GetValue<string>();
        break;
    }

    url = page["@odata.nextLink"]!.GetValue<string>();
}
// Keep deltaLink. Pass it as the next url to get changes. Use "/v1.0/groups/delta?$deltatoken=latest" to start from now.
```

`$select` without `members` removes `members@delta` from the response.

### Tokens and resets

Tokens belong to one server "epoch". The epoch changes when the server starts and on every `POST /_admin/reset`. A token from an older epoch gets:

```json
HTTP 410 Gone
{ "error": { "code": "resyncRequired", "message": "The delta token is no longer valid because the server state was reset. ..." } }
```

When you get `410`, start again without a token.

## Admin API

`/_admin` is not part of Graph. It has no authentication and uses plain JSON, not Graph shapes. Every mutation is reflected in delta.

Principals are `user:<id-or-upn>`, `group:<id>` or `siteGroup:<id>` (a site group of the same site as the item's drive). `driveId` is optional when the server has one drive, which is true of the sample. Items are addressed by `itemPath` (drive-relative, for example `/General/office-wifi.md`) or by `itemId` (the wire id from Graph responses). Use exactly one of them.

**Grant a permission.** Role is `read`, `write` or `owner`. Granting the same role again changes nothing.

```bash
curl -X POST http://localhost:8080/_admin/permissions \
  -H 'Content-Type: application/json' \
  -d '{"itemPath":"/Shared/contractor-onboarding.md","principal":"user:dave","role":"read"}'
```

**Revoke a permission.** `role` is optional. Without it, all direct grants for that principal on the item are removed. An item with no direct grant for the principal returns `404`.

```bash
curl -X DELETE http://localhost:8080/_admin/permissions \
  -H 'Content-Type: application/json' \
  -d '{"itemPath":"/Shared/contractor-onboarding.md","principal":"user:erin"}'
```

Both return JSON with `changed`, `sequence`, `driveId`, `itemId`, `path` and `created`. `changed` is `false` when nothing changed.

**Add a group member.** The member can be a user or a group. Adding a group that would create a cycle returns `400`.

```bash
curl -X POST http://localhost:8080/_admin/groups/eng-frontend/members \
  -H 'Content-Type: application/json' \
  -d '{"member":"user:erin"}'
```

**Remove a group member.** The member must be a direct member, or the call returns `404`.

```bash
curl -X DELETE http://localhost:8080/_admin/groups/eng-frontend/members \
  -H 'Content-Type: application/json' \
  -d '{"member":"user:bob"}'
```

**Change site group members.** Same body as for groups; the member must be a user or an Entra group. The site can be the fixture id or the wire id, the group its fixture id or its numeric id. Every item whose effective permissions include the site group is reported as a sharing change in drive delta (`sharedChanged` with `Prefer: deltashowsharingchanges`), and users who gain or lose access see the item appear or disappear in their own delta.

```bash
curl -X POST http://localhost:8080/_admin/sites/kb/siteGroups/kb-members/members \
  -H 'Content-Type: application/json' -d '{"member":"user:erin"}'
curl -X DELETE http://localhost:8080/_admin/sites/kb/siteGroups/kb-members/members \
  -H 'Content-Type: application/json' -d '{"member":"user:erin"}'
```

**Simulate throttling.** The next `count` Graph requests (sub-requests of a batch count individually; `/_admin` and downloads do not) are answered with `429`, a `Retry-After: <retryAfterSeconds>` header (default 1) and Graph's body (`TooManyRequests`). The SDK's default retry handler waits and retries, so a correct client never notices. `DELETE` clears it, and so does reset.

```bash
curl -X POST http://localhost:8080/_admin/throttle \
  -H 'Content-Type: application/json' -d '{"count":3,"retryAfterSeconds":2}'
curl -X DELETE http://localhost:8080/_admin/throttle
```

Retries only happen when the client was built with the SDK's default handlers (`GraphClientFactory.Create`). The `HttpClientRequestAdapter` setup above with a bare `HttpClient` does not retry, which is also true for production.

**Add or replace a file.** Multipart fields: `path` (required), `file` (required), `driveId` (optional). Missing parent folders are created with no permissions. Returns `201` when the file is new and `200` when it replaces one. A folder cannot be replaced with a file.

```bash
curl -X POST http://localhost:8080/_admin/files \
  -F path=/General/new-policy.md \
  -F file=@./new-policy.md
```

**Delete a file or folder.** Deleting a folder deletes its contents. Each deleted item is reported in delta. Query parameters: `path` (required), `driveId` (optional). The drive root cannot be deleted.

```bash
curl -X DELETE "http://localhost:8080/_admin/files?path=/General/new-policy.md"
```

**Reset.** Reloads the fixture, clears the change log and starts a new epoch. All outstanding delta tokens become `410`.

```bash
curl -X POST http://localhost:8080/_admin/reset
# {"status":"reset","epoch":"..."}
```

**Dump state.** Returns the live state in the fixture format (YAML). Save it to a file to reuse it as a fixture.

```bash
curl http://localhost:8080/_admin/state
```

## Writing a fixture

A fixture is one YAML file. Start from `fixtures/sample.yaml`. The top-level keys are `tenant`, `users`, `groups`, `sites`.

```yaml
tenant:
  domain: contoso-mock.onmicrosoft.com      # UPNs and group mail addresses use this domain

users:
  - id: alice                               # stable, human-readable id; used in tokens and principals
    displayName: Alice Backend
    upn: alice@contoso-mock.onmicrosoft.com

groups:
  - id: eng-backend
    displayName: Engineering - Backend
    members: [user:alice]                   # user:<id> or group:<id>; nesting is allowed

sites:
  - id: kb
    displayName: Knowledge Base
    siteGroups:                             # optional; SharePoint site groups of this site
      - id: kb-members
        displayName: Knowledge Base Members
        members: [group:all-staff]          # user:<id> or group:<id>
    drives:
      - id: kb-docs
        name: Documents
        items:
          - path: /General                  # a folder: no content and no source
            permissions:
              - { principal: group:all-staff, role: read }   # role defaults to read
              - { principal: siteGroup:kb-members, role: read }
          - path: /General/office-wifi.md
            content: |                      # text file, UTF-8
              # Office Wi-Fi
              Network: ContosoStaff.
          - path: /General/floor-plan.png
            source: floor-plan.png          # file under fixtures/files/ (or next to the fixture)
          - path: /Engineering/Backend
            breakInheritance: true          # ignore grants from ancestors for this subtree
            permissions:
              - { principal: group:eng-backend, role: write }
```

Rules:

- **Ids.** Users, groups, sites and drives use the fixture id (`alice`, `eng-backend`, `kb`, `kb-docs`). These ids are what you put in tokens and principals.
- **Wire ids.** Graph ids are deterministic and derived from the fixture ids. Users and groups are UUIDv5 values of `user:<id>` and `group:<id>`. Drive ids look like `b!...`. Item ids are `01` followed by hex. Your code should read ids from responses, not compute them. The admin API and fixture lookups accept fixture ids for users, groups, sites and drives, and UPNs for users.
- **Content.** An item has at most one of `content` (text), `source` (a file, relative to the fixture directory or to its `files/` subdirectory) or `contentBase64` (binary inline; this is what `/_admin/state` writes for non-text files). An item with none of them is a folder.
- **Paths.** Drive-relative, case-insensitive, with or without the leading slash. Parent folders that are not listed are created implicitly, with no permissions of their own.
- **Sites.** The path is `/sites/<id>` unless the site sets `path:`. `root: true` marks the tenant root site (`/sites/root`, path `/`). If no site is marked, an empty root site named after the tenant is added. Sites are addressed as `<tenant>.sharepoint.com` plus the path (the host is the first label of `tenant.domain`).
- **Site groups.** `siteGroups` on a site list groups with `id` (unique across the whole fixture), `displayName` and `members` (users and Entra groups; site groups cannot nest). They are numbered from 3 within the site. A `siteGroup:<id>` principal is only valid on drives of the same site.
- **Permissions.** Each has `principal` (`user:<id>`, `group:<id>` or `siteGroup:<id>`) and `role` (`read`, `write` or `owner`; default `read`). Grants on a folder apply to everything below it.
- **Inheritance.** `breakInheritance: true` on an item stops grants from above it applying to that item and its descendants, as in SharePoint. The item's own grants still apply.
- **Validation.** Unknown principals or group members, duplicate ids or paths, unknown roles and missing source files stop the server at startup with an error that names the problem.

Use `GET /_admin/state` to see how the server stored a fixture.

## Known differences from Microsoft Graph

These are known and deliberate, or not yet implemented:

- **Hashes.** `file.hashes` has `sha1Hash` and `sha256Hash` only. There is no `quickXorHash`, which SharePoint returns.
- **Addressing.** `root:/path:` addressing works. `/items/{id}:/path:` does not.
- **Folder fields.** `size` is the total of the readable descendants. `childCount` counts readable children only. Real Graph counts everything.
- **Visibility of folders.** Intermediate folders are not visible just because a file below them is readable. The drive root and site metadata are visible to any valid token.
- **Delta.**
  - Only `Prefer: deltashowsharingchanges` is recognised. `deltashowremovedasdeleted`, `deltatraversepermissiongaps` and `deltaExcludeParent` are not implemented.
  - Timestamp tokens and `hierarchicalsharing` are not implemented.
  - Incremental pages do not include parent folders.
  - No `Preference-Applied` header is returned.
  - Folder-scoped delta reports only that subtree.
- **Permissions.** Only direct grants with the roles `read`, `write` and `owner`. No sharing links. Real Graph also sends the deprecated `grantedTo` next to `grantedToV2`; the mock sends `grantedToV2` only. Graph's documentation says SharePoint libraries do not return `inheritedFrom`; the mock does, because the project spec asks for it, so do not depend on it against a real tenant. The newer `grantedToV2.sharePointGroup` form is not emitted.
- **Site groups.** Their members cannot be listed through Graph v1.0 (see [Site groups](#site-groups)); the mock does not add an endpoint for it. Site-group ids and `loginName` follow the examples in the docs and community reports, not a verified dump of a real tenant.
- **Sites.** `GET /sites` without `search` lists the fixture's sites only, whereas `search=*` and `getAllSites` also return the implicit root. `getAllSites` is application-permission only on real Graph; the mock lets user tokens call it. Site ids are `host,guid,guid` but the guids are made up. Sub-sites, `/groups/{id}/sites/root` and site `permissions` are not implemented. `search` is a substring match on name and displayName; Graph's is a free-text index.
- **Query options.** `$filter` on directory objects is the small subset above. Graph also returns `Request_UnsupportedQuery` for some unsupported operators; the mock answers `BadRequest`. `$expand=children` is not accepted on children listings.
- **Batching.** `GET` only. The sub-request `Authorization` is always the batch's. `body` for non-JSON responses is base64url like Graph, but response headers are only the few the mock sets.
- **Throttling.** Simulated on demand through `/_admin/throttle`; the mock never throttles by itself, and Graph's `retry-after` in a batch is only in the sub-response headers.
- **Timestamps.** Fixture items use a fixed base time. Changes made at runtime use the server clock.
- **Auth.** Tokens are not validated. Any `user:<known id>` works.
- **Tenant.** One tenant. Multiple sites and drives are supported.
- **`/me` in app context** returns `400`, as Graph does for application tokens.

## Development

Prerequisites: [mise](https://mise.jdx.dev/) (it installs the .NET SDK pinned in `mise.toml`), or the .NET SDK from `global.json`.

```bash
mise install
mise exec -- dotnet build MockGraph.sln
mise exec -- dotnet test MockGraph.sln
mise exec -- dotnet format MockGraph.sln --verify-no-changes
docker build -t mock-graph-server:dev .
```

Layout:

- `src/MockGraph.Server/` is the server. `Endpoints/` holds the routes, `State/` the in-memory model and change log, `OData/` the query and token handling, `Mapping/` converts to `Microsoft.Graph.Models`.
- `tests/MockGraph.Tests/` runs the server in process with `WebApplicationFactory` and the real `Microsoft.Graph` SDK. These tests are the contract.
- `fixtures/sample.yaml` and `fixtures/files/` hold the sample data. `fixtures/company.yaml`, `company.notes.md`, `company.visibility.yaml` and `fixtures/files/company/` hold the rich company fixture.
- `tools/fixture-files/` rebuilds the binary company documents (`.docx`, `.pdf`, `.xlsx`, `.pptx`) from plain-text sources; `tools/fixture-visibility/` recomputes the visibility oracle. Both are Python scripts, not part of the image. After editing `company.yaml`, rerun the oracle script; `CompanyFixtureTests` fails when they disagree.

Conventions:

- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/).
- Branch names are `<area>-<three-word-description>`.
- CI runs build, tests and `dotnet format --verify-no-changes`. A formatting diff fails the build.
- Releases: push a `v*` tag to publish a versioned image to GHCR. Pushes to `main` publish `latest`.

### Contract tests against a real tenant

`tests/MockGraph.ContractTests` is a read-only suite that runs against the mock by default and against real Microsoft Graph when `GRAPH_BASE_URL` and `GRAPH_TOKEN` are set. See [docs/contract-tests.md](docs/contract-tests.md).

## License

[MIT](LICENSE). Third-party components shipped in the Docker image are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
