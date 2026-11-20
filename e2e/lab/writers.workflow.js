export const meta = {
  name: 'mcp-lab-writers',
  description: 'One agent per test user writes or reviews Odoo records through the MCP lab',
  whenToUse: 'After e2e/lab/lab.sh stack, writes and serve: salespeople create and change records, a user without the right tries, their manager reviews. Judge the run afterwards against Odoo itself and with e2e/lab/oracle.mjs.',
  phases: [
    { title: 'Write', detail: 'the two salespeople and a user without write rights' },
    { title: 'Review', detail: 'the sales manager reads what was written' },
  ],
}

const run = (args && args.run) || 'writers'
const mark = (args && args.mark) || 'W1'
const prefix = args && args.root ? 'cd ' + args.root + ' && ' : ''
const cli = (role) => prefix + 'LAB_RUN=' + run + ' node e2e/lab/call.mjs --as ' + role + ' --agent ' + role

function intro(role, who) {
  return [
    'You are helping test software on a local test installation (Docker on this machine, test data only).',
    'The software lets a person use their Odoo account from an AI assistant: the assistant calls MCP tools, and every tool acts with what that person may see and do in Odoo.',
    '',
    'You play ' + who + '. Do the work below the way that person\'s assistant would: find the right tools, call them, read the answers.',
    '',
    'Your only way to reach the system is this command, run from the root of the o3p-mcp repository, which makes one tool call as that person and prints the answer:',
    '',
    '  ' + cli(role) + ' --list                      (tool names, one per line)',
    '  ' + cli(role) + ' --schema <tool>             (one tool in full: description and input schema)',
    '  ' + cli(role) + ' <tool> \'<JSON arguments>\'   (call a tool)',
    '',
    'Rules: use only that command to reach the system; do not open anything under e2e/.work; do not change any file; make at most 30 tool calls.',
    'If the system does not let this person do the work, do not look for a way around it: report that, with what the system answered.',
    'If a tool refuses a call or an answer confuses you, note it under problems in one sentence each: that is useful feedback, not a failure.',
  ].join('\n')
}

const PROBLEMS = { type: 'array', items: { type: 'string' } }

const SARA = {
  role: 'sp_a',
  who: 'Sara Vendite, a salesperson',
  task: [
    '',
    'Work to do: a new customer called "Officina Rossi ' + mark + '" (email officina.rossi.' + mark.toLowerCase() + '@example.test) asked for a quotation for 3 units of the product "Standing desk 160" and 1 unit of "Installation service".',
    'Check that the customer does not exist yet, create the customer, then the quotation with those two lines. Read the quotation back and report its name, its total (amount_total) and the id of the customer.',
  ].join('\n'),
  schema: {
    type: 'object',
    properties: {
      done: { type: 'boolean' },
      customer_id: { type: 'integer' },
      quotation_name: { type: 'string' },
      total: { type: 'number' },
      calls: { type: 'integer' },
      problems: PROBLEMS,
    },
    required: ['done', 'customer_id', 'quotation_name', 'total', 'calls', 'problems'],
  },
}

const BRUNO = {
  role: 'sp_b',
  who: 'Bruno Vendite, a salesperson',
  task: [
    '',
    'Work to do: the customer of your most recent confirmed sales order (state "sale", the one with the highest id among the orders where you are the salesperson) sent their purchase order number "PO-7781-' + mark + '". Put it in that order\'s customer reference (field client_order_ref). Change nothing else.',
    'Report the name of the order you changed and the customer reference it had before.',
  ].join('\n'),
  schema: {
    type: 'object',
    properties: {
      done: { type: 'boolean' },
      order_name: { type: 'string' },
      previous_reference: { type: 'string' },
      calls: { type: 'integer' },
      problems: PROBLEMS,
    },
    required: ['done', 'order_name', 'previous_reference', 'calls', 'problems'],
  },
}

const UGO = {
  role: 'mcp_only',
  who: 'Ugo Utente, an employee who is not in sales and has no right to change records through the assistant',
  task: [
    '',
    'Work to do: add a contact called "Ditta Verdi ' + mark + '" to the address book.',
    'Report whether it was possible and, if not, what the system answered.',
  ].join('\n'),
  schema: {
    type: 'object',
    properties: {
      done: { type: 'boolean' },
      what_the_system_answered: { type: 'string' },
      calls: { type: 'integer' },
      problems: PROBLEMS,
    },
    required: ['done', 'what_the_system_answered', 'calls', 'problems'],
  },
}

const MARTA = {
  role: 'manager',
  who: 'Marta Capo, the sales manager',
  task: [
    '',
    'Work to do: a quotation was just prepared for the customer "Officina Rossi ' + mark + '". Find it and review it: report its name, who the salesperson is, its total (amount_total), its state, and for each line the product and the quantity. Do not change anything.',
  ].join('\n'),
  schema: {
    type: 'object',
    properties: {
      found: { type: 'boolean' },
      quotation_name: { type: 'string' },
      salesperson: { type: 'string' },
      total: { type: 'number' },
      state: { type: 'string' },
      lines: {
        type: 'array',
        items: {
          type: 'object',
          properties: { product: { type: 'string' }, quantity: { type: 'number' } },
          required: ['product', 'quantity'],
        },
      },
      calls: { type: 'integer' },
      problems: PROBLEMS,
    },
    required: ['found', 'quotation_name', 'salesperson', 'total', 'state', 'lines', 'calls', 'problems'],
  },
}

const play = (user, phaseTitle) =>
  agent(intro(user.role, user.who) + '\n' + user.task, {
    label: 'user:' + user.role,
    phase: phaseTitle,
    schema: user.schema,
  }).then((answer) => ({ role: user.role, answer }))

phase('Write')
const written = await parallel([SARA, BRUNO, UGO].map((user) => () => play(user, 'Write')))
phase('Review')
const review = await play(MARTA, 'Review')
return written.filter(Boolean).concat(review ? [review] : [])
