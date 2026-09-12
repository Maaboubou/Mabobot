# Web Console Architecture

This document is the migration contract for the Mabobot management console.
It keeps the existing runtime behavior authoritative while the UI and
configuration layers are replaced.

## Product model

The console is organized around operator tasks, not backend modules:

1. **Overview** — the duty desk: a single status band (WeChat connection,
   listener count, last activity, background tasks, disk plus hardware
   temperature and host uptime), a five-second liveness pulse (a dot plus
   "messages in the last five minutes" beside the event timeline, and a
   seconds-level relative time on the status band's last-message fact), an
   attention queue that only appears when something needs a human, today's
   KPIs with seven-day sparklines, 24-hour activity, Codex quota/usage and a
   system-level event timeline. Raw chat traffic and configuration cards do
   not belong here; every number links to the page that owns it.

   The page refreshes as a whole every 30 seconds; only the pulse runs on its
   own five-second timer (`GET /api/dashboard/pulse`, an in-memory read of the
   chat-log index that may rescan sooner than the trend window). It stops when
   the tab is switched away or the document is hidden, keeps the previous
   reading and marks it stale when a request fails, and drops every animation
   under `prefers-reduced-motion`. Below 1199.98px the two columns stack and
   each panel returns to block layout — keeping the subgrid there let the
   chart's `min-width` widen the implicit column and clip the right half of
   both activity panels.
2. **Chats** — group/private-chat configuration and effective capabilities.
3. **AI Assistant** — first-class Chatbot configuration, roles, Judge, models,
   chat archives and diagnostics.
4. **Automations** — all other user-facing capabilities.
5. **AI Resources** — model connections, task routing, usage, Codex sessions,
   call diagnostics and network tools.
6. **Operations** — runtime logs and operational troubleshooting.
7. **System** — credentials, network, storage and lifecycle operations.

Plugin lifecycle controls and raw internal identifiers belong to an explicit
developer mode. They are not part of the default operator workflow.

## Configuration model

Configuration has three visible layers:

```text
system dependency -> capability global default -> chat override
```

Every chat-level override must expose its effective value and source. Removing
an override resumes the global default. Runtime-only state is not presented as
persisted configuration.

The capability service is the compatibility boundary over existing plugin
manifests. Its public descriptors normalize legacy types, group fields, redact
secrets and hide storage layout from the browser. Migrated manifests may add:

- `title`
- `group`
- `scope`
- `level` (`basic`, `advanced`, `developer`)
- `control`
- `sensitive`
- validation metadata
- dependency/visibility metadata
- apply/restart behavior

The old `enabled_chats` field is compatibility-only. Chat assignment is owned
by the Chats domain.

### Implemented plugin chat configuration

Plugin fields opt in with `config_schema.<key>.scope: "global_and_chat"`;
sensitive fields are excluded. Overrides are keyed by database chat ID and
plugin ID in `chat_plugin_configs`, independently of grants. A partial chat
policy PATCH saves overrides atomically with any other submitted policy fields
using the existing chat version. Runtime API v2 exposes
`context.config.resolve(chat_id=...)` for a fresh effective configuration.
Listener declarations and execution order remain global.

Named templates in `plugin_config_templates` store complete snapshots of the
overridable fields and have their own optimistic version. Importing copies
values into the editor; template changes never propagate to chats.

The translation editor saves directly through the chat policy endpoint and
updates the parent form's version without discarding other unsaved fields.
The generic plugin editor still stages a draft for the chat page to save.
These UI paths share the same backend contract. See the
[plugin chat configuration guide](PLUGIN_CHAT_CONFIGURATION.md) for field
declarations, runtime examples, endpoints and validation boundaries.

## Routing contract

Every main view has a stable URL and supports refresh, deep links and browser
history. The implemented routes are:

```text
/
/chats
/assistant
/assistant/chats
/assistant/roles
/automations
/ai
/ai/models
/ai/mappings
/ai/usage
/ai/sessions
/ai/calls
/ai/network
/operations
/operations/logs
/system
/system/integrations
/system/runtime
/system/developer
```

模型供应商、API 地址、共享凭据和代理的编辑入口是 `/ai/models`。

Contextual entries route to the same editor. They must not create duplicate
forms or duplicate field definitions.

## Visual and theme contract

The console uses a warm editorial system: a cream canvas, coral primary
actions, warm ink text, hairline borders and dark product surfaces. Product
headings use a serif display stack while controls and body copy use the
sans-serif UI stack. Color must be referenced through semantic CSS variables;
page-level features must not introduce a separate palette.

`docs/UI_DESIGN_STANDARD.md` is the authoritative UI standard for tokens,
component shapes, interaction feedback and copy. This document stays
authoritative for information architecture, routing and configuration layers.

Light and dark themes are first-class. The browser restores `mabobot.colorTheme`
before loading styles to avoid a theme flash. If no choice is stored, the
system preference is used. The page-header switch persists an explicit light
or dark choice, updates Bootstrap's `data-bs-theme`, and keeps native controls
in the matching color scheme.

## Compatibility and removal rules

- Existing FastAPI behavior, plugin IDs, database rows and config files remain
  valid until their replacement path is verified.
- New APIs initially adapt existing storage rather than migrating it in place.
- A legacy renderer or endpoint is removed only after its replacement covers
  all callers and regression checks prove equivalent behavior.
- Plugin-specific form branches are temporary migration code. The end state is
  one settings renderer plus intentional domain components for Chatbot.
- Raw config dumps, duplicate Chatbot forms and DOM-only navigation are removal
  targets, not permanent compatibility surfaces.

## Security boundary for the LAN phase

Authentication is intentionally deferred while the console is LAN-only. The
remaining safeguards are still required:

- bind scope must be explicit in deployment documentation;
- management APIs are same-origin by default; a separate frontend must use an
  explicit `WEB_CORS_ORIGINS` allowlist;
- public APIs never return raw secrets;
- all dynamic content is escaped;
- configuration mutations are validated and atomic;
- a future authentication middleware can be introduced without changing page
  or capability contracts.

## Completion gates

- A new chat can enable Chatbot, choose role/model and set trigger behavior in
  one continuous workflow.
- Every setting shows its effective value and inheritance source.
- Common workflows do not expose raw keys or JSON.
- Routes survive refresh and browser back/forward navigation.
- Light and dark themes cover the shell, forms, modals and task workspaces.
- All plugin settings use normalized descriptors.
- Legacy duplicate renderers and unused CSS/HTML are removed.
- Existing backend tests plus Web contract and critical-flow tests pass.
- No management API response exposes stored credentials.
