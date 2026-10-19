import { Button } from "@/components/ui/Button";
import { errorMessage } from "@/lib/api/client";
import type { FieldSample, Keyspace } from "@/lib/api/types";
import { plural } from "@/lib/format";
import {
  formatFieldPath,
  formatKinds,
  sampleNotes,
  useFieldSample,
  useIsSampling,
  useSampleFields,
} from "./fieldSamples";

function FieldsTable({ sample }: { sample: FieldSample }) {
  if (sample.fields.length === 0) return <p className="mt-2 text-[12px] text-muted">The sample found no documents</p>;
  return (
    <table className="mt-2 border-collapse text-[12px]" data-selectable>
      <thead>
        <tr className="text-left text-muted">
          {["Path", "Types", "Documents"].map((h) => (
            <th key={h} className="border-b border-border px-2 py-1 font-medium">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {sample.fields.map((field) => (
          <tr key={JSON.stringify(field.path)} className="hover:bg-hover">
            <td className="px-2 py-0.5 font-mono">{formatFieldPath(field.path)}</td>
            <td className="px-2 py-0.5 text-muted">{formatKinds(field.types)}</td>
            <td className="px-2 py-0.5">
              {field.docs.toLocaleString()} <span className="text-muted">· {field.percent}%</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * A collection's detail page, under its DDL (R-04-11): the field paths of its last manual sample, or an invitation to
 * take one. "Sample fields…" and "Sample again" sample at once with the setting's size; the tree's menu entry is the
 * one that asks. A sample that failed shows the server's text and leaves the previous one in place.
 */
export function SampledFields({ profileId, keyspace }: { profileId: string; keyspace: Keyspace }) {
  const cached = useFieldSample(profileId, keyspace);
  const run = useSampleFields();
  const sampling = useIsSampling(profileId, keyspace);
  const sample = cached.data ?? null;
  const start = () => {
    console.info("Sampling the fields of a collection");
    run.mutate(
      { profileId, keyspace, size: null },
      { onError: (error) => console.warn("Sampling the fields failed", error) },
    );
  };
  const button = (
    <Button
      size="sm"
      variant={sample ? "ghost" : "secondary"}
      className={sample ? "text-accent" : undefined}
      disabled={sampling}
      onClick={start}
    >
      {sampling ? "Sampling…" : sample ? "Sample again" : "Sample fields…"}
    </Button>
  );

  return (
    <section className="mx-4 mb-4 flex flex-col gap-1.5">
      <h3 className="text-[13px] font-semibold">Fields (sampled)</h3>
      {cached.isPending && <p className="text-[12px] text-muted">Loading…</p>}
      {cached.isError && (
        <p className="text-[12px] whitespace-pre-wrap text-danger" data-selectable>
          {errorMessage(cached.error)}
        </p>
      )}
      {cached.isSuccess && sample === null && (
        <>
          <div className="flex items-center gap-3">
            <span className="text-[12px] text-muted">Not sampled yet</span>
            {button}
          </div>
          <p className="text-[11px] text-muted">The editor’s completion offers the sampled field names.</p>
        </>
      )}
      {sample && (
        <>
          <p className="flex flex-wrap items-center gap-x-1.5 text-[12px] text-muted">
            <span>{plural(sample.documents, "document")}</span>
            <span aria-hidden>·</span>
            <span>sampled {new Date(sample.sampled_at).toLocaleString()}</span>
            <span aria-hidden>·</span>
            {button}
          </p>
          {sampleNotes(sample).map((note) => (
            <p key={note.label} className="text-[12px] text-muted">
              <span className="font-medium text-warning">{note.label}</span> — {note.explanation}
            </p>
          ))}
        </>
      )}
      {run.isError && (
        <p className="text-[12px] whitespace-pre-wrap text-danger" role="alert" data-selectable>
          {errorMessage(run.error)}
        </p>
      )}
      {sample && <FieldsTable sample={sample} />}
    </section>
  );
}
