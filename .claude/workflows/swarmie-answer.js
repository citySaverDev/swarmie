export const meta = {
  name: 'swarmie-answer',
  description: 'Answer one stage of a Swarmie simulation: persona subagents role-play pending tasks and write answers to the run directory',
  whenToUse: 'Called by the /swarmie-sim skill once per pipeline stage (parse, archetypes, reactions, report). Not useful on its own.',
  phases: [
    { title: 'Answer', detail: 'one subagent per batch of pending Swarmie tasks' },
    { title: 'Retry', detail: 'one bounded retry for batches that left tasks unanswered' },
  ],
}

// args: { bridge: "<command prefix that runs claude_sim/bridge.py>", dir: "<run dir>", stage: "parse|archetypes|reactions|report", batches: N }
const A = args || {}
if (!A.bridge || !A.dir || !A.stage || !A.batches) {
  throw new Error('swarmie-answer needs args {bridge, dir, stage, batches}')
}
const N = Math.max(1, Math.min(6, Number(A.batches)))
const PERSONA_STAGE = A.stage === 'reactions'

const RESULT = {
  type: 'object',
  properties: {
    submitted: { type: 'integer', description: 'answers accepted by the bridge' },
    unanswered_keys: { type: 'array', items: { type: 'string' } },
    problems: { type: 'array', items: { type: 'string' } },
  },
  required: ['submitted', 'unanswered_keys', 'problems'],
}

function prompt(i, retry) {
  const file = `${A.dir}/answers_${A.stage}_${i}${retry ? '_retry' : ''}.json`
  const roleplay = PERSONA_STAGE
    ? `Each task is ONE independent persona in a simulated online community. Answer every task as if it were a separate,
fresh call with no memory of the other tasks: stay in that persona's voice, tone and biases; let skeptics push back hard,
let indifferent people be brief; do not converge on similar wording, sentiment or objections across tasks. Objection tags
must be short and reusable (e.g. "price", "trust", "switching cost"), because Swarmie counts identical tags.
Never mention that this is a simulation or that you are an AI.`
    : `These tasks are the analyst/synthesis steps of the pipeline. Follow each system prompt exactly, be specific to the
pitch and the data provided, and do not invent data that is not in the input.`
  return `You are acting as the language model inside the open-source Swarmie startup-validation pipeline.
Swarmie's own code built these prompts; you only supply the model's replies.

1. Read your tasks:  ${A.bridge} show --dir ${A.dir} --batch ${i} --of ${N}${retry ? ' --only-missing' : ''}
   The output has "system_prompts" (shared instructions, by id) and "tasks" (each with key, kind, system_id, user).
2. For each task, produce exactly the JSON object its system prompt asks for (follow its schema and enums exactly).
${roleplay}
3. Write ALL answers to ${file} as a JSON array: [{"key": "<task key>", "response": { ...the JSON object... }}, ...]
   Use the Write tool. The file must be valid JSON. Copy each key exactly.
4. Submit:  ${A.bridge} answer --dir ${A.dir} --file ${file}
   If "rejected" is non-empty or "batch_keys_missing" lists any of your keys, fix those entries, rewrite the file and
   submit again (at most 2 more attempts).
5. Return submitted (accepted count from the last submit), unanswered_keys (your keys still missing) and problems.
Do not edit any other file and do not run any other bridge command.${retry ? '\nThis is a retry: the show output already lists only the tasks that are still unanswered.' : ''}`
}

phase('Answer')
log(`Stage "${A.stage}": ${N} subagent${N === 1 ? '' : 's'}`)
const first = await parallel(Array.from({ length: N }, (_, i) => () =>
  agent(prompt(i, false), {
    label: `${A.stage} batch ${i + 1}/${N}`,
    phase: 'Answer',
    schema: RESULT,
    effort: PERSONA_STAGE ? 'low' : undefined,
  })))

const needRetry = first
  .map((r, i) => ({ r, i }))
  .filter(({ r }) => r === null || (r.unanswered_keys && r.unanswered_keys.length))
let retried = []
if (needRetry.length) {
  phase('Retry')
  log(`Retrying ${needRetry.length} batch(es) once`)
  retried = await parallel(needRetry.map(({ i }) => () =>
    agent(prompt(i, true), { label: `${A.stage} retry ${i + 1}/${N}`, phase: 'Retry', schema: RESULT,
      effort: PERSONA_STAGE ? 'low' : undefined })))
}

const finalUnanswered = needRetry.flatMap(({ i }, j) => {
  const r = retried[j]
  return r === null ? [`batch ${i + 1}: subagent failed`] : (r.unanswered_keys || [])
})
return {
  stage: A.stage,
  batches: N,
  submitted: first.filter(Boolean).reduce((s, r) => s + r.submitted, 0) + retried.filter(Boolean).reduce((s, r) => s + r.submitted, 0),
  failed_batches_first_pass: needRetry.length,
  still_unanswered: finalUnanswered,
  problems: first.concat(retried).filter(Boolean).flatMap(r => r.problems || []),
}
