# Courier transports

The courier moves messages between the outside world and the rooms. It is
deterministic code: no model runs on its path. Since clodia-platform#737 it
works through **transports**, one per medium:

| Scheme   | Transport                           | Receives | Transmits                |
|----------|-------------------------------------|----------|--------------------------|
| `mailto` | `server/api/mail_courier.py`        | yes      | with the send gate (#736) |
| `tg`     | —                                   | planned  | planned                  |
| `wa`     | private pack                        | planned  | planned                  |

## The pass

`courier.core.run_cycle(only=None)` runs, under one lock:

1. `transport.receive(only)` for every registered transport. A failing
   transport is counted (`<scheme>_errors`) and the others go on.
2. `fire_triggers(per_room)`: one turn per room with what **all** transports
   delivered there (#628).

It returns the pass's counters, which the process view shows (#738).

## Writing a transport

Implement `courier.transport.Transport`:

- `scheme` and `label`;
- `async receive(only) -> ReceiveResult`: fetch what is new for the rooms the
  transport serves, put it in the topics (files with their provenance, the
  post), and return what each room's trigger should list. Priming and
  de-duplication are the transport's: only it knows what identifies a message
  already seen;
- `async send(message: Outbound) -> SendResult`: hand ONE message to the
  medium. The courier calls it only for a message the gate has authorised;
  queueing and retries are not the transport's job. Raise
  `TransmissionUnsupported` if the transport does not transmit.

Then register it with `courier.core.register(transport)`. Addresses carry
their scheme (`tg:12345`); a bare address with `@` is mail.

`courier/test_737_transport.py` has an in-memory transport that goes through
the same reception and transmission tests: a new transport should pass them.
