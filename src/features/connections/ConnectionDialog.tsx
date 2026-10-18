import { Fragment, useState, type InputHTMLAttributes, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { Checkbox, FieldRow, Input, Select, Textarea } from "@/components/ui/Field";
import { TabPanel, Tabs } from "@/components/ui/Tabs";
import { errorMessage } from "@/lib/api/client";
import type {
  ConnectionTestResult,
  Engine,
  ProfileDraft,
  ProfileEntry,
  ProfileSecretsInput,
  ServerProfileDraft,
  SshConfig,
  SslConfig,
} from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { useEngines, useImportUrl, useSaveProfile, useTestConnection } from "./hooks";
import { PROFILE_COLOR_ORDER } from "./profileColors";

export const ENGINE_LABELS: Record<Engine, string> = {
  postgres: "PostgreSQL",
  mysql: "MySQL",
  mssql: "SQL Server",
  couchbase: "Couchbase",
  sqlite: "SQLite",
};

const SSL_MODES: SslConfig["mode"][] = ["disable", "prefer", "require", "verify-ca", "verify-full"];
/** The engine's own name for each stored mode (the stored value stays libpq-style); other engines show it raw. */
const SSL_MODE_LABELS: Partial<Record<Engine, Record<SslConfig["mode"], string>>> = {
  mysql: {
    disable: "disable (DISABLED)",
    prefer: "prefer (PREFERRED)",
    require: "require (REQUIRED)",
    "verify-ca": "verify-ca (VERIFY_CA)",
    "verify-full": "verify-full (VERIFY_IDENTITY)",
  },
  mssql: {
    disable: "disable (encryption off)",
    prefer: "prefer (request: encrypts the login; all traffic only if the server forces it)",
    require: "require (encrypt, no certificate check)",
    "verify-ca": "verify-ca (encrypt, check the CA)",
    "verify-full": "verify-full (check the CA and host name)",
  },
  couchbase: {
    disable: "disable (HTTP)",
    prefer: "prefer (= disable: HTTP)",
    require: "require (HTTPS, no certificate check)",
    "verify-ca": "verify-ca (HTTPS, check the CA)",
    "verify-full": "verify-full (HTTPS, check the CA and host name)",
  },
};
const SSL_MODE_HINTS: Partial<Record<Engine, string>> = {
  mysql: "verify-full cannot be used through an SSH tunnel",
  mssql: "verify-ca and verify-full need a CA file; verify-full cannot be used through an SSH tunnel",
  couchbase:
    "TLS uses ports 18093 and 18091; verify-ca and verify-full need the cluster's CA file (the app does not read the macOS keychain)",
};
/** The mode a new profile starts with (the others start with `prefer`): Couchbase's TLS is on other ports, so HTTP. */
const DEFAULT_SSL_MODES: Partial<Record<Engine, SslConfig["mode"]>> = { couchbase: "disable" };
/** Shown under Host and above the SSL mode hint when the password would cross the network in clear text. */
const PLAIN_TEXT_NOTE =
  "The password goes to the cluster in clear text (HTTP Basic auth): use TLS (require or verify-*) for a remote cluster";
/** This machine: `localhost`, `::1` and any 127.x address (`[::1]` and a port are taken off first, see `hostName`). */
const LOOPBACK_NAMES: ReadonlySet<string> = new Set(["localhost", "::1"]);
const LOOPBACK_V4 = /^127(\.\d+){1,3}$/;
/** The Import URL placeholder for the selected engine (PostgreSQL's when it has none). */
const URL_EXAMPLES: Partial<Record<Engine, string>> = {
  postgres: "postgres://user@host:5432/db",
  mysql: "mysql://user@host:3306/db or jdbc:mysql://host:3306/db",
  mssql: "mssql://user@host:1433/db or jdbc:sqlserver://host:1433;databaseName=db",
  couchbase: "couchbase://cb1,cb2/bucket · couchbases://host/bucket · http://host:8093",
  sqlite: "sqlite:///path/to.db",
};
/** Engine-specific wording of the General tab's server fields (the defaults suit the SQL engines). */
const HOST_HINTS: Partial<Record<Engine, string>> = {
  couchbase: "One host, or several separated by commas (cb1,cb2); only one through an SSH tunnel",
};
const PORT_PLACEHOLDERS: Partial<Record<Engine, string>> = { couchbase: "8093 (18093 with TLS)" };
const DATABASE_LABELS: Partial<Record<Engine, string>> = { couchbase: "Default context" };
const DATABASE_PLACEHOLDERS: Partial<Record<Engine, string>> = { couchbase: "bucket or bucket.scope" };
const DATABASE_HINTS: Partial<Record<Engine, string>> = {
  couchbase:
    "The default query context: where unqualified names (FROM users) resolve. A bucket alone whose name holds a dot goes in back-ticks: `my.bucket`",
};
const OPTIONS_HINTS: Partial<Record<Engine, string>> = { couchbase: "manager_port=8091, discover_nodes=false" };
const SSL_FILE_FIELDS = [
  { field: "ca_file", label: "CA file" },
  { field: "cert_file", label: "Client cert" },
  { field: "key_file", label: "Client key" },
] as const;
/** SQL Server has no client certificates: only the CA file is offered (values stored earlier stay as they are). */
const CA_FILE_ONLY_ENGINES: readonly Engine[] = ["mssql"];
/** Shown under "Read-only connection" for engines whose server cannot enforce it (spec §3.4). */
const SERVER_READ_ONLY_NOTE =
  "SQL Server cannot enforce read-only on the connection: DbaGuy blocks write statements itself. For a hard guarantee, use a login that can only read.";

const DEFAULT_SSL: SslConfig = { mode: "prefer", ca_file: null, cert_file: null, key_file: null };
const DEFAULT_SSH: SshConfig = {
  enabled: false,
  host: "",
  port: null,
  user: null,
  identity_file: null,
  auth: "agent_or_key",
};

type TabId = "general" | "ssl" | "ssh" | "advanced";
type CommonField = "name" | "folder" | "color" | "read_only" | "safety" | "save_password";
type SafetyLevel = ProfileDraft["safety"];

/** The safety levels (spec §3.4): when a statement needs confirming before it runs. */
const SAFETY_LEVELS: { value: SafetyLevel; label: string; explanation: string }[] = [
  { value: "relaxed", label: "Relaxed", explanation: "Runs every statement without asking" },
  {
    value: "standard",
    label: "Standard",
    explanation: "Asks before UPDATE or DELETE without WHERE, DROP, TRUNCATE and ALTER … DROP",
  },
  { value: "strict", label: "Strict", explanation: "Asks before every statement that is not a read" },
];
type Feedback = { ok: boolean; text: string } | null;

function emptyDraft(engine: Engine): ProfileDraft {
  const common = { name: "", folder: "", color: "none", read_only: false, safety: "standard", save_password: true } as const;
  if (engine === "sqlite") return { ...common, engine, path: "" };
  return {
    ...common,
    engine,
    host: "127.0.0.1",
    port: null,
    database: null,
    username: null,
    options: {},
    ssl: { ...DEFAULT_SSL, mode: DEFAULT_SSL_MODES[engine] ?? DEFAULT_SSL.mode },
    ssh: DEFAULT_SSH,
  };
}

/** One entry of a host list without its port or brackets: `cb1:8091` is `cb1`, `[::1]:8093` is `::1`. */
function hostName(entry: string): string {
  const name = entry.trim().toLowerCase();
  const bracketed = /^\[([^\]]*)\](?::\d*)?$/.exec(name);
  if (bracketed) return bracketed[1];
  // One colon is host:port; several are a bare IPv6 address.
  return name.indexOf(":") === name.lastIndexOf(":") ? name.split(":")[0] : name;
}

/** The host (or each host of a list) is not this machine: the password would travel the network. */
function hasRemoteHost(hosts: string): boolean {
  return hosts
    .split(",")
    .map(hostName)
    .some((name) => name !== "" && !LOOPBACK_NAMES.has(name) && !LOOPBACK_V4.test(name));
}

/** Hints one under the other; undefined when there are none (the row then shows no hint line). */
function stacked(...hints: ReactNode[]): ReactNode {
  const shown = hints.filter(Boolean);
  if (shown.length === 0) return undefined;
  return shown.map((hint, i) => (
    <Fragment key={i}>
      {i > 0 && <br />}
      {hint}
    </Fragment>
  ));
}

function toDraft(entry: ProfileEntry): ProfileDraft {
  const { id: _id, ...draft } = entry.profile;
  return draft;
}

/** The fields that describe the profile itself rather than how to reach the database. */
function commonFields(draft: ProfileDraft): Pick<ProfileDraft, CommonField> {
  const { name, folder, color, read_only, safety, save_password } = draft;
  return { name, folder, color, read_only, safety, save_password };
}

function optionsToText(draft: ProfileDraft): string {
  if (draft.engine === "sqlite") return "";
  return Object.entries(draft.options ?? {})
    .map(([key, value]) => `${key}=${value}`)
    .join("\n");
}

function parseOptions(text: string): Record<string, string> {
  return Object.fromEntries(
    text
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.includes("="))
      .map((line) => {
        const i = line.indexOf("=");
        return [line.slice(0, i).trim(), line.slice(i + 1).trim()];
      }),
  );
}

function parsePort(text: string): number | null {
  const digits = text.replace(/\D/g, "");
  return digits ? Number(digits) : null;
}

type InputProps = Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange">;

/** Text input for an optional field: an empty box means `null`. */
function NullableInput({
  value,
  onValue,
  ...props
}: InputProps & { value: string | null | undefined; onValue: (value: string | null) => void }) {
  return <Input {...props} value={value ?? ""} onChange={(e) => onValue(e.target.value || null)} />;
}

function PortInput({
  value,
  onValue,
  ...props
}: InputProps & { value: number | null | undefined; onValue: (value: number | null) => void }) {
  return (
    <Input
      {...props}
      className="w-24"
      inputMode="numeric"
      value={value ?? ""}
      onChange={(e) => onValue(parsePort(e.target.value))}
    />
  );
}

/**
 * Password box with keep / replace / clear semantics:
 * `null` keeps the stored secret, a string replaces it, and `""` (via "Clear") removes it.
 */
function SecretInput({
  value,
  onChange,
  stored,
}: {
  value: string | null;
  onChange: (value: string | null) => void;
  stored: boolean;
}) {
  const clearing = value === "";
  return (
    <div className="flex items-center gap-2">
      <Input
        type="password"
        autoComplete="new-password"
        placeholder={clearing ? "Will be removed on save" : stored ? "Saved in Keychain" : ""}
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
      />
      {stored && (
        <button
          type="button"
          className="shrink-0 text-[12px] text-accent outline-none focus-visible:underline"
          onClick={() => onChange(clearing ? null : "")}
        >
          {clearing ? "Undo" : "Clear"}
        </button>
      )}
    </div>
  );
}

function ConnectionDialogBody({
  onOpenChange,
  entry,
}: {
  onOpenChange: (open: boolean) => void;
  entry?: ProfileEntry;
}) {
  const engines = useEngines();
  const save = useSaveProfile();
  const test = useTestConnection();
  const importUrl = useImportUrl();
  const [tab, setTab] = useState<TabId>("general");
  const [draft, setDraft] = useState<ProfileDraft>(() => (entry ? toDraft(entry) : emptyDraft("postgres")));
  const [optionsText, setOptionsText] = useState(() => optionsToText(draft));
  const [password, setPassword] = useState<string | null>(null);
  const [sshPassword, setSshPassword] = useState<string | null>(null);
  const [url, setUrl] = useState("");
  const [feedback, setFeedback] = useState<Feedback>(null);

  const server = draft.engine === "sqlite" ? null : draft;
  const ssl = server?.ssl ?? DEFAULT_SSL;
  const ssh = server?.ssh ?? DEFAULT_SSH;
  const engineInfo = engines.data?.find((e) => e.engine === draft.engine);
  const engineOptions = engines.data?.map((e) => e.engine) ?? [draft.engine];
  const sslFileFields = SSL_FILE_FIELDS.filter(
    ({ field }) => field === "ca_file" || !CA_FILE_ONLY_ENGINES.includes(draft.engine),
  );
  // Couchbase authenticates with HTTP Basic: without TLS (and without an SSH tunnel) a remote cluster sees the
  // password in clear text.
  const plainTextPassword =
    draft.engine === "couchbase" &&
    (ssl.mode === "disable" || ssl.mode === "prefer") &&
    !ssh.enabled &&
    !!server &&
    hasRemoteHost(server.host);
  // Shown under Host (General) and under the SSL mode: the one place that decides it is this predicate.
  const plainTextNote = plainTextPassword ? <span className="text-warning">{PLAIN_TEXT_NOTE}</span> : null;

  const patchCommon = (changes: Partial<Pick<ServerProfileDraft, CommonField>>) =>
    setDraft((d) => ({ ...d, ...changes }));
  const patchServer = (changes: Partial<ServerProfileDraft>) =>
    setDraft((d) => (d.engine === "sqlite" ? d : { ...d, ...changes }));
  const patchSsl = (changes: Partial<SslConfig>) => patchServer({ ssl: { ...ssl, ...changes } });
  const patchSsh = (changes: Partial<SshConfig>) => patchServer({ ssh: { ...ssh, ...changes } });

  const buildDraft = (): ProfileDraft => (server ? { ...server, options: parseOptions(optionsText) } : draft);
  const buildSecrets = (): ProfileSecretsInput => (server ? { password, ssh_password: sshPassword } : {});
  const canSave = draft.name.trim() !== "" && (server ? server.host.trim() !== "" : draft.engine === "sqlite" && draft.path.trim() !== "");

  const fail = (error: unknown) => setFeedback({ ok: false, text: errorMessage(error) });
  const showTestResult = (result: ConnectionTestResult) =>
    setFeedback({ ok: result.ok, text: `${result.ok ? "✓" : "✗"} ${result.message} (${result.elapsed_ms} ms)` });

  const switchEngine = (engine: Engine) => {
    setDraft((d) => ({ ...emptyDraft(engine), ...commonFields(d) }));
    setOptionsText("");
  };

  const onSave = () => {
    setFeedback(null);
    save.mutate(
      { id: entry?.profile.id, profile: buildDraft(), secrets: buildSecrets() },
      { onSuccess: () => onOpenChange(false), onError: fail },
    );
  };
  const onTest = () => {
    setFeedback(null);
    test.mutate(
      { profile: buildDraft(), secrets: buildSecrets(), existingId: entry?.profile.id },
      { onSuccess: showTestResult, onError: fail },
    );
  };
  const onImport = () => {
    setFeedback(null);
    importUrl.mutate(url, {
      onSuccess: ({ profile, password: importedPassword }) => {
        const imported: ProfileDraft = { ...emptyDraft(profile.engine), ...profile };
        setDraft((d) => ({ ...imported, ...commonFields(d), name: imported.name }));
        setOptionsText(optionsToText(imported));
        if (importedPassword) setPassword(importedPassword);
      },
      onError: fail,
    });
  };

  const tabs: { value: TabId; label: string }[] = server
    ? [
        { value: "general", label: "General" },
        { value: "ssl", label: "SSL" },
        { value: "ssh", label: "SSH" },
        { value: "advanced", label: "Advanced" },
      ]
    : [{ value: "general", label: "General" }];

  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      title={entry ? `Edit ${entry.profile.name}` : "New Connection"}
      footer={
        <>
          <Button onClick={onTest} disabled={test.isPending}>
            {test.isPending ? "Testing…" : "Test"}
          </Button>
          <div className="flex-1" />
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" onClick={onSave} disabled={save.isPending || !canSave}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      <Tabs value={tab} onValueChange={setTab} items={tabs} />

      <TabPanel value="general" current={tab}>
        {!entry && (
          <FieldRow label="Import URL">
            <div className="flex gap-2">
              <Input
                placeholder={URL_EXAMPLES[draft.engine] ?? URL_EXAMPLES.postgres}
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
              <Button onClick={onImport} disabled={!url.trim() || importUrl.isPending}>
                {importUrl.isPending ? "Importing…" : "Import"}
              </Button>
            </div>
          </FieldRow>
        )}
        <FieldRow label="Engine">
          <Select value={draft.engine} disabled={!!entry} onChange={(e) => switchEngine(e.target.value as Engine)}>
            {engineOptions.map((engine) => (
              <option key={engine} value={engine}>
                {ENGINE_LABELS[engine]}
              </option>
            ))}
          </Select>
        </FieldRow>
        <FieldRow label="Name">
          <Input value={draft.name} onChange={(e) => patchCommon({ name: e.target.value })} />
        </FieldRow>
        <FieldRow label="Folder" hint="Use / for nesting, e.g. Work/Prod">
          <Input value={draft.folder} onChange={(e) => patchCommon({ folder: e.target.value })} />
        </FieldRow>
        <FieldRow label="Color">
          <Select value={draft.color} onChange={(e) => patchCommon({ color: e.target.value as ProfileDraft["color"] })}>
            {PROFILE_COLOR_ORDER.map((color) => (
              <option key={color} value={color}>
                {color}
              </option>
            ))}
          </Select>
        </FieldRow>
        {server ? (
          <>
            <FieldRow label="Host" hint={stacked(HOST_HINTS[draft.engine], plainTextNote)}>
              <div className="flex gap-2">
                <Input value={server.host} onChange={(e) => patchServer({ host: e.target.value })} />
                <PortInput
                  placeholder={PORT_PLACEHOLDERS[draft.engine] ?? String(engineInfo?.default_port ?? "")}
                  value={server.port}
                  onValue={(port) => patchServer({ port })}
                />
              </div>
            </FieldRow>
            <FieldRow label={DATABASE_LABELS[draft.engine] ?? "Database"} hint={DATABASE_HINTS[draft.engine]}>
              <NullableInput
                placeholder={DATABASE_PLACEHOLDERS[draft.engine]}
                value={server.database}
                onValue={(database) => patchServer({ database })}
              />
            </FieldRow>
            <FieldRow label="User">
              <NullableInput value={server.username} onValue={(username) => patchServer({ username })} />
            </FieldRow>
            <FieldRow label="Password">
              <SecretInput value={password} onChange={setPassword} stored={!!entry?.has_password} />
            </FieldRow>
          </>
        ) : (
          <FieldRow label="File">
            <Input
              placeholder="/Users/me/app.db"
              value={draft.engine === "sqlite" ? draft.path : ""}
              onChange={(e) => setDraft((d) => (d.engine === "sqlite" ? { ...d, path: e.target.value } : d))}
            />
          </FieldRow>
        )}
        <FieldRow
          label=""
          hint={draft.read_only && engineInfo?.server_read_only === false ? SERVER_READ_ONLY_NOTE : undefined}
        >
          <div className="flex flex-col gap-1.5">
            {server && (
              <Checkbox
                label="Save password in Keychain"
                checked={draft.save_password}
                onChange={(e) => patchCommon({ save_password: e.target.checked })}
              />
            )}
            <Checkbox
              label="Read-only connection"
              checked={draft.read_only}
              onChange={(e) => patchCommon({ read_only: e.target.checked })}
            />
          </div>
        </FieldRow>
        <FieldRow label="Safety" hint={SAFETY_LEVELS.find((level) => level.value === draft.safety)?.explanation}>
          <Select value={draft.safety} onChange={(e) => patchCommon({ safety: e.target.value as SafetyLevel })}>
            {SAFETY_LEVELS.map((level) => (
              <option key={level.value} value={level.value}>
                {level.label}
              </option>
            ))}
          </Select>
        </FieldRow>
      </TabPanel>

      <TabPanel value="ssl" current={tab}>
        <FieldRow label="Mode" hint={stacked(plainTextNote, SSL_MODE_HINTS[draft.engine])}>
          <Select value={ssl.mode} onChange={(e) => patchSsl({ mode: e.target.value as SslConfig["mode"] })}>
            {SSL_MODES.map((mode) => (
              <option key={mode} value={mode}>
                {SSL_MODE_LABELS[draft.engine]?.[mode] ?? mode}
              </option>
            ))}
          </Select>
        </FieldRow>
        {sslFileFields.map(({ field, label }) => (
          <FieldRow key={field} label={label}>
            <NullableInput
              placeholder="/path/to/file.pem"
              value={ssl[field]}
              onValue={(value) => patchSsl({ [field]: value })}
            />
          </FieldRow>
        ))}
      </TabPanel>

      <TabPanel value="ssh" current={tab}>
        <FieldRow label="">
          <Checkbox
            label="Connect through an SSH tunnel"
            checked={ssh.enabled}
            onChange={(e) => patchSsh({ enabled: e.target.checked })}
          />
        </FieldRow>
        <FieldRow label="SSH host" hint="A host or an alias from ~/.ssh/config">
          <div className="flex gap-2">
            <Input value={ssh.host} onChange={(e) => patchSsh({ host: e.target.value })} />
            <PortInput placeholder="22" value={ssh.port} onValue={(port) => patchSsh({ port })} />
          </div>
        </FieldRow>
        <FieldRow label="SSH user">
          <NullableInput value={ssh.user} onValue={(user) => patchSsh({ user })} />
        </FieldRow>
        <FieldRow label="Auth">
          <Select value={ssh.auth} onChange={(e) => patchSsh({ auth: e.target.value as SshConfig["auth"] })}>
            <option value="agent_or_key">SSH agent / key</option>
            <option value="password">Password</option>
          </Select>
        </FieldRow>
        {ssh.auth === "password" ? (
          <FieldRow label="SSH password">
            <SecretInput value={sshPassword} onChange={setSshPassword} stored={!!entry?.has_ssh_password} />
          </FieldRow>
        ) : (
          <FieldRow label="Identity file" hint="Optional; the agent and ~/.ssh/config are used otherwise">
            <NullableInput
              placeholder="~/.ssh/id_ed25519"
              value={ssh.identity_file}
              onValue={(identity_file) => patchSsh({ identity_file })}
            />
          </FieldRow>
        )}
      </TabPanel>

      <TabPanel value="advanced" current={tab}>
        <FieldRow label="Options" hint={OPTIONS_HINTS[draft.engine] ?? "One key=value per line, passed to the driver"}>
          <Textarea className="h-32 font-mono text-[12px]" value={optionsText} onChange={(e) => setOptionsText(e.target.value)} />
        </FieldRow>
      </TabPanel>

      {feedback && (
        <p className={cn("mt-3 whitespace-pre-wrap text-[12px]", feedback.ok ? "text-success" : "text-danger")} data-selectable>
          {feedback.text}
        </p>
      )}
    </Dialog>
  );
}

/** Create (no `entry`) or edit a connection profile. The form state is rebuilt on every open. */
export function ConnectionDialog({
  open,
  onOpenChange,
  entry,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  entry?: ProfileEntry;
}) {
  if (!open) return null;
  return <ConnectionDialogBody key={entry?.profile.id ?? "new"} onOpenChange={onOpenChange} entry={entry} />;
}
