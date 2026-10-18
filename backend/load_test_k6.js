// Browse real API pages and optionally submit stubbed jobs through the Postgres queue.
import http from 'k6/http'
import { sleep } from 'k6'
import { Trend, Rate, Counter } from 'k6/metrics'

const base = __ENV.BASE
const users = JSON.parse(open(__ENV.ACCOUNTS))
const jobsPerUser = Number(__ENV.JOBS_PER_USER)
const phases = JSON.parse(__ENV.PHASES || '[]')
const names = ['health', 'chats', 'saved', 'cart', 'orders', 'credits', 'submit', 'job_status']
const phaseNames = ['chats', 'saved', 'orders', 'credits', 'submit', 'job_status']
const latency = Object.fromEntries(names.map(name => [name, new Trend(`api_${name}`, true)]))
const errors = Object.fromEntries(names.map(name => [name, new Rate(`err_${name}`)]))
const phaseLatency = Object.fromEntries(phases.flatMap(phase => phaseNames.map(name =>
  [`${phase.name}_${name}`, new Trend(`phase_${phase.name}_${name}`, true)])))
const phaseErrors = Object.fromEntries(phases.flatMap(phase => phaseNames.map(name =>
  [`${phase.name}_${name}`, new Rate(`phase_err_${phase.name}_${name}`)])))
const submittedJobs = new Counter('jobs_submitted')
const started = Number(__ENV.START_MS)

export const options = {
  scenarios: {
    users: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: __ENV.STAGES ? JSON.parse(__ENV.STAGES).map(stage =>
        ({ duration: `${stage.seconds}s`, target: stage.target })) : [
        { duration: `${__ENV.RAMP_S}s`, target: Number(__ENV.VUS) },
        { duration: `${__ENV.HOLD_S}s`, target: Number(__ENV.VUS) },
      ],
      gracefulRampDown: '10s',
      gracefulStop: '30s',
    },
  },
  summaryTrendStats: ['p(50)', 'p(95)', 'p(99)', 'avg', 'max', 'count'],
  thresholds: {
    api_chats: ['p(95)<6000'],
    api_saved: ['p(95)<6000'],
    api_credits: ['p(95)<6000'],
    ...(jobsPerUser > 0 ? { api_submit: ['p(95)<10000'] } : {}),
    http_req_failed: ['rate<0.01'],
  },
}

function record(name, response, expected) {
  const failed = response.status !== expected
  latency[name].add(response.timings.duration)
  errors[name].add(failed)
  const seconds = (Date.now() - started) / 1000
  const phase = phases.find(item => seconds <= item.end_s)
  const key = phase ? `${phase.name}_${name}` : ''
  if (phase && phaseLatency[key]) {
    phaseLatency[key].add(response.timings.duration)
    phaseErrors[key].add(failed)
  }
  return failed ? null : response
}

let pending = null
let submitted = 0
let nextSubmit = 0

export default function () {
  const account = users[(__VU - 1) % users.length]
  const headers = { Authorization: `Bearer ${account.token}`, 'Content-Type': 'application/json' }
  const params = name => ({ headers, tags: { name } })
  if (pending) {
    const status = record('job_status', http.get(`${base}/api/jobs/${pending}`, params('job_status')), 200)
    if (status && ['succeeded', 'failed', 'cancelled'].includes(status.json('status'))) pending = null
  } else if (jobsPerUser && submitted < jobsPerUser && Date.now() >= nextSubmit) {
    const response = record('submit', http.post(`${base}/api/chats/${account.chat}/message`,
      JSON.stringify({ query: 'Nike running shoes under Rs. 3000', history: [] }),
      { headers: { ...headers, 'Idempotency-Key': `k6-${__VU}-${submitted}-${__ITER}` }, tags: { name: 'submit' } }), 202)
    if (response) { pending = response.json('job_id'); submitted += 1; submittedJobs.add(1) }
    nextSubmit = Date.now() + 5000
  }

  const page = http.batch([
    ['GET', `${base}/api/health`, null, params('health')],
    ['GET', `${base}/api/chats`, null, params('chats')],
    ['GET', `${base}/api/saved`, null, params('saved')],
    ['GET', `${base}/api/cart`, null, params('cart')],
    ['GET', `${base}/api/orders`, null, params('orders')],
    ['GET', `${base}/api/account/credits`, null, params('credits')],
    ['GET', `${base}/api/jobs/${account.probe}`, null, params('job_status')],
  ])
  const pageNames = ['health', 'chats', 'saved', 'cart', 'orders', 'credits', 'job_status']
  for (let index = 0; index < pageNames.length; index++) record(pageNames[index], page[index], 200)
  sleep(5 + Math.random() * 5)
}

export function handleSummary(data) {
  return { [__ENV.SUMMARY]: JSON.stringify(data) }
}
