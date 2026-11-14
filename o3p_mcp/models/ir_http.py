from werkzeug.exceptions import HTTPException

from odoo import models
from odoo.http import request

from .oauth import METADATA_PATH, REGISTER_PATH, REVOKE_PATH, TOKEN_PATH


# Endpoints browser-based MCP clients call cross-origin. Introspection and the
# data API are server to server and answer no cross-origin requests.
CORS_PATHS = frozenset({METADATA_PATH, REGISTER_PATH, TOKEN_PATH, REVOKE_PATH})
# MCP clients send MCP-Protocol-Version on every request, discovery included.
CORS_ALLOW_HEADERS = "Authorization, Content-Type, MCP-Protocol-Version"


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _pre_dispatch(cls, rule, args):
        try:
            super()._pre_dispatch(rule, args)
        except HTTPException:
            # Odoo answers a CORS preflight by aborting with 204 here; the
            # headers it set are still pending on the future response.
            if (
                request.httprequest.method == "OPTIONS"
                and request.httprequest.path in CORS_PATHS
                and rule.endpoint.routing.get("cors")
            ):
                request.future_response.headers.set(
                    "Access-Control-Allow-Headers", CORS_ALLOW_HEADERS
                )
            raise
