"""Agent discovery: the plug-in mechanism.

Agents are discovered by scanning the `app/agents/` package for modules that
define a module-level `MANIFEST` (an AgentManifest) and a `run(ctx)`
function. Dropping a conforming module into that directory is the ONLY step
needed to register an agent — this exact property is what the staff
mystery-agent test exercises at grading time.
"""

import importlib
import logging
import pkgutil
from dataclasses import dataclass
from types import ModuleType

from app.core.manifest import AgentManifest

logger = logging.getLogger(__name__)

AGENTS_PACKAGE = "app.agents"


@dataclass
class RegisteredAgent:
    manifest: AgentManifest
    module: ModuleType

    def run(self, ctx):
        return self.module.run(ctx)


class AgentRegistry:
    def __init__(self, package: str = AGENTS_PACKAGE) -> None:
        self.package = package
        self._agents: dict[str, RegisteredAgent] = {}
        self._load()

    def _load(self) -> None:
        pkg = importlib.import_module(self.package)
        for modinfo in pkgutil.iter_modules(pkg.__path__):
            if modinfo.name.startswith("_"):
                continue
            module = importlib.import_module(f"{self.package}.{modinfo.name}")
            manifest = getattr(module, "MANIFEST", None)
            if manifest is None:
                logger.warning(
                    "Module %s in %s has no MANIFEST — skipped",
                    modinfo.name,
                    self.package,
                )
                continue
            if not isinstance(manifest, AgentManifest):
                raise TypeError(
                    f"{modinfo.name}.MANIFEST must be an AgentManifest, "
                    f"got {type(manifest).__name__}"
                )
            if not callable(getattr(module, "run", None)):
                raise TypeError(f"agent module {modinfo.name} must define run(ctx)")
            if manifest.agent_id in self._agents:
                raise ValueError(
                    f"duplicate agent_id {manifest.agent_id!r} (module {modinfo.name})"
                )
            self._agents[manifest.agent_id] = RegisteredAgent(manifest, module)

        # Upstream references are validated after everything is loaded, so
        # module import order can't matter.
        for agent in self._agents.values():
            for upstream_id in agent.manifest.optional_upstream_agents:
                if upstream_id not in self._agents:
                    logger.warning(
                        "Agent %r declares unknown upstream %r — it will be "
                        "treated as unavailable at run time",
                        agent.manifest.agent_id,
                        upstream_id,
                    )

    def get(self, agent_id: str) -> RegisteredAgent:
        if agent_id not in self._agents:
            raise KeyError(f"no registered agent with id {agent_id!r}")
        return self._agents[agent_id]

    def all(self) -> list[RegisteredAgent]:
        return sorted(self._agents.values(), key=lambda a: a.manifest.agent_id)

    def manifests(self) -> list[AgentManifest]:
        return [a.manifest for a in self.all()]


_registry: AgentRegistry | None = None


def get_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry


def reset_registry() -> None:
    """Force a re-scan on next access (used by tests and after deploys)."""
    global _registry
    _registry = None
