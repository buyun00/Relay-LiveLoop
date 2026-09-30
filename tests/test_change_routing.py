from __future__ import annotations

import unittest
from types import SimpleNamespace

from host.change_routing import (
    RouteSelectionError,
    classify_native_assemblies,
    select_route,
    verify_route_selection,
)


def _analysis(component: str, disposition: str, *, hotfix_shape_verified: bool = False) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "relay.liveloop.change-analysis",
        "version": 1,
        "component": component,
        "disposition": disposition,
        "inputSnapshot": "sha256:" + "1" * 64,
        "profileDigest": "sha256:" + "2" * 64,
        "providerId": f"synthetic-{component}-provider",
        "evidenceSha256": "3" * 64,
    }
    if component == "code":
        value["hotfixShapeVerified"] = hotfix_shape_verified
    return value


class ChangeRoutingTests(unittest.TestCase):
    def test_routes_are_selected_from_both_component_deltas(self) -> None:
        cases = [
            ("BODY_ONLY", True, "UNCHANGED", "HOTFIX"),
            ("STRUCTURE", False, "UNCHANGED", "MODULE_RELOAD"),
            ("UNCHANGED", False, "CHANGED", "RESOURCE_ONLY"),
            ("BODY_ONLY", True, "CHANGED", "HOTFIX_AND_ASSET_RELOAD"),
            ("STRUCTURE", False, "CHANGED", "MODULE_AND_ASSET_RELOAD"),
            ("UNCHANGED", False, "UNCHANGED", "NO_CHANGES"),
        ]
        for code_kind, compatible, resource_kind, expected in cases:
            with self.subTest(code=code_kind, resource=resource_kind):
                selection = select_route(
                    _analysis("code", code_kind, hotfix_shape_verified=compatible),
                    _analysis("resource", resource_kind),
                )
                self.assertEqual(expected, selection["route"])
                self.assertEqual(selection, verify_route_selection(selection))

    def test_unknown_or_malformed_classification_fails_closed(self) -> None:
        for code, resource in (
            (_analysis("code", "UNKNOWN"), _analysis("resource", "CHANGED")),
            (_analysis("code", "STRUCTURE"), _analysis("resource", "UNKNOWN")),
            ({"disposition": "STRUCTURE"}, _analysis("resource", "CHANGED")),
        ):
            with self.subTest(code=code, resource=resource):
                with self.assertRaises(RouteSelectionError):
                    select_route(code, resource)

    def test_body_only_requires_hotfix_compatible_analysis_shape(self) -> None:
        with self.assertRaises(RouteSelectionError):
            select_route(
                _analysis("code", "BODY_ONLY", hotfix_shape_verified=False),
                _analysis("resource", "UNCHANGED"),
            )

    def test_route_selection_hash_detects_tampering(self) -> None:
        selection = select_route(
            _analysis("code", "STRUCTURE"),
            _analysis("resource", "CHANGED"),
        )
        tampered = dict(selection)
        tampered["route"] = "MODULE_RELOAD"
        with self.assertRaises(RouteSelectionError):
            verify_route_selection(tampered)

    def test_native_analysis_distinguishes_method_body_structure_and_no_change(self) -> None:
        profile = SimpleNamespace(module_id="Synthetic.Module")
        body_only, body_hotfix_compatible = classify_native_assemblies(
            profile,
            [{
                "name": "Synthetic.Assembly",
                "moduleId": "Synthetic.Module",
                "structureEqual": True,
                "changedMethods": [{"typeName": "SyntheticType", "signature": "System.Void Apply()"}],
            }],
        )
        structural, structural_hotfix_compatible = classify_native_assemblies(
            profile,
            [{
                "name": "Synthetic.Assembly",
                "moduleId": "Synthetic.Module",
                "structureEqual": False,
                "changedMethods": [],
            }],
        )
        unchanged, unchanged_hotfix_compatible = classify_native_assemblies(
            profile,
            [{
                "name": "Synthetic.Assembly",
                "moduleId": "Synthetic.Module",
                "structureEqual": True,
                "changedMethods": [],
            }],
        )
        self.assertEqual(("BODY_ONLY", True), (body_only, body_hotfix_compatible))
        self.assertEqual(("STRUCTURE", False), (structural, structural_hotfix_compatible))
        self.assertEqual(("UNCHANGED", False), (unchanged, unchanged_hotfix_compatible))

    def test_body_only_outside_single_assembly_hotfix_shape_is_not_relabeled_as_structure(self) -> None:
        profile = SimpleNamespace(module_id="Synthetic.Module")
        disposition, compatible = classify_native_assemblies(
            profile,
            [
                {
                    "name": "Synthetic.AssemblyA",
                    "moduleId": "Synthetic.Module",
                    "structureEqual": True,
                    "changedMethods": [{"typeName": "A", "signature": "void A()"}],
                },
                {
                    "name": "Synthetic.AssemblyB",
                    "moduleId": "Synthetic.Module",
                    "structureEqual": True,
                    "changedMethods": [{"typeName": "B", "signature": "void B()"}],
                },
            ],
        )
        self.assertEqual("BODY_ONLY", disposition)
        self.assertFalse(compatible)


if __name__ == "__main__":
    unittest.main()
