from __future__ import annotations

import unittest
import asyncio
from types import SimpleNamespace

from radio_tasks import (
    RADIO_CATALOG_TASK_TYPE,
    RADIO_PROGRAMME_TASK_TYPE,
    create_radio_task_definition,
    _active_radio_features,
    reconcile_radio_automation_tasks,
)


class RadioTasksTest(unittest.TestCase):
    def test_reconciliation_lock_is_owned_by_each_automation_service(self):
        async def check():
            async def configs():
                await asyncio.sleep(0.01)
                return []

            service = SimpleNamespace(
                registry=SimpleNamespace(list_definitions=lambda: []),
                repository=SimpleNamespace(list_configs=configs),
            )
            subsystem = SimpleNamespace(service=SimpleNamespace(_active={}))
            await asyncio.gather(
                reconcile_radio_automation_tasks(service, subsystem),
                reconcile_radio_automation_tasks(service, subsystem),
            )

        asyncio.run(check())
        asyncio.run(check())

    def test_definitions_use_shared_automation_and_distinct_refresh_contracts(self):
        subsystem = object()
        catalog = create_radio_task_definition("org.waveflow/yunting", RADIO_CATALOG_TASK_TYPE, subsystem)
        programme = create_radio_task_definition("org.waveflow/yunting", RADIO_PROGRAMME_TASK_TYPE, subsystem)
        self.assertEqual(catalog.task_id, "radio_refresh:catalog:org.waveflow/yunting")
        self.assertEqual(programme.task_id, "radio_refresh:programme:org.waveflow/yunting")
        self.assertEqual(catalog.scheduled_task_type, RADIO_CATALOG_TASK_TYPE)
        self.assertEqual(programme.scheduled_task_type, RADIO_PROGRAMME_TASK_TYPE)
        self.assertTrue(catalog.allow_automatic_scheduling)
        self.assertEqual(catalog.conflict_group, programme.conflict_group)

    def test_active_radio_features_are_projected_without_inventing_programme_tasks(self):
        def instance(identity, features, owned=True):
            return SimpleNamespace(
                state="HEALTHY_ACTIVE", health="healthy",
                manifest=SimpleNamespace(
                    provider_contracts=(SimpleNamespace(contract="radio_provider", features=frozenset(features)),),
                    owned_schemes=((identity.rsplit("/", 1)[-1], "radio_provider"),) if owned else (),
                )
            )

        service = SimpleNamespace(_active={
            "org.waveflow/yunting": instance("org.waveflow/yunting", {"catalog", "resolve_stream", "programme"}),
            "org.waveflow/myradio": instance("org.waveflow/myradio", {"catalog", "resolve_stream"}),
            "org.waveflow/hitfm": instance("org.waveflow/hitfm", {"catalog", "resolve_stream"}),
            "org.waveflow/broken": instance("org.waveflow/broken", {"catalog"}, owned=False),
        })
        features = _active_radio_features(service)
        self.assertEqual(features["org.waveflow/yunting"], {"catalog", "resolve_stream", "programme"})
        self.assertNotIn("org.waveflow/broken", features)

    def test_reconcile_creates_only_declared_feature_tasks(self):
        def active_instance(identity, features):
            return SimpleNamespace(
                state="HEALTHY_ACTIVE", health="healthy",
                manifest=SimpleNamespace(
                    provider_contracts=(SimpleNamespace(contract="radio_provider", features=frozenset(features)),),
                    owned_schemes=((identity.rsplit("/", 1)[-1], "radio_provider"),),
                )
            )

        class Registry:
            def __init__(self): self.definitions = []
            def list_definitions(self): return list(self.definitions)
            def get(self, task_id): return next(item for item in self.definitions if item.task_id == task_id)

        class Repository:
            def __init__(self): self.configs = []
            async def list_configs(self): return list(self.configs)
            async def ensure_config(self, _definition): return None
            async def delete_config(self, task_id):
                self.configs = [item for item in self.configs if item.task_id != task_id]

        class Service:
            def __init__(self):
                self._active = {
                    "org.waveflow/yunting": active_instance("org.waveflow/yunting", {"catalog", "programme"}),
                    "org.waveflow/myradio": active_instance("org.waveflow/myradio", {"catalog", "resolve_stream"}),
                }
                self.registry = Registry()
                self.repository = Repository()
                self.service = self
            async def add_definition(self, definition): self.registry.definitions.append(definition)
            async def remove_definition(self, task_id, delete_config=True):
                self.registry.definitions = [item for item in self.registry.definitions if item.task_id != task_id]

        service = Service()
        result = __import__("asyncio").run(reconcile_radio_automation_tasks(service, service))
        self.assertEqual(result["expected_task_count"], 3)
        self.assertEqual(
            {item.task_id for item in service.registry.definitions},
            {
                "radio_refresh:catalog:org.waveflow/yunting",
                "radio_refresh:programme:org.waveflow/yunting",
                "radio_refresh:catalog:org.waveflow/myradio",
            },
        )


if __name__ == "__main__":
    unittest.main()
