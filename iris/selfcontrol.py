"""What the running front-end lets Iris do to herself.

Some of what she can be asked to change belongs to whoever is hosting her
rather than to the agent: only the panel has a window to fade, and only the
process itself can stop the process. Rather than have the tools reach across
into main.py or panel/app.py - which would tie them to whichever one happens
to be running - each entry point registers the handles it can offer, and the
tools ask for them by name.

The effect is that "close yourself" works in every mode, "make yourself
translucent" works only where there is something to fade, and the tool can say
plainly which of the two it is instead of failing obscurely.

Deliberately absent: anything that changes the confirmation gate. Iris being
able to turn off the thing that asks your permission would make asking your
permission meaningless - the one setting a model must not be able to reach is
the one restraining it. Change it yourself, in the panel or in .env.
"""

_handlers: dict[str, object] = {}


class Unavailable(RuntimeError):
    """Asked for something this way of running Iris cannot do."""


def provide(name: str, handler) -> None:
    """Offer a capability. Called by whichever entry point is starting up."""
    _handlers[name] = handler


def has(name: str) -> bool:
    return name in _handlers


def offered() -> list[str]:
    return sorted(_handlers)


def call(name: str, *args, **kwargs):
    handler = _handlers.get(name)
    if handler is None:
        raise Unavailable(name)
    return handler(*args, **kwargs)
