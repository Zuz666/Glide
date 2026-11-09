// Unofficial free online Edge speech through the pinned Python CLI. Text stays on stdin, never in arguments.
import { execFile } from "child_process";
import path from "path";
import { promisify } from "util";
import { pylibDir, pythonCommand } from "./paths";
import type { Voice } from "../shared/ipc";

const runPython = promisify(execFile);

function pythonContext() {
  const py = pythonCommand();
  const env: NodeJS.ProcessEnv = { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", PYTHONNOUSERSITE: "1" };
  if (py.bundled) {
    // Like the engine, don't mix the embedded runtime with a registered host Python or its packages.
    env.PYTHONHOME = path.dirname(py.cmd);
    env.PYTHONPATH = pylibDir();
  } else {
    env.PYTHONPATH = [pylibDir(), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  }
  return { py, env };
}

/** Fetch the service's catalogue instead of shipping a voice-name allowlist. */
export async function edgeVoices(signal: AbortSignal): Promise<Voice[]> {
  const { py, env } = pythonContext();
  try {
    const { stdout } = await runPython(py.cmd, [...py.args, "-c",
      "import asyncio,json,edge_tts; print(json.dumps(asyncio.run(edge_tts.list_voices())))"],
    { env, windowsHide: true, encoding: "utf8", signal, timeout: 15000, killSignal: "SIGKILL", maxBuffer: 2 * 1024 * 1024 });
    const catalogue: { ShortName: string; Gender: string }[] = JSON.parse(stdout);
    return catalogue.map((v) => ({ id: v.ShortName, name: `${v.ShortName} · ${v.Gender}` }))
      .sort((a, b) => a.id.localeCompare(b.id));
  } catch {
    throw new Error("Edge TTS: could not load voices; check the internet connection and edge-tts installation");
  }
}

export async function edgeSynth(text: string, voice: string, signal: AbortSignal): Promise<Buffer> {
  if (!voice) throw new Error("Edge TTS: choose a voice in Settings > Voice");
  const { py, env } = pythonContext();
  const request = runPython(py.cmd, [...py.args, "-m", "edge_tts", "--file", "-", "--voice", voice, "--write-media", "-"],
    { env, windowsHide: true, encoding: "buffer", signal, timeout: 15000, killSignal: "SIGKILL", maxBuffer: 16 * 1024 * 1024 });
  let inputError: Error | undefined;
  request.child.stdin?.on("error", (error: Error) => { inputError = error; });
  request.child.stdin?.end(text, "utf8");
  let stdout: Buffer;
  try {
    ({ stdout } = await request);
  } catch (error) {
    // Don't forward Python's traceback: it can contain request data. Keep actionable failures for the UI.
    const failure = error as Error & { code?: string | number; killed?: boolean; stderr?: Buffer };
    if (failure.code === "ERR_CHILD_PROCESS_STDIO_MAXBUFFER") throw new Error("Edge TTS: audio output exceeded the size limit");
    if (signal.aborted || failure.killed) throw new Error("Edge TTS: synthesis timed out");
    if (failure.code === "ENOENT") throw new Error("Edge TTS: Python runtime not found");
    if (failure.stderr?.includes("No module named edge_tts")) {
      throw new Error("Edge TTS: install edge-tts==7.3.1 in the Python runtime");
    }
    throw new Error(`Edge TTS: synthesis failed (${failure.code || "process error"}); check the internet connection and Python runtime`);
  }
  if (inputError) throw new Error("Edge TTS: could not send text to Python");
  if (!stdout.length) throw new Error("Edge TTS: no audio in the answer");
  return stdout;
}
