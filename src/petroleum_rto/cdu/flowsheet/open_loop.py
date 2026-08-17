"""Assembly of the M1 open-loop steady CDU flowsheet."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from ... import __version__ as SOFTWARE_VERSION
from ..core.config import (
    CaseConfig,
    ModelConfig,
    input_bundle_fingerprint,
    validate_config_compatibility,
)
from ..core.conservation import material_balance
from ..core.types import BalanceReport, MaterialStream, UnitResult
from ..equipment import (
    Desalter,
    EquivalentPreheater,
    Furnace,
    IsothermalFlash,
    OverheadCondenser,
    ReducedColumn,
    quality_proxies,
)
from ..properties.components import ComponentCatalog
from ..properties.thermo import ReducedThermo
from .results import SteadyFlowsheetResult

MAIN_PRODUCT_NAMES = (
    "gasoline",
    "kerosene",
    "light_diesel",
    "heavy_diesel",
    "residue",
)
BOUNDARY_OUTLET_NAMES = (*MAIN_PRODUCT_NAMES, "offgas", "aqueous", "brine")


def _float_parameter(parameters: Mapping[str, object], name: str) -> float:
    value = parameters[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"equipment parameter {name!r} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"equipment parameter {name!r} must be finite")
    return result


def _four_float_parameters(
    parameters: Mapping[str, object],
    name: str,
) -> tuple[float, float, float, float]:
    value = parameters[name]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"equipment parameter {name!r} must be a sequence")
    if len(value) != 4:
        raise ValueError(f"equipment parameter {name!r} must contain four values")
    converted = tuple(
        _float_parameter({name: item}, name)
        for item in value
    )
    return cast(tuple[float, float, float, float], converted)


def _float_mapping_parameter(
    parameters: Mapping[str, object],
    name: str,
) -> dict[str, float]:
    value = parameters[name]
    if not isinstance(value, Mapping):
        raise TypeError(f"equipment parameter {name!r} must be an object")
    return {
        str(key): _float_parameter({str(key): item}, str(key))
        for key, item in value.items()
    }


def _versions_without_none(values: Mapping[str, str | None]) -> dict[str, str]:
    return {name: value for name, value in values.items() if value is not None}


def _maximum_absolute_component_residual(balance: BalanceReport) -> float:
    return max((abs(value) for value in balance.component_residuals_kg_s.values()), default=0.0)


@dataclass(frozen=True)
class OpenLoopCDU:
    """Configuration-driven M1 process chain without physical recycles."""

    model: ModelConfig
    case: CaseConfig
    catalog: ComponentCatalog
    software_version: str = SOFTWARE_VERSION

    def __post_init__(self) -> None:
        validate_config_compatibility(
            self.model,
            self.case,
            software_version=self.software_version,
            catalog=self.catalog,
        )

    def _preheater(self, name: str, thermo: ReducedThermo) -> EquivalentPreheater:
        parameters = self.model.equipment[name]
        return EquivalentPreheater(
            thermo=thermo,
            effectiveness=_float_parameter(parameters, "effectiveness"),
            target_temperature_k=_float_parameter(parameters, "target_temperature_k"),
            pressure_drop_pa=_float_parameter(parameters, "pressure_drop_pa"),
        )

    def solve(self) -> SteadyFlowsheetResult:
        """Run the complete M1 steady chain and return all declared boundaries."""

        thermo = ReducedThermo(self.catalog)
        unit_results: dict[str, UnitResult] = {}
        streams: dict[str, MaterialStream] = {self.case.feed.name: self.case.feed}

        pre_desalter = self._preheater("pre_desalter_preheater", thermo).solve(
            self.case.feed,
            outlet_name="pre_desalter_crude",
        )
        unit_results["pre_desalter_preheater"] = pre_desalter
        pre_desalter_crude = pre_desalter.outlets["pre_desalter_crude"]
        streams[pre_desalter_crude.name] = pre_desalter_crude

        desalter_parameters = self.model.equipment["desalter"]
        wash_water = MaterialStream(
            name="wash_water",
            mass_flow_kg_s=(
                self.case.feed.mass_flow_kg_s
                * _float_parameter(desalter_parameters, "wash_water_ratio")
            ),
            temperature_k=pre_desalter_crude.temperature_k,
            pressure_pa=pre_desalter_crude.pressure_pa,
            mass_fractions={"water": 1.0},
            metadata={
                "source": "model assumption",
                "temperature_basis": "equal to pre-desalter crude because site value is unavailable",
            },
        )
        streams[wash_water.name] = wash_water
        desalter = Desalter(
            thermo=thermo,
            water_removal_efficiency=_float_parameter(
                desalter_parameters,
                "water_removal_efficiency",
            ),
            salt_removal_efficiency=_float_parameter(
                desalter_parameters,
                "salt_removal_efficiency",
            ),
            oil_entrainment_fraction=_float_parameter(
                desalter_parameters,
                "oil_entrainment_fraction",
            ),
            pressure_drop_pa=_float_parameter(desalter_parameters, "pressure_drop_pa"),
        ).solve(pre_desalter_crude, wash_water)
        unit_results["desalter"] = desalter
        desalted_crude = desalter.outlets["desalted_crude"]
        brine = desalter.outlets["brine"]
        streams[desalted_crude.name] = desalted_crude
        streams[brine.name] = brine

        post_desalter = self._preheater("post_desalter_preheater", thermo).solve(
            desalted_crude,
            outlet_name="flash_feed",
        )
        unit_results["post_desalter_preheater"] = post_desalter
        flash_feed = post_desalter.outlets["flash_feed"]
        streams[flash_feed.name] = flash_feed

        flash = IsothermalFlash(
            thermo=thermo,
            temperature_k=self.case.operating_conditions["flash_temperature_k"],
            pressure_pa=self.case.operating_conditions["flash_pressure_pa"],
        ).solve(flash_feed)
        unit_results["flash"] = flash
        flash_vapor = flash.outlets["vapor"]
        flash_liquid = flash.outlets["liquid"]
        streams[flash_vapor.name] = flash_vapor
        streams[flash_liquid.name] = flash_liquid

        pre_furnace = self._preheater("pre_furnace_preheater", thermo).solve(
            flash_liquid,
            outlet_name="furnace_feed",
        )
        unit_results["pre_furnace_preheater"] = pre_furnace
        furnace_feed = pre_furnace.outlets["furnace_feed"]
        streams[furnace_feed.name] = furnace_feed

        furnace_parameters = self.model.equipment["furnace"]
        furnace = Furnace(
            thermo=thermo,
            efficiency=_float_parameter(furnace_parameters, "efficiency"),
            heat_loss_w=_float_parameter(furnace_parameters, "heat_loss_w"),
            maximum_outlet_temperature_k=_float_parameter(
                furnace_parameters,
                "maximum_outlet_temperature_k",
            ),
            pressure_drop_pa=_float_parameter(furnace_parameters, "pressure_drop_pa"),
        ).solve(
            furnace_feed,
            outlet_temperature_k=self.case.operating_conditions[
                "furnace_outlet_temperature_k"
            ],
            outlet_name="furnace_outlet",
        )
        unit_results["furnace"] = furnace
        furnace_outlet = furnace.outlets["furnace_outlet"]
        streams[furnace_outlet.name] = furnace_outlet

        column_parameters = self.model.equipment["column"]
        column_result = ReducedColumn(
            thermo=thermo,
            pressure_pa=self.case.operating_conditions["tower_top_pressure_pa"],
            cut_points_k=_four_float_parameters(column_parameters, "cut_points_k"),
            separation_widths_k=_four_float_parameters(
                column_parameters,
                "separation_widths_k",
            ),
            product_temperatures_k=_float_mapping_parameter(
                column_parameters,
                "product_temperatures_k",
            ),
        ).solve(furnace_outlet, flash_vapor)
        column = column_result.unit_result
        unit_results["column"] = column
        streams.update({stream.name: stream for stream in column.outlets.values()})

        condenser_parameters = self.model.equipment["condenser"]
        condenser = OverheadCondenser(
            thermo=thermo,
            temperature_k=self.case.operating_conditions["condenser_temperature_k"],
            pressure_pa=self.case.operating_conditions["tower_top_pressure_pa"],
            condensation_width_k=_float_parameter(
                condenser_parameters,
                "condensation_width_k",
            ),
        ).solve(column.outlets["overhead"])
        unit_results["condenser"] = condenser
        streams.update({stream.name: stream for stream in condenser.outlets.values()})

        gasoline = condenser.outlets["oil_condensate"].renamed("gasoline")
        streams[gasoline.name] = gasoline
        products: dict[str, MaterialStream] = {
            "gasoline": gasoline,
            "kerosene": column.outlets["kerosene"],
            "light_diesel": column.outlets["light_diesel"],
            "heavy_diesel": column.outlets["heavy_diesel"],
            "residue": column.outlets["residue"],
            "offgas": condenser.outlets["offgas"],
            "aqueous": condenser.outlets["aqueous"],
            "brine": brine,
        }
        qualities = {
            name: quality_proxies(products[name], self.catalog)
            for name in MAIN_PRODUCT_NAMES
        }

        inlet_enthalpy = thermo.stream_enthalpy_w(self.case.feed) + thermo.stream_enthalpy_w(
            wash_water
        )
        outlet_enthalpy = (
            thermo.stream_enthalpy_w(products["offgas"], phase="vapor")
            + sum(
                thermo.stream_enthalpy_w(stream)
                for name, stream in products.items()
                if name != "offgas"
            )
        )
        total_process_duty = sum(result.duty_w for result in unit_results.values())
        balance = material_balance(
            [self.case.feed, wash_water],
            products.values(),
            energy_residual_w=inlet_enthalpy + total_process_duty - outlet_enthalpy,
        )
        versions = validate_config_compatibility(
            self.model,
            self.case,
            software_version=self.software_version,
            catalog=self.catalog,
        )
        diagnostics = {
            "fresh_feed_kg_s": self.case.feed.mass_flow_kg_s,
            "wash_water_kg_s": wash_water.mass_flow_kg_s,
            "flash_vapor_fraction": flash.diagnostics["mass_vapor_fraction"],
            "furnace_process_duty_w": furnace.duty_w,
            "furnace_fuel_duty_w": furnace.diagnostics["fuel_duty_w"],
            "total_process_duty_w": total_process_duty,
            "overall_mass_residual_kg_s": balance.residual_kg_s,
            "maximum_component_residual_kg_s": _maximum_absolute_component_residual(
                balance
            ),
            "salt_residual_kg_s": balance.salt_residual_kg_s,
            "energy_residual_w": cast(float, balance.energy_residual_w),
        }
        diagnostics.update(
            {
                f"{name}_yield_mass_fraction": (
                    products[name].mass_flow_kg_s / self.case.feed.mass_flow_kg_s
                )
                for name in MAIN_PRODUCT_NAMES
            }
        )
        return SteadyFlowsheetResult(
            status="success",
            streams=streams,
            products=products,
            unit_results=unit_results,
            qualities=qualities,
            balance=balance,
            diagnostics=diagnostics,
            versions=_versions_without_none(versions.as_dict()),
            input_fingerprint=input_bundle_fingerprint(
                self.model,
                self.case,
                versions,
                catalog=self.catalog,
            ),
            warnings=(
                "Outputs are reduced-order synthetic estimates, not calibrated plant predictions.",
                "Wash-water temperature is assumed equal to the pre-desalter crude temperature.",
                "Water phase change is lumped into the column duty; equipment duties are not plant energy predictions.",
            ),
        )


def run_open_loop(
    model: ModelConfig,
    case: CaseConfig,
    catalog: ComponentCatalog,
    *,
    software_version: str = SOFTWARE_VERSION,
) -> SteadyFlowsheetResult:
    """Convenience entry point for one deterministic M1 steady run."""

    return OpenLoopCDU(model, case, catalog, software_version).solve()
