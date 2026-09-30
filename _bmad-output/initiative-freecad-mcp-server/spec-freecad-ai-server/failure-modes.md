# Failure modes

Every failure the server can produce, what it looks like to a caller, and
what it means. The recurring theme is that the dangerous failures are the
silent ones: results that look valid but are not what was asked for.

## The shape of a failure

A failing tool returns data, never an exception:

```json
{
  "error": "no such document: ghost",
  "kind": "DocumentNotFound",
  "hint": "Call `new_document` with that name first."
}
```

`kind` is a stable class name. `hint` is the next action. A caller that reads
the hint can recover without inventing a fix.

## Bridge failures

| kind | Cause | Recovery |
| --- | --- | --- |
| `BridgeUnreachable` | FreeCAD is not answering on the port | Call `connect`, or free the port |
| `PortInUse` | Another FreeCAD holds the port; the one launched could not bind | Set `FREECAD_AI_PORT`, or stop the other instance |
| `DocumentNotFound` | No such open document | `new_document` first |
| `ObjectNotFound` | No such object in the document | `list_objects` |
| `PropertyNotFound` | No such property, and the message lists the real ones | `get_properties` |
| `DocumentExists` | `new_document(reuse=False)` on a taken name | Pick another name |
| `NoShape` | The operation needs geometry the object lacks | Use a solid |
| `NoSuchDimension` | Wrong property name for a primitive; message lists the real ones | `list_primitive_types` |
| `BadOperation` | Unsupported boolean, or identical base and tool | Use `cut`, `fuse`, `common`, and two different objects |
| `BadGeometry` | Edge index out of range, non-positive radius, zero normal, ambiguous `distance` | `describe_geometry` |
| `EmptyResult` | Objects do not intersect, or the section plane misses the shape | `measure` and `distance` first |
| `ExportFailed` | Unsupported extension, or the target directory does not exist | Create it, use a supported suffix |
| `SaveFailed` | Path unwritable or file locked | Check the path |
| `NoSuchFeature` | This FreeCAD build lacks the object type | Probe rather than assume |
| `BridgeInternalError` | An unclassified failure inside the bridge | A bug; the message carries the original |

## Silent failures that are now errors

These previously returned a result that looked fine. Each is now caught,
because each produced a wrong model rather than an error.

- **An empty boolean intersection.** FreeCAD returns a valid `Compound`
  holding no solids, with `isNull()` false and volume zero. A `common` of
  disjoint shapes reported success.
- **An unfittable fillet or chamfer.** A radius larger than the geometry
  produced a null or degenerate shape rather than a clear refusal.
- **A section plane that misses.** `slice` returns no wires, which is
  indistinguishable from a legitimately empty result.
- **FreeCAD dying.** The server reconnected to a fresh process and handed the
  caller an empty kernel with no indication that the previous documents were
  gone.

## Behaviours that are correct but surprising

- **Overlapping solids are 0 apart.** That is a position answer, not an error.
- **A `cut` of non-touching objects succeeds** and returns just the base. Only
  an empty *intersection* is an error, because only that yields no material.
- **Edge indices are 1-based** and match FreeCAD's own `Edge1`, `Edge2`
  naming, which `describe_geometry` reports.
- **`shape_summary` omits null-valued properties** because XML-RPC cannot
  represent null. A missing key means "no value", not "zero".
- **The array is not parametric.** Its copies are static, so editing the
  source does not update the array.
- **A restart notice appears on the next call after a restart only.** It is
  read-and-clear, so one restart is reported once rather than on every
  subsequent call.

## Diagnostics

FreeCAD's output goes to `%TEMP%\freecad-ai-bridge-<port>.log`. It is never
captured on a pipe — an unread pipe fills and FreeCAD blocks in `write()`,
which presents as a hang with the process still alive. When FreeCAD exits
non-zero during startup, its output is included in the error.
