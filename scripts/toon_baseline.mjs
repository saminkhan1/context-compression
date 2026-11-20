#!/usr/bin/env node
// Benchmark-only TOON encoder. Runtime hooks must not depend on this script.

import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const INSTRUCTION = "TOON encoded JSON. Interpret this as a lossless TOON "
  + "representation of the original parsed data.";
const USAGE = "Usage: toon_baseline.mjs [--no-instruction] "
  + "[--fallback-raw-on-fail] <input.json|jsonl|csv|tsv> <output>";

async function main() {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      "no-instruction": { type: "boolean" },
      "fallback-raw-on-fail": { type: "boolean" },
    },
  });
  if (positionals.length !== 2) {
    console.error(USAGE);
    process.exit(2);
  }
  const [inputPath, outputPath] = positionals;
  const toon = await loadToonModule();
  const raw = fs.readFileSync(inputPath, "utf8").replace(/^﻿/, "");
  let output;
  try {
    const value = parseInput(raw, path.extname(inputPath).toLowerCase());
    const encoded = toon.encode(value);
    if (JSON.stringify(toon.decode(encoded)) !== JSON.stringify(value)) {
      throw new Error("TOON round-trip changed the parsed source value");
    }
    output = values["no-instruction"] ? encoded : `${INSTRUCTION}\n${encoded}`;
  } catch (error) {
    if (!values["fallback-raw-on-fail"]) {
      throw error;
    }
    output = values["no-instruction"]
      ? raw
      : `Raw source fallback because TOON did not round-trip this file.\n${raw}`;
    console.error(`TOON round-trip failed for ${inputPath}; wrote raw fallback`);
  }
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, `${output}\n`, "utf8");
}

async function loadToonModule() {
  try {
    return await import("@toon-format/toon");
  } catch (firstError) {
    // npm exec exposes packages through PATH rather than NODE_PATH.
    const binSuffix = `${path.sep}node_modules${path.sep}.bin`;
    const binDirs = (process.env.PATH || "")
      .split(path.delimiter)
      .filter((entry) => entry.endsWith(binSuffix));
    for (const binDir of binDirs) {
      const candidate = path.join(
        path.dirname(binDir), "@toon-format", "toon", "dist", "index.mjs",
      );
      if (fs.existsSync(candidate)) {
        return import(pathToFileURL(candidate).href);
      }
    }
    throw new Error(
      "Cannot import @toon-format/toon. Run with a local install or: "
        + "npm exec --yes --package @toon-format/toon@2.3.0 -- "
        + "node scripts/toon_baseline.mjs <input> <output>\n"
        + firstError.message,
    );
  }
}

function parseInput(text, ext) {
  if (ext === ".json") {
    return JSON.parse(text);
  }
  if (ext === ".jsonl") {
    return text.split(/\r?\n/).filter((line) => line.trim()).map((line) => JSON.parse(line));
  }
  if (ext === ".csv" || ext === ".tsv") {
    const rows = parseDelimitedRows(text, ext === ".tsv" ? "\t" : ",")
      .filter((row) => row.length > 1 || row[0] !== "");
    const [headers = [], ...dataRows] = rows;
    return dataRows.map((row) => Object.fromEntries(
      headers.map((header, index) => [header, row[index] ?? ""]),
    ));
  }
  throw new Error(`Unsupported input extension: ${ext}`);
}

// RFC 4180 rows; quoted cells may contain delimiters, quotes, and newlines.
function parseDelimitedRows(text, delimiter) {
  const rows = [];
  let row = [];
  let cell = "";
  let inQuotes = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (inQuotes) {
      if (char === '"' && text[index + 1] === '"') {
        cell += '"';
        index += 1;
      } else if (char === '"') {
        inQuotes = false;
      } else {
        cell += char;
      }
    } else if (char === '"') {
      inQuotes = true;
    } else if (char === delimiter) {
      row.push(cell);
      cell = "";
    } else if (char === "\n") {
      rows.push([...row, cell]);
      row = [];
      cell = "";
    } else if (char !== "\r") {
      cell += char;
    }
  }
  if (inQuotes) {
    throw new Error("Unclosed quoted delimited cell");
  }
  if (cell || row.length > 0) {
    rows.push([...row, cell]);
  }
  return rows;
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
