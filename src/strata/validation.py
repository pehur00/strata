"""Workspace validation service.

Cross-reference checks are defined here, decoupled from any UI layer.
Both the CLI and TUI should call :func:`validate_workspace` and handle
the returned result for display.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import ArchitectureWorkspace


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def validate_workspace(workspace: ArchitectureWorkspace) -> ValidationResult:
    """Run all cross-reference checks and return a :class:`ValidationResult`."""
    result = ValidationResult()

    cap_ids = {c.id for c in workspace.enterprise.capabilities}
    domain_ids = {d.id for d in workspace.data.domains}
    product_ids = {p.id for p in workspace.data.products}

    for application in workspace.enterprise.applications:
        for cid in application.capability_ids:
            if cid not in cap_ids:
                result.errors.append(
                    f"Application '{application.name}' references unknown capability '{cid}'"
                )

    for product in workspace.data.products:
        if product.domain_id not in domain_ids:
            result.errors.append(
                f"Data product '{product.name}' references unknown domain '{product.domain_id}'"
            )

    for flow in workspace.data.flows:
        if flow.source_domain not in domain_ids:
            result.warnings.append(
                f"Flow '{flow.name}': source '{flow.source_domain}' not modelled (external?)"
            )
        if flow.target_domain not in domain_ids:
            result.warnings.append(
                f"Flow '{flow.name}': target '{flow.target_domain}' not modelled (external?)"
            )
        if flow.data_product_id and flow.data_product_id not in product_ids:
            result.errors.append(
                f"Flow '{flow.name}' references unknown product '{flow.data_product_id}'"
            )

    for sol in workspace.solutions:
        comp_ids = {c.id for c in sol.components}
        for comp in sol.components:
            for dep in comp.dependencies:
                if dep not in comp_ids:
                    result.warnings.append(
                        f"Solution '{sol.name}' / component '{comp.name}' "
                        f"depends on unknown component '{dep}'"
                    )
        for cid in sol.business_capability_ids:
            if cid not in cap_ids:
                result.warnings.append(
                    f"Solution '{sol.name}' references unknown capability '{cid}'"
                )

    return result
