import json
import logging
import time

import psycopg2
from werkzeug.exceptions import HTTPException, MethodNotAllowed

from odoo import SUPERUSER_ID, api, fields, http
from odoo.http import request
from odoo.sql_db import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY
from odoo.tools import json_default

from ..models.api import ApiError
from ..models.mcp_token import LAST_USED_RESOLUTION
from ..models.oauth import OAuthError
from .oauth import NO_STORE, client_credentials


_logger = logging.getLogger(__name__)

API_PATH = "/o3p_mcp/api/v1/<string:op>"
MAX_REQUEST_BYTES = 256 * 1024


def api_response(status, payload, headers=()):
    body = json.dumps(payload, ensure_ascii=False, default=json_default)
    return request.make_response(
        body,
        headers=[("Content-Type", "application/json; charset=utf-8"), *NO_STORE, *headers],
        status=status,
    )


def write_audit(values, token_id=None, strict=False):
    """Record the call; a failure to record never changes the answer.

    ``strict`` is for a call that wrote records: the failure is raised, so
    that the caller undoes the write instead of committing it unrecorded.
    """
    env = api.Environment(request.env.cr, SUPERUSER_ID, {})
    try:
        with env.cr.savepoint():
            env["o3p.mcp.api.log"].create(values)
            if token_id:
                token = env["o3p.mcp.token"].browse(token_id)
                now = fields.Datetime.now()
                if not token.last_used_at or now - token.last_used_at >= LAST_USED_RESOLUTION:
                    token.last_used_at = now
    except Exception as exc:  # noqa: BLE001 - the answer must not depend on the audit
        if strict:
            raise
        _logger.warning(
            "MCP data API: the call could not be recorded (%s)", type(exc).__name__
        )


class O3pMcpApiController(http.Controller):
    @http.route(
        API_PATH,
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        # A reading operation runs in a read-only subtransaction (see
        # o3p.mcp.api._run); the request writes the audit row afterwards.
        # A writing operation commits only together with its audit row.
        readonly=False,
        save_session=False,
        max_content_length=MAX_REQUEST_BYTES,
    )
    def call(self, op, **_params):
        started = time.monotonic()
        audit = {"op": op[:64], "row_count": 0}
        token_id = None
        # Set once a writing operation has changed records: from then on any
        # failure rolls the whole request back.
        written = False
        recorded = False
        try:
            # Odoo adds OPTIONS to every route; this one serves POST only.
            if request.httprequest.method != "POST":
                raise MethodNotAllowed(["POST"])
            client_id, secret, method = self._client_credentials()
            try:
                if method != "client_secret_basic":
                    raise OAuthError("invalid_client", status=401)
                request.env["o3p.mcp.client"]._authenticate(
                    client_id, secret, method, kind="resource_server"
                )
            except OAuthError as exc:
                raise ApiError("invalid_client") from exc
            try:
                body = request.get_json_data()
            except ValueError as exc:
                raise ApiError("invalid_params") from exc
            if not isinstance(body, dict):
                raise ApiError("invalid_params")
            Api = request.env["o3p.mcp.api"]
            scope = Api._op_scope(op)
            token, user = Api._check_token(body.get("token"), scope)
            token_id = token.id
            audit.update(user_id=user.id, client_id=token.client_id.id)
            request.update_env(
                user=user.id,
                context=dict(
                    request.env.context, lang=user.lang or "en_US", tz=user.tz or "UTC"
                ),
            )
            writes = op in Api._WRITE_OPS
            result, model, rows, *extra = request.env["o3p.mcp.api"]._run(
                op, body.get("params", {})
            )
            written = writes
            audit.update(model=model, row_count=rows)
            if extra and extra[0]:
                if writes:
                    audit.update(extra[0])
                else:
                    audit["tables"] = ",".join(extra[0])
            payload = {"result": result}
            size = len(json.dumps(payload, ensure_ascii=False, default=json_default).encode())
            if size > request.env["o3p.mcp.api"]._max_response_bytes():
                raise ApiError("result_too_large")
            audit["status"] = 200
            if writes:
                # No write commits without its row in the call log.
                audit["duration_ms"] = int((time.monotonic() - started) * 1000)
                write_audit(audit, token_id, strict=True)
                recorded = True
            return api_response(200, payload)
        except ApiError as error:
            if written:
                request.env.cr.rollback()
            for key in ("record_ids", "field_names"):
                audit.pop(key, None)
            audit.update(status=error.status, error=error.code, row_count=0)
            if error.tables:
                audit["tables"] = ",".join(error.tables)
            headers = []
            if error.status == 401:
                headers.append(("WWW-Authenticate", 'Basic realm="o3p_mcp"'))
            return api_response(error.status, error.to_dict(), headers)
        except (HTTPException, *PG_CONCURRENCY_EXCEPTIONS_TO_RETRY):
            # Werkzeug answers the former; Odoo retries the request on the latter.
            raise
        except Exception as exc:  # noqa: BLE001 - the contract's fixed answer, always recorded
            _logger.warning("MCP data API %s failed unexpectedly: %s", op, type(exc).__name__)
            if written or isinstance(exc, psycopg2.Error):
                # The transaction is aborted, or holds a write that was not
                # recorded: start over to record the call.
                request.env.cr.rollback()
            for key in ("record_ids", "field_names"):
                audit.pop(key, None)
            error = ApiError("query_error")
            audit.update(status=error.status, error=error.code, row_count=0)
            return api_response(error.status, error.to_dict())
        finally:
            if "status" in audit and not recorded:
                audit["duration_ms"] = int((time.monotonic() - started) * 1000)
                write_audit(audit, token_id)

    @staticmethod
    def _client_credentials():
        try:
            return client_credentials()
        except OAuthError as exc:
            raise ApiError("invalid_client") from exc
