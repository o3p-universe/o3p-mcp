// A small OKF bundle in a fresh git repository: a few linked concepts, a
// clean worktree and a committer identity that is not any Odoo user's.
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

export const SERVER_IDENTITY = { name: "OKF Server", email: "okf-server@e2e.invalid" };

const CONCEPTS = {
  "services/order-service.md": {
    type: "Service",
    title: "Order service",
    description: "Accepts, validates and stores customer orders.",
    tags: ["orders", "service"],
    body:
      "# Order service\n\nThe order service accepts orders from the web shop and the B2B portal, " +
      "checks stock with the warehouse and stores the order. Refunds follow the " +
      "[refund policy](../policies/refund-policy.md); when orders pile up, use the " +
      "[order backlog runbook](../runbooks/order-backlog.md).\n",
    relations: [
      { type: "related_to", target: "/policies/refund-policy.md" },
      { type: "related_to", target: "/runbooks/order-backlog.md" },
    ],
  },
  "policies/refund-policy.md": {
    type: "Policy",
    title: "Refund policy",
    description: "When and how a customer order is refunded.",
    tags: ["orders", "refunds", "policy"],
    body:
      "# Refund policy\n\nA customer may ask for a refund within 30 days of delivery. " +
      "Refunds above 500 EUR need a second approval. The " +
      "[order service](../services/order-service.md) records every refund against its order.\n",
    relations: [{ type: "related_to", target: "/services/order-service.md" }],
  },
  "runbooks/order-backlog.md": {
    type: "Runbook",
    title: "Order backlog runbook",
    description: "What to do when unprocessed orders accumulate.",
    tags: ["orders", "runbook", "on-call"],
    body:
      "# Order backlog runbook\n\nWhen more than 200 orders wait longer than 15 minutes, " +
      "check the warehouse stock API, then restart the " +
      "[order service](../services/order-service.md) workers one at a time. " +
      "See the [SLA](../glossary/sla.md) for the response targets.\n",
    relations: [
      { type: "related_to", target: "/services/order-service.md" },
      { type: "related_to", target: "/glossary/sla.md" },
    ],
  },
  "glossary/sla.md": {
    type: "Glossary Term",
    title: "Service level agreement",
    description: "Response and resolution targets agreed with customers.",
    tags: ["glossary", "sla"],
    body:
      "# Service level agreement\n\nThe SLA sets a first response within 4 business hours " +
      "and a resolution target of 2 business days for order incidents.\n",
    relations: [],
  },
};

function frontmatter(concept) {
  const lines = [
    "---",
    `type: ${JSON.stringify(concept.type)}`,
    `title: ${JSON.stringify(concept.title)}`,
    `description: ${JSON.stringify(concept.description)}`,
    `tags: ${JSON.stringify(concept.tags)}`,
    'lang: "en"',
    "generated:",
    '  at: "2026-11-01T09:00:00Z"',
    '  by: "human:seed"',
    'status: "stable"',
  ];
  if (concept.relations.length) {
    lines.push("relations:");
    for (const relation of concept.relations) {
      lines.push(`  - type: ${JSON.stringify(relation.type)}`);
      lines.push(`    target: ${JSON.stringify(relation.target)}`);
    }
  }
  lines.push("---", "");
  return lines.join("\n");
}

export function git(dir, ...args) {
  return execFileSync("git", ["-C", dir, ...args], { encoding: "utf8" }).trim();
}

// Creates the bundle under `root` (a new directory) and returns its path.
export function createBundle(root) {
  fs.mkdirSync(root, { recursive: true });
  const index = [
    "---",
    'okf_version: "0.2"',
    "---",
    "",
    "# Shop operations",
    "",
    "Knowledge about the shop's order handling, used by the o3p_mcp end-to-end test.",
    "",
  ];
  for (const [file, concept] of Object.entries(CONCEPTS)) {
    const target = path.join(root, file);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, frontmatter(concept) + concept.body);
    index.push(`- [${concept.title}](${file})`);
  }
  fs.writeFileSync(path.join(root, "index.md"), `${index.join("\n")}\n`);
  git(root, "init", "-q", "-b", "main");
  git(root, "config", "user.name", SERVER_IDENTITY.name);
  git(root, "config", "user.email", SERVER_IDENTITY.email);
  git(root, "config", "commit.gpgsign", "false");
  git(root, "add", "-A");
  git(root, "commit", "-q", "-m", "Seed the e2e bundle");
  return root;
}
