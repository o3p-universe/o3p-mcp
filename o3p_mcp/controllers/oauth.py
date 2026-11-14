from odoo import http
from odoo.http import request

from ..models.oauth import METADATA_PATH, REGISTER_PATH, OAuthError


MAX_REGISTRATION_BYTES = 64 * 1024
NO_STORE = [("Cache-Control", "no-store"), ("Pragma", "no-cache")]


def json_response(data, status=200, headers=()):
    return request.make_json_response(
        data, headers=[*NO_STORE, *headers], status=status
    )


def error_response(error):
    headers = []
    if error.status == 429:
        headers.append(("Retry-After", "3600"))
    return json_response(error.to_dict(), status=error.status, headers=headers)


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
