export const meta = {
  name: 'mcp-lab-knowledge',
  description: 'Two users write company knowledge for their audience through the MCP lab, three others look for it',
  whenToUse: 'After e2e/lab/lab.sh stack, knowledge and serve: knowledge isolation between users. Judge the run afterwards with e2e/lab/oracle.mjs, with the markers of the written notes added to the canaries.',
  phases: [
    { title: 'Write', detail: 'a salesperson and the sales manager each write a note' },
    { title: 'Read', detail: 'three other users look for both notes' },
  ],
}

const run = (args && args.run) || 'knowers'
const mark = (args && args.mark) || 'K1'
const prefix = args && args.root ? 'cd ' + args.root + ' && ' : ''
const cli = (role) => prefix + 'LAB_RUN=' + run + ' node e2e/lab/call.mjs --as ' + role + ' --agent ' + role

function intro(role, who) {
  return [
    'You are helping test software on a local test installation (Docker on this machine, test data only).',
    'The software lets a person use their Odoo account and the company knowledge base from an AI assistant: the assistant calls MCP tools, and every tool acts with what that person may see and do.',
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
    'If the system does not let this person do or see something, do not look for a way around it: report what the system answered.',
    'If a tool refuses a call or an answer confuses you, note it under problems in one sentence each: that is useful feedback, not a failure.',
  ].join('\n')
}

const PROBLEMS = { type: 'array', items: { type: 'string' } }
const WROTE = {
  type: 'object',
  properties: {
    saved: { type: 'boolean' },
    bundle: { type: 'string' },
    uri: { type: 'string' },
    readers: { type: 'string' },
    calls: { type: 'integer' },
    problems: PROBLEMS,
  },
  required: ['saved', 'bundle', 'uri', 'readers', 'calls', 'problems'],
}

const SARA = {
  role: 'sp_a',
  who: 'Sara Vendite, a salesperson who also writes notes for the sales team',
  task: [
    '',
    'Work to do: count your confirmed sales orders (state "sale", you as salesperson) and their total (amount_total) from the Odoo data tools. Then save that finding as a short note in the company knowledge base, for the sales team only: other employees must not be able to read it.',
    'Find out which knowledge bundle is read by the sales team only, and write the note there. Its title must begin with the code "KBS-' + mark + '". Afterwards search for the note to confirm it is there.',
    'Report the bundle you chose, the URI of the note, and who can read that bundle according to the system (readers).',
  ].join('\n'),
  schema: WROTE,
}

const MARTA = {
  role: 'manager',
  who: 'Marta Capo, the sales manager',
  task: [
    '',
    'Work to do: record a management decision in the company knowledge base, for management only: salespeople and other employees must not be able to read it. The decision: list prices of desks go up by 4 percent from January.',
    'Find out which knowledge bundle is read by management only, and write the note there. Its title must begin with the code "KBM-' + mark + '". Afterwards search for the note to confirm it is there.',
    'Report the bundle you chose, the URI of the note, and who can read that bundle according to the system (readers).',
  ].join('\n'),
  schema: WROTE,
}

const FOUND = {
  type: 'object',
  properties: {
    bundles_offered: { type: 'array', items: { type: 'string' } },
    found_sales_note: { type: 'boolean' },
    found_management_note: { type: 'boolean' },
    what_was_found: { type: 'string' },
    calls: { type: 'integer' },
    problems: PROBLEMS,
  },
  required: ['bundles_offered', 'found_sales_note', 'found_management_note', 'what_was_found', 'calls', 'problems'],
}
const READ_TASK = [
  '',
  'Work to do: two notes were just added to the company knowledge base. One is about a salesperson\'s confirmed orders; its title begins with "KBS-' + mark + '". The other is about a price increase for desks; its title begins with "KBM-' + mark + '".',
  'Look for both, in every way the tools give you: search by words and by the codes, list the bundles and their concepts, try the graph and resource tools if there are any. Report which bundles you are offered, which of the two notes you found, and what exactly you could read of them.',
].join('\n')
const READERS = [
  { role: 'sp_b', who: 'Bruno Vendite, a salesperson', task: READ_TASK, schema: FOUND },
  { role: 'editor', who: 'Eva, who maintains the general knowledge base and is not in sales', task: READ_TASK, schema: FOUND },
  { role: 'mcp_only', who: 'Ugo Utente, an employee who is not in sales', task: READ_TASK, schema: FOUND },
]

const play = (user, phaseTitle) =>
  agent(intro(user.role, user.who) + '\n' + user.task, {
    label: 'user:' + user.role,
    phase: phaseTitle,
    schema: user.schema,
  }).then((answer) => ({ role: user.role, answer }))

phase('Write')
const written = await parallel([SARA, MARTA].map((user) => () => play(user, 'Write')))
phase('Read')
const read = await parallel(READERS.map((user) => () => play(user, 'Read')))
return written.concat(read).filter(Boolean)
