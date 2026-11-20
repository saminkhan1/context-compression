// Large-regime export: the TOON benchmark's own question generators applied to its own
// TOKEN_EFFICIENCY_DATASETS (up to 2000 records). Writes the same layout as export.ts so
// run.py works unchanged. Usage: BENCH_SEED=777 node export_large.ts <outdir>
import * as fs from 'node:fs'
import * as path from 'node:path'
import process from 'node:process'

const TOON = process.env.TOON_REPO ?? new URL('../../toon-repo', import.meta.url).pathname
const Q = `${TOON}/benchmarks/src/questions`
const { TOKEN_EFFICIENCY_DATASETS } = await import(`${TOON}/benchmarks/src/datasets.ts`)
const { FORMATS, supportsCSV } = await import(`${TOON}/benchmarks/src/formats.ts`)
const { createIdGenerator } = await import(`${Q}/utils.ts`)
const g = {
  tabular: (await import(`${Q}/tabular.ts`)).generateTabularQuestions,
  nested: (await import(`${Q}/nested.ts`)).generateNestedQuestions,
  analytics: (await import(`${Q}/analytics.ts`)).generateAnalyticsQuestions,
  github: (await import(`${Q}/github.ts`)).generateGithubQuestions,
  eventLogs: (await import(`${Q}/event-logs.ts`)).generateEventLogsQuestions,
  config: (await import(`${Q}/nested-config.ts`)).generateNestedConfigQuestions,
  keyed: (await import(`${Q}/keyed.ts`)).generateKeyedQuestions,
  group: (await import(`${Q}/nested-group.ts`)).generateNestedGroupQuestions,
  structure: (await import(`${Q}/structure.ts`)).generateStructureQuestions,
}

const out = process.argv[2]
fs.mkdirSync(out, { recursive: true })
const by = (n: string) => TOKEN_EFFICIENCY_DATASETS.find((d: any) => d.name === n)!.data
const idGen = createIdGenerator()
const getId = () => idGen.next().value
const tabular = by('tabular').employees
const nested = by('nested').orders
const analytics = by('analytics').metrics
const github = by('github').repositories
const logs = by('event-logs').logs
const questions = [
  ...g.tabular(tabular, getId),
  ...g.nested(nested, getId),
  ...g.analytics(analytics, getId),
  ...g.github(github, getId),
  ...g.eventLogs(logs, getId),
  ...g.config(by('nested-config'), getId),
  ...g.keyed(by('keyed').flags, getId),
  ...g.group(by('nested-group').contacts, getId),
  ...g.structure(tabular, nested, analytics, github, logs, getId),
]
const rows = TOKEN_EFFICIENCY_DATASETS.map((d: any) => ({
  name: d.name,
  key: d.name,
  corruption: null,
  supportsCSV: supportsCSV(d),
  structureClass: d.metadata.structureClass,
  data: d.data,
  encodings: Object.fromEntries(
    Object.values(FORMATS)
      .filter((f: any) => f.name !== 'csv' || supportsCSV(d))
      .map((f: any) => [f.name, f.encode(d.data)]),
  ),
}))
fs.writeFileSync(path.join(out, 'accuracy-datasets.json'), JSON.stringify(rows))
fs.writeFileSync(path.join(out, 'questions.json'), JSON.stringify(questions))
fs.writeFileSync(path.join(out, 'formats.json'), JSON.stringify(
  Object.fromEntries(Object.values(FORMATS).map((f: any) => [f.name, { primer: f.primer, fence: f.fence }])),
))
console.log('ok', questions.length, 'questions')
