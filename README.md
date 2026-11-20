# O3P MCP

Odoo 18 Community addon that makes Odoo an OAuth 2.1 authorization server for
an external MCP server. MCP clients sign in with an Odoo account; the MCP
server checks every access token through Odoo's introspection endpoint.

This is the `18.0` branch. The `20.0` branch has the same features for
Odoo 20; the protocol, the introspection answer and the data API are
identical on both. Differences specific to Odoo 18 are listed under
"Odoo 18 notes".

## Groups and scopes

The addon adds an **MCP** application category with two groups, shown on
the user form as one MCP selection, and the categories **MCP SQL** and
**MCP records** with one group each, each shown as a selection of its own:

| Group | Scopes granted |
|---|---|
| MCP / User | `okf.read`, `odoo.read` |
| MCP / Editor (implies User) | the User scopes and `okf.write` |
| MCP SQL / Allowed | `odoo.sql`, for users who also hold MCP / User |
| MCP records / Editor | `odoo.write`, for users who also hold MCP / User |

MCP SQL / Allowed and MCP records / Editor grant nothing by themselves:
removing MCP / User (or MCP / Editor, which implies it) ends every MCP right
of a user in one step.

Up to version 1.40 the User group granted `odoo.sql` too. Upgrading from
such a version gives MCP SQL / Allowed to every internal user who holds
MCP / User, so nobody loses SQL; new users get it only when it is assigned.

| Scope | Allows |
|---|---|
| `okf.read` | Any MCP call; required in every token |
| `okf.write` | Live write tools and producer runs on the knowledge base |
| `odoo.read` | Reading Odoo records through the data API, with the user's own rights |
| `odoo.sql` | Running read-only SQL through the data API on the rows the user may read |
| `odoo.write` | Creating and changing Odoo records through the data API, with the user's own rights, on the models opened in the settings |

Only active internal users hold scopes: archived users, portal and public
users (even when placed in an MCP group) and users without the User group
hold none.

## Audiences

An audience (**Settings › Technical › MCP › Audiences**) is a named set of
Odoo groups whose users may read the same knowledge: a `code` (a lowercase
letter, then lowercase letters, digits, `-` or `_`, at most 32 characters),
a name and the groups. A user belongs to an audience when holding any of
its groups, directly or through another group. Only users who may sign in
belong to audiences.

Token introspection names the user's audiences, sorted by code, at most 64.
They are read from the user's current groups at every call, so a changed
group or an archived audience shows at the next introspection. The MCP
server serves each knowledge bundle to the audiences its project file names
for it, and to every signed-in user when it names none. okf-mcp
0.13.0-rc.1 and older ignore the field and serve every bundle to everyone.

## Clients, codes and tokens

- **Clients** (`o3p.mcp.client`) are MCP clients, public (no secret) or
  confidential (`client_secret_post` / `client_secret_basic`), and the
  resource-server credentials the MCP server uses for introspection.
- **Authorization codes** live 5 minutes and work once.
- **Access tokens** live 60 minutes; **refresh tokens** 30 days from their
  issue. Every refresh rotates the refresh token: the new pair belongs to the
  same family, the previous access token stays valid until it expires, and
  presenting an already used refresh token revokes the whole family. One
  exception covers a client that lost the refresh response: if the same
  client presents the just-used refresh token within 10 seconds and the new
  token has not been used yet, the request is refused with `invalid_grant`
  and nothing is revoked. Authorization codes have no such grace.
- Codes, tokens and client secrets are 256-bit random values. Odoo stores only
  their SHA-256 digests, so a token cannot be read back from the database.
- A token answers as active only while it is unexpired and unrevoked, its
  client is active, its user is active and the user still holds `okf.read`.
  The scopes reported are the granted scopes still allowed by the user's
  current groups.
- A daily scheduled action deletes expired codes, tokens whose refresh
  token has expired, and self-registered clients that have not completed a
  token exchange within 24 hours.

## Endpoints

All URLs are relative to the issuer (see Settings).

| Method and path | Purpose |
|---|---|
| `GET /.well-known/oauth-authorization-server` | Authorization server metadata (RFC 8414) |
| `POST /o3p_mcp/oauth/register` | Dynamic client registration (RFC 7591) |
| `GET`, `POST /o3p_mcp/oauth/authorize` | Sign-in and consent (authorization code with PKCE) |
| `POST /o3p_mcp/oauth/token` | Token endpoint (authorization code and refresh token grants) |
| `POST /o3p_mcp/oauth/introspect` | Token introspection for the MCP server (RFC 7662) |
| `POST /o3p_mcp/oauth/revoke` | Token revocation (RFC 7009) |
| `POST /o3p_mcp/api/v1/<op>` | Data API for the MCP server, as the signed-in user; read-only until an administrator opens models for writing |

The metadata, registration, token and revocation endpoints answer
cross-origin requests (`Access-Control-Allow-Origin: *`); their preflight
allows the `Authorization`, `Content-Type` and `MCP-Protocol-Version`
request headers, the last one being sent by MCP clients on every request.
Introspection and the data API answer no cross-origin requests. None of the
API endpoints creates a session.

### Client registration

The body is a JSON object with at least `redirect_uris`. Each redirect URI
must be an `https` URL, an `http` URL on a loopback host (`localhost`,
`127.0.0.1`, `[::1]`) or a private-use scheme such as
`com.example.app:/callback`. Hosts must be DNS names or IP addresses;
backslashes, fragments, user information, numeric host names such as
`127.1` and schemes such as `javascript:`, `data:` or `file:` are refused.
The same rules apply to redirect URIs edited by an administrator. Redirect URIs are later
compared exactly, except that an `http` redirect URI on a loopback host may
use any port (RFC 8252 §7.3): scheme, host, path and query must still match.

`token_endpoint_auth_method` may be `none` (public client),
`client_secret_post` or `client_secret_basic` (the RFC 7591 default). Grant
types are `authorization_code` and optionally `refresh_token`; the only
response type is `code`. Confidential clients receive their `client_secret`
once, in the registration response.

Each IPv4 address, or IPv6 /64 network, may register at most 200 clients per
hour (a setting); further requests get HTTP 429 with `Retry-After`. The limit
is that high because hosted connectors register every user's client from a
few shared addresses, so all the people of one organisation who connect in
the same hour count against one address. A self-registered
client that has not completed a token exchange within 24 hours is deleted.

### Authorization

`GET /o3p_mcp/oauth/authorize` takes the usual OAuth 2.1 parameters:
`response_type=code`, `client_id`, `redirect_uri`, `scope`, `state`,
`code_challenge` with `code_challenge_method=S256`, and `resource`.

- A visitor who is not signed in is sent to the Odoo login page and comes
  back afterwards.
- An unknown or archived client, or a redirect URI that does not match one
  of the client's registered URIs, shows an error page; Odoo never redirects
  to an unregistered URI. Matching is exact apart from the port of loopback
  `http` URIs. When the client has a single redirect URI,
  `redirect_uri` may be omitted.
- PKCE with `S256` is required; `plain` is refused.
- `resource` must be the configured MCP URL (a trailing slash is ignored).
  When it is omitted, the configured MCP URL is used, since it is the only
  resource Odoo issues tokens for.
- `scope` may list any of the five scopes; `okf.read` is always added,
  since every call needs it, and without a known scope all five are
  requested. `offline_access` (asked for by clients that want refresh
  tokens, which are always issued) and unknown scopes are ignored rather
  than refused, as RFC 6749 §3.3 allows. The grant is narrowed to what the
  user's groups allow, so an MCP user who asks for `okf.write` does not get
  it; the token response's `scope` names what was granted. A token only carries the scopes requested when it was authorized:
  a client that asked for `okf.read okf.write` has to sign in again to use
  the Odoo scopes.
- A user without the MCP User group sees an error page with a link that
  returns `error=access_denied` to the client. This is checked right after
  the client and redirect URI, before any other parameter.

The consent page names the client, the user, the MCP server and the scopes,
and shows where the answer is sent: the redirect host in its ASCII (IDNA)
form, so a look-alike Unicode name appears as `xn--…`. It cannot be framed. Approving posts the
form back with Odoo's CSRF token and redirects to the client with `code`,
`state` and `iss` (RFC 9207); denying redirects with `error=access_denied`.
For clients created by an administrator, other request errors are
redirected to the client with `error`, `error_description`, `state` and
`iss`. Self-registered clients are never redirected to before the user has
answered: their request errors show the error page with an explicit
"Return to <host>" link.

### Token endpoint

`POST /o3p_mcp/oauth/token` takes a form-encoded body. Clients authenticate
with the method they registered: `client_id` alone for public clients,
`client_id` and `client_secret` in the body for `client_secret_post`, HTTP
Basic for `client_secret_basic`. Mixing Basic with body credentials is
refused. Responses carry `Cache-Control: no-store`.

- `grant_type=authorization_code` with `code`, `code_verifier`,
  `redirect_uri` (the one used at authorization, under the same matching
  rule; it may be omitted only when the client has a single redirect URI) and optionally `resource`.
  A code is consumed by its first exchange, successful or not; presenting it
  again revokes the tokens it was exchanged for.
- `grant_type=refresh_token` with `refresh_token` and optionally `scope`
  (a subset of the original grant that includes `okf.read`; `offline_access`
  and unknown scopes are ignored, a scope beyond the grant or without
  `okf.read` gets `invalid_scope` and leaves the refresh token usable) and
  `resource`. The user must still be
  active and in the MCP groups: losing the Editor group drops `okf.write`
  from the new token, losing MCP SQL / Allowed drops `odoo.sql`, losing the
  User group refuses the refresh and revokes the family.

The response is `access_token`, `token_type` (`Bearer`), `expires_in`,
`refresh_token` and `scope`. Errors follow RFC 6749 (`invalid_client` with
HTTP 401, `invalid_grant`, `invalid_scope`, `invalid_target`,
`unsupported_grant_type`).

### Introspection

The MCP server posts `token=<access token>` to
`/o3p_mcp/oauth/introspect`, authenticated with HTTP Basic as a
resource-server client. Missing or wrong credentials get HTTP 401 with
`WWW-Authenticate: Basic`. An active token answers:

```json
{ "active": true, "sub": "42", "username": "jdoe", "name": "Jane Doe",
  "email": "jdoe@example.com", "scope": "okf.read okf.write odoo.read odoo.sql",
  "audiences": ["hr"],
  "aud": "https://mcp.example.com/mcp", "exp": 1794000000, "client_id": "c_…" }
```

`sub` is the Odoo user ID, `username` the login, `aud` the configured MCP
URL, `exp` the access token expiry (seconds since the epoch) and `client_id`
the MCP client the token was issued to. `scope` is recomputed from the user's
current groups at every call, and so is `audiences`, the codes of the
audiences the user belongs to (an empty list when there are none). `email` is left out when the user has none.
Every other case (unknown, expired, revoked, refresh token, archived user or
client, user without the MCP User group) answers exactly
`{ "active": false }`.

### Revocation

`POST /o3p_mcp/oauth/revoke` with `token` (access or refresh token) and
optionally `token_type_hint`, authenticated like the token endpoint. Revoking
either token revokes every token of the same authorization. Unknown tokens
and tokens of other clients are ignored; the answer is always an empty
HTTP 200 once the client is authenticated.

## Data API

The MCP server reads Odoo data on behalf of a signed-in user through
`POST /o3p_mcp/api/v1/<op>`. It authenticates with HTTP Basic as the
resource-server client (the introspection credentials, form-urlencoded before
base64 as RFC 6749 §2.3.1 requires) and passes the user's access token in the
JSON body:

```json
{ "token": "<the user's access token>", "params": { "model": "res.partner" } }
```

Odoo checks the token as introspection does (unrevoked, unexpired, issued to
an active client for the configured MCP server URL, user active and internal,
not a portal user, `okf.read` still allowed by the groups) and requires the
operation's scope (`odoo.read`, `odoo.sql` for `sql`, `odoo.write` for
`create` and `update`). The operation then runs as that user, in the
user's language and time zone: Odoo's access rights, record rules and field
groups decide what the answer contains. A reading operation runs in a
read-only database transaction with a time limit, so it cannot change
anything; `create` and `update` are described under Writing records. The endpoint
answers no cross-origin requests and never creates a session.

A success is `200 { "result": … }`. An error is
`{ "error": { "code": "…", "message": "…" } }` with a fixed message that
never contains an exception text, a parameter or record data (the one
exception, PostgreSQL's answer to an SQL query, is described under SQL):

| Status | Code | When |
|---|---|---|
| 400 | `invalid_params` | The body or a parameter is not valid |
| 401 | `invalid_client` | The resource-server credentials are missing or wrong |
| 403 | `invalid_token` | The user's token fails any check above |
| 403 | `insufficient_scope` | The token lacks the operation's scope |
| 403 | `access_denied` | The user may not read the model or the records; for a write, the model is not opened for writing or the user may not create or change the records |
| 404 | `unknown_op` | The operation does not exist |
| 404 | `unknown_model` | The model does not exist, is transient or abstract, or is hidden |
| 413 | `result_too_large` | The answer exceeds the configured size |
| 422 | `query_error` | The database or the ORM refused the query, or anything else failed unexpectedly (the call is still recorded) |
| 429 | `too_many_requests` | SQL only: the user, or all users together, already run the most queries allowed at once |
| 503 | `unavailable` | SQL only: no sandbox is configured, or it cannot be reached |
| 504 | `timeout` | The query exceeded the time limit |

Validation errors and `unknown_model` also carry `details`, so a client can
correct its call:

```json
{ "error": { "code": "invalid_params", "message": "…",
  "details": { "field": "create_date", "reason": "granularity_required",
               "hint": "Append :day, :week, :month, :quarter or :year to a date field (or :hour to a datetime field)." } } }
```

`field` is the field path or parameter name the client sent, `model` the
model concerned (for `unknown_model` and `no_model_access`, and the model a
field reached through a path belongs to), `reason` one of `unknown_param`,
`bad_value`, `bad_domain`, `too_deep`, `unknown_field`, `not_readable`,
`not_filterable`, `not_searchable`, `not_sortable`, `not_groupable`,
`not_relational`, `bypass_relation`, `no_relation_access`,
`no_model_access`, `unknown_model`, `bad_operator`, `bad_order`, `bad_aggregate`,
`granularity_required` or `bad_granularity`, for writes also
`model_not_writable`, `no_write_access`, `no_record_access`, `not_writable`,
`unknown_record`, `required_missing` or `bad_company`, and `hint` a fixed sentence for
that reason (for `hr.employee`: use `hr.employee.public`). Details contain
schema names and fixed texts only, never record data or Odoo's own error
text.

### Operations

| Operation | Parameters | Result |
|---|---|---|
| `models` | `query` (optional, matched on the technical and the display name), `limit` (1–50, default 50), `offset` (optional, 0–10000) | `{ "models": [{ "model", "name" }], "total", "truncated" }`: the models the user may read, sorted by technical name; `total` counts all that match, `truncated` tells whether more follow |
| `describe` | `model` | `{ "model", "name", "table", "access", "fields": [{ "name", "string", "type", "relation"?, "required", "readonly", "stored", "searchable", "sortable", "groupable", "writable", "selection"?, "help"? }] }`: the fields the user may read; `access` is `{ "create", "write" }`, whether the model is opened for writing and Odoo gives the user that right, and `writable` whether `create` and `update` take a value for the field; `table` is the database table or view of the model, `null` when it is computed from a query; `stored` tells whether the field has a column of its own (one2many and many2many have none), `searchable`, `sortable` and `groupable` whether domains, `order` and `groupby` accept it for this user |
| `search` | `model`, `domain`, `fields`, `order`?, `limit` (1–200, default 80), `offset`? (0–1000000) | `{ "records", "count", "truncated" }` |
| `read` | `model`, `ids` (1–100), `fields` | `{ "records" }` |
| `group` | `model`, `domain`, `groupby`? (0–5), `aggregates` (1–20), `order`?, `limit` (1–500, default 80) | `{ "groups": [{ <groupby and aggregate keys> }] }` |
| `context` | none | `{ "user": { "id", "name", "login" }, "lang", "tz", "company": { "id", "name" }, "companies": [{ "id", "name" }], "now", "today" }`: the signed-in user, the language and time zone answers come in, the user's default company and all the user's companies, the current time in UTC and today's date in the user's time zone |

`group` runs Odoo's grouped read as the user, so counts and totals cover
only the records the user may read; it never adds empty groups. Without
`groupby`, or with an empty list, the answer is one group: the totals over
every record the domain finds for the user. Group
values come as in the web client: `[id, name]` for relations,
`[start, label]` for dates. `models` never lists transient, abstract or
hidden models. Records come in
Odoo's `read` format: `id` plus the requested fields, many-to-one values as
`[id, name]`, dates and datetimes as UTC strings. `count` counts every record
the domain matches for the user; `truncated` tells whether more follow.

Parameters are checked strictly, as the user, and a request with any invalid
part, or a parameter the operation does not take, is refused with
`invalid_params`; nothing is silently dropped:

- `fields` lists field names the user may read and that are not hidden.
- `domain` is a JSON domain list (`["&", ["name", "ilike", "x"], …]`) parsed
  by Odoo's domain machinery. Every field path is checked hop by hop:
  readable and not hidden; every hop but the last a relational field to a
  model whose searches apply that model's rules; at most 4 hops. A relation
  to a model the user may not read is refused with `access_denied`. `any` /
  `not any` sub-domains are checked against the related model.
- Conditions on one2many and many2many fields are rewritten to the `any` /
  `not any` form, because Odoo applies the related model's record rules only
  there: `["child_ids", "in", [7]]` becomes
  `["child_ids", "any", [["id", "in", [7]]]]`, `= 7` and `!= 7` work the same
  way, `= false` becomes `not any []`, `!= false` becomes `any []`, and
  `ilike`-style operators match the related records' display name. Forms
  without a faithful rewrite (comparisons, `=?`, lists holding `false`) are
  refused. Paths are expanded the same way,
  `a.b` into `["a", "any", [["b", …]]]`, as Odoo itself does.
- On a many-to-one field, record ids and `false` compare the column itself
  (`["partner_id", "=", 7]`, `["partner_id", "in", [7, false]]`). Text
  matches the related records' display name with the related model's rules
  applied: `["partner_id", "ilike", "acme"]` becomes
  `["partner_id", "any", [["display_name", "ilike", "acme"]]]`, and `=`,
  `in` and their negations work the same way. On a relation that bypasses
  access (Odoo 18's `auto_join`, such as a project's customer),
  such text conditions are refused, since Odoo would match names, e-mails or
  VAT numbers of records the user may not read; search the related model
  first and filter on its ids.
- Every field a condition uses, at every step of its path, must be
  searchable: stored, or computed with a search method (`describe` marks
  it). Odoo silently ignores a condition on a computed field it cannot
  search, which would give a wrong answer without an error, so the API
  refuses it (`not_searchable`). Sorting and grouping likewise need fields
  Odoo can sort and group on (`not_sortable`, `not_groupable`).
- Operators: `=`, `!=`, `>`, `>=`, `<`, `<=`, `=?`, `in`, `not in`,
  `like`, `not like`, `ilike`, `not ilike`, `=like`, `=ilike`, `any`,
  `not any`. Values are JSON scalars or lists of scalars. `child_of` and
  `parent_of` are refused: Odoo walks the hierarchy as superuser, through
  records the user may not read, so their results would reveal how hidden
  records are linked.
- `order` is a comma-separated list of `field`, `field asc` or `field desc`
  on sortable, readable, non-hidden fields.
- `groupby` items are groupable readable fields; date and datetime fields
  need a granularity (`date:month`: `day`, `week`, `month`, `quarter`,
  `year`, and `hour` for datetimes) and other fields take none.
- `aggregates` items are `__count`, `field:sum`, `field:avg`, `field:min` or
  `field:max` on numeric (integer, float, monetary) readable fields, and
  `field:count` or `field:count_distinct` on stored readable fields.
- `group` orders by its own items: `amount_total:sum desc`, `__count desc`,
  `date:month asc`.
- `read` returns the records the user may read, in the order of `ids`; ids
  that do not exist and records the user may not read are left out alike,
  so the answer does not reveal whether a hidden record exists.

### What users can see

The API shows what Odoo itself shows the user, no less and no more:

- Values Odoo computes with elevated rights are visible exactly as in
  Odoo's own interface for that user. For example, with the stock and sale
  apps, a product's forecast quantities (`outgoing_qty`,
  `virtual_available`) count every confirmed sales order, including orders
  of other salespeople the user may not open; the API returns the same
  numbers the product form shows that user. Add such fields to the hidden
  field names if they should not reach MCP clients.
- A many-to-one value shows the related record's display name even when
  the user may not open that record (`"partner_id": [7, "Customer"]`), as
  Odoo's own forms and lists do.
- Calendar attendees (`calendar.attendee`) are readable as Odoo makes them
  readable to internal users.
- Some models hide data when they are read rather than through access
  rules: Odoo blanks other users' private calendar events (`Busy`). A domain,
  an order or a group would still match the hidden values, so on such
  models (any model whose `read`, `fetch`, `_fetch_query`, `search_fetch` or
  `_read_format` an addon overrides) the API filters, sorts and groups only
  on `id` and on the fields the model declares public (for calendar events:
  times, duration, organizer, privacy, recurrence); anything else is
  refused with `invalid_params`, also through relations such as
  `res.partner.meeting_ids`. Reading fields is unaffected, since Odoo's own
  read hides what it must. Overrides reviewed as hiding nothing are listed
  in `models/denylist.py` (`account.move`, `account.move.line`, `crm.lead`,
  `hr.employee`, `ir.actions.act_window`, `mrp.bom.line`).

### Writing records

`create` and `update` (scope `odoo.write`) change records as the signed-in
user. Nothing can be written until an administrator lists the writable
models in the settings; the list is empty by default.

| Operation | Parameters | Result |
|---|---|---|
| `create` | `model`, `records` (1–20 objects of field values), `company`? | `{ "records": [{ "id", "display_name" }] }` |
| `update` | `model`, `ids` (1–100), `values` (one object of field values), `company`? | `{ "records": [{ "id", "display_name" }] }` |

Every part of a call is checked as the user before anything is written, and
a call writes all of its records or none:

- The model must be exposed, on the list of writable models, and Odoo must
  give the user the right to create or change its records
  (`model_not_writable`, `no_write_access`). Some models are never writable,
  whatever the list says, because their records grant access, hold code Odoo
  runs, or send messages: `ir.*`, `res.users*`, `res.groups*`,
  `base.automation*`, `o3p.mcp.*`, `mail.mail`, `mail.message`,
  `mail.followers`, `mail.notification`, `mail.template*`, `mail.alias*`,
  `sms.*`, `payment.*`, every hidden model and every model that hides data
  when it is read.
- A field takes a value when the user may read and write it and it is not
  hidden, not read-only on its definition, not computed without an inverse,
  not related to another record's field, and of a supported type
  (`not_writable`). Odoo's own ORM accepts values for read-only fields; the
  API does not, so a status changes through Odoo's buttons, not by setting
  the field. `describe` marks the writable fields.
- Values: JSON strings for char, text and html fields (html only where Odoo
  sanitises it), numbers for integer, float and monetary fields, booleans,
  `"YYYY-MM-DD"` for dates and `"YYYY-MM-DD HH:MM:SS"` in UTC for datetimes,
  a selection's key, a record id for a many-to-one, and for a many-to-many a
  list of ids, which replaces the linked records, or
  `{ "add": [ids], "remove": [ids] }`. `false` empties a field. One-to-many,
  binary, json, properties and reference fields cannot be written.
- A related record must be one the user may read, on an exposed model; an id
  that does not exist and one the user may not read get the same answer
  (`unknown_record`), as do the `ids` of `update`.
- `create` names a required field without a value or a default
  (`required_missing`).
- `company` is the id of one of the user's companies, which then is the
  active company of the call (`bad_company`); without it the user's default
  company applies.

When Odoo itself refuses the change (a constraint, a validation), the answer
is `query_error` with `details: { "message": … }`: Odoo's own message for
the user, as its web client would show it, at most 500 characters. An access
error keeps the fixed `access_denied`.

A write runs in a sub-transaction with the data API's time limit and is
committed together with its row in the call log: when the row cannot be
written, the write is undone and the answer is `query_error`. Messages and
notifications a refused write had queued are dropped with it. A conflict
between two calls that change the same record at the same moment is left to
Odoo, which runs the later request again: the record holds the values of
one call, never a mix.

Restrictions that exist only in a form view, such as a field a view makes
read-only for confirmed documents, do not apply, as for Odoo's import and
its external API.

### SQL

The `sql` operation (scope `odoo.sql`) runs one read-only PostgreSQL query
written against Odoo's table and column names, on the rows and fields the
user may read, and nothing else:

| Parameters | Result |
|---|---|
| `query` (one `SELECT`, `VALUES` or `TABLE` statement, at most 20000 characters), `limit` (1–1000, default 100) | `{ "columns": [{ "name", "type" }], "rows": [[…]], "truncated", "elapsedMs", "tables": [{ "name", "model", "rows" }] }` |

The query never runs in Odoo's database. For every table it names, Odoo
selects, as the user, the records the user may read: the result of the
model's search with the user's access rights, record rules and companies
(all the companies of the user), archived records included, and for a model
stored partly in a parent model (`_inherits`, such as `res_users` in
`res_partner`) the parent's rules too. Only the columns the query names
are taken, and only stored fields the user may read (field groups) and the
data API does not hide; translated fields hold the text in the user's
language and company-dependent fields the value for the user's company. A
many-to-many table holds only the pairs whose both records the user may
read. These rows are copied into temporary tables of a separate PostgreSQL
server (the sandbox), where the query runs in a read-only transaction, as a
database role without any privilege, within the data API's time limit.
The sandbox connection is closed after each query.

What a user can therefore see:

- the same records and fields as in Odoo, under the table and column names
  of the database (`res_partner.name`, `sale_order.amount_total`; `describe`
  gives a model's table), with stored values Odoo computed with elevated
  rights as described above;
- joins, aggregates, window functions, common table expressions and
  PostgreSQL's functions over those rows only: a join from a partner to
  its orders counts only the orders the user may read.

What a user cannot see or do:

- records outside the user's rights, fields outside the user's groups,
  hidden models and fields (the data API's lists, plus `ir.attachment`,
  `mail.message` and any model whose reads an addon customises), columns
  pointing to a hidden model, binary columns, non-stored fields and
  one-to-many fields (join the other table instead);
- a table that is unknown, hidden or not readable: all three answer
  `relation "…" does not exist`, so the answer does not tell them apart;
- Odoo's database, files, other databases or other users' queries; any
  write; several statements; `SELECT INTO`, `FOR UPDATE`, `COPY`.

Limits: each table the query names may hold at most the configured rows
for the user (default 200000) and the copied data at most the configured
size (default 64 MB), otherwise `result_too_large`; each user runs at most
the configured number of queries at once (default 2), and all users
together at most one per sandbox role (default 4), otherwise
`too_many_requests`; the copy and the query each stop at the data API's
time limit (`timeout`). The answer is read from a cursor in the sandbox, at
most `limit` + 1 rows, in small batches; the sandbox measures each row
(the text form of its values) and sends no values for a row larger than
the data API's largest answer, so a row, or rows together, larger than
that answer `result_too_large` without the data ever reaching Odoo. Names in the query decide what is copied, so a
query that names few columns of large tables is faster than `SELECT *`.

Values come as JSON: numbers for integer, numeric and float columns,
ISO 8601 strings for dates and times, seconds for intervals, objects and
arrays for `json`, `jsonb` and arrays, `\x…` hexadecimal for `bytea`, text
for anything else; `type` is PostgreSQL's type name. `tables` lists the
tables copied, with their model (empty for a many-to-many table) and the
number of rows the user may read in them.

A `query_error` from `sql` carries PostgreSQL's own answer, because the
sandbox holds only rows the user may read:

```json
{ "error": { "code": "query_error", "message": "The query could not be run.",
  "details": { "sqlstate": "42703", "position": 8,
               "message": "column \"nope\" does not exist" } } }
```

`position` is the character position in the query (1-based) or `null`;
`message` holds at most 500 characters. A statement other than one
`SELECT` is refused with `sqlstate` `0A000`, a syntax error with `42601`.

### Hidden models and fields

Some data is never exposed, whatever the user's rights, administrators
included. The lists are settings (one pattern per line, `*` matches
anything); an empty list, or the default list itself, is not stored, so the
defaults of later versions apply.

- Models, by default:
  - credentials and payment data: `ir.config_parameter`,
    `res.users.apikeys*`, `o3p.mcp.*`, `auth_totp*`, `ir.mail_server`,
    `fetchmail.server`, `payment.provider`, `payment.token` (saved-card
    references), `payment.transaction` (provider references),
    `mail.push.device*` (push subscription keys);
  - models that point at other records by model name and id without
    checking that the user may read those records: `mail.followers`,
    `mail.notification`, `mail.tracking.value` (old and new field values),
    `rating.rating`, `snailmail.letter` (postal letters with the
    recipient's address), `bus.*`.
- Field names, on every model, by default: `*password*`, `*secret*`,
  `*token*`, `*api_key*`, `*shared_key*`, `provider_ref*`.
- Always: binary fields (files and images), and relational fields that
  point to a hidden model.

The defaults come from a review of a standard install (sale, purchase,
stock, account, crm, hr, project, website, mrp, payment). Attachments,
messages, activities and scheduled messages also point at records, but Odoo
checks access to the linked record for them, so they stay readable. The
addon's tests list every stored text field with a sensitive-looking name
(`ref`, `token`, `key`, `secret`, `password`, IBAN or account number,
`provider_…`) that is not hidden, with the reason it may be read, such as
business references and SEO keywords; a new field of that kind fails the
tests until it is hidden or reviewed.

### Call log

Every call that reaches the operation or fails authentication is recorded
in **Settings › Technical › MCP › API Calls**: time, user, MCP client,
operation, model (for `sql`: the tables copied), HTTP status, error code,
row count and duration, and for `create` and `update` the ids of the
records written and the names of the fields set. Parameters, SQL queries,
results and written values are never stored. Rows older than the configured number of days
(default 30) are deleted by the daily scheduled action.

## Settings

**Settings › MCP** (administrators only), stored as `ir.config_parameter`
values under `o3p_mcp.*`:

| Setting | Parameter | Default |
|---|---|---|
| Issuer: public URL of this Odoo as authorization server, https (http only on localhost), no path | `o3p_mcp.issuer` | `web.base.url` at install |
| MCP server URL: the public MCP endpoint, the only resource and token audience | `o3p_mcp.resource_url` | empty |
| Access token lifetime, 1 to 1440 minutes | `o3p_mcp.access_token_minutes` | 60 |
| Refresh token lifetime, 1 to 365 days, counted from each refresh | `o3p_mcp.refresh_token_days` | 30 |
| Client registrations per hour from one address or IPv6 /64, 1 to 100000 | `o3p_mcp.registration_limit` | 200 |
| Data API hidden models, one pattern per line | `o3p_mcp.api_denied_models` | see Data API |
| Data API hidden field names, one pattern per line | `o3p_mcp.api_denied_fields` | see Data API |
| Data API writable models, one pattern per line | `o3p_mcp.api_writable_models` | empty: nothing can be written |
| Data API largest answer (KB; stored in bytes) | `o3p_mcp.api_max_response_bytes` | 1000 KB |
| Data API time limit per query (seconds) | `o3p_mcp.api_timeout_seconds` | 15 |
| Data API call log retention (days) | `o3p_mcp.api_log_days` | 30 |
| SQL sandbox host; empty disables `sql` | `o3p_mcp.sql_host` | empty |
| SQL sandbox port | `o3p_mcp.sql_port` | 5432 |
| SQL sandbox database | `o3p_mcp.sql_database` | `o3p_sqlbox` |
| SQL sandbox role prefix (roles `<prefix>_1` … `<prefix>_<count>`) | `o3p_mcp.sql_user` | `o3p_sqlbox` |
| SQL sandbox role count: queries running at once over all users, 1 to 100 | `o3p_mcp.sql_roles` | 4 |
| SQL sandbox SSL mode (libpq `sslmode`, or `auto`: see SQL sandbox setup) | `o3p_mcp.sql_sslmode` | `auto` |
| SQL sandbox CA certificates (libpq `sslrootcert`: a file, or `system`) | `o3p_mcp.sql_sslrootcert` | empty (libpq's default) |
| SQL queries per user at once, 1 to 64 | `o3p_mcp.sql_user_concurrency` | 2 |
| SQL rows per table | `o3p_mcp.sql_max_rows` | 200000 |
| SQL data copied per query (MB; stored in bytes) | `o3p_mcp.sql_max_copy_bytes` | 64 MB |

The issuer is used for the metadata, the endpoint URLs and the `iss`
parameter. It does not follow `web.base.url`, which Odoo rewrites whenever
an administrator signs in (unless `web.base.url.freeze` is set), so clients
always see the same authorization server. The section also shows the
metadata URL and the introspection URL, and offers two buttons:

- **Create or rotate credentials** creates the resource-server client the MCP
  server introspects with, or gives it a new secret. The client ID and secret
  appear once, in a notification; the previous secret stops working at once.
- **Check MCP server** calls the `/health` URL next to the MCP endpoint
  (`https://mcp.example.com/mcp` → `https://mcp.example.com/health`) and
  reports the HTTP status.

The SQL block also shows where the sandbox password was found, and **Check
SQL sandbox** logs in as every sandbox role and reports any problem: a role
attribute (superuser, `CREATEROLE`, `CREATEDB`, replication, `BYPASSRLS`),
a connection limit other than 1, a role membership, a privilege beyond
`CONNECT` and `TEMPORARY` on the sandbox database, a schema the role may
create objects in, relations or extensions in the sandbox database, another
database the role may enter, file or large-object functions it may run, a
sandbox on the server that holds the Odoo database, or a role that can log
in to the Odoo database. It notes an unlimited `temp_file_limit` and whether
the connections are encrypted, and warns when the sandbox is not on this
host and the SSL mode is weaker than `verify-full`.

## Management

**Settings › Technical › MCP** (administrators only):

- **Clients**: registered MCP clients and the resource-server client, with
  their redirect URIs (one per line, same rules as registration),
  authentication method, registration address and last use. Archiving a
  client deactivates its tokens; **Revoke all tokens** revokes them.
- **Audiences**: the audiences, with their groups and the number of users
  in them.
- **API Calls**: the data API call log (read-only).
- **Tokens**: issued tokens with user, client, scope, state (active,
  rotated, expired, revoked) and times. **Revoke**, on a token or on a
  selection, revokes every token of the same authorization.

Tokens do not depend on the user's Odoo session: logging out or changing the
password leaves them valid. Removing the user's MCP groups or archiving the
user takes effect at the next introspection; revoking tokens takes effect
at once in Odoo (the MCP server may cache an answer for up to a minute).

## Connecting an MCP client

The MCP server is [okf-mcp](https://github.com/mfdaves/okf-mcp)
0.13.0-rc.1 or newer, which serves MCP over HTTP and checks every token
through Odoo. The Odoo data tools come from the
[okf-odoo](https://github.com/mfdaves/okf-odoo) tool pack, which calls the
data API below. Both are on npm; the okf-mcp versions with HTTP are
published under the `next` tag.

1. Install the addon and give users the **MCP / User** or **MCP / Editor**
   group, **MCP SQL / Allowed** to those who may run SQL and **MCP records /
   Editor** to those who may create and change records; list the writable
   models in the settings.
2. In **Settings › MCP**, check the issuer, enter the MCP server URL and
   create the introspection credentials.
3. In the directory of the okf project, install okf-mcp and the tool pack
   (okf-mcp loads tool packs only from the project's `node_modules`):

   ```sh
   npm install @mfdaves/okf-mcp@next @mfdaves/okf-odoo
   ```

4. Write `okf.project.yaml` there, with at least one knowledge bundle and the
   tool pack. The tool pack's `url` is the Odoo server: `https`, or `http`
   for a loopback host such as Odoo on the same machine.

   ```yaml
   project: Example
   bundles:
     - id: company
       root: knowledge
   toolPacks:
     - package: "@mfdaves/okf-odoo"
       config:
         url: https://odoo.example.com
         clientIdEnv: O3P_MCP_CLIENT_ID
         clientSecretEnv: O3P_MCP_CLIENT_SECRET
   ```

5. Start okf-mcp with Odoo as its authorization server: the issuer shown in
   the settings, the introspection URL, and a public URL equal to the MCP
   server URL. okf-mcp (for introspection) and the tool pack (for the data
   API) both authenticate with the introspection client ID and secret, each
   from its own environment variables:

   ```sh
   export OKF_INTROSPECTION_CLIENT_ID=c_… OKF_INTROSPECTION_CLIENT_SECRET=…
   export O3P_MCP_CLIENT_ID="$OKF_INTROSPECTION_CLIENT_ID"
   export O3P_MCP_CLIENT_SECRET="$OKF_INTROSPECTION_CLIENT_SECRET"
   npx okf --project okf.project.yaml mcp --http \
     --public-url https://mcp.example.com/mcp \
     --authorization-server https://odoo.example.com \
     --auth introspect:https://odoo.example.com/o3p_mcp/oauth/introspect
   ```

   okf-mcp listens on `127.0.0.1:8766` (`--host` and `--port` change it); a
   reverse proxy publishes it at the MCP server URL. Its protected-resource
   metadata then lists the scopes `odoo.read` and `odoo.sql` next to
   `okf.read` and `okf.write`.

6. Add the MCP server URL to the MCP client. The client reads the server's
   protected-resource metadata, discovers Odoo, registers itself, opens the
   Odoo sign-in page and asks the user for consent.

A signed-in user then has, next to the knowledge tools, `odoo_context`
(okf-odoo newer than 0.1.2), `odoo_find_models`,
`odoo_describe_model`, `odoo_search`, `odoo_read` and `odoo_group` (scope
`odoo.read`) and, with MCP SQL / Allowed, `odoo_sql` (scope `odoo.sql`),
each running as that user through the data API. `odoo_sql` needs the SQL
sandbox (see SQL sandbox
setup); without one it answers `unavailable`. With MCP records / Editor
the user also has `odoo_create` and `odoo_update` (scope `odoo.write`,
okf-odoo newer than 0.1.2), announced to the client as tools that are not
read-only. The data tools are offered only
over HTTP to a signed-in user. okf-odoo also has a producer that writes
Odoo's modules, models, fields, groups and menus as an `odoo` knowledge
bundle, to which the tools' descriptions point agents; it reads Odoo with an
API key of its own (see the okf-odoo README).

### okf-mcp options

- `--public-url` is mandatory with `--auth introspect:…` and must equal the
  MCP server URL configured in Odoo; one trailing slash is ignored on either
  side. Odoo reports that URL as `aud`, and okf-mcp accepts only tokens whose
  `aud` matches.
- Write tools additionally need `--write --actor <actor>`, and `--git-commit`
  for changes to be committed; the token then needs `okf.write`, which only
  MCP editors get.
- Binding a non-loopback `--host` requires `--auth`.
- The issuer given to `--authorization-server` must be https (or
  `localhost`), and the introspection URL https (http only on a loopback
  host). The issuer setting in Odoo follows the same rule.
- okf-mcp sends the introspection credentials with HTTP Basic,
  form-urlencoding them first as RFC 6749 §2.3.1 requires; Odoo decodes them
  the same way. Client IDs (`c_` and hexadecimal digits) and secrets
  (URL-safe base64 from `secrets.token_urlsafe`) contain only URL-safe
  characters, so the encoding leaves them unchanged.
- okf-mcp answers `GET /health` both at its origin and next to the MCP
  endpoint (`/okf/mcp` → `/okf/health`), so **Check MCP server** works for
  either layout.

## Deployment notes

- The issuer starts as `web.base.url` when the addon is installed; set it
  to the public URL in the settings if that differs.
- Behind a reverse proxy, run Odoo with `proxy_mode` and have the proxy set
  `X-Forwarded-For`, `X-Forwarded-Host` and `X-Forwarded-Proto`. Without
  them every request appears to come from the proxy, and the per-address
  registration limit applies to all clients together.
- The metadata, registration, token, introspection and revocation endpoints
  run without a session, so Odoo must pick the database from the request
  alone: serve a single database or set `dbfilter` so the host name selects
  it.

## Odoo 18 notes

- The groups belong to the module categories **MCP**, **MCP SQL** and
  **MCP records** (Odoo 20 groups them under privileges of the same names).
  Access to the addon's own records is granted by `ir.model.access` to
  Settings administrators only.
- Odoo 18 has no light and regular internal users: a user is internal or
  portal. The MCP groups imply no other group, so giving them to a user
  changes neither the user type nor any right on Odoo records.
- The data API applies Odoo 18's access rights (`ir.model.access`), record
  rules (`ir.rule`) and field groups, with the same results as on Odoo 20.
  Group values are formatted as on Odoo 20 (`[id, name]` for relations,
  `[start, label]` for dates).
- The daily clean-up deletes up to 1,000 rows per call; while rows remain,
  Odoo calls it again up to 10 times in the same run, and anything left
  waits for the next day.
- The index used by the registration limit is created when the addon is
  installed and kept as it is on later upgrades.
- SQL works as on Odoo 20, with Odoo 18's own machinery: the rows of a
  table are those `_search` returns for the user (`ir.model.access`,
  `ir.rule` including the rules of `_inherits` parents), and the columns
  are rendered by `_field_to_sql` (translated fields in the user's
  language, company-dependent fields for the user's company) and limited
  to fields the user may read (`Field.is_accessible`). `res.users`
  customises its reads only to let users read their own fields with
  elevated rights and to add non-stored group fields, so it is reviewed as
  safe for SQL (`READ_OVERRIDES_REVIEWED` in `models/denylist.py`, the
  review shared with the other operations);
  other models whose reads an addon customises stay hidden from SQL
  unless that review lists them.
  The SQL answers and error envelopes are the 20.0 branch's, byte for byte
  (see Tests).

## SQL sandbox setup

The `sql` operation needs its own PostgreSQL server (tested with 17),
separate from the one holding the Odoo database, reachable from Odoo, with
an empty database and one login role per query that may run at once. With
Docker, an extra `postgres` service is enough; it holds no data and can
run on temporary storage.

1. Choose a password and run `scripts/sandbox-setup.sql` on the sandbox
   server as its superuser:

   ```sh
   psql -h sandbox -U postgres -d postgres \
        -v password="$O3P_SQL_PASSWORD" -v roles=4 \
        -v prefix=o3p_sqlbox -v database=o3p_sqlbox \
        -f scripts/sandbox-setup.sql
   ```

   It creates the database `o3p_sqlbox` and the roles `o3p_sqlbox_1` …
   `o3p_sqlbox_4`, all with that password: login, no other attribute, no
   membership, `CONNECTION LIMIT 1`, `CONNECT` and `TEMPORARY` on the
   sandbox database only, `temp_file_limit` 256 MB, `work_mem` 32 MB. It
   closes every database of that server to `PUBLIC` and takes the `public`
   schema of the sandbox database away from `PUBLIC`. Running it again
   resets the passwords and limits. With the official `postgres` image,
   mounting `scripts/sandbox-init.sh` into `/docker-entrypoint-initdb.d/`
   and `scripts/sandbox-setup.sql` at `/o3p/sandbox-setup.sql` runs it at
   first start from the `O3P_SQL_PASSWORD`, `O3P_SQL_ROLES`,
   `O3P_SQL_USER` and `O3P_SQL_DATABASE` variables
   (`scripts/compose.test.yaml` shows the service).
2. Give Odoo the password through the `O3P_SQL_PASSWORD` environment
   variable, or as `o3p_sql_password` in the `[options]` section of the
   Odoo configuration file (Odoo then logs that it does not know the
   option, and keeps it). The password is never stored in the database.
3. In **Settings › MCP › SQL**, enter the sandbox host (and port, database,
   role prefix and count if they differ), save, and press **Check SQL
   sandbox**.

Rows the user may read travel from Odoo to the sandbox and the answer
back, so the connection must not be readable or changeable on the way. The
SSL mode setting `auto` (the default) uses `prefer` for a sandbox on the
same host as Odoo, that is a unix socket directory (`/var/run/postgresql`),
`localhost`, a loopback address, or a single-label host name such as a
Docker or Compose service name (`sandbox`), and `verify-full` for any other
host: the sandbox server then needs a TLS certificate for its host name,
and Odoo the CA that signed it (the CA certificates setting, or `system`
for the system's CA store). A single-label name that is not on the same
host needs the SSL mode set explicitly. Any explicit mode is used as set;
**Check SQL sandbox** warns about a remote sandbox without `verify-full`.

Each Odoo database needs its own sandbox roles (another prefix, or another
server): the advisory locks that hand out roles are per Odoo database. The
sandbox server sees the Odoo server only as a client, and the roles cannot
log in to the Odoo database because they do not exist there.

## Installation

Copy the `o3p_mcp` folder into an addons path and install **o3p - mcp**, or use
the O3P installer with `o3p-mcp.o3p.json`, which picks the branch matching the
Odoo version (`18.0`). The addon depends on `base_setup` and `web` and uses
the Python packages `requests`, `idna` (it comes with `requests`) and
`pglast` (the PostgreSQL parser that checks SQL queries; `pip install
pglast`).

## Tests

`scripts/run-tests.sh` starts PostgreSQL 17, a second PostgreSQL 17 server
as the SQL sandbox (set up by `scripts/sandbox-setup.sql`) and the Odoo
image of the manifest's series (`odoo:18.0` here) with `pglast` added
(`scripts/Dockerfile.test`, built on first use) with Docker Compose,
installs the addon in a throw-away database, runs its tests and removes the
containers again. `ODOO_SERIES=20.0` selects another image; the Compose
project is `o3p-mcp-test18` for Odoo 18, or `O3P_MCP_TEST_PROJECT`. An
optional argument replaces the default test tags (`/o3p_mcp`). The SQL
tests compare, for every table and four users with different rights and
companies, the rows SQL sees with what the ORM shows, and run the escape
attempts of `o3p_mcp/tests/sql_attacks.py` (role switching,
schema-qualified names, functions that run SQL, files, catalogs, several
statements, timeouts and more): every attempt must fail and no answer may
contain hidden data.

`tests/fixtures/contract_20.json` holds the data API error bodies, their fixed
messages, the introspection answers and a set of SQL calls with their
answers, generated from the `20.0` branch (`scripts/contract-20.sh
<20.0 checkout>` regenerates the data API and SQL parts); the tests compare
the answers on Odoo 18 with it byte for byte, SQL answers except
`elapsedMs`.

`scripts/check-upgrade.sh <git ref> <before.py> <after.py>` checks an
upgrade on a throw-away database with the same stack: it installs the addon
as of the git ref, runs the first script in `odoo shell`, upgrades to the
working tree and runs the second, which prints `UPGRADE OK` when what it
expects holds. `scripts/upgrade/` holds the pairs; `sql-group-before.py` and
`sql-group-after.py` check that the users who held MCP / User before 1.41
keep `odoo.sql` and that nobody else receives it.
