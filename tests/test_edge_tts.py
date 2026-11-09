#!/usr/bin/env python3
"""Edge's real child-process boundary: classify failures, reject partial audio, and never expose stderr secrets.

A local Node child stands in for the Python CLI. No service, API key, user configuration or audio device is used.
"""
import os
import subprocess
import sys
import tempfile

import desktop_ts

if not desktop_ts.node or not desktop_ts.available():
    print("SKIP: needs node and desktop/node_modules")
    sys.exit(77)

RUN = r"""
const assert = require('node:assert/strict');
const path = require('node:path');
const main = process.argv[2];
let scenario = 'noaudio';
const secret = 'PRIVATE-TOKEN-DO-NOT-LOG';
const text = 'PRIVATE-PLAYER-TEXT-DO-NOT-LOG';
const child = `
  const scenario = process.argv[1];
  process.stdin.resume();
  process.stdin.on('end', () => {
    const secret = 'PRIVATE-TOKEN-DO-NOT-LOG';
    if (scenario === 'timeout') { setInterval(() => {}, 1000); return; }
    if (scenario === 'empty') return;
    if (scenario === 'partial') process.stdout.write(Buffer.from([1, 2, 3]));
    const error = scenario === 'noaudio' ? 'edge_tts.exceptions.NoAudioReceived: no audio'
      : scenario === 'http' ? 'aiohttp.client_exceptions.WSServerHandshakeError: 429, url=wss://service/?TrustedClientToken=' + secret
      : scenario === 'partial' ? 'aiohttp.client_exceptions.ServerDisconnectedError: ' + secret
      : scenario === 'module' ? 'python: No module named edge_tts'
      : 'ValueError: ' + secret;
    process.stderr.write('Traceback containing PRIVATE-PLAYER-TEXT-DO-NOT-LOG and ' + secret + '\\n' + error + '\\n', () => { process.exitCode = 1; });
  });
`;
const paths = path.join(main, 'paths.js');
require.cache[paths] = { id: paths, filename: paths, loaded: true, exports: {
  pylibDir: () => '',
  pythonCommand: () => ({ cmd: scenario === 'missing' ? path.join(process.argv[3], 'absent-python') : process.execPath,
    args: ['-e', child, '--', scenario], bundled: false }),
} };
const { edgeSynth, EdgeTtsError } = require(path.join(main, 'edge-tts.js'));

(async () => {
  const cases = [
    ['noaudio', 'NoAudioReceived'], ['http', 'WSServerHandshakeError'],
    ['partial', 'ServerDisconnectedError'], ['empty', 'EmptyAudio'],
    ['module', 'ModuleMissing'], ['missing', 'PythonMissing'],
    ['generic', 'ProcessError'], ['timeout', 'Timeout'],
  ];
  for (const [name, reason] of cases) {
    scenario = name;
    const signal = name === 'timeout' ? AbortSignal.timeout(200) : new AbortController().signal;
    const failure = await edgeSynth(text, 'ru-RU-SvetlanaNeural', signal).then(
      () => assert.fail('Failed or empty synthesis must not become playable audio'), e => e);
    assert(failure instanceof EdgeTtsError, name);
    assert.equal(failure.reason, reason, name);
    assert.equal(failure.audioBytes, name === 'partial' ? 3 : 0, name);
    if (name === 'http') assert.equal(failure.httpStatus, 429);
    if (name === 'partial' || name === 'noaudio') assert.equal(failure.exitCode, 1);
    const exposed = failure.message + failure.stack + JSON.stringify(failure);
    for (const forbidden of [secret, text, 'TrustedClientToken', 'wss://service']) {
      assert(!exposed.includes(forbidden), `${name}: leaked ${forbidden}`);
    }
  }
  console.log('OK: 8 Edge failure/privacy cases');
})().catch(e => { console.error(e); process.exitCode = 1; });
"""

with tempfile.TemporaryDirectory(prefix="glide-edge-tts-") as tmp:
    script = os.path.join(tmp, "check.cjs")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(RUN)
    result = subprocess.run([desktop_ts.node, script, os.path.join(desktop_ts.main(), "main"), tmp],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    print(result.stdout + result.stderr)
    assert result.returncode == 0, "Edge failure/privacy checks failed"
