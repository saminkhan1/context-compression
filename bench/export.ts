// Export TOON-benchmark datasets, questions and reference encodings to JSON so
// the Python side can add candidate formats and run models without the AI SDK.
// Usage: BENCH_SEED=12345 node export.ts <outdir>
import * as fs from 'node:fs'
import * as path from 'node:path'
import process from 'node:process'

const TOON = process.env.TOON_REPO ?? new URL('../../toon-repo', import.meta.url).pathname
const { ACCURACY_DATASETS, TOKEN_EFFICIENCY_DATASETS } = await import(`${TOON}/benchmarks/src/datasets.ts`)
const { FORMATS, supportsCSV } = await import(`${TOON}/benchmarks/src/formats.ts`)
const { generateQuestions } = await import(`${TOON}/benchmarks/src/questions/index.ts`)
const { encodeDataset } = await import(`${TOON}/benchmarks/src/structural-corruption.ts`)

const out = process.argv[2]
fs.mkdirSync(out, { recursive: true })

function dump(datasets: typeof ACCURACY_DATASETS, file: string) {
  const rows = datasets.map((d, i) => ({
    name: d.name,
    key: d.corruption ? `${d.name}:${d.corruption.kind}:${i}` : d.name,
    corruption: d.corruption?.kind ?? null,
    supportsCSV: supportsCSV(d),
    structureClass: d.metadata.structureClass,
    data: d.data,
    encodings: Object.fromEntries(
      Object.values(FORMATS)
        .filter(f => f.name !== 'csv' || supportsCSV(d))
        .map(f => [f.name, encodeDataset(f, d)]),
    ),
  }))
  fs.writeFileSync(path.join(out, file), JSON.stringify(rows))
}

dump(ACCURACY_DATASETS, 'accuracy-datasets.json')
dump(TOKEN_EFFICIENCY_DATASETS as any, 'token-datasets.json')
fs.writeFileSync(path.join(out, 'questions.json'), JSON.stringify(generateQuestions()))
fs.writeFileSync(path.join(out, 'formats.json'), JSON.stringify(
  Object.fromEntries(Object.values(FORMATS).map(f => [f.name, { primer: f.primer, fence: f.fence }])),
))
console.log('ok', out)
