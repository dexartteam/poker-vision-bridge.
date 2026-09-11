import { spawnSync } from 'node:child_process';
const steps = {
  '01': ['tests/vision/test_api.py'],
  '02': ['tests/vision/test_contracts_reducer.py'],
  '03': ['tests/vision/test_contracts_reducer.py'],
  '04': ['tests/vision/test_scheduler.py'],
  '05': ['tests/vision/test_scheduler.py'],
  '06': ['tests/vision/test_provider.py'],
  '07': ['tests/vision/test_contracts_reducer.py'],
  '08': ['tests/vision/test_api.py'],
  '09': ['tests/vision/test_replay.py'],
  10: ['tests/vision/test_scheduler.py', 'tests/vision/test_api.py'],
  11: ['tests/vision'],
  12: ['tests'],
};
const step = process.argv[2];
if (!Object.hasOwn(steps, step)) {
  console.error('Specify an implemented step: 01…12. Unknown steps never pass.');
  process.exit(2);
}
function run(command, args) {
  const result = spawnSync(command, args, { stdio: 'inherit' });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
run('npm', ['run', 'typecheck']);
run('npm', ['run', 'lint']);
run('npm', ['run', 'check:architecture']);
// The small browser suite runs in full: camera, profile, real detector, and controller transport races.
run('npm', ['test']);
run('uv', ['run', 'pytest', '-q', ...steps[step]]);
if (step === '11') run('npm', ['run', 'benchmark']);
if (step === '12') {
  run('npm', ['run', 'build']);
  run('npm', ['run', 'smoke:production']);
}
console.log(
  `Step ${step}: automated checks passed. Real-camera and visual acceptance are tracked separately in docs/vision/PROGRESS.md.`,
);
