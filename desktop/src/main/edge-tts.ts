// Unofficial free online Edge speech through the pinned Python CLI. Text stays on stdin, never in arguments.
import { execFile } from "child_process";
import path from "path";
import { promisify } from "util";
import { pylibDir, pythonCommand } from "./paths";
import type { Voice } from "../shared/ipc";

const runPython = promisify(execFile);

/** Safe diagnostic fields only: never retain Python's traceback or a service URL with its query tokens. */
export class EdgeTtsError extends Error {
  constructor(message: string, readonly reason: string, readonly audioBytes: number,
              readonly exitCode?: string | number, readonly httpStatus?: number) {
    super(`Edge TTS: ${message}`);
    this.name = "EdgeTtsError";
  }
}

const PYTHON_FAILURE = /(?:^|\n)(?:[\w.]+\.)?(NoAudioReceived|WSServerHandshakeError|ClientConnectorCertificateError|ClientConnectorDNSError|ClientConnectorError|ServerDisconnectedError|ClientConnectionError|ClientPayloadError|TimeoutError|ConnectionResetError|UnexpectedResponse|UnknownResponse|WebSocketError):/g;

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
  if (!voice) throw new EdgeTtsError("choose a voice in Settings > Voice", "VoiceMissing", 0);
  const { py, env } = pythonContext();
  const request = runPython(py.cmd, [...py.args, "-m", "edge_tts", "--file", "-", "--voice", voice, "--write-media", "-"],
    { env, windowsHide: true, encoding: "buffer", signal, timeout: 15000, killSignal: "SIGKILL", maxBuffer: 16 * 1024 * 1024 });
  let audioBytes = 0;
  request.child.stdout?.on("data", (chunk: Buffer) => { audioBytes += chunk.length; });
  let inputError: Error | undefined;
  request.child.stdin?.on("error", (error: Error) => { inputError = error; });
  request.child.stdin?.end(text, "utf8");
  let stdout: Buffer;
  try {
    ({ stdout } = await request);
  } catch (error) {
    // Extract only known exception classes and numeric HTTP status. Tracebacks may contain text and tokens.
    const failure = error as Error & { code?: string | number; killed?: boolean; stderr?: Buffer };
    const exitCode = typeof failure.code === "number" ? failure.code
      : /^(?:E[A-Z_]+|ABORT_ERR)$/.test(failure.code || "") ? failure.code : undefined;
    if (failure.code === "ERR_CHILD_PROCESS_STDIO_MAXBUFFER") {
      throw new EdgeTtsError("audio output exceeded the size limit", "OutputLimit", audioBytes, exitCode);
    }
    if (signal.aborted || failure.killed) {
      throw new EdgeTtsError("synthesis timed out", "Timeout", audioBytes, exitCode);
    }
    if (failure.code === "ENOENT") throw new EdgeTtsError("Python runtime not found", "PythonMissing", audioBytes, exitCode);
    const stderr = failure.stderr?.toString("utf8") || "";
    if (stderr.includes("No module named edge_tts")) {
      throw new EdgeTtsError("install edge-tts==7.3.1 in the Python runtime", "ModuleMissing", audioBytes, exitCode);
    }
    const reason = [...stderr.matchAll(PYTHON_FAILURE)].at(-1)?.[1] || "ProcessError";
    const status = stderr.match(/WSServerHandshakeError:\s*([1-5]\d{2})\b/);
    const httpStatus = status ? Number(status[1]) : undefined;
    const message = reason === "NoAudioReceived" ? "service returned no audio (NoAudioReceived)"
      : httpStatus ? `WebSocket connection rejected (HTTP ${httpStatus})`
      : `synthesis failed (${reason}${exitCode === undefined ? "" : `, exit ${exitCode}`})`;
    throw new EdgeTtsError(message, reason, audioBytes, exitCode, httpStatus);
  }
  if (inputError) throw new EdgeTtsError("could not send text to Python", "InputError", audioBytes);
  if (!stdout.length) throw new EdgeTtsError("no audio in the answer", "EmptyAudio", 0);
  return stdout;
}
