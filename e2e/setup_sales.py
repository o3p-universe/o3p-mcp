# Sales data and users for the data API checks (plan step D4). Run inside
# `odoo shell -d o3p_e2e` (stdin) after setup.py, with `sale` installed.
# Prints one line "E2E_SALES <json>": the users, the records the checks
# refer to, and what the ORM gives each user for the same questions (the
# truth the data API answers are compared with).
#
# The data is deterministic (seeded): customers, products, about 60 orders
# over June to November 2026 for two salespeople restricted to their own
# documents, a sales manager and no salesperson at all; confirmed, quotation
# and cancelled orders; notes and activities on some orders; one saved
# payment method.
import fnmatch
import json
import os
import random
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from odoo import Command
from odoo.tools import json_default

password = os.environ.get("E2E_PASSWORD", "e2e-Passw0rd!")
env = env  # noqa: F821, PLW0127 - provided by odoo shell
ref = env.ref
rng = random.Random(20261114)
TZ = "Europe/Rome"

internal = ref("base.group_user")
system = ref("base.group_system")
salesman = ref("sales_team.group_sale_salesman")
sale_manager = ref("sales_team.group_sale_manager")
mcp_user = ref("o3p_mcp.group_mcp_user")

Users = env["res.users"].with_context(no_reset_password=True)
people = {
    "sp_a": ("sara.vendite@example.test", "Sara Vendite", [internal, salesman, mcp_user]),
    "sp_b": ("bruno.vendite@example.test", "Bruno Vendite", [internal, salesman, mcp_user]),
    "manager": ("marta.capo@example.test", "Marta Capo", [internal, sale_manager, mcp_user]),
    "d_admin": ("dario.admin@example.test", "Dario Admin", [internal, system, sale_manager, mcp_user]),
    "reader": ("okf.reader@example.test", "OKF Reader", [internal, system]),
}
users = {}
records = {}
for key, (login, name, groups) in people.items():
    user = Users.create(
        {
            "login": login,
            "name": name,
            "email": login,
            "password": password,
            "tz": TZ,
            "lang": "en_US",
            "group_ids": [Command.set([group.id for group in groups])],
        }
    )
    records[key] = user
    users[key] = {"id": user.id, "login": login, "name": name}
# The MCP user without sales rights is setup.py's "user".
mcp_only = env["res.users"].search([("login", "=", "ugo.user@example.test")])
mcp_only.tz = TZ
records["mcp_only"] = mcp_only
users["mcp_only"] = {"id": mcp_only.id, "login": mcp_only.login, "name": mcp_only.name}

# API key of the technical user the okf-odoo producer reads metadata with.
api_key = (
    env["res.users.apikeys"].with_user(records["reader"]).sudo()._generate("rpc", "okf-odoo producer", None)
)

# ---------------------------------------------------------------------------
# Customers and products
# ---------------------------------------------------------------------------

italy = ref("base.it")
names = [
    "Alpi Arredamenti", "Bottega Rossi", "Cantina Lago Blu", "Dolomiti Sport",
    "Edilnord Costruzioni", "Ferramenta Verdi", "Gelateria Polare", "Hotel Belvedere",
    "Idraulica Bianchi", "Libreria Portici", "Molino Antico", "Nautica Riviera",
]
customers = env["res.partner"].create(
    [
        {
            "name": name,
            "is_company": True,
            "email": f"orders@{name.lower().replace(' ', '')}.example",
            "city": rng.choice(["Milano", "Torino", "Bologna", "Verona", "Trento"]),
            "country_id": italy.id,
        }
        for name in names
    ]
)
# Customers only one salesperson sells to: the other must learn nothing of them.
only_a = customers[0:3]
only_b = customers[3:6]
shared = customers[6:]

Product = env["product.product"]
products = Product.create(
    [
        {"name": "Installation service", "type": "service", "list_price": 480.0, "default_code": "SRV-INST"},
        {"name": "Maintenance plan (yearly)", "type": "service", "list_price": 1200.0, "default_code": "SRV-MAINT"},
        {"name": "Office chair Ergo", "type": "consu", "list_price": 189.0, "default_code": "CH-ERGO"},
        {"name": "Standing desk 160", "type": "consu", "list_price": 640.0, "default_code": "DK-160"},
        {"name": "Cable kit", "type": "consu", "list_price": 25.5, "default_code": "CB-KIT"},
        {"name": "Meeting table Oval", "type": "consu", "is_storable": True, "list_price": 1350.0, "default_code": "TB-OVAL"},
        {"name": "Acoustic panel", "type": "consu", "is_storable": True, "list_price": 95.0, "default_code": "AC-PANEL"},
        {"name": "Server rack 42U", "type": "consu", "is_storable": True, "list_price": 2890.0, "default_code": "RK-42U"},
    ]
)
products.product_tmpl_id.write({"taxes_id": [Command.clear()]})
# Storable products sold only in salesperson B's confirmed orders.
b_only_products = products.filtered(lambda p: p.default_code in ("RK-42U",))
general_products = products - b_only_products

# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

SaleOrder = env["sale.order"]


def order_date(month, day, hour=10, minute=0):
    return datetime(2026, month, day, hour, minute)


def make_order(user, partner, when, lines, state):
    order = SaleOrder.create(
        {
            "partner_id": partner.id,
            "user_id": user.id if user else False,
            "date_order": when,
            "order_line": [
                Command.create({"product_id": product.id, "product_uom_qty": qty, "price_unit": product.list_price})
                for product, qty in lines
            ],
        }
    )
    if state in ("sale", "sent"):
        if state == "sale":
            order.action_confirm()
        else:
            order.state = "sent"
        order.date_order = when
    elif state == "cancel":
        order._action_cancel()
    return order


def random_lines(pool):
    count = rng.randint(1, 4)
    return [(product, rng.choice([1, 1, 2, 3, 5, 10])) for product in rng.sample(list(pool), count)]


def random_state():
    roll = rng.random()
    return "sale" if roll < 0.62 else "sent" if roll < 0.75 else "draft" if roll < 0.9 else "cancel"


plan = []
for user_key, own, count in (("sp_a", only_a, 24), ("sp_b", only_b, 24), ("manager", shared, 6)):
    for _index in range(count):
        partner = rng.choice(list(own) + list(shared))
        when = order_date(rng.randint(6, 11), rng.randint(1, 28), rng.randint(7, 18), rng.choice([0, 15, 30, 45]))
        plan.append((records[user_key], partner, when, random_lines(general_products), random_state()))
# B sells the server racks, confirmed, to B-only customers.
for partner, qty in ((only_b[0], 3), (only_b[1], 2)):
    plan.append((records["sp_b"], partner, order_date(10, rng.randint(1, 28)), [(b_only_products[0], qty)], "sale"))
# Orders without a salesperson: every salesperson may read them.
for _index in range(3):
    plan.append((None, rng.choice(list(shared)), order_date(rng.randint(6, 11), rng.randint(1, 28)), random_lines(general_products), "sale"))
# Near a month boundary: 31 August 23:30 UTC is 1 September in Rome.
plan.append((records["sp_a"], only_a[0], datetime(2026, 8, 31, 23, 30), [(products[1], 1)], "sale"))
plan.append((records["sp_b"], only_b[2], datetime(2026, 8, 31, 22, 30), [(products[3], 2)], "sale"))
orders = SaleOrder.browse()
for user, partner, when, lines, state in plan:
    orders |= make_order(user, partner, when, lines, state)

b_orders = orders.filtered(lambda o: o.user_id == records["sp_b"])
a_orders = orders.filtered(lambda o: o.user_id == records["sp_a"])
# Internal notes and activities on some of B's orders, as people leave them.
for order in b_orders[:4]:
    order.message_post(
        body=f"Customer asked for a 12% discount on {order.name}; margin is thin.",
        message_type="comment",
        subtype_xmlid="mail.mt_note",
    )
    order.activity_schedule("mail.mail_activity_data_todo", user_id=records["sp_b"].id, summary="Call back about the discount")

# Tags: many-to-many pairs of orders the other salesperson may not read.
Tag = env["crm.tag"]
tag_b = Tag.create({"name": "Key account B"})
tag_a = Tag.create({"name": "Key account A"})
b_orders.write({"tag_ids": [Command.link(tag_b.id)]})
a_orders[:5].write({"tag_ids": [Command.link(tag_a.id)]})

# The largest table: 30,000 contacts every internal user may read.
cities = ["Milano", "Torino", "Bologna", "Verona", "Trento", "Bari", "Napoli", "Genova", "Padova", "Parma"]
bulk = []
for index in range(30_000):
    bulk.append({"name": f"Contact {index:05d}", "city": cities[index % len(cities)], "email": f"c{index}@contacts.example"})
for start in range(0, len(bulk), 5000):
    env["res.partner"].create(bulk[start : start + 5000])

# A saved payment method of a B-only customer.
payment_token = None
try:
    with env.cr.savepoint():
        provider = env["payment.provider"].create({"name": "E2E Gateway"})
        method = env["payment.method"].create(
            {"name": "Card", "code": "card", "provider_id": provider.id}
        )
        token = env["payment.token"].create(
            {
                "provider_id": provider.id,
                "payment_method_id": method.id,
                "partner_id": only_b[0].id,
                "provider_ref": "pm_e2e_SECRET_4242",
                "payment_details": "4242",
            }
        )
        payment_token = {"id": token.id, "provider_ref": token.provider_ref}
except Exception as error:  # noqa: BLE001
    payment_token = {"error": type(error).__name__}

env.flush_all()

# The SQL sandbox, configured and checked as an administrator does in
# Settings › MCP › SQL.
sql_settings = env["res.config.settings"].create(
    {
        "o3p_mcp_sql_host": "127.0.0.1",
        "o3p_mcp_sql_port": int(os.environ.get("E2E_SANDBOX_PORT", "15433")),
        "o3p_mcp_sql_database": "o3p_sqlbox",
        "o3p_mcp_sql_user": "o3p_sqlbox",
        "o3p_mcp_sql_roles": 4,
    }
)
sql_settings.execute()
check = env["res.config.settings"].create({}).action_o3p_mcp_check_sql()
sql_check = {"type": check["params"]["type"], "message": check["params"]["message"]}

# ---------------------------------------------------------------------------
# What the ORM gives each user
# ---------------------------------------------------------------------------

# The addon's default lists of hidden models and field names, so that the
# expectations follow the version under test.
from odoo.addons.o3p_mcp.models.denylist import (  # noqa: E402
    DEFAULT_DENIED_FIELDS as DENIED_FIELDS,
    DEFAULT_DENIED_MODELS as DENIED_MODELS,
)


def field_denied(field):
    if field.type == "binary" or any(fnmatch.fnmatchcase(field.name.lower(), p) for p in DENIED_FIELDS):
        return True
    return bool(field.relational and any(fnmatch.fnmatchcase(field.comodel_name, p) for p in DENIED_MODELS))


def as_user(model, user):
    return env[model].with_user(user).with_context(tz=user.tz or "UTC", lang=user.lang or "en_US")


def month_key(value, tz):
    local = value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz))
    return f"{local.year:04d}-{local.month:02d}"


def plain(value):
    return json.loads(json.dumps(value, default=json_default))


truth = {}
for key in ("sp_a", "sp_b", "manager", "d_admin", "mcp_only"):
    user = records[key]
    SO = as_user("sale.order", user)
    entry = {"can_read_orders": SO.has_access("read")}
    if entry["can_read_orders"]:
        rows = SO.search_read([], ["user_id", "amount_total", "date_order", "partner_id"], order="id")
        entry["order_ids"] = [row["id"] for row in rows]
        entry["own_order_ids"] = SO.search([("user_id", "=", user.id)], order="id").ids
        by_user = defaultdict(lambda: [0.0, 0])
        by_month = defaultdict(lambda: [0.0, 0])
        for row in rows:
            salesperson = row["user_id"][0] if row["user_id"] else False
            by_user[salesperson][0] += row["amount_total"]
            by_user[salesperson][1] += 1
            month = month_key(row["date_order"], user.tz)
            by_month[month][0] += row["amount_total"]
            by_month[month][1] += 1
        entry["by_user"] = {str(k): v for k, v in by_user.items()}
        entry["by_month"] = dict(by_month)
        grouped = SO.with_context(read_group_expand=False, fill_temporal=False)
        entry["orm_group_user"] = plain(
            grouped.formatted_read_group([], ["user_id"], ["amount_total:sum", "__count"])
        )
        entry["orm_group_month"] = plain(
            grouped.formatted_read_group([], ["date_order:month"], ["amount_total:sum", "__count"])
        )
        report = as_user("sale.report", user)
        entry["orm_report_user"] = plain(
            report.with_context(read_group_expand=False, fill_temporal=False).formatted_read_group(
                [], ["user_id"], ["price_total:sum", "__count"]
            )
        )
        visible_partners = {row["partner_id"][0] for row in rows}
        entry["partners_with_visible_orders"] = sorted(visible_partners)
        entry["line_count"] = as_user("sale.order.line", user).with_context(active_test=False).search_count([])
        entry["line_total"] = sum(
            as_user("sale.order.line", user).with_context(active_test=False).search([]).mapped("price_total")
        )
        entry["tag_pairs"] = sum(len(order.tag_ids) for order in SO.search([]))
    entry["partner_count"] = as_user("res.partner", user).with_context(active_test=False).search_count([])
    restricted_columns = {}
    for model_name in ("res.partner", "sale.order", "res.users"):
        model = as_user(model_name, user)
        restricted_columns[model._table] = sorted(
            name
            for name, field in model._fields.items()
            if field.store and field.column_type and not field.inherited and not model.has_field_access(field, "read")
        )
    entry["restricted_columns"] = restricted_columns
    model = as_user("sale.order", user)
    fields_all = sorted(model._fields)
    entry["sale_order_fields"] = {
        "restricted": sorted(n for n, f in model._fields.items() if not model.has_field_access(f, "read")),
        "denied": sorted(n for n, f in model._fields.items() if field_denied(f)),
        "all": fields_all,
    }
    truth[key] = entry

hidden_from_a = (orders - orders.filtered(lambda o: o.user_id in (records["sp_a"], env["res.users"]))).ids
storable_b = b_only_products[0]
facts = {
    "order_ids": orders.ids,
    "a_order_ids": a_orders.ids,
    "b_order_ids": b_orders.ids,
    "hidden_from_a": hidden_from_a,
    "only_a_customers": only_a.ids,
    "only_b_customers": only_b.ids,
    "b_only_product": {
        "id": storable_b.id,
        "template_id": storable_b.product_tmpl_id.id,
        "sold_qty_confirmed": sum(
            orders.order_line.filtered(lambda l: l.product_id == storable_b and l.order_id.state == "sale").mapped("product_uom_qty")
        ),
    },
    "b_orders_with_notes": b_orders[:4].ids,
    "payment_token": payment_token,
    "nonexistent_order_id": max(orders.ids) + 1000,
    "hidden_order_names": orders.browse(hidden_from_a).mapped("name"),
    "b_only_customer_names": only_b.mapped("name"),
    "b_order_amounts": sorted(set(b_orders.mapped("amount_total"))),
    "tag_b": tag_b.id,
    "largest_table": {"table": "res_partner", "rows": env["res.partner"].with_context(active_test=False).search_count([])},
}
# Models whose rows point at other records by (model name, id): the hostile
# sweep looks there for traces of orders the user may not read.
pointer_models = []
for name in sorted(env.registry):
    model = env[name]
    if model._abstract or model._transient:
        continue
    fields_ = model._fields
    model_field = next((f for f in ("res_model", "model") if f in fields_ and fields_[f].type in ("char", "selection") and fields_[f].store), None)
    id_field = next((f for f in ("res_id",) if f in fields_ and fields_[f].type in ("integer", "many2one_reference") and fields_[f].store), None)
    if model_field and id_field:
        pointer_models.append({"model": name, "model_field": model_field, "id_field": id_field})

print(
    "E2E_SALES "
    + json.dumps(
        {
            "users": users,
            "groups": {"salesman": salesman.id, "sale_manager": sale_manager.id},
            "sql_check": sql_check,
            "producer_api_key": api_key,
            "facts": facts,
            "truth": truth,
            "pointer_models": pointer_models,
            "orm_outgoing_qty_as_a": as_user("product.product", records["sp_a"]).browse(storable_b.id).outgoing_qty,
        },
        default=json_default,
    )
)
env.cr.commit()
