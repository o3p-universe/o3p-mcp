export const meta = {
  name: 'mcp-lab-users',
  description: 'One agent per test user does the ordinary work of that role through the MCP lab',
  whenToUse: 'After e2e/lab/lab.sh up: each test user answers questions or writes knowledge through MCP. Judge the run afterwards with e2e/lab/oracle.mjs and against the ORM.',
  phases: [{ title: 'Users', detail: 'one agent per test user' }],
}

const run = (args && args.run) || 'users'
// Commands are given relative to the repository root, where agents start;
// args.root names it when they start elsewhere.
const prefix = args && args.root ? 'cd ' + args.root + ' && ' : ''
const cli = (role) => prefix + 'LAB_RUN=' + run + ' node e2e/lab/call.mjs --as ' + role + ' --agent ' + role

function intro(role, who) {
  return [
    'You are helping test software on a local test installation (Docker on this machine, test data only).',
    'The software lets a person use their Odoo account from an AI assistant: the assistant calls MCP tools, and every tool answers with what that person may see in Odoo.',
    '',
    'You play ' + who + '. Do the work below the way that person\'s assistant would: find the right tools, call them, read the answers.',
    '',
    'Your only way to reach the system is this command, run from the root of the o3p-mcp repository, which makes one tool call as that person and prints the answer:',
    '',
    '  ' + cli(role) + ' --list                      (tool names, one per line)',
    '  ' + cli(role) + ' --schema <tool>             (one tool in full: description and input schema)',
    '  ' + cli(role) + ' <tool> \'<JSON arguments>\'   (call a tool)',
    '',
    'Rules: use only that command to reach the system; do not open anything under e2e/.work; do not change any file; make at most 25 tool calls.',
    'If a tool refuses a call or an answer confuses you, note it under problems in one sentence each: that is useful feedback, not a failure.',
  ].join('\n')
}

const SALES_TASK = [
  '',
  'Work to do: answer these four questions about sales orders (model sale.order) from the Odoo data tools.',
  '1. How many sales orders can you see in total?',
  '2. Of those, in how many are you the salesperson?',
  '3. What is the sum of amount_total over all the sales orders you can see?',
  '4. Group the orders you can see by the month of their order date (date_order), as the grouping tool does it. Which month has the highest sum of amount_total, and what is that sum? Give the month as YYYY-MM.',
  '',
  'Report the numbers exactly as the tools gave them; do not estimate.',
].join('\n')

const SALES_SCHEMA = {
  type: 'object',
  properties: {
    orders_visible: { type: 'integer' },
    own_orders: { type: 'integer' },
    total_amount: { type: 'number' },
    best_month: { type: 'string' },
    best_month_total: { type: 'number' },
    calls: { type: 'integer' },
    problems: { type: 'array', items: { type: 'string' } },
  },
  required: ['orders_visible', 'own_orders', 'total_amount', 'best_month', 'best_month_total', 'calls', 'problems'],
}

const NOTE_TASK = [
  '',
  'Work to do: add one short note to the company knowledge base with the knowledge tools.',
  'The note: title "Returns desk opening hours (' + run + ')", saying that the returns desk is open on weekdays from 9:00 to 13:00 and that returns above 500 EUR need the shift lead. Give it a fitting type and one or two tags, in the bundle named shop.',
  'First look at how existing concepts in that bundle are written, validate your note, then save it. Afterwards search for it to confirm it is there.',
  'Report the URI of the saved note.',
].join('\n')

const NOTE_SCHEMA = {
  type: 'object',
  properties: {
    saved: { type: 'boolean' },
    uri: { type: 'string' },
    found_by_search: { type: 'boolean' },
    calls: { type: 'integer' },
    problems: { type: 'array', items: { type: 'string' } },
  },
  required: ['saved', 'uri', 'found_by_search', 'calls', 'problems'],
}

const USERS = [
  { role: 'sp_a', who: 'Sara Vendite, a salesperson', task: SALES_TASK, schema: SALES_SCHEMA },
  { role: 'sp_b', who: 'Bruno Vendite, a salesperson', task: SALES_TASK, schema: SALES_SCHEMA },
  { role: 'manager', who: 'Marta Capo, the sales manager', task: SALES_TASK, schema: SALES_SCHEMA },
  { role: 'editor', who: 'Eva Editor, who maintains the company knowledge base and has no access to sales', task: NOTE_TASK, schema: NOTE_SCHEMA },
]

const results = await parallel(
  USERS.map((user) => () =>
    agent(intro(user.role, user.who) + '\n' + user.task, {
      label: 'user:' + user.role,
      phase: 'Users',
      schema: user.schema,
    }).then((answer) => ({ role: user.role, answer })),
  ),
)
return results.filter(Boolean)
