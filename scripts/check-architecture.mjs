import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
const files = (dir) =>
  readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() && !['static', '__pycache__'].includes(e.name)
      ? files(join(dir, e.name))
      : e.isFile()
        ? [join(dir, e.name)]
        : [],
  );
const violations = [];
for (const file of files('app/vision').filter((f) => f.endsWith('.py'))) {
  const text = readFileSync(file, 'utf8');
  if (/(?:from|import)\s+app\.(?:coordinator|solver|openpoker|telegram|main|models)/.test(text))
    violations.push(file + ': legacy execution dependency');
}
for (const file of files('web').filter((f) => f.endsWith('.ts'))) {
  const text = readFileSync(file, 'utf8');
  if (/(?:OPENAI_API_KEY|api\.openai\.com|\/v1\/events|\/v1\/stream)/.test(text))
    violations.push(file + ': server or bridge boundary');
  if (
    file.includes('/core/') &&
    !file.endsWith('.test.ts') &&
    /\b(?:fetch|document|window|navigator)\b|from ['"].*(?:session|transport|capture|ui)\//.test(
      text,
    )
  )
    violations.push(file + ': core side effect');
}
if (violations.length) {
  console.error(violations.join('\n'));
  process.exit(1);
}
console.log('Architecture boundaries passed');
