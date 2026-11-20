// Grade results/*.jsonl with the TOON benchmark's own deterministic comparator.
// Usage: node grade.ts results/seed-12345/*.jsonl  -> writes *.graded.jsonl
import * as fs from 'node:fs'
import process from 'node:process'

const TOON = process.env.TOON_REPO ?? new URL('../../toon-repo', import.meta.url).pathname
const { compareAnswers } = await import(`${TOON}/benchmarks/src/normalize.ts`)

for (const file of process.argv.slice(2).filter(f => !f.endsWith('.graded.jsonl'))) {
  const lines = fs.readFileSync(file, 'utf8').split('\n').filter(Boolean)
  const graded = lines.map((l) => {
    const r = JSON.parse(l)
    const res = r.error
      ? { match: false }
      : compareAnswers(r.actual ?? '', r.expected, r.answerType ?? 'string', r.normalizationOptions ?? undefined)
    return JSON.stringify({ ...r, isCorrect: res.match })
  })
  fs.writeFileSync(file.replace(/\.jsonl$/, '.graded.jsonl'), `${graded.join('\n')}\n`)
}
