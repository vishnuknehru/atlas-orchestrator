import React, { useCallback, useEffect, useState } from "react";
import {
  AgentConfigEntry,
  AgentRegistry,
  atlasApi,
  Billing,
  SettingsProject,
  SettingsResponse,
} from "./api";

const inputClass =
  "w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-sm text-white";
const labelClass = "block text-xs text-slate-400 mb-1";
const sectionClass = "bg-slate-900/70 border border-slate-800 rounded-lg p-4 space-y-4";
const BILLING_OPTIONS: Billing[] = ["subscription", "api_key", "local"];

const ADAPTER_LABELS: Record<string, string> = {
  claude_cli: "Claude Code CLI",
  codex_cli: "Codex CLI",
  manual_handoff: "Manual handoff (you run the tool, e.g. Antigravity)",
  openai_compatible_http: "Local model server (OpenAI-compatible HTTP)",
};

function emptyAgent(adapter: string): AgentConfigEntry {
  return {
    adapter,
    models: [],
    billing: adapter === "manual_handoff" ? "manual" : "subscription",
    provider: null,
    model_family: null,
    endpoint: null,
    allow_remote_endpoint: false,
    declared_family_required: false,
  };
}

/** Drop empty optional strings so the backend sees null, not "". */
function normaliseAgent(entry: AgentConfigEntry): AgentConfigEntry {
  const blankToNull = (v?: string | null) => (v && v.trim() ? v.trim() : null);
  return {
    ...entry,
    models: entry.models.map((m) => m.trim()).filter(Boolean),
    provider: blankToNull(entry.provider),
    model_family: blankToNull(entry.model_family),
    endpoint: blankToNull(entry.endpoint),
  };
}

function CredentialBadge({ configured }: { configured: boolean | null }): React.ReactElement {
  if (configured === null) {
    return <span className="text-xs text-slate-400">secret store unavailable</span>;
  }
  return configured ? (
    <span className="text-xs px-2 py-0.5 rounded border bg-emerald-900/60 text-emerald-300 border-emerald-700">
      Stored
    </span>
  ) : (
    <span className="text-xs px-2 py-0.5 rounded border bg-rose-900/60 text-rose-300 border-rose-700">
      Not stored
    </span>
  );
}

/** Write-only secret field: the value is sent once and the input is cleared; nothing is read back. */
function SecretForm({
  label,
  placeholder,
  submitLabel,
  disabled,
  onSubmit,
}: {
  label: string;
  placeholder: string;
  submitLabel: string;
  disabled?: boolean;
  onSubmit: (secret: string) => Promise<void>;
}): React.ReactElement {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await onSubmit(value);
      setValue("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="flex gap-2 items-end">
      <label className="flex-1">
        <span className={labelClass}>{label}</span>
        <input
          type="password"
          autoComplete="off"
          spellCheck={false}
          placeholder={placeholder}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          required
          disabled={disabled || busy}
          className={`${inputClass} font-mono`}
        />
      </label>
      <button
        type="submit"
        disabled={disabled || busy || !value.trim()}
        className="bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-white font-medium text-sm py-1.5 px-3 rounded"
      >
        {busy ? "Saving…" : submitLabel}
      </button>
    </form>
  );
}

function GithubCredentialCard({
  project,
  storeAvailable,
  onChanged,
  onError,
}: {
  project: SettingsProject;
  storeAvailable: boolean;
  onChanged: () => Promise<void>;
  onError: (msg: string) => void;
}): React.ReactElement {
  const [message, setMessage] = useState<string | null>(null);

  const save = async (secret: string) => {
    try {
      const res = await atlasApi.setGithubCredential(project.id, secret);
      setMessage(
        res.verified
          ? "Stored and verified with GitHub."
          : `Stored, but GitHub verification failed (${res.error_reason}). Check the token's repository access and permissions.`
      );
      await onChanged();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to store GitHub credential");
    }
  };

  const remove = async () => {
    try {
      await atlasApi.removeGithubCredential(project.id);
      setMessage("Removed. The project is blocked until a new token is stored.");
      await onChanged();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to remove GitHub credential");
    }
  };

  return (
    <div className="bg-slate-950 border border-slate-800 rounded p-3 space-y-3" data-testid={`gh-cred-${project.id}`}>
      <div className="flex items-center justify-between gap-2">
        <div>
          <div className="font-medium text-sm text-white">{project.name}</div>
          <div className="text-xs text-slate-400 font-mono">{project.github_repo}</div>
        </div>
        <div className="flex items-center gap-2">
          {project.blocked_reason && (
            <span className="text-xs text-rose-300 font-mono">BLOCKED({project.blocked_reason})</span>
          )}
          <CredentialBadge configured={project.github_credential_configured} />
        </div>
      </div>
      <SecretForm
        label="Fine-grained personal access token"
        placeholder="github_pat_…"
        submitLabel={project.github_credential_configured ? "Replace & verify" : "Save & verify"}
        disabled={!storeAvailable}
        onSubmit={save}
      />
      <div className="flex items-center justify-between gap-2">
        <p className="text-[11px] text-slate-400">
          Limit the token to this repository with contents: write, pull requests: write, checks: read
          and actions: read.
        </p>
        {project.github_credential_configured && (
          <button
            type="button"
            onClick={() => void remove()}
            className="text-xs text-rose-300 hover:text-rose-200 whitespace-nowrap"
          >
            Remove token
          </button>
        )}
      </div>
      {message && <p className="text-xs text-slate-300">{message}</p>}
    </div>
  );
}

function AgentFields({
  entry,
  adapters,
  onChange,
}: {
  entry: AgentConfigEntry;
  adapters: SettingsResponse["adapters"];
  onChange: (next: AgentConfigEntry) => void;
}): React.ReactElement {
  const isManual = entry.adapter === "manual_handoff";
  const set = (patch: Partial<AgentConfigEntry>) => onChange({ ...entry, ...patch });

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
      <label>
        <span className={labelClass}>Adapter</span>
        <select
          value={entry.adapter}
          onChange={(e) => {
            const adapter = e.target.value;
            let billing = entry.billing;
            if (adapter === "manual_handoff") billing = "manual";
            else if (billing === "manual") billing = "subscription";
            set({ adapter, billing });
          }}
          className={inputClass}
        >
          {adapters.map((a) => (
            <option key={a.name} value={a.name}>
              {ADAPTER_LABELS[a.name] ?? a.name}
            </option>
          ))}
        </select>
      </label>
      {isManual && (
        <div>
          <span className={labelClass}>Billing</span>
          <p className="text-xs text-slate-300 bg-slate-900 border border-slate-800 rounded px-3 py-1.5">
            manual: you run the tool yourself on your own subscription, then import its result. ATLAS never
            launches it (Antigravity: ADR-003).
          </p>
        </div>
      )}
      {!isManual && (
        <label>
          <span className={labelClass}>Billing</span>
          <select
            value={entry.billing}
            onChange={(e) => set({ billing: e.target.value as Billing })}
            className={inputClass}
          >
            {BILLING_OPTIONS.map((b) => (
              <option key={b} value={b}>
                {b}
              </option>
            ))}
          </select>
        </label>
      )}
      {!isManual && (
        <label>
          <span className={labelClass}>Models (comma-separated, first is primary)</span>
          <input
            type="text"
            value={entry.models.join(",")}
            onChange={(e) => set({ models: e.target.value.split(",") })}
            className={`${inputClass} font-mono`}
          />
        </label>
      )}
      <label>
        <span className={labelClass}>Provider {isManual ? "(required)" : "(optional)"}</span>
        <input
          type="text"
          value={entry.provider ?? ""}
          placeholder={isManual ? "google or local" : "derived from adapter"}
          onChange={(e) => set({ provider: e.target.value })}
          className={inputClass}
        />
      </label>
      <label>
        <span className={labelClass}>Model family (optional)</span>
        <input
          type="text"
          value={entry.model_family ?? ""}
          onChange={(e) => set({ model_family: e.target.value })}
          className={inputClass}
        />
      </label>
      <label>
        <span className={labelClass}>Endpoint (loopback unless remote is allowed)</span>
        <input
          type="text"
          value={entry.endpoint ?? ""}
          placeholder="http://127.0.0.1:11434/v1"
          onChange={(e) => set({ endpoint: e.target.value })}
          className={`${inputClass} font-mono`}
        />
      </label>
      {entry.endpoint && (
        <label className="flex items-center gap-2 text-xs text-amber-300">
          <input
            type="checkbox"
            checked={entry.allow_remote_endpoint}
            onChange={(e) => set({ allow_remote_endpoint: e.target.checked })}
          />
          Allow a remote endpoint (source code leaves this machine)
        </label>
      )}
      {isManual && (
        <label className="flex items-center gap-2 text-xs text-slate-300">
          <input
            type="checkbox"
            checked={entry.declared_family_required}
            onChange={(e) => set({ declared_family_required: e.target.checked })}
          />
          Require model family to be declared at import
        </label>
      )}
    </div>
  );
}

function AgentRegistryEditor({
  settings,
  onSaved,
  onError,
}: {
  settings: SettingsResponse;
  onSaved: () => Promise<void>;
  onError: (msg: string) => void;
}): React.ReactElement {
  const [draft, setDraft] = useState<AgentRegistry>(settings.registry);
  const [newId, setNewId] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const defaultAdapter = settings.adapters[0]?.name ?? "claude_cli";

  useEffect(() => setDraft(settings.registry), [settings.registry]);

  const dirty = JSON.stringify(draft) !== JSON.stringify(settings.registry);
  const update = (id: string, entry: AgentConfigEntry) => {
    setSaved(false);
    setDraft({ agents: { ...draft.agents, [id]: entry } });
  };
  const remove = (id: string) => {
    const { [id]: _removed, ...rest } = draft.agents;
    setSaved(false);
    setDraft({ agents: rest });
  };
  const add = (e: React.FormEvent) => {
    e.preventDefault();
    const id = newId.trim();
    if (!id) return;
    if (draft.agents[id]) {
      onError(`Agent ${id} already exists`);
      return;
    }
    update(id, emptyAgent(defaultAdapter));
    setNewId("");
  };
  const save = async () => {
    setSaving(true);
    try {
      const agents = Object.fromEntries(
        Object.entries(draft.agents).map(([id, entry]) => [id, normaliseAgent(entry)])
      );
      await atlasApi.updateAgents({ agents });
      setSaved(true);
      await onSaved();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to save agent registry");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className={sectionClass}>
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-white">Agents</h2>
          <p className="text-xs text-slate-400">
            Saved to ~/.atlas/agents.yaml. Every project&apos;s role assignments are re-checked on save.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {saved && !dirty && <span className="text-xs text-emerald-300">Saved</span>}
          {dirty && (
            <button
              type="button"
              onClick={() => setDraft(settings.registry)}
              className="text-xs text-slate-400 hover:text-slate-200"
            >
              Discard changes
            </button>
          )}
          <button
            type="button"
            onClick={() => void save()}
            disabled={!dirty || saving}
            className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white font-medium text-sm py-1.5 px-3 rounded"
          >
            {saving ? "Saving…" : "Save agents"}
          </button>
        </div>
      </div>

      <div className="space-y-3">
        {Object.entries(draft.agents).map(([id, entry]) => {
          const savedEntry = settings.registry.agents[id];
          const keyStatus = settings.agent_api_keys[id];
          return (
            <div key={id} className="bg-slate-950 border border-slate-800 rounded p-3 space-y-3">
              <div className="flex items-center justify-between">
                <span className="font-semibold text-sm text-white font-mono">{id}</span>
                <button
                  type="button"
                  onClick={() => remove(id)}
                  className="text-xs text-rose-300 hover:text-rose-200"
                >
                  Remove
                </button>
              </div>
              <AgentFields entry={entry} adapters={settings.adapters} onChange={(next) => update(id, next)} />
              {entry.billing === "api_key" && (
                <div className="border-t border-slate-800 pt-3 space-y-2">
                  {savedEntry?.billing === "api_key" ? (
                    <>
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-slate-400">API key</span>
                        <CredentialBadge configured={keyStatus ?? null} />
                        {keyStatus && (
                          <button
                            type="button"
                            onClick={() =>
                              void atlasApi
                                .removeAgentApiKey(id)
                                .then(onSaved)
                                .catch((err: unknown) =>
                                  onError(err instanceof Error ? err.message : "Failed to remove API key")
                                )
                            }
                            className="text-xs text-rose-300 hover:text-rose-200"
                          >
                            Remove key
                          </button>
                        )}
                      </div>
                      <SecretForm
                        label={`API key (stored as atlas.agent.${id})`}
                        placeholder="sk-…"
                        submitLabel={keyStatus ? "Replace key" : "Save key"}
                        disabled={!settings.secret_store_available}
                        onSubmit={async (secret) => {
                          try {
                            await atlasApi.setAgentApiKey(id, secret);
                            await onSaved();
                          } catch (err) {
                            onError(err instanceof Error ? err.message : "Failed to store API key");
                          }
                        }}
                      />
                    </>
                  ) : (
                    <p className="text-xs text-slate-400">Save the agents first, then add its API key here.</p>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <form onSubmit={add} className="flex gap-2 items-end border-t border-slate-800 pt-4">
        <label className="flex-1">
          <span className={labelClass}>New agent id</span>
          <input
            type="text"
            placeholder="e.g. local-reviewer"
            value={newId}
            onChange={(e) => setNewId(e.target.value)}
            className={`${inputClass} font-mono`}
          />
        </label>
        <button
          type="submit"
          className="bg-slate-700 hover:bg-slate-600 text-white font-medium text-sm py-1.5 px-3 rounded"
        >
          Add agent
        </button>
      </form>
    </section>
  );
}

function ProjectRolesCard({
  project,
  settings,
  onSaved,
  onError,
}: {
  project: SettingsProject;
  settings: SettingsResponse;
  onSaved: () => Promise<void>;
  onError: (msg: string) => void;
}): React.ReactElement {
  const [roles, setRoles] = useState<Record<string, string>>(project.roles);
  useEffect(() => setRoles(project.roles), [project.roles]);

  const allowedRoles = (agentId: string): string[] => {
    const adapter = settings.registry.agents[agentId]?.adapter;
    return settings.adapters.find((a) => a.name === adapter)?.allowed_roles ?? [];
  };
  const dirty = JSON.stringify(roles) !== JSON.stringify(project.roles);
  const evaluation = project.role_evaluation;

  const save = async () => {
    try {
      await atlasApi.updateProjectRoles(project.id, roles);
      await onSaved();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to save role assignments");
    }
  };

  return (
    <div className="bg-slate-950 border border-slate-800 rounded p-3 space-y-3">
      <div className="flex items-center justify-between">
        <span className="font-medium text-sm text-white">{project.name}</span>
        <button
          type="button"
          onClick={() => void save()}
          disabled={!dirty}
          className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white font-medium text-xs py-1 px-3 rounded"
        >
          Save roles
        </button>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        {settings.roles.map((role) => (
          <label key={role}>
            <span className={labelClass}>{role}</span>
            <select
              value={roles[role] ?? ""}
              onChange={(e) => setRoles({ ...roles, [role]: e.target.value })}
              className={inputClass}
            >
              {!settings.registry.agents[roles[role]] && (
                <option value={roles[role]}>{roles[role]} (missing)</option>
              )}
              {Object.keys(settings.registry.agents).map((agentId) => (
                <option key={agentId} value={agentId} disabled={!allowedRoles(agentId).includes(role)}>
                  {agentId}
                </option>
              ))}
            </select>
          </label>
        ))}
      </div>
      {evaluation.ok ? (
        <p className="text-xs text-emerald-300">Role assignments satisfy the independence rules.</p>
      ) : (
        <p className="text-xs text-rose-300">
          BLOCKED({evaluation.blocked_reason}): {evaluation.detail}
        </p>
      )}
      {evaluation.warnings.map((w) => (
        <p key={w} className="text-xs text-amber-300">
          {w}
        </p>
      ))}
    </div>
  );
}

function PolicyView({ config }: { config: SettingsResponse["global_config"] }): React.ReactElement {
  return (
    <section className={sectionClass}>
      <div>
        <h2 className="text-base font-semibold text-white">Policy</h2>
        <p className="text-xs text-slate-400">
          Read-only view of ~/.atlas/config.yaml. Safety-critical values are locked by ATLAS.
        </p>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {Object.entries(config).map(([section, values]) => (
          <div key={section} className="bg-slate-950 border border-slate-800 rounded p-3">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-2">{section}</h3>
            <dl className="space-y-1">
              {Object.entries(values).map(([key, value]) => (
                <div key={key} className="flex justify-between gap-2 text-xs">
                  <dt className="text-slate-400 font-mono">{key}</dt>
                  <dd className="text-slate-200 font-mono">{String(value)}</dd>
                </div>
              ))}
            </dl>
          </div>
        ))}
      </div>
    </section>
  );
}

export function Settings(): React.ReactElement {
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setSettings(await atlasApi.getSettings());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load settings");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-6">
      {error && (
        <div role="alert" className="bg-rose-950/80 border border-rose-800 text-rose-200 px-4 py-3 rounded text-sm">
          {error}
        </div>
      )}
      {!settings ? (
        <p className="text-sm text-slate-400">Loading settings…</p>
      ) : (
        <>
          {!settings.secret_store_available && (
            <div className="bg-amber-950/60 border border-amber-800 text-amber-200 px-4 py-3 rounded text-sm">
              No OS secret store is available (macOS Keychain or Linux Secret Service). Credentials can&apos;t be
              stored until one is set up; ATLAS never falls back to plaintext files.
            </div>
          )}

          <section className={sectionClass}>
            <div>
              <h2 className="text-base font-semibold text-white">GitHub credentials</h2>
              <p className="text-xs text-slate-400">
                Tokens go straight to the OS secret store and are verified with GitHub. ATLAS never shows them again.
              </p>
            </div>
            {settings.projects.length === 0 ? (
              <p className="text-sm text-slate-400">Register a project on the dashboard first.</p>
            ) : (
              settings.projects.map((p) => (
                <GithubCredentialCard
                  key={p.id}
                  project={p}
                  storeAvailable={settings.secret_store_available}
                  onChanged={load}
                  onError={setError}
                />
              ))
            )}
          </section>

          <AgentRegistryEditor settings={settings} onSaved={load} onError={setError} />

          {settings.projects.length > 0 && (
            <section className={sectionClass}>
              <div>
                <h2 className="text-base font-semibold text-white">Role assignments</h2>
                <p className="text-xs text-slate-400">
                  Which agent fills each role per project. Agents whose adapter can&apos;t take a role are disabled.
                </p>
              </div>
              {settings.projects.map((p) => (
                <ProjectRolesCard key={p.id} project={p} settings={settings} onSaved={load} onError={setError} />
              ))}
            </section>
          )}

          <PolicyView config={settings.global_config} />
        </>
      )}
    </div>
  );
}
