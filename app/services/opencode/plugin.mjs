// Managed production bridge for the pinned, retain-patched OpenCode runtime.
// This module is loaded by OpenCode. It never imports the Python host.
import { Plugin } from "@opencode/plugin/effect"
import { Effect } from "effect"
import fs from "node:fs"
import net from "node:net"
import path from "node:path"
import { createHash, randomUUID } from "node:crypto"

const VERSION = "1.0.0"
const terminal = new Set(["completed", "failed", "cancelled", "unknown"])
const canonical = (value) => value && typeof value === "object"
  ? Array.isArray(value) ? value.map(canonical)
    : Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]))
  : value
const hash = (value) => createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex")

export default Plugin.define({
  id: "mabobot-host",
  effect: (ctx) => Effect.gen(function* () {
    const root = process.env.MABOBOT_OPENCODE_RUNTIME
    if (!root || ctx.location.directory !== path.join(root, "control")) return
    if (ctx.app.version !== "2.3.0+mabobot.p0.1") throw new Error("unsupported OpenCode version")
    const run = Effect.runPromiseWith(yield* Effect.context())
    const generation = randomUUID()
    // Only a trusted host launch can select the single real test connection/model.
    const liveModel = process.env.MABOBOT_OPENCODE_MODEL
    const liveVariant = process.env.MABOBOT_OPENCODE_VARIANT
    const state = path.join(root, "bridge")
    const socketPath = path.join(state, "bridge.sock")
    const journal = path.join(state, "attempts.json")
    fs.mkdirSync(state, { recursive: true, mode: 0o700 })
    if ((fs.statSync(state).mode & 0o077) !== 0) throw new Error("bridge directory must be private")
    const ledger = Object.assign(Object.create(null), fs.existsSync(journal) ? JSON.parse(fs.readFileSync(journal, "utf8")) : {})
    const active = new Map()
    const bindings = new Map()
    const chatBindings = new Map()
    const allowedModels = JSON.parse(process.env.MABOBOT_OPENCODE_MODELS || "[]")
    const profile = process.env.MABOBOT_OPENCODE_PROFILE
    const runtimeID = process.env.MABOBOT_OPENCODE_GENERATION
    if (!profile || !runtimeID || !allowedModels.length) throw new Error("missing host binding")
    const persist = () => {
      const tmp = journal + ".new"
      const fd = fs.openSync(tmp, "w", 0o600)
      try { fs.writeFileSync(fd, JSON.stringify(ledger)); fs.fsyncSync(fd) } finally { fs.closeSync(fd) }
      fs.renameSync(tmp, journal)
      const directory = fs.openSync(state, "r")
      try { fs.fsyncSync(directory) } finally { fs.closeSync(directory) }
    }
    for (const row of Object.values(ledger)) {
      if (!terminal.has(row.status)) row.status = "unknown"
    }
    persist()
    const requireBinding = (event) => {
      const row = bindings.get(event.sessionID)
      if (!row || row.status !== "running") throw new Error("unbound or inactive strict session")
      if (["id", "providerID", "variant"].some((key) => event.model[key] !== row.request.model[key]))
        throw new Error("resolved model mismatch: " + JSON.stringify(event.model))
      return row
    }
    yield* ctx.session.hook("generate", (event) => Effect.sync(() => {
      const row = requireBinding(event)
      event.system = [{ text: row.request.instructions }]
      event.messages = [{ role: "user", content: [{ type: "text", text: row.request.input }] }]
      event.tools = {}
      event.options = {
        ...(row.request.max_tokens === null ? {} : { maxTokens: row.request.max_tokens }),
        ...(row.request.temperature === null ? {} : { temperature: row.request.temperature }),
      }
    }))
    // Fail closed for any control-directory call not dispatched by this bridge.
    yield* ctx.session.hook("model.request", (event) => Effect.sync(() => {
      const chat = chatBindings.get(event.sessionID)
      if (event.kind === "primary" && chat?.active) {
        if (chat.model && ["id", "providerID", "variant"].some((key) => event.model[key] !== chat.model[key]))
          throw new Error("chat model binding mismatch")
        return
      }
      if (event.kind !== "generate") throw new Error("agent loop/title/compaction forbidden")
      requireBinding(event)
    }))
    yield* ctx.session.hook("title", (event) => Effect.sync(() => { event.result = "Mabobot conversation" }))
    yield* ctx.session.hook("context", (event) => Effect.sync(() => {
      const binding = chatBindings.get(event.sessionID)
      if (!binding?.active) throw new Error("unbound chat session")
      event.system = [{ text: binding.instructions }]
      // Stable MCP catalog, deterministic per-session exposure. Execution is checked again by MCP.
      event.tools = Object.fromEntries(Object.entries(event.tools).filter(([name]) => binding.tools.includes(name)))
    }))
    yield* ctx.session.hook("experimental.ws.handshake", () => Effect.die(new Error("unverified transport")))
    // Check the converted HTTP request, not merely the earlier hook object.
    // Keep only structure and hashes in diagnostics; never record task text.
    yield* ctx.session.hook("http.request", (event) => Effect.promise(async () => {
      const chat = chatBindings.get(event.sessionID)
      if (chat?.active) {
        const body = await event.request.clone().json()
        if ((chat.model && body.model !== chat.model.id) || (body.tools ?? []).some((tool) => !chat.tools.includes(tool.function?.name ?? tool.name)))
          throw new Error("chat outbound tool/model policy violated")
        return
      }
      const row = requireBinding(event)
      if ((row.outbound_count ?? 0) >= 1) throw new Error("host attempt budget exhausted")
      row.outbound_count = 1
      persist()
      const body = await event.request.clone().json()
      const text = row.request.input
      const rules = row.request.instructions
      const validMessages = Array.isArray(body.messages) && body.messages.length === 2
        && body.messages[0].role === "system" && body.messages[0].content === rules
        && body.messages[1].role === "user" && body.messages[1].content === text
      const validInput = body.instructions === rules && Array.isArray(body.input) && body.input.length === 1
        && body.input[0].role === "user" && body.input[0].content?.length === 1
        && body.input[0].content[0].type === "input_text" && body.input[0].content[0].text === text
      if ((!validMessages && !validInput) || (body.tools?.length ?? 0) !== 0 || body.model !== row.request.model.id)
        throw new Error("strict outbound contract violated")
      if (row.request.model.providerID === "openai" && row.request.model.variant !== "default"
        && body.reasoning?.effort !== row.request.model.variant) throw new Error("reasoning mapping mismatch")
      row.outbound_shape = { verified: true, model: body.model, fields: Object.keys(body).sort(),
        instruction_hash: hash(rules), input_hash: hash(text), tools: [], transport: "http",
        reasoning_effort: body.reasoning?.effort ?? null }
      persist()
    }))
    yield* ctx.session.hook("http.response", (event) => Effect.sync(() => {
      if (chatBindings.get(event.sessionID)?.active) return
      const row = requireBinding(event)
      row.http_status = event.response.status
      const retry = event.response.headers.get("retry-after")
      if (retry) row.retry_after_seconds = Math.max(0, Math.min(3600,
        Number.isFinite(Number(retry)) ? Number(retry) : (Date.parse(retry) - Date.now()) / 1000))
      persist()
      if (!event.response.body) return
      const streaming = (event.response.headers.get("content-type") ?? "").includes("text/event-stream")
      const decoder = new TextDecoder()
      let buffered = ""
      const inspect = (line) => {
        try {
          const data = JSON.parse(line.startsWith("data:") ? line.slice(5).trim() : line)
          if (data.choices?.some((choice) => choice.finish_reason === "length")
            || data.type === "response.incomplete" || data.status === "incomplete") row.output_truncated = true
        } catch { /* Transport/parser failures are classified by the native engine. */ }
      }
      const response = event.response
      event.response = new Response(response.body.pipeThrough(new TransformStream({
        transform(chunk, controller) {
          controller.enqueue(chunk)
          buffered += decoder.decode(chunk, { stream: true })
          if (streaming) {
            let newline
            while ((newline = buffered.indexOf("\n")) >= 0) {
              inspect(buffered.slice(0, newline))
              buffered = buffered.slice(newline + 1)
            }
          }
          if (buffered.length > 2097152) buffered = ""
        },
        flush() { buffered += decoder.decode(); if (buffered) inspect(buffered); persist() },
      })), { status: response.status, statusText: response.statusText, headers: response.headers })
    }))

    const cleanup = async (row) => {
      if (!row.session_id || row.cleaned) return
      // Only sessions created and owned by this ledger may be removed.
      try {
        const session = await run(ctx.session.get({ sessionID: row.session_id }))
        if (session.metadata?.mabobot_attempt !== row.request.attempt_id) throw new Error("session ownership mismatch")
        await run(ctx.session.remove({ sessionID: row.session_id }))
        bindings.delete(row.session_id)
        row.cleaned = true
      } catch { row.cleaned = false }
      persist()
    }
    const execute = async (row, controller) => {
      const timer = setTimeout(() => controller.abort(), Math.max(0, row.request.deadline - Date.now()))
      try {
        const session = await run(ctx.session.create({
          title: "Mabobot direct generation",
          agent: "direct-runner", model: row.request.model,
          metadata: { mabobot_attempt: row.request.attempt_id },
        }), { signal: controller.signal })
        row.session_id = session.id
        bindings.set(session.id, row)
        persist()
        const result = await run(ctx.session.generate({ sessionID: session.id, prompt: row.request.input }), {
          signal: controller.signal,
        })
        if (controller.signal.aborted) row.status = "cancelled"
        else { row.status = row.output_truncated ? "failed" : "completed"; row.result = result }
      } catch (error) {
        row.status = controller.signal.aborted ? "cancelled" : "failed"
        row.error = controller.signal.aborted ? "local_interruption_remote_usage_unknown" : String(error).slice(0, 500)
      } finally {
        row.error_code = row.output_truncated ? "output_truncated" : row.http_status === 429 ? "rate_limited"
          : [401, 403].includes(row.http_status) ? "authentication_failed" : row.status === "failed" ? "native_failed" : null
        clearTimeout(timer)
        active.delete(row.request.attempt_id)
        persist()
      }
    }
    const validate = (request) => {
      const keys = ["attempt_id", "task_id", "run_id", "snapshot_version", "instructions", "input", "model", "deadline", "max_tokens", "temperature"]
      if (!request || Object.keys(request).some((key) => !keys.includes(key))) throw new Error("unknown request field")
      for (const key of keys.slice(0, 6)) {
        if (typeof request[key] !== "string" || !request[key]) throw new Error("missing " + key)
      }
      if (!/^[a-zA-Z0-9_-]{1,100}$/.test(request.attempt_id)) throw new Error("invalid attempt id")
      if (!request.model || !allowedModels.some((model) => ["id", "providerID", "variant"].every((key) => request.model[key] === model[key]))
        || Object.keys(request.model).some((key) => !["id", "providerID", "variant"].includes(key))) throw new Error("unverified model")
      if (!Number.isFinite(request.deadline) || request.deadline > Date.now() + 3600000) throw new Error("invalid deadline")
      if (!(liveModel && request.max_tokens === null)
        && (!Number.isInteger(request.max_tokens) || request.max_tokens < 1 || request.max_tokens > 4096)) throw new Error("invalid output limit")
      if (!(liveModel && request.temperature === null)
        && (!Number.isFinite(request.temperature) || request.temperature < 0 || request.temperature > 2)) throw new Error("invalid temperature")
    }
    const dispatch = async (message) => {
      if (message.method === "capabilities") return {
        protocol: 1, adapter: VERSION, engine: ctx.app.version, generation, pid: process.pid,
        profile, runtime_id: runtimeID, strict_generation: true, experimental_bridge: false,
        transport: "http", credential_source: liveModel ? "opencode_native" : "none",
      }
      if (message.method === "inventory") {
        const response = await run(ctx.model.list())
        const models = response.data ?? response
        const connection = await run(ctx.integration.connection.active("openai"))
        return { authenticated: !!connection, method: connection?.method,
          models: models.filter((model) => model.providerID === "openai" && model.enabled).map((model) => ({
            id: model.id, variants: model.variants?.map((variant) => variant.id) ?? [],
          })) }
      }
      if (message.method === "bind_chat") {
        if (!/^ses_/.test(message.session_id) || chatBindings.has(message.session_id)) throw new Error("session already bound or invalid")
        if (!Array.isArray(message.tools) || message.tools.some((name) => typeof name !== "string" || !name.startsWith("mabobot_"))) throw new Error("invalid host tool policy")
        if (typeof message.instructions !== "string") throw new Error("missing host instructions")
        if (message.model && !allowedModels.some((model) => ["id", "providerID", "variant"].every((key) => message.model[key] === model[key]))) throw new Error("unverified chat model")
        chatBindings.set(message.session_id, { active: true, tools: message.tools, instructions: message.instructions, model: message.model })
        return { bound: true, generation }
      }
      if (message.method === "retire_chat") {
        const binding = chatBindings.get(message.session_id)
        if (!binding) throw new Error("unknown chat session")
        binding.active = false
        return { retired: true }
      }
      const id = message.attempt_id ?? message.request?.attempt_id
      if (message.method === "submit") {
        validate(message.request)
        const digest = hash(message.request)
        if (Object.hasOwn(ledger, id)) {
          if (ledger[id].digest !== digest) throw new Error("attempt digest conflict")
          return ledger[id]
        }
        if (message.request.deadline <= Date.now()) throw new Error("deadline expired")
        if (Object.keys(ledger).length >= 10000) throw new Error("bridge journal capacity reached; host retention required")
        const row = { digest, request: message.request, status: "running", generation, acked: false }
        ledger[id] = row
        persist() // durable acceptance BEFORE any upstream request
        const controller = new AbortController()
        const operation = { controller, done: undefined }
        active.set(id, operation)
        operation.done = execute(row, controller)
        return row
      }
      if (!Object.hasOwn(ledger, id)) throw new Error("unknown attempt")
      const row = ledger[id]
      if (message.method === "cancel") {
        active.get(id)?.controller.abort()
        return { ...row, cancel_requested: true } // still running until Effect actually settles
      }
      if (message.method === "ack") {
        if (!terminal.has(row.status)) throw new Error("attempt not terminal")
        row.acked = true
        persist()
        await cleanup(row)
        // Retain the tombstone; a full ledger fails closed instead of replaying IDs.
      } else if (message.method !== "status") throw new Error("unknown method")
      return row
    }
    // A stale socket is removed only inside the private, harness-owned directory.
    if (fs.existsSync(socketPath)) fs.unlinkSync(socketPath)
    const clients = new Set()
    const server = net.createServer((socket) => {
      socket.setEncoding("utf8")
      clients.add(socket)
      socket.on("close", () => clients.delete(socket))
      socket.on("error", () => {})
      socket.setTimeout(10000, () => socket.destroy())
      let buffer = ""
      socket.on("data", (chunk) => {
        buffer += chunk
        if (Buffer.byteLength(buffer) > 2097152) return socket.destroy()
        const newline = buffer.indexOf("\n")
        if (newline < 0) return
        socket.removeAllListeners("data")
        socket.setTimeout(40000, () => socket.destroy())
        Promise.resolve().then(() => dispatch(JSON.parse(buffer.slice(0, newline)))).then(
          (result) => socket.end(JSON.stringify({ result }) + "\n"),
          (error) => socket.end(JSON.stringify({ error: String(error.message) }) + "\n"),
        )
      })
    })
    yield* Effect.addFinalizer(() => Effect.promise(async () => {
      const operations = [...active.values()]
      for (const operation of operations) operation.controller.abort()
      await Promise.allSettled(operations.map((operation) => operation.done))
      for (const socket of clients) socket.destroy()
      await new Promise((resolve) => server.close(resolve))
      if (fs.existsSync(socketPath)) fs.unlinkSync(socketPath)
    }))
    yield* Effect.promise(() => new Promise((resolve, reject) => {
      server.once("error", reject)
      server.listen(socketPath, () => { fs.chmodSync(socketPath, 0o600); resolve() })
    }))
  }),
})
