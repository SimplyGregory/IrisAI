"""Iris - a voice-driven agent with control of the local machine."""

__all__ = ["Iris", "make_agent"]


def make_agent():
    """Build the agent for whichever backend is configured.

    Both classes expose the same send/reset/usage interface, so the entry
    points do not care which one they got.
    """
    from iris import config

    if config.BACKEND == "sdk":
        from iris.agent_sdk import IrisSDK

        return IrisSDK()
    from iris.agent import Iris

    return Iris()


def __getattr__(name):
    if name == "Iris":
        from iris.agent import Iris

        return Iris
    raise AttributeError(name)
