from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from odoo import http
from odoo.http import request

from ..models.oauth import (
    AUTHORIZE_PATH,
    METADATA_PATH,
    REGISTER_PATH,
    AuthorizationError,
    OAuthError,
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
    return json_response(error.to_dict(), status=error.status, headers=headers)


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
