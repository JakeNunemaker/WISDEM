import numpy as np

from .LandBOSSEBaseComponent import LandBOSSEBaseComponent
from wisdem.landbosse.model import FoundationCost


class FoundationCostComponent(LandBOSSEBaseComponent):
    def setup(self):
        # Inputs
        self.add_input('start_delay_hours', val=1., units='h')
        self.add_input('critical_wind_speed_m_per_s', val=1., units='m/s')
        self.add_input('wind_height_of_interest_m', val=1., units='m')
        self.add_input('wind_shear_exponent', val=1.)
        self.add_input('operational_hours_per_day', units='h', val=1.)  # Hours
        self.add_input('overtime_multiplier', val=1.)
        self.add_input('gust_velocity_m_per_s', val=1., units='m/s')
        self.add_input('rated_thrust_N', val=1., units='N')
        self.add_input('bearing_pressure_n_m2', val=1.)
        self.add_input('depth', val=1., units='m')
        self.add_input('construct_duration', val=1.)
        self.add_input('num_turbines', val=1)
        self.add_input('turbine_rating_MW', units='MW', val=1.)
        self.add_input('critical_speed_non_erection_wind_delays_m_per_s', units='m/s', val=1)
        self.add_input('critical_height_non_erection_wind_delays_m', units='m', val=1)

        # Discrete inputs
        self.add_discrete_input('project_data', val=None)
        self.add_discrete_input('hour_day', val={'long': 24, 'normal': 10})
        self.add_discrete_input('time_construct', val='normal')

        # Discrete inputs, dataframes
        self.add_discrete_input('crew_price', val=None)
        self.add_discrete_input('rsmeans', val=None)
        self.add_discrete_input('material_price', val=None)
        self.add_discrete_input('components', val=None)
        self.add_discrete_input('crew', val=None)
        self.add_discrete_input('weather_window', val=None)

        # Outputs
        self.add_output('wind_multiplier', val=1., desc='Wind multiplier')
        self.add_output('F_dead_kN_per_turbine', val=1., desc='Turbine dead load', units='kN')
        self.add_output('F_horiz_kN_per_turbine', val=1., desc='Turbine horizontal load', units='kN')
        self.add_output('M_tot_kN_m_per_turbine', val=1., desc='Units kN m')
        self.add_output('Radius_g_m', val=1.)
        self.add_output('Radius_b_m', val=1.)
        self.add_output('Radius_m', val=1.)
        self.add_output('foundation_volume_concrete_m3_per_turbine', val=1.)
        self.add_output('steel_mass_short_ton_per_turbine', val=1., units='ton')

        # Discrete outputs
        self.add_discrete_output('operation_data_id_days_crews_workers', val=None)
        self.add_discrete_output('material_needs_per_turbine', val=None)
        self.add_discrete_output('total_foundation_cost', val=None)
        self.add_discrete_input('foundation_cost_csv', val=None)

    def compute(self, inputs, outputs, discrete_inputs=None, discrete_outputs=None):
        # Create real dictionaries to pass to the module
        inputs_dict = {key: inputs[key][0] for key in inputs.keys()}
        discrete_inputs_dict = {key: value for key, value in discrete_inputs.items()}
        master_inputs_dict = {**inputs_dict, **discrete_inputs_dict}
        master_outputs_dict = dict()

        # crew_cost sheet is being renamed so that the crew_cost is the same name as
        # the project data spreadsheet
        master_inputs_dict['crew_cost'] = discrete_inputs['crew_price']
        master_inputs_dict['rsmeans'] = discrete_inputs['rsmeans']
        master_inputs_dict['material_price'] = discrete_inputs['material_price']
        master_inputs_dict['components'] = discrete_inputs['components']

        # Pull NumPy arrays out of the component data
        components = discrete_inputs['components']
        for column in components.keys():
            master_inputs_dict[column] = np.array(components[column])

        # Grab the RSMeans per diem
        crew_price = discrete_inputs['crew_price']
        rsmeans_per_diem_series = crew_price.loc[crew_price['Labor type ID'] == 'RSMeans']['Per diem USD per day']
        rsmeans_per_diem = rsmeans_per_diem_series.iloc[0]
        master_inputs_dict['rsmeans_per_diem'] = rsmeans_per_diem

        # Run the module
        module = FoundationCost(master_inputs_dict, master_outputs_dict, 'WISDEM')
        module.run_module()

        # Copy outputs to the discrete outputs


        # Report the cost outputs if needed
        if self.options['verbosity']:
            self.print_verbose_module_type_operation(type(self).__name__,
                                                     master_outputs_dict['foundation_module_type_operation'])
            self.print_verbose_details(type(self).__name__,
                                       master_outputs_dict['foundation_csv'])
