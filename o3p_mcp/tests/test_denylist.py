"""The default denylist, checked against every installed model.

A stored text field whose name looks sensitive must be hidden (its model or
the field on the denylist) or reviewed below with the reason it may be read.
The same holds for models that point at other records by model name and id
and that internal users may read. Entries for modules that are not installed
are kept: they document the review of a standard install (sale, purchase,
stock, account, crm, hr, project, website_sale, mrp) on Odoo 18 and 20.
"""

import fnmatch

from odoo.tests import TransactionCase, new_test_user, tagged

from ..models.denylist import DEFAULT_DENIED_FIELDS, DEFAULT_DENIED_MODELS


SENSITIVE_NAMES = (
    "*ref*",
    "*token*",
    "*key*",
    "*secret*",
    "*password*",
    "*iban*",
    "*acc_number*",
    "*account_number*",
    "provider_*",
)

# (model, field): why it may be read through the data API.
REVIEWED_FIELDS = {
    ("account.analytic.applicability", "account_prefix"): "account code prefix, accounting configuration",
    ("account.analytic.distribution.model", "account_prefix"): "account code prefix, accounting configuration",
    ("account.analytic.line", "ref"): "business reference of an analytic entry",
    ("account.bank.statement", "reference"): "business reference of a bank statement",
    ("account.group", "code_prefix_start"): "account code range of an account group (Odoo 18)",
    ("account.group", "code_prefix_end"): "account code range of an account group (Odoo 18)",
    ("account.reconcile.model.partner.mapping", "payment_ref_regex"): "a matching rule, reconciliation configuration",
    ("account.bank.statement.line", "payment_ref"): "payment label of a statement line, shown in accounting",
    ("account.bank.statement.line", "account_number"): "counterpart bank account of a statement line, shown in accounting",
    ("account.move", "sequence_prefix"): "prefix of the entry number",
    ("account.move", "ref"): "business reference of an invoice or entry",
    ("account.move", "payment_reference"): "communication printed on the invoice",
    ("account.move.line", "ref"): "business reference of a journal item",
    ("account.payment", "payment_reference"): "communication of a payment, shown on the payment form",
    ("crm.lead", "referred"): "name of who referred the lead",
    ("crm.team.member", "assignment_domain_preferred"): "a lead assignment domain, not a reference",
    ("customer.product.reference", "customer_product_reference"): "the customer's code for a product",
    ("ir.asset", "key"): "technical key of a website asset",
    ("ir.attachment", "key"): "technical key of a website attachment",
    ("ir.model", "website_form_key"): "technical key of a website form",
    ("ir.sequence", "prefix"): "prefix of a document numbering sequence",
    ("ir.ui.view", "key"): "technical key of a view",
    ("ir.ui.view", "website_meta_keywords"): "public SEO keywords of a page",
    ("mail.mail", "references"): "e-mail header of an outgoing mail; mail.mail is readable by administrators only",
    ("product.public.category", "website_meta_keywords"): "public SEO keywords",
    ("product.template", "website_meta_keywords"): "public SEO keywords",
    ("purchase.bill.line.match", "reference"): "business reference of a purchase line",
    ("purchase.bill.union", "reference"): "business reference of a bill or purchase",
    ("purchase.order", "partner_ref"): "the vendor's reference of a purchase order",
    ("res.company", "bank_account_code_prefix"): "account code prefix, accounting configuration",
    ("res.company", "cash_account_code_prefix"): "account code prefix, accounting configuration",
    ("res.company", "transfer_account_code_prefix"): "account code prefix, accounting configuration",
    ("res.users.settings", "push_to_talk_key"): "the user's push-to-talk keyboard shortcut",
    ("stock.move.line", "reference"): "business reference of a stock move line",
    ("res.partner", "ref"): "the contact's internal reference, shown on the contact form",
    ("res.partner.bank", "acc_number"): "bank account number, printed on invoices and shown on the contact to every employee",
    ("res.partner.bank", "sanitized_acc_number"): "the same number without spaces",
    ("res.partner.bank", "account_number"): "bank account number, printed on invoices and shown on the contact to every employee",
    ("res.partner.bank", "sanitized_account_number"): "the same number without spaces",
    ("sale.order", "client_order_ref"): "the customer's reference of a quotation",
    ("sale.order", "reference"): "payment reference of an order",
    ("sale.order.line", "edi_customer_product_ref"): "the customer's code for an ordered product",
    ("stock.lot", "ref"): "internal reference of a lot or serial number",
    ("stock.move", "reference"): "business reference of a stock move",
    ("stock.picking", "carrier_tracking_ref"): "the carrier's tracking number of a delivery",
    ("theme.ir.asset", "key"): "technical key of a theme asset",
    ("theme.ir.attachment", "key"): "technical key of a theme attachment",
    ("theme.ir.ui.view", "key"): "technical key of a theme view",
    ("website", "google_analytics_key"): "public tracking id, emitted in every page",
    ("website.checkout.step", "step_href"): "URL of a checkout step, not a reference",
    ("withdrawal.request", "order_reference"): "business reference of an order",
}

# Models with (res_model, res_id) pointers that internal users may read.
REVIEWED_POINTER_MODELS = {
    "ir.attachment": "searches check access to the attached document",
    "mail.message": "searches check access to the document of the message",
    "mail.activity": "record rule: own activities or a readable document (res_access_read)",
    "mail.scheduled.message": "record rule: readable document only (res_access)",
    "calendar.event": "calendar privacy rules apply; Odoo shows the linked document on the event to its readers",
    "res.users.settings.embedded.action": "record rule: the user's own settings only",
}


def matches(name, patterns):
    return any(fnmatch.fnmatchcase(name.lower(), pattern) for pattern in patterns)


@tagged("post_install", "-at_install")
class TestDenylistReview(TransactionCase):
    def test_default_lists(self):
        api = self.env["o3p.mcp.api"]
        for model in (
            "ir.config_parameter",
            "res.users.apikeys",
            "o3p.mcp.token",
            "auth_totp.device",
            "ir.mail_server",
            "fetchmail.server",
            "payment.provider",
            "payment.token",
            "payment.transaction",
            "mail.followers",
            "mail.notification",
            "mail.tracking.value",
            "mail.push.device",
            "rating.rating",
            "snailmail.letter",
            "bus.bus",
            "bus.presence",
        ):
            with self.subTest(model=model):
                self.assertTrue(api._model_denied(model))
        self.assertEqual(api._denied_model_patterns(), DEFAULT_DENIED_MODELS)
        self.assertEqual(api._denied_field_patterns(), DEFAULT_DENIED_FIELDS)
        for name in ("password", "totp_secret", "access_token", "api_key", "plausible_shared_key", "provider_ref", "provider_reference"):
            with self.subTest(field=name):
                self.assertTrue(matches(name, [p.lower() for p in DEFAULT_DENIED_FIELDS]))

    def test_sensitive_fields_are_hidden_or_reviewed(self):
        api = self.env["o3p.mcp.api"]
        unreviewed = []
        for name in sorted(self.env.registry):
            model = self.env[name]
            if model._abstract or model._transient or api._model_denied(name):
                continue
            for field_name, field in sorted(model._fields.items()):
                if (
                    field.type in ("char", "text")
                    and field.store
                    and matches(field_name, SENSITIVE_NAMES)
                    and not api._field_denied(field)
                    and (name, field_name) not in REVIEWED_FIELDS
                ):
                    unreviewed.append(f"{name}.{field_name}")
        self.assertEqual(
            unreviewed,
            [],
            "Hide these fields (denylist) or review them in REVIEWED_FIELDS with a reason.",
        )

    def test_pointer_models_are_hidden_or_reviewed(self):
        api = self.env["o3p.mcp.api"]
        employee = new_test_user(self.env, "o3p_mcp_review", groups="base.group_user")
        unreviewed = []
        for name in sorted(self.env.registry):
            model = self.env[name]
            if model._abstract or model._transient or api._model_denied(name):
                continue
            fields_ = model._fields
            points = any(
                f in fields_ and fields_[f].type in ("char", "selection") and fields_[f].store
                for f in ("res_model", "model")
            ) and "res_id" in fields_ and fields_["res_id"].store
            if (
                points
                and model.with_user(employee).has_access("read")
                and name not in REVIEWED_POINTER_MODELS
            ):
                unreviewed.append(name)
        self.assertEqual(
            unreviewed,
            [],
            "Hide these models (denylist) or review them in REVIEWED_POINTER_MODELS with a reason.",
        )
