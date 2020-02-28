import numpy as np

from .LandBOSSEBaseComponent import LandBOSSEBaseComponent
from wisdem.landbosse.model import SitePreparationCost


class SitePreparationCostComponent(LandBOSSEBaseComponent):
    def setup(self):

        # Inputs, numeric
        self.add_input("num_turbines", val=11, desc="Number of turbines")
        self.add_input("turbine_spacing_rotor_diameters", val=1, desc="Turbine spacing in rotor diameters.")
        self.add_input("rotor_diameter_m", val=1.0, units="m", desc="Rotor diameter")
        self.add_input("road_length_adder_m", val=1.0, units="m", desc="Road length adder (the road that leads to the site)")
        self.add_input("road_width_ft", val=1.0, units="ft", desc="Road width (feet)")
        self.add_input("crane_width", val=1.0, units="m", desc="Crane width (meters)")
        self.add_input("overtime_multiplier", val=1.5, desc="Overtime multiplier for hours worked over 40")
        self.add_input("construct_duration", val=12, desc="Construction duration in months")
        self.add_input("num_access_roads", val=1, desc="Number of roads providing access to the sites.")

        # inputs, discrete (including dataframes)
        self.add_discrete_input("hour_day", val={"long": 24, "normal": 10}, desc="Hours per day")
        self.add_discrete_input("rsmeans", val=None, desc="rsmeans data")
        self.add_discrete_input("time_construct", val="normal",
                                desc="Hours per day that are available for construction")

    def compute(self, inputs, outputs, discrete_inputs=None, discrete_outputs=None):
        # Create real dictionaries to pass to the module
        inputs_dict = {key: inputs[key][0] for key in inputs.keys()}
        discrete_inputs_dict = {key: value for key, value in discrete_inputs.items()}
        master_inputs_dict = {**inputs_dict, **discrete_inputs_dict}
        master_outputs_dict = dict()

        # crew_cost sheet is being renamed so that the crew_cost is the same name as
        # the project data spreadsheet
        master_inputs_dict['rsmeans'] = discrete_inputs['rsmeans']
        master_inputs_dict['material_price'] = discrete_inputs['material_price']

        # Run the module
        module = SitePreparationCost(master_inputs_dict, master_outputs_dict, 'WISDEM')
        module.run_module()
