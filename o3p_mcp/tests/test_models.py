import hashlib
from datetime import timedelta, timezone
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

from ..models.oauth import OAuthError
from .common import RESOURCE, O3pMcpCommon


INACTIVE = {"active": False}


@tagged("post_install", "-at_install")
class TestTokens(O3pMcpCommon, TransactionCase):
    def issue(self, user=None, scopes=("okf.read", "okf.write"), client=None):
        return self.Token._issue(
            client or self.client, user or self.editor, set(scopes), RESOURCE
        )

    def assertOAuthError(self, error, func, *args, **kwargs):
        # Not assertRaises: it rolls back to a savepoint, and the writes made
        # before the error (family revocation) must stay, as they do when the
        # controller answers the error.
        try:
            func(*args, **kwargs)
        except OAuthError as exc:
            self.assertEqual(exc.error, error)
        else:
            self.fail(f"{error} was not raised")

    def test_issue_stores_only_hashes(self):
        token, response = self.issue()
        self.assertEqual(
            set(response),
            {"access_token", "token_type", "expires_in", "refresh_token", "scope"},
        )
        self.assertEqual(response["token_type"], "Bearer")
        self.assertEqual(response["expires_in"], 3600)
        self.assertEqual(response["scope"], "okf.read okf.write")
        self.assertGreaterEqual(len(response["access_token"]), 43)
        self.assertNotEqual(response["access_token"], response["refresh_token"])
        self.assertEqual(
            token.access_hash,
            hashlib.sha256(response["access_token"].encode()).hexdigest(),
        )
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT row_to_json(t)::text FROM o3p_mcp_token t WHERE id = %s",
            [token.id],
        )
        row = self.env.cr.fetchone()[0]
        self.assertNotIn(response["access_token"], row)
        self.assertNotIn(response["refresh_token"], row)
        self.assertTrue(token.client_id.last_used_at)

    def test_introspect_active_token(self):
        token, response = self.issue()
        answer = self.Token._introspect(response["access_token"])
        self.assertEqual(
            answer,
            {
                "active": True,
                "sub": str(self.editor.id),
                "username": "o3p_mcp_editor",
                "name": "MCP Editor",
                "email": self.editor.email,
                "scope": "okf.read okf.write",
                "audiences": [],
                "aud": RESOURCE,
                "exp": int(token.expires_at.replace(tzinfo=timezone.utc).timestamp()),
                "client_id": self.client.client_id,
            },
        )
        self.assertTrue(token.last_used_at)

    def test_introspect_omits_missing_email(self):
        self.editor.email = False
        _token, response = self.issue()
        answer = self.Token._introspect(response["access_token"])
        self.assertTrue(answer["active"])
        self.assertNotIn("email", answer)

    def test_unknown_and_refresh_tokens_are_inactive(self):
        _token, response = self.issue()
        self.assertEqual(self.Token._introspect("not-a-token"), INACTIVE)
        self.assertEqual(self.Token._introspect(""), INACTIVE)
        self.assertEqual(self.Token._introspect(None), INACTIVE)
        self.assertEqual(self.Token._introspect("x" * 10000), INACTIVE)
        self.assertEqual(self.Token._introspect(response["refresh_token"]), INACTIVE)

    def test_expired_token_is_inactive(self):
        token, response = self.issue()
        token.expires_at = fields.Datetime.now() - timedelta(seconds=1)
        self.assertEqual(self.Token._introspect(response["access_token"]), INACTIVE)
        self.assertEqual(token.state, "active")
        token.refresh_expires_at = token.expires_at
        self.assertEqual(token.state, "expired")

    def test_rotate(self):
        token, first = self.issue()
        new, second = self.Token._rotate(first["refresh_token"], self.client)
        self.assertNotEqual(new, token)
        self.assertEqual(new.family, token.family)
        self.assertNotEqual(second["access_token"], first["access_token"])
        self.assertNotEqual(second["refresh_token"], first["refresh_token"])
        self.assertEqual(second["scope"], "okf.read okf.write")
        self.assertTrue(token.rotated_at)
        self.assertEqual(token.state, "rotated")
        self.assertTrue(self.Token._introspect(second["access_token"])["active"])
        # The previous access token stays valid until it expires.
        self.assertTrue(self.Token._introspect(first["access_token"])["active"])

    def test_rotate_can_narrow_but_not_widen_scope(self):
        _token, first = self.issue()
        _new, second = self.Token._rotate(
            first["refresh_token"], self.client, scopes={"okf.read"}
        )
        self.assertEqual(second["scope"], "okf.read")
        self.assertOAuthError(
            "invalid_scope",
            self.Token._rotate,
            second["refresh_token"],
            self.client,
            scopes={"okf.read", "okf.write"},
        )

    def test_rotate_ignores_offline_access_and_unknown_scopes(self):
        _token, first = self.issue()
        _new, second = self.Token._rotate(
            first["refresh_token"], self.client, scopes={"offline_access"}
        )
        self.assertEqual(second["scope"], "okf.read okf.write")
        _new, third = self.Token._rotate(
            second["refresh_token"], self.client, scopes={"okf.read", "nope"}
        )
        self.assertEqual(third["scope"], "okf.read")

    def test_rotate_requires_read_scope_before_claiming(self):
        token, first = self.issue()
        self.assertOAuthError(
            "invalid_scope", self.Token._rotate, first["refresh_token"], self.client,
            scopes={"okf.write"},
        )
        self.assertFalse(token.rotated_at)
        self.assertFalse(token.revoked_at)
        self.Token._rotate(first["refresh_token"], self.client)

    def test_replayed_refresh_token_revokes_the_family(self):
        other, other_response = self.issue(self.reader, scopes=("okf.read",))
        token, first = self.issue()
        new, second = self.Token._rotate(first["refresh_token"], self.client)
        self.assertEqual(token.successor_id, new)
        # The new token is in use, so the old refresh token is a replay.
        self.assertTrue(self.Token._introspect(second["access_token"])["active"])
        self.assertOAuthError(
            "invalid_grant", self.Token._rotate, first["refresh_token"], self.client
        )
        self.assertTrue(token.revoked_at)
        self.assertTrue(new.revoked_at)
        self.assertEqual(self.Token._introspect(second["access_token"]), INACTIVE)
        self.assertOAuthError(
            "invalid_grant", self.Token._rotate, second["refresh_token"], self.client
        )
        # Other families are untouched.
        self.assertFalse(other.revoked_at)
        self.assertTrue(self.Token._introspect(other_response["access_token"])["active"])

    def test_retry_within_grace_keeps_the_family(self):
        token, first = self.issue()
        new, second = self.Token._rotate(first["refresh_token"], self.client)
        self.assertOAuthError(
            "invalid_grant", self.Token._rotate, first["refresh_token"], self.client
        )
        self.assertFalse(token.revoked_at)
        self.assertFalse(new.revoked_at)
        self.assertTrue(self.Token._introspect(second["access_token"])["active"])
        self.Token._rotate(second["refresh_token"], self.client)

    def test_retry_after_grace_revokes_the_family(self):
        token, first = self.issue()
        new, _second = self.Token._rotate(first["refresh_token"], self.client)
        token.rotated_at = fields.Datetime.now() - timedelta(seconds=11)
        self.assertOAuthError(
            "invalid_grant", self.Token._rotate, first["refresh_token"], self.client
        )
        self.assertTrue(new.revoked_at)

    def test_retry_after_the_next_token_was_refreshed_revokes_the_family(self):
        token, first = self.issue()
        new, second = self.Token._rotate(first["refresh_token"], self.client)
        newest, _third = self.Token._rotate(second["refresh_token"], self.client)
        self.assertOAuthError(
            "invalid_grant", self.Token._rotate, first["refresh_token"], self.client
        )
        self.assertTrue(newest.revoked_at)

    def test_retry_by_another_client_is_refused_without_effect(self):
        other_client = self.env["o3p.mcp.client"].create(
            {"name": "Other", "redirect_uris": ["https://other.example.com/cb"]}
        )
        token, first = self.issue()
        new, _second = self.Token._rotate(first["refresh_token"], self.client)
        token.rotated_at = fields.Datetime.now() - timedelta(seconds=11)
        self.assertOAuthError(
            "invalid_grant", self.Token._rotate, first["refresh_token"], other_client
        )
        self.assertFalse(new.revoked_at)

    def test_revoke_family(self):
        token, first = self.issue()
        new, second = self.Token._rotate(first["refresh_token"], self.client)
        other, third = self.issue()
        token._revoke_family()
        self.assertEqual(self.Token._introspect(first["access_token"]), INACTIVE)
        self.assertEqual(self.Token._introspect(second["access_token"]), INACTIVE)
        self.assertTrue(self.Token._introspect(third["access_token"])["active"])
        self.assertEqual(new.state, "revoked")

    def test_refresh_token_is_bound_to_its_client(self):
        other_client = self.env["o3p.mcp.client"].create(
            {"name": "Other", "redirect_uris": ["https://other.example.com/cb"]}
        )
        _token, response = self.issue()
        self.assertOAuthError(
            "invalid_grant", self.Token._rotate, response["refresh_token"], other_client
        )
        # The failed attempt did not consume the refresh token.
        self.Token._rotate(response["refresh_token"], self.client)

    def test_expired_refresh_token(self):
        token, response = self.issue()
        token.refresh_expires_at = fields.Datetime.now() - timedelta(seconds=1)
        self.assertOAuthError(
            "invalid_grant", self.Token._rotate, response["refresh_token"], self.client
        )

    def test_archived_user_is_inactive(self):
        _token, response = self.issue()
        self.editor.active = False
        self.assertEqual(self.Token._introspect(response["access_token"]), INACTIVE)
        self.assertOAuthError(
            "invalid_grant", self.Token._rotate, response["refresh_token"], self.client
        )

    def test_user_turned_portal_is_inactive(self):
        _token, response = self.issue()
        self.editor.write(
            {
                "group_ids": [
                    Command.unlink(self.env.ref("base.group_user").id),
                    Command.link(self.env.ref("base.group_portal").id),
                ]
            }
        )
        self.assertTrue(self.editor.share)
        self.assertEqual(self.Token._introspect(response["access_token"]), INACTIVE)

    def test_removed_groups_drop_the_scope(self):
        _token, response = self.issue()
        self.demote_to_reader(self.editor)
        self.assertEqual(
            self.Token._introspect(response["access_token"])["scope"], "okf.read"
        )
        self.remove_mcp_access(self.editor)
        self.assertEqual(self.Token._introspect(response["access_token"]), INACTIVE)

    def test_rotation_follows_the_groups(self):
        token, first = self.issue()
        self.demote_to_reader(self.editor)
        _new, second = self.Token._rotate(first["refresh_token"], self.client)
        self.assertEqual(second["scope"], "okf.read")
        self.remove_mcp_access(self.editor)
        self.assertOAuthError(
            "invalid_grant", self.Token._rotate, second["refresh_token"], self.client
        )
        self.assertTrue(token.revoked_at)

    def test_archived_client_is_inactive(self):
        _token, response = self.issue()
        self.client.active = False
        self.assertEqual(self.Token._introspect(response["access_token"]), INACTIVE)

    def test_purge_expired(self):
        past = fields.Datetime.now() - timedelta(minutes=1)
        live, _response = self.issue()
        refreshable, _response = self.issue()
        refreshable.expires_at = past
        expired, _response = self.issue()
        expired.write({"expires_at": past, "refresh_expires_at": past})
        self.Code._create_code(
            self.client, self.editor, "https://x.example/cb", "c" * 43, {"okf.read"}, RESOURCE
        )
        old_code = self.Code.search([], limit=1)
        old_code.expires_at = past
        self.assertEqual(self.Token._purge_expired(), 2)
        self.assertTrue(live.exists())
        self.assertTrue(refreshable.exists())
        self.assertFalse(expired.exists())
        self.assertFalse(old_code.exists())

    def test_purge_unused_self_registered_clients(self):
        Client = self.env["o3p.mcp.client"]
        payload = {"redirect_uris": ["https://app.example.com/cb"], "token_endpoint_auth_method": "none"}

        def register(ip, hours_ago):
            client = Client.search(
                [("client_id", "=", Client._register_client(payload, ip)["client_id"])]
            )
            self.env.cr.execute(
                "UPDATE o3p_mcp_client SET create_date = now() at time zone 'UTC'"
                " - make_interval(hours => %s) WHERE id = %s",
                [hours_ago, client.id],
            )
            client.invalidate_recordset(["create_date"])
            return client

        stale = register("203.0.113.1", 25)
        archived = register("203.0.113.2", 25)
        archived.active = False
        used = register("203.0.113.3", 25)
        self.Token._issue(used, self.editor, {"okf.read"}, RESOURCE)
        recent = register("203.0.113.4", 23)
        self.env.cr.execute(
            "UPDATE o3p_mcp_client SET create_date = create_date - interval '25 hours'"
            " WHERE id = %s",
            [self.client.id],
        )
        self.client.invalidate_recordset(["create_date"])
        self.assertEqual(self.Token._purge_expired(), 2)
        self.assertFalse(stale.exists())
        self.assertFalse(archived.exists())
        self.assertTrue(used.exists())
        self.assertTrue(recent.exists())
        # Clients created by an administrator are never purged.
        self.assertTrue(self.client.exists())

    def test_cron_purge_reports_progress(self):
        expired, _response = self.issue()
        past = fields.Datetime.now() - timedelta(minutes=1)
        expired.write({"expires_at": past, "refresh_expires_at": past})
        cron_class = type(self.env["ir.cron"])
        with patch.object(cron_class, "_commit_progress", return_value=60.0) as progress:
            self.Token._cron_purge()
        progress.assert_called_once_with(1, remaining=0)
        self.assertFalse(expired.exists())

    def test_code_is_single_use(self):
        code = self.Code._create_code(
            self.client, self.editor, "https://x.example/cb", "c" * 43, {"okf.read"}, RESOURCE
        )
        record = self.Code._find(code)
        self.assertTrue(record)
        self.assertEqual(record.code_hash, hashlib.sha256(code.encode()).hexdigest())
        self.assertTrue(record._claim())
        self.assertTrue(record.used)
        self.assertFalse(record._claim())
        self.assertFalse(self.Code._find("not-a-code"))


@tagged("post_install", "-at_install")
class TestClientAuthentication(O3pMcpCommon, TransactionCase):
    def assertInvalidClient(self, *args, **kwargs):
        with self.assertRaises(OAuthError) as caught:
            self.env["o3p.mcp.client"]._authenticate(*args, **kwargs)
        self.assertEqual(caught.exception.error, "invalid_client")
        self.assertEqual(caught.exception.status, 401)

    def test_public_client(self):
        Client = self.env["o3p.mcp.client"]
        self.assertEqual(Client._authenticate(self.client.client_id, None, "none"), self.client)
        self.assertInvalidClient(self.client.client_id, "a-secret", "none")
        self.assertInvalidClient(self.client.client_id, "a-secret", "client_secret_post")
        self.assertInvalidClient("c_unknown", None, "none")
        self.assertInvalidClient(None, None, "none")

    def test_confidential_client(self):
        Client = self.env["o3p.mcp.client"]
        client = Client.create(
            {
                "name": "Confidential",
                "redirect_uris": ["https://confidential.example.com/cb"],
                "token_endpoint_auth_method": "client_secret_basic",
            }
        )
        secret = client._set_new_secret()
        self.assertNotIn(secret, client.secret_hash)
        self.assertEqual(
            Client._authenticate(client.client_id, secret, "client_secret_basic"), client
        )
        self.assertInvalidClient(client.client_id, secret, "client_secret_post")
        self.assertInvalidClient(client.client_id, secret + "x", "client_secret_basic")
        self.assertInvalidClient(client.client_id, None, "client_secret_basic")
        self.assertInvalidClient(
            client.client_id, secret, "client_secret_basic", kind="resource_server"
        )
        new_secret = client._set_new_secret()
        self.assertInvalidClient(client.client_id, secret, "client_secret_basic")
        Client._authenticate(client.client_id, new_secret, "client_secret_basic")
        client.active = False
        self.assertInvalidClient(client.client_id, new_secret, "client_secret_basic")

    def test_redirect_uris_are_validated(self):
        Client = self.env["o3p.mcp.client"]
        for uris in (
            ["javascript:alert(1)"],
            ["http://app.example.com/cb"],
            ["https://evil.io\\.chatgpt.com/cb"],
            ["https://user@app.example.com/cb"],
            "x",
        ):
            with self.subTest(uris=uris), self.assertRaises(ValidationError):
                Client.create({"name": "Bad", "redirect_uris": uris})
        Client.create({"name": "Server", "kind": "resource_server"})
