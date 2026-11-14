import base64
import binascii
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

from odoo import http
from odoo.http import request

from ..models.oauth import (
    AUTHORIZE_PATH,
    METADATA_PATH,
    REGISTER_PATH,
    TOKEN_PATH,
    AuthorizationError,
    OAuthError,
    parse_scope,
)


MAX_REGISTRATION_BYTES = 64 * 1024
NO_STORE = [("Cache-Control", "no-store"), ("Pragma", "no-cache")]
# The consent page must never be framed (clickjacking) nor cached.
PAGE_HEADERS = [
    *NO_STORE,
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "frame-ancestors 'none'"),
]
# Authorization parameters carried from the consent page to its form post.
AUTHORIZATION_FIELDS = (
    "response_type",
    "client_id",
    "redirect_uri",
    "scope",
    "state",
    "code_challenge",
    "code_challenge_method",
    "resource",
)


def json_response(data, status=200, headers=()):
    return request.make_json_response(
        data, headers=[*NO_STORE, *headers], status=status
    )


def error_response(error):
    headers = []
    if error.status == 429:
        headers.append(("Retry-After", "3600"))
    if error.status == 401 and request.httprequest.headers.get("Authorization"):
        headers.append(("WWW-Authenticate", 'Basic realm="o3p_mcp"'))
    return json_response(error.to_dict(), status=error.status, headers=headers)


def client_credentials():
    """Return ``(client_id, secret, method)`` presented with the request.

    Credentials come from HTTP Basic (``client_secret_basic``), from the
    form body (``client_secret_post``) or, for public clients, as a bare
    ``client_id`` (``none``). Mixing Basic with body credentials is refused.
    """
    form = request.httprequest.form
    header = request.httprequest.headers.get("Authorization")
    if header:
        scheme, _space, value = header.partition(" ")
        try:
            if scheme.lower() != "basic":
                raise ValueError("not basic")
            decoded = base64.b64decode(value.strip(), validate=True).decode()
            client_id, colon, secret = decoded.partition(":")
            if not colon:
                raise ValueError("no secret")
        except (ValueError, binascii.Error) as exc:
            raise OAuthError(
                "invalid_client", "Client authentication failed.", status=401
            ) from exc
        client_id, secret = unquote_plus(client_id), unquote_plus(secret)
        if "client_secret" in form or form.get("client_id", client_id) != client_id:
            raise OAuthError(
                "invalid_request", "Use one client authentication method only."
            )
        return client_id, secret, "client_secret_basic"
    if "client_secret" in form:
        return form.get("client_id"), form["client_secret"], "client_secret_post"
    return form.get("client_id"), None, "none"


def with_query(uri, params):
    """Return ``uri`` with ``params`` appended to its query string."""
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query += [(key, value) for key, value in params.items() if value is not None]
    return urlunsplit(parts._replace(query=urlencode(query)))


def client_redirect(uri, params):
    issuer = request.env["o3p.mcp.oauth"]._issuer()
    return request.redirect(with_query(uri, {**params, "iss": issuer}), local=False)


def authorization_error_response(error):
    if not error.show:
        return client_redirect(
            error.redirect_uri,
            {
                "error": error.error,
                "error_description": error.description,
                "state": error.state,
            },
        )
    return_url = None
    if error.redirect_uri:
        return_url = with_query(
            error.redirect_uri,
            {
                "error": error.error,
                "state": error.state,
                "iss": request.env["o3p.mcp.oauth"]._issuer(),
            },
        )
    return request.render(
        "o3p_mcp.oauth_error",
        {"message": error.description, "return_url": return_url},
        status=403 if error.error == "access_denied" else 400,
        headers=PAGE_HEADERS,
    )


class O3pMcpOauthController(http.Controller):
    @http.route(
        METADATA_PATH,
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
        cors="*",
        save_session=False,
    )
    def metadata(self):
        return request.make_json_response(request.env["o3p.mcp.oauth"]._metadata())

    @http.route(
        REGISTER_PATH,
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        readonly=False,
        save_session=False,
        max_content_length=MAX_REGISTRATION_BYTES,
    )
    def register(self):
        try:
            try:
                metadata = request.get_json_data()
            except ValueError as exc:
                raise OAuthError(
                    "invalid_client_metadata", "The body must be a JSON object."
                ) from exc
            response = request.env["o3p.mcp.client"]._register_client(
                metadata, request.httprequest.remote_addr
            )
        except OAuthError as error:
            return error_response(error)
        return json_response(response, status=201)

    @http.route(
        TOKEN_PATH,
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        cors="*",
        readonly=False,
        save_session=False,
    )
    def token(self, **_params):
        # Parameters are read from the form body only, never the query string.
        form = request.httprequest.form
        try:
            client_id, secret, method = client_credentials()
            client = request.env["o3p.mcp.client"]._authenticate(
                client_id, secret, method
            )
            grant_type = form.get("grant_type")
            if grant_type == "authorization_code":
                response = request.env["o3p.mcp.code"]._exchange(
                    form.get("code"),
                    client,
                    form.get("redirect_uri"),
                    form.get("code_verifier"),
                    form.get("resource"),
                )
            elif grant_type == "refresh_token":
                scope = form.get("scope")
                _token, response = request.env["o3p.mcp.token"]._rotate(
                    form.get("refresh_token"),
                    client,
                    scopes=parse_scope(scope) if scope else None,
                    resource=form.get("resource"),
                )
            elif not grant_type:
                raise OAuthError("invalid_request", "grant_type is required.")
            else:
                raise OAuthError(
                    "unsupported_grant_type",
                    "Supported grant types: authorization_code, refresh_token.",
                )
        except OAuthError as error:
            return error_response(error)
        return json_response(response)

    @http.route(AUTHORIZE_PATH, type="http", auth="user", methods=["GET"])
    def authorize(self, **params):
        oauth = request.env["o3p.mcp.oauth"]
        try:
            values = oauth._check_authorization(params, request.env.user)
        except AuthorizationError as error:
            return authorization_error_response(error)
        client = values["client"]
        fields = [
            (name, params[name])
            for name in AUTHORIZATION_FIELDS
            if isinstance(params.get(name), str) and params[name]
        ]
        return request.render(
            "o3p_mcp.oauth_consent",
            {
                "action": AUTHORIZE_PATH,
                "client_name": client.name,
                "client_id": client.client_id,
                "self_registered": bool(client.registration_ip),
                "user_name": request.env.user.name,
                "resource": values["resource"],
                "redirect_target": self._redirect_target(values["redirect_uri"]),
                "scopes": oauth._scope_descriptions(values["scopes"]),
                "fields": fields,
            },
            headers=PAGE_HEADERS,
        )

    @http.route(
        AUTHORIZE_PATH,
        type="http",
        auth="user",
        methods=["POST"],
        csrf=True,
        readonly=False,
    )
    def authorize_decision(self, decision=None, **params):
        try:
            values = request.env["o3p.mcp.oauth"]._check_authorization(
                params, request.env.user
            )
        except AuthorizationError as error:
            return authorization_error_response(error)
        if decision != "approve":
            return client_redirect(
                values["redirect_uri"],
                {"error": "access_denied", "state": values["state"]},
            )
        code = request.env["o3p.mcp.code"]._create_code(
            values["client"],
            request.env.user,
            values["redirect_uri"],
            values["code_challenge"],
            values["scopes"],
            values["resource"],
        )
        return client_redirect(
            values["redirect_uri"], {"code": code, "state": values["state"]}
        )

    @staticmethod
    def _redirect_target(uri):
        parts = urlsplit(uri)
        if parts.scheme in ("http", "https"):
            return parts.netloc
        return f"{parts.scheme}:"
